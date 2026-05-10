# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Frappe Site Controller

Manages ERPNext site creation on a running Frappe bench (ERPNext Helm release).
Each document represents a single Frappe site and drives a one-shot Kubernetes
Job that runs ``bench new-site`` inside the bench's workload containers.

Desired state lives here in MariaDB.  The site is created asynchronously via a
background task; reconciliation polls the Job status every 5 minutes to detect
completion or failure.
"""

import re
import secrets

import frappe
from frappe.model.document import Document

from kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup import make_backup_name
from kubeport.utils import metrics

# Frappe app names are python module names: lowercase, start with a letter,
# only letters/digits/underscore. We also allow ``-`` because a few published
# apps on PyPI use it. No other characters are permitted — ``install_apps`` is
# interpolated into a shell command, so accepting shell metacharacters here
# would create an injection path in the site-creation Job.
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Site names feed three places that each have different constraints:
#  - the autoname pattern ``{bench_release}/{site_name}`` — a ``/`` in site_name
#    would split the docname into extra segments;
#  - the K8s Job name slug via ``_job_name`` — sanitized, but a site with only
#    non-alphanumerics would slug to empty;
#  - the bench pod as the ``$SITE_NAME`` env var — safe under double quotes,
#    but newlines/control chars still confuse logs and downstream tooling.
# We require a hostname-style label: start/end alphanumeric, interior may
# include ``.`` ``-`` or ``_``. This matches what ``bench new-site`` expects.
_SITE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


class FrappeSite(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password
		backup_retention_count: DF.Int
		backup_retention_days: DF.Int
		backup_schedule: DF.Data | None
		backup_schedule_last_run: DF.Datetime | None
		bench_release: DF.Link
		cluster: DF.Data | None
		operation_job_name: DF.Data | None
		operation_job_token: DF.Data | None
		db_root_password: DF.Password | None
		db_root_secret: DF.Data | None
		db_root_secret_key: DF.Data | None
		db_type: DF.Literal["mariadb"]
		force_create: DF.Check
		install_apps: DF.SmallText | None
		namespace: DF.Data | None
		operation_token: DF.Data | None
		site_name: DF.Data
		status: DF.Literal["Draft", "In Progress", "Active", "Failed", "Deleting", "Migrating"]
		status_detail: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		"""Populate derived fields from bench_release and guard identity immutability."""
		self._validate_site_name()

		if not self.is_new():
			expected = f"{self.bench_release}/{self.site_name}"
			if self.name != expected:
				frappe.throw(
					"Bench release and site name are immutable after creation. "
					"Create a new Frappe Site document to change the identity."
				)

		if self.bench_release:
			release = frappe.get_doc("Helm Release", self.bench_release)
			self.cluster = release.cluster
			self.namespace = release.namespace or "default"
			if release.status not in ("Deployed", "Degraded"):
				frappe.throw(
					f"Bench release '{self.bench_release}' has status '{release.status}'. "
					"Only deployed or degraded releases can host site creation."
				)

		# DB root credentials are optional at validate time.  When the bench
		# release bundles MariaDB (the default for Frappe charts), the
		# ``create_site`` action auto-wires the chart's ``<release>-mariadb``
		# Secret onto this doc.  Operators using external-DB topology must
		# supply credentials; ``create_site`` enforces that explicitly.

		self._validate_install_apps()
		self._validate_backup_schedule()

	def _preflight_db_topology(self) -> None:
		"""Refuse to enqueue a create-site Job when the DB wiring obviously won't work.

		Cheap synchronous Service/Secret lookup against the target cluster.  A
		green pre-flight is necessary but not sufficient — the worker still
		runs the full resolution chain and may discover host info via configMap
		mounts the pre-flight skips.  A red pre-flight reliably means the
		bundled-MariaDB happy path is broken (no release-owned MariaDB exists),
		so we surface that as a UI-thread error instead of letting the Job get
		submitted just to fail seconds later.
		"""
		from kubernetes import client as k8s_client

		from kubeport.tasks.site_tasks import (
			_resolve_db_root_secret_for_release,
			can_resolve_db_host_for_release,
		)
		from kubeport.utils.k8s_client import get_k8s_api_client

		release = frappe.get_doc("Helm Release", self.bench_release)
		cluster = release.cluster
		namespace = release.namespace or "default"
		release_name = release.release_name
		use_external_db = bool(getattr(release, "use_external_database", 0))

		if use_external_db:
			# External DB topology: operator owns the dbHost wiring (chart
			# values or container_env) and must supply credentials on the
			# Frappe Site doc.  Skip Service-based pre-flight; trust values.
			if not self.db_root_password and not self.db_root_secret:
				frappe.throw(
					"This release uses an external database. Set DB Root Password "
					"or DB Root Secret on this Frappe Site before creating it."
				)
			return

		# Bundled flow: chart should have provisioned <release>-mariadb in
		# the release namespace.  If not, the worker has nothing to resolve.
		if not can_resolve_db_host_for_release(cluster, namespace, release_name):
			frappe.throw(
				f"No release-owned MariaDB Service was found for Helm release "
				f"'{release_name}' in namespace '{namespace}'. Either deploy the "
				"release with bundled MariaDB enabled (the Kubeport default), or "
				"check 'Use External Database' on the Helm Release form and set "
				"'dbHost' in the values yourself."
			)

		# Bundled flow with no operator-supplied creds: the worker auto-wires
		# the chart's ``<release>-mariadb`` Secret. If even that is missing,
		# the operator needs to act — fail early instead of in the worker.
		if not self.db_root_password and not self.db_root_secret:
			try:
				api_client = get_k8s_api_client(cluster)
				core_v1 = k8s_client.CoreV1Api(api_client=api_client)
			except Exception:
				# Transient client-build failure: let the worker handle it
				# rather than blocking the click.
				return
			secret_name, _ = _resolve_db_root_secret_for_release(
				core_v1=core_v1,
				namespace=namespace,
				release_name=release_name,
			)
			if not secret_name:
				frappe.throw(
					f"Helm release '{release_name}' did not expose a MariaDB "
					"root Secret named '<release>-mariadb' (Bitnami convention). "
					"Set DB Root Password or DB Root Secret on this Frappe Site, "
					"or redeploy the release with bundled MariaDB enabled."
				)

	def _validate_site_name(self):
		if not self.site_name:
			return
		if not _SITE_NAME_RE.match(self.site_name):
			frappe.throw(
				f"Invalid site name '{self.site_name}'. "
				"Use lowercase letters, digits, dots, hyphens, or underscores; "
				"must start and end with a letter or digit (e.g. 'erp.example.com')."
			)

	def _validate_install_apps(self):
		if not self.install_apps:
			return
		for raw_line in self.install_apps.splitlines():
			app = raw_line.strip()
			if not app:
				continue
			if not _APP_NAME_RE.match(app):
				frappe.throw(
					f"Invalid app name '{app}' in Install Apps. "
					"App names must start with a lowercase letter and contain only "
					"lowercase letters, digits, underscores, or hyphens."
				)

	def _validate_backup_schedule(self):
		if self.backup_schedule:
			schedule = self.backup_schedule.strip()
			# Reject blank-after-strip so an operator typing whitespace doesn't
			# silently disable the schedule while leaving the field non-empty.
			if not schedule:
				frappe.throw("Backup Schedule must be a five-field cron expression or blank.")
			from croniter import croniter

			if not croniter.is_valid(schedule):
				frappe.throw(
					f"Invalid Backup Schedule '{schedule}'. "
					"Use a five-field cron expression, e.g. '0 2 * * *' for daily at 02:00."
				)
			# Persist the stripped form so the croniter base computation can
			# round-trip without surprise whitespace.
			self.backup_schedule = schedule
		if (self.backup_retention_count or 0) < 0:
			frappe.throw("Retention: Max Backups must be 0 or positive.")
		if (self.backup_retention_days or 0) < 0:
			frappe.throw("Retention: Max Age must be 0 or positive.")

	@frappe.whitelist()
	def create_site(self):
		"""Submit a Kubernetes Job to create this site on the bench.

		The job runs ``bench new-site`` inside the bench's scheduler pod.
		Status transitions: Draft → In Progress → Active | Failed.
		"""
		if not self.bench_release:
			frappe.throw("A bench release is required.")
		if not self.site_name:
			frappe.throw("A site name is required.")
		if self.status == "In Progress":
			frappe.throw("Site creation is already in progress.")
		if self.status == "Active" and not self.force_create:
			frappe.throw("This site already exists. Enable Force Create to recreate it.")

		self._preflight_db_topology()

		# Retry from Failed: Frappe's ``make_site_config`` never overwrites an
		# existing ``site_config.json`` and ``bench new-site --force`` does not
		# clear it either.  A prior partial creation (e.g. the one that just
		# failed) silently poisons the next attempt.  Auto-engage force_create
		# so the bench Job's pre-clean runs and the bench-side ``--force`` fires
		# — the operator should not have to hunt for a toggle to retry.
		if self.status == "Failed" and not self.force_create:
			self.db_set("force_create", 1)
			self.force_create = 1

		operation_token = secrets.token_hex(16)
		correlation_id = metrics.new_correlation_id()
		self.db_set("status", "In Progress")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		# Reset the job-launching token; the worker sets it again once the Job
		# is actually submitted.  Clearing here prevents a stale reconciliation
		# from matching on a fresh operation_token.
		self.db_set("operation_job_token", "")
		self.db_set("operation_job_name", "")
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info("enqueue create_site_task site=%s", self.name)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.create_site_task",
			site_docname=self.name,
			operation_token=operation_token,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			f"Site creation for '{self.site_name}' has been queued. "
			"Status will update automatically when the job completes.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def delete_site(self):
		"""Submit a Kubernetes Job to drop this site from the bench.

		Runs ``bench drop-site --no-backup --force`` inside the bench's
		workload container.  Reconciliation verifies the site is gone and
		auto-deletes this row.  Only valid from ``Active`` (the normal path)
		or ``Failed`` rows that previously launched a Job (so a real site
		may exist on the bench).

		Status transitions: Active|Failed → Deleting → [doc deleted] | Failed.
		"""
		if self.status not in ("Active", "Failed"):
			frappe.throw(
				f"Delete Site is not available while status is '{self.status}'. "
				"Wait for the current operation to finish or cancel it first."
			)
		if self.status == "Failed" and not self.operation_job_name:
			# No Job ever ran for this row — there is no site on the bench to drop.
			# Operator can just delete the row directly.
			frappe.throw(
				"This site never reached Active and has no recorded operation Job. "
				"Delete the row directly — there is nothing to drop on the bench."
			)

		operation_token = secrets.token_hex(16)
		correlation_id = metrics.new_correlation_id()
		self.db_set("status", "Deleting")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		# Clear the previous operation's Job pointer; the worker sets these
		# again once the drop-site Job is actually submitted.
		self.db_set("operation_job_token", "")
		self.db_set("operation_job_name", "")
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info("enqueue delete_site_task site=%s", self.name)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.delete_site_task",
			site_docname=self.name,
			operation_token=operation_token,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			f"Drop-site requested for '{self.site_name}'. "
			"The row will be removed automatically once the bench confirms the site is gone.",
			alert=True,
			indicator="red",
		)

	@frappe.whitelist()
	def migrate_site(self):
		"""Submit a Kubernetes Job that runs ``bench migrate`` against this site.

		Only valid while the site is ``Active``.  The site is briefly
		unavailable while migrations apply.  Reconciliation probes the site
		for functionality after the Job and transitions back to ``Active`` on
		success or ``Failed`` on error.
		"""
		if self.status != "Active":
			frappe.throw(
				f"Migrate Site is only available for Active sites (current status: '{self.status}')."
			)

		operation_token = secrets.token_hex(16)
		correlation_id = metrics.new_correlation_id()
		self.db_set("status", "Migrating")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		self.db_set("operation_job_token", "")
		self.db_set("operation_job_name", "")
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info("enqueue migrate_site_task site=%s", self.name)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.migrate_site_task",
			site_docname=self.name,
			operation_token=operation_token,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			f"Migration requested for '{self.site_name}'. "
			"Status will update automatically when the job completes.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def backup_site(self) -> dict:
		"""Queue a backup Job for this Active site."""
		if self.status != "Active":
			frappe.throw(f"Backup is only available for Active sites (current status: '{self.status}').")
		if self._has_in_flight_backup():
			frappe.throw("A backup or restore operation is already in progress for this site.")

		backup_docname = self._enqueue_backup(triggered_by=frappe.session.user)
		frappe.msgprint(
			f"Backup for '{self.site_name}' has been queued.",
			alert=True,
			indicator="blue",
		)
		return {"backup_docname": backup_docname}

	def _enqueue_backup(self, *, triggered_by: str) -> str:
		"""Insert the backup row, flip the site to In Progress, and enqueue the worker.

		Shared by the manual ``backup_site`` button and the scheduled-backup
		path in ``kubeport/tasks/reconciliation.py``.  Callers are responsible
		for the upstream guards (status == Active, no in-flight backup); this
		helper focuses on the ordered side effects.
		"""
		release = frappe.get_doc("Helm Release", self.bench_release)
		operation_token = secrets.token_hex(16)
		backup_doc = frappe.get_doc(
			{
				"doctype": "Frappe Site Backup",
				"frappe_site": self.name,
				"site_name": self.site_name,
				"backup_name": make_backup_name(self.site_name),
				"source_bench_release": self.bench_release,
				"source_release_name": release.release_name,
				"cluster": self.cluster or release.cluster,
				"namespace": self.namespace or release.namespace or "default",
				"status": "Pending",
				"storage_backend": "pvc",
				"operation_token": operation_token,
				"triggered_by": triggered_by,
			}
		)
		backup_doc.insert(ignore_permissions=True)

		correlation_id = metrics.new_correlation_id()
		self.db_set("status", "In Progress")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		self.db_set("operation_job_token", "")
		self.db_set("operation_job_name", "")
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info(
				"enqueue backup_site_task site=%s backup=%s", self.name, backup_doc.name
			)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.backup_site_task",
			site_docname=self.name,
			backup_docname=backup_doc.name,
			operation_token=operation_token,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		return backup_doc.name

	@frappe.whitelist()
	def restore_site(self, backup_docname: str, confirm_destructive: bool = False) -> dict:
		"""Queue a restore Job from an Available backup."""
		if isinstance(confirm_destructive, str):
			confirm_destructive = confirm_destructive.lower() in ("1", "true", "yes")
		if not confirm_destructive:
			frappe.throw(
				"Restore overwrites the current site database and files. Confirm explicitly to proceed.",
				title="Destructive restore required",
			)
		if self.status != "Active":
			frappe.throw(f"Restore is only available for Active sites (current status: '{self.status}').")
		if self._has_in_flight_backup():
			frappe.throw("A backup or restore operation is already in progress for this site.")

		backup = frappe.get_doc("Frappe Site Backup", backup_docname)
		if backup.status != "Available":
			frappe.throw(f"Backup '{backup_docname}' is not Available.")
		if backup.storage_backend != "pvc" or not backup.storage_path:
			frappe.throw("Backup has no restorable PVC archive path.")
		if backup.cluster != self.cluster:
			frappe.throw("Backup cluster does not match this site.")
		if (backup.namespace or "default") != (self.namespace or "default"):
			frappe.throw("Backup namespace does not match this site.")
		if backup.site_name != self.site_name:
			frappe.throw("Backup site name does not match this site.")

		operation_token = secrets.token_hex(16)
		correlation_id = metrics.new_correlation_id()
		self.db_set("status", "Migrating")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		self.db_set("operation_job_token", "")
		self.db_set("operation_job_name", "")
		backup.db_set("status", "Restoring")
		backup.db_set("status_detail", "")
		backup.db_set("operation_token", operation_token)
		backup.db_set("operation_job_token", "")
		backup.db_set("operation_job_name", "")
		backup.db_set("operation_started_at", frappe.utils.now_datetime())
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info(
				"enqueue restore_site_task site=%s backup=%s", self.name, backup.name
			)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.restore_site_task",
			site_docname=self.name,
			backup_docname=backup.name,
			operation_token=operation_token,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			f"Restore for '{self.site_name}' has been queued.",
			alert=True,
			indicator="blue",
		)
		return {"backup_docname": backup.name}

	@frappe.whitelist()
	def get_site_health(self) -> dict[str, object]:
		"""Return per-resource readiness for the form drilldown panel.

		The site is hosted on its bench release; the bench's workload
		readiness is the per-site health surface.  Pure observed state —
		never persisted.  Called via ``frappe.xcall`` from the form, not
		from ``onload``, so a slow cluster cannot block document load.
		"""
		from kubeport.utils.release_health import walk

		if not self.bench_release:
			return {"rows": [], "error": "This site has no bench release."}

		try:
			return {
				"rows": [r.to_dict() for r in walk(self.bench_release)],
				"error": "",
			}
		except Exception as e:
			frappe.logger("kubeport").warning(
				"Could not read Frappe Site health for '%s': %s",
				self.name,
				e,
			)
			return {"rows": [], "error": _format_observed_state_error(e)}

	def _has_in_flight_backup(self) -> bool:
		status_filter = ["in", ["Pending", "In Progress", "Restoring"]]
		if frappe.db.exists(
			"Frappe Site Backup",
			{
				"frappe_site": self.name,
				"status": status_filter,
			},
		):
			return True
		return bool(
			frappe.db.exists(
				"Frappe Site Backup",
				{
					"cluster": self.cluster,
					"namespace": self.namespace or "default",
					"site_name": self.site_name,
					"status": status_filter,
				},
			)
		)

	@frappe.whitelist()
	def cancel_site(self, confirm_destructive: bool = False):
		"""Cancel an in-flight operation (create / delete / migrate).

		Rotates the operation token so any concurrent worker or reconciler
		holding the old token is a no-op, marks the row ``Failed``, and
		enqueues Job cleanup.  Operator can re-issue the appropriate action
		from the ``Failed`` state.

		``confirm_destructive`` is required for ``Migrating`` because
		cancelling a running migration can leave MariaDB schema changes
		half-applied.
		"""
		if self.status not in ("In Progress", "Deleting", "Migrating"):
			frappe.throw("Cancel is only available while an operation is in progress.")

		if isinstance(confirm_destructive, str):
			confirm_destructive = confirm_destructive.lower() in ("1", "true", "yes")

		if self.status == "Migrating" and not confirm_destructive:
			frappe.throw(
				"Cancelling a running migration can leave the site's database "
				"schema in a half-applied state with no automatic rollback. "
				"Confirm explicitly to proceed.",
				title="Destructive cancel required",
			)

		job_name = self.operation_job_name
		prior_status = self.status
		# Rotate the token before enqueueing so any in-flight worker sees a
		# mismatch and exits cleanly.
		self.db_set("operation_token", secrets.token_hex(16))
		user = frappe.session.user or "unknown"
		op_label = {
			"In Progress": "creation",
			"Deleting": "deletion",
			"Migrating": "migration",
		}[prior_status]
		self.db_set("status", "Failed")
		if prior_status == "Migrating":
			self.db_set(
				"status_detail",
				f"Cancelled {op_label} by {user} (destructive cancel acknowledged).",
			)
		else:
			self.db_set("status_detail", f"Cancelled {op_label} by {user}.")
		self.db_set("operation_job_token", "")

		if job_name:
			correlation_id = metrics.new_correlation_id()
			with metrics.correlation_scope(correlation_id):
				metrics.logger("kubeport.site").info(
					"enqueue cancel_site_task site=%s job=%s", self.name, job_name
				)
			frappe.enqueue(
				"kubeport.tasks.site_tasks.cancel_site_task",
				cluster=self.cluster,
				namespace=self.namespace or "default",
				job_name=job_name,
				correlation_id=correlation_id,
				queue="long",
				enqueue_after_commit=True,
			)

		# Cascade the cancellation to any in-flight backup/restore that was
		# launched against this site.  Without this, those rows stay stuck in
		# Pending / In Progress / Restoring with a token that can never match
		# the rotated site token, so reconciliation skips them and
		# ``_has_in_flight_backup`` blocks new backups indefinitely.
		_cancel_inflight_backups_for_site(self.name, reason=f"Site operation cancelled by {user}.")

		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": self.name, "status": "Failed"},
			doctype="Frappe Site",
			docname=self.name,
		)
		frappe.msgprint(
			f"Cancellation requested for '{self.site_name}'.",
			alert=True,
			indicator="orange",
		)

	def on_trash(self):
		"""Guard direct row deletion against orphaning real sites on the bench.

		``Active`` rows must go through ``delete_site`` so the bench-side site
		(database + files) is dropped first; otherwise deleting the row would
		leak the site into permanent obscurity.

		``Migrating`` rows are refused: cancelling a running migration can
		corrupt the schema, and we want the operator to go through the gated
		``cancel_site`` path explicitly rather than backdoor a kill via row
		trash.

		For any other status, if the row carries an ``operation_job_name`` we
		rotate the operation token (so an in-flight reconciliation is rejected
		by its token check) and enqueue ``cancel_site_task`` to clean up the
		Job and its creds Secret.  This covers the in-flight cases
		(``In Progress`` / ``Deleting``) and a ``Failed`` row whose Job survived
		(cancelled mid-flight, drop-site that left the site present).
		"""
		if self.status == "Active":
			frappe.throw(
				"This site exists on the bench. Click 'Delete Site' to drop it first, "
				"then this row will be removed automatically."
			)

		if self.status == "Migrating":
			frappe.throw(
				"This site has a migration in progress. Cancel the migration first "
				"(it requires explicit confirmation), then delete the row."
			)

		if not self.operation_job_name:
			# Even with no site Job to cancel, an in-flight backup or restore
			# row referencing this site could exist; trash should not leave it
			# orphaned.
			_cancel_inflight_backups_for_site(self.name, reason="Source site row was deleted.")
			return

		self.db_set("operation_token", secrets.token_hex(16))
		correlation_id = metrics.new_correlation_id()
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.site").info(
				"enqueue cancel_site_task on_trash site=%s job=%s",
				self.name,
				self.operation_job_name,
			)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.cancel_site_task",
			cluster=self.cluster,
			namespace=self.namespace or "default",
			job_name=self.operation_job_name,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)
		_cancel_inflight_backups_for_site(self.name, reason="Source site row was deleted.")


def _format_observed_state_error(error: Exception) -> str:
	from kubeport.utils import helm

	if helm.is_release_not_found_error(error):
		return "No Helm release exists yet for this site's bench. Deploy the bench release before reading live state."
	return str(error)


def _cancel_inflight_backups_for_site(site_docname: str, *, reason: str) -> None:
	"""Fail any in-flight backup or restore rows linked to ``site_docname``.

	Called from ``FrappeSite.cancel_site`` and ``FrappeSite.on_trash`` to
	keep backup-row state consistent with the parent site's cancellation.
	The site's ``operation_token`` is already rotated, but each backup row
	carries an independent ``operation_token`` (set at insert and never
	rotated by site-level paths), so without this cascade the backup row
	stays in ``Pending`` / ``In Progress`` / ``Restoring`` forever and
	``_has_in_flight_backup`` blocks new backups.

	For each affected row we:
	  1. Rotate ``operation_token`` so any in-flight worker is a no-op.
	  2. Set ``status = 'Failed'`` with a clear ``status_detail``.
	  3. Best-effort enqueue ``cancel_site_task`` if a Job is recorded so
	     the cluster Job and its creds Secret are cleaned up.
	"""
	rows = frappe.get_all(
		"Frappe Site Backup",
		filters={
			"frappe_site": site_docname,
			"status": ("in", ("Pending", "In Progress", "Restoring")),
		},
		fields=["name", "cluster", "namespace", "operation_job_name"],
	)
	if not rows:
		return

	for row in rows:
		new_token = secrets.token_hex(16)
		frappe.db.set_value(
			"Frappe Site Backup",
			row.name,
			{
				"operation_token": new_token,
				"operation_job_token": "",
				"status": "Failed",
				"status_detail": reason[:500],
				"completed_at": frappe.utils.now_datetime(),
			},
		)
		frappe.publish_realtime(
			"frappe_site_backup_status_update",
			{"site_docname": site_docname, "backup_docname": row.name, "status": "Failed"},
			doctype="Frappe Site Backup",
			docname=row.name,
		)
		if row.operation_job_name and row.cluster:
			correlation_id = metrics.new_correlation_id()
			with metrics.correlation_scope(correlation_id):
				metrics.logger("kubeport.site").info(
					"enqueue cancel_site_task cascade site=%s backup=%s job=%s",
					site_docname,
					row.name,
					row.operation_job_name,
				)
			frappe.enqueue(
				"kubeport.tasks.site_tasks.cancel_site_task",
				cluster=row.cluster,
				namespace=row.namespace or "default",
				job_name=row.operation_job_name,
				correlation_id=correlation_id,
				queue="long",
				enqueue_after_commit=True,
			)
