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
		bench_release: DF.Link
		cluster: DF.Data | None
		creation_job_name: DF.Data | None
		creation_job_token: DF.Data | None
		db_root_password: DF.Password | None
		db_root_secret: DF.Data | None
		db_root_secret_key: DF.Data | None
		db_type: DF.Literal["mariadb", "postgres"]
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

		if not self.db_root_password and not self.db_root_secret:
			frappe.throw(
				"Either DB Root Password or DB Root Secret must be provided. "
				"DB Root Secret (referencing an existing Kubernetes Secret) is recommended."
			)

		self._validate_install_apps()

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
			frappe.throw(
				"This site already exists. Enable Force Create to recreate it."
			)

		operation_token = secrets.token_hex(16)
		self.db_set("status", "In Progress")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		# Reset the job-launching token; the worker sets it again once the Job
		# is actually submitted.  Clearing here prevents a stale reconciliation
		# from matching on a fresh operation_token.
		self.db_set("creation_job_token", "")
		self.db_set("creation_job_name", "")
		frappe.enqueue(
			"kubeport.tasks.site_tasks.create_site_task",
			site_docname=self.name,
			operation_token=operation_token,
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
		if self.status == "Failed" and not self.creation_job_name:
			# No Job ever ran for this row — there is no site on the bench to drop.
			# Operator can just delete the row directly.
			frappe.throw(
				"This site never reached Active and has no recorded operation Job. "
				"Delete the row directly — there is nothing to drop on the bench."
			)

		operation_token = secrets.token_hex(16)
		self.db_set("status", "Deleting")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		# Clear the previous operation's Job pointer; the worker sets these
		# again once the drop-site Job is actually submitted.
		self.db_set("creation_job_token", "")
		self.db_set("creation_job_name", "")
		frappe.enqueue(
			"kubeport.tasks.site_tasks.delete_site_task",
			site_docname=self.name,
			operation_token=operation_token,
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
		self.db_set("status", "Migrating")
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		self.db_set("creation_job_token", "")
		self.db_set("creation_job_name", "")
		frappe.enqueue(
			"kubeport.tasks.site_tasks.migrate_site_task",
			site_docname=self.name,
			operation_token=operation_token,
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
	def cancel_site(self):
		"""Cancel an in-flight operation (create / delete / migrate).

		Rotates the operation token so any concurrent worker or reconciler
		holding the old token is a no-op, marks the row ``Failed``, and
		enqueues Job cleanup.  Operator can re-issue the appropriate action
		from the ``Failed`` state.
		"""
		if self.status not in ("In Progress", "Deleting", "Migrating"):
			frappe.throw("Cancel is only available while an operation is in progress.")

		job_name = self.creation_job_name
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
		self.db_set("status_detail", f"Cancelled {op_label} by {user}.")
		self.db_set("creation_job_token", "")

		if job_name:
			frappe.enqueue(
				"kubeport.tasks.site_tasks.cancel_site_task",
				cluster=self.cluster,
				namespace=self.namespace or "default",
				job_name=job_name,
				queue="short",
				enqueue_after_commit=True,
			)

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
		leak the site into permanent obscurity.  Reconciliation removes the
		row itself once the drop-site Job confirms the site is gone.

		For in-flight operations (``In Progress`` / ``Deleting`` / ``Migrating``)
		we still let the operator delete the row, but rotate the token and
		clean up the K8s Job so it does not run untracked.
		"""
		if self.status == "Active":
			frappe.throw(
				"This site exists on the bench. Click 'Delete Site' to drop it first, "
				"then this row will be removed automatically."
			)

		if self.status not in ("In Progress", "Deleting", "Migrating"):
			return

		job_name = self.creation_job_name
		if not job_name:
			return

		self.db_set("operation_token", secrets.token_hex(16))
		frappe.enqueue(
			"kubeport.tasks.site_tasks.cancel_site_task",
			cluster=self.cluster,
			namespace=self.namespace or "default",
			job_name=job_name,
			queue="short",
			enqueue_after_commit=True,
		)
