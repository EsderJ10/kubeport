"""
Reconciliation Sweeper

Periodic task that compares desired state (Frappe DB) with actual state
(Kubernetes cluster) and flags drift.  Runs via ``scheduler_events`` in
``hooks.py`` every 5 minutes.

Covers four DocTypes:
- **Helm Release** — combines ``helm status`` with workload readiness checks
- **Service Bundle** — uses K8s API to check raw manifest resources
- **Frappe Site** — polls Kubernetes Job status for in-progress site creations
- **Frappe Site Backup** — polls backup/restore Jobs and finalizes backup rows
"""

from datetime import UTC, datetime
from typing import Any

import frappe
from kubernetes import client

from kubeport.utils.k8s_resources import check_resources_exist

_HEALTHY_RELEASE_STATUSES = ["Deployed", "Degraded"]
_IN_FLIGHT_RELEASE_STATUSES = ["In Progress", "Uninstalling"]
_HELM_STATUS_DETAIL_LIMIT = 500
_HELM_OPERATION_STALE_MINUTES = 30

# Three-state result from the bench ground-truth probe.  "unknown" means the
# probe could not reach the bench pod or exec failed transiently — the caller
# must NOT finalize the doc on this value; it should wait for the next tick.
SITE_PROBE_EXISTS = "exists"
SITE_PROBE_MISSING = "missing"
SITE_PROBE_UNKNOWN = "unknown"

# Maximum time the synchronous archive probe Job is allowed to take from
# submission to readable logs.  busybox is small (~1MB) so cold-pull + start
# is typically <10s; on a cluster with the image cached it is 2-3s.  This
# bounds reconciliation tick latency added by gone-Job recovery.
_BACKUP_PROBE_TIMEOUT_SECONDS = 15
_BACKUP_PROBE_IMAGE = "busybox:1.36"
# Cap how many synchronous archive probes one reconciliation tick will run.
# Each probe can take up to _BACKUP_PROBE_TIMEOUT_SECONDS, and the periodic
# task budget is the RQ default of 300 s minus the rest of the work.  At 5
# gone-Job probes we spend at most ~75 s; remaining gone-Job rows defer to
# the next tick (their state is already terminal on-disk so deferring is
# safe).
_BACKUP_PROBE_PER_TICK_LIMIT = 5
# busybox runs `cat` — adding the small overhead of a Job + Pod startup,
# 60 s is comfortably past the worst image-pull case while still letting
# the cluster reclaim the resources promptly.
_BACKUP_PROBE_TTL_SECONDS = 60

# The worker records operation_job_name after Kubernetes acknowledges Job
# apply.  The sweep must not race that apply -> db_set window.
_ORPHAN_SWEEP_GRACE_SECONDS = 300


def reconcile_all_releases():
	"""Periodic task: compare desired state (DB) with actual state (K8s cluster).

	Runs every 5 minutes via scheduler_events in hooks.py.
	"""
	_reconcile_helm_releases()
	_reconcile_stale_helm_operations()
	_reconcile_service_bundles()
	_reconcile_frappe_sites()
	_sweep_orphan_site_jobs()


def reconcile_site_backups():
	"""Periodic task: finalize in-flight Frappe Site Backup and Restore operations.

	Runs every 5 minutes as a separate scheduled job so that blocking K8s probe
	calls (pod exec, log reads) cannot consume the reconcile_all_releases budget
	and trigger the RQ 300-second task timeout.
	"""
	_reconcile_frappe_site_backups()


def _reconcile_helm_releases():
	"""Check all Deployed/Degraded Helm Releases.

	Combines ``helm status`` (release-level state) with the workload-readiness
	walker (per-resource Ready signals) to decide between ``Deployed``,
	``Degraded``, and ``Failed``.  ``pending-*`` helm states are treated as
	``Failed`` so a release stuck mid-upgrade no longer hides as "Degraded"
	indefinitely.

	Worker-owned states (``In Progress``, ``Uninstalling``) are deliberately
	skipped — those rows are owned by their workers and a reconciliation
	write would race the worker's terminal write.
	"""
	from kubeport.utils import helm
	from kubeport.utils.release_health import classify_release_from_cluster, walk

	releases = frappe.get_all(
		"Helm Release",
		filters={"status": ["in", _HEALTHY_RELEASE_STATUSES]},
		fields=["name", "cluster", "namespace", "release_name", "status", "operation_token"],
	)

	for release in releases:
		try:
			helm_result = helm.status(
				release_name=release.release_name,
				namespace=release.namespace or "default",
				cluster_name=release.cluster,
			)

			runtime_status = ""
			if isinstance(helm_result, dict):
				info = helm_result.get("info", {})
				if isinstance(info, dict):
					runtime_status = info.get("status", "")

			next_status, detail = classify_release_from_cluster(
				release_docname=release.name,
				runtime_status=runtime_status,
				walk_fn=walk,
			)

			updated = _set_helm_reconciliation_state(
				release_docname=release.name,
				expected_token=release.operation_token,
				next_status=next_status,
				detail=detail,
			)

			if updated and next_status != "Deployed":
				frappe.log_error(
					title=f"Helm Drift Detected: {release.name}",
					message=f"Status: {next_status}. Detail: {detail}",
				)

		except Exception as e:
			if _is_helm_release_not_found_error(e):
				detail = f"Helm release is missing from the cluster: {_truncate_status_detail(str(e))}"
			else:
				detail = f"Reconciliation error: {_truncate_status_detail(str(e))}"

			# If helm status fails entirely, mark as degraded with the error
			# (or skip if a concurrent operation has rotated the token).
			_set_helm_reconciliation_state(
				release_docname=release.name,
				expected_token=release.operation_token,
				next_status="Degraded",
				detail=detail,
			)
			frappe.log_error(
				title=f"Helm Reconciliation Error: {release.name}",
				message=str(e),
			)


def _reconcile_stale_helm_operations():
	"""Recover Helm Release rows whose worker-owned state has gone stale."""
	from kubeport.kubeport.doctype.helm_release.helm_release import (
		_get_site_image_digest_for_hash,
		calculate_release_spec_hash,
	)
	from kubeport.utils import helm
	from kubeport.utils.release_health import classify_release_from_cluster, walk

	releases = frappe.get_all(
		"Helm Release",
		filters={"status": ["in", _IN_FLIGHT_RELEASE_STATUSES]},
		fields=[
			"name",
			"cluster",
			"namespace",
			"release_name",
			"chart",
			"chart_version",
			"values",
			"site_image",
			"status",
			"operation_token",
			"operation_started_at",
			"modified",
		],
	)

	for release in releases:
		if not _helm_operation_is_stale(release):
			continue

		if release.status == "Uninstalling":
			_reconcile_stale_uninstall(release)
			continue

		try:
			helm_result = helm.status(
				release_name=release.release_name,
				namespace=release.namespace or "default",
				cluster_name=release.cluster,
			)
			runtime_status = _runtime_status_from_helm_result(helm_result)
			next_status, detail = classify_release_from_cluster(
				release_docname=release.name,
				runtime_status=runtime_status,
				walk_fn=walk,
			)

			fields: dict[str, object] = {
				"status": next_status,
				"helm_status_detail": _truncate_status_detail(f"Recovered stale operation: {detail}"),
				"operation_type": "",
				"operation_started_at": None,
			}
			if next_status in _HEALTHY_RELEASE_STATUSES:
				spec_hash = calculate_release_spec_hash(
					chart=release.chart,
					chart_version=release.chart_version,
					namespace=release.namespace,
					release_name=release.release_name,
					values_yaml=release.values,
					site_image=getattr(release, "site_image", None),
					site_image_digest=_get_site_image_digest_for_hash(getattr(release, "site_image", None)),
				)
				fields.update(
					{
						"last_applied_chart_version": release.chart_version or "",
						"desired_spec_hash": spec_hash,
						"last_applied_spec_hash": spec_hash,
						"pending_changes": 0,
					}
				)

			_set_stale_helm_operation_state(
				release_docname=release.name,
				expected_token=release.operation_token,
				expected_status=release.status,
				fields=fields,
			)
		except Exception as e:
			_set_stale_helm_operation_state(
				release_docname=release.name,
				expected_token=release.operation_token,
				expected_status=release.status,
				fields={
					"status": "Failed",
					"helm_status_detail": _truncate_status_detail(
						f"Stale operation reconciliation error: {e}"
					),
					"operation_type": "",
					"operation_started_at": None,
				},
			)
			frappe.log_error(
				title=f"Stale Helm Operation Recovery Failed: {release.name}",
				message=str(e),
			)


def _reconcile_stale_uninstall(release) -> None:
	from kubeport.utils import helm

	try:
		helm_result = helm.status(
			release_name=release.release_name,
			namespace=release.namespace or "default",
			cluster_name=release.cluster,
		)
		runtime_status = _runtime_status_from_helm_result(helm_result) or "unknown"
		_set_stale_helm_operation_state(
			release_docname=release.name,
			expected_token=release.operation_token,
			expected_status=release.status,
			fields={
				"status": "Failed",
				"helm_status_detail": _truncate_status_detail(
					"Stale uninstall: Helm still reports release status "
					f"'{runtime_status}' after {_HELM_OPERATION_STALE_MINUTES} minutes."
				),
				"operation_type": "",
				"operation_started_at": None,
			},
		)
	except Exception as e:
		if _is_helm_release_not_found_error(e):
			_set_stale_helm_operation_state(
				release_docname=release.name,
				expected_token=release.operation_token,
				expected_status=release.status,
				fields={
					"status": "Draft",
					"helm_revision": 0,
					"helm_status_detail": "",
					"last_applied_chart_version": "",
					"last_applied_spec_hash": "",
					"pending_changes": 0,
					"operation_type": "",
					"operation_started_at": None,
				},
			)
			return

		_set_stale_helm_operation_state(
			release_docname=release.name,
			expected_token=release.operation_token,
			expected_status=release.status,
			fields={
				"status": "Failed",
				"helm_status_detail": _truncate_status_detail(f"Stale uninstall reconciliation error: {e}"),
				"operation_type": "",
				"operation_started_at": None,
			},
		)
		frappe.log_error(
			title=f"Stale Helm Uninstall Recovery Failed: {release.name}",
			message=str(e),
		)


def _reconcile_service_bundles():
	"""Check all Deployed Service Bundles for resource drift.

	Groups bundles by cluster so only one ``ApiClient`` is built per cluster
	per reconciliation tick.
	"""
	from collections import defaultdict

	from kubeport.utils.k8s_client import get_k8s_api_client

	deployed_bundles = frappe.get_all(
		"Service Bundle",
		filters={"status": ["in", _HEALTHY_RELEASE_STATUSES]},
		fields=["name", "cluster", "namespace", "content", "status"],
	)

	by_cluster: dict[str, list["frappe._dict"]] = defaultdict(list)
	for bundle in deployed_bundles:
		by_cluster[bundle.cluster].append(bundle)

	for cluster_name, bundles in by_cluster.items():
		try:
			api_client = get_k8s_api_client(cluster_name)
		except Exception as e:
			for bundle in bundles:
				frappe.db.set_value("Service Bundle", bundle.name, "status", "Degraded")
				frappe.db.set_value(
					"Service Bundle",
					bundle.name,
					"status_detail",
					_truncate_status_detail(f"Reconciliation error: {e}"),
				)
				frappe.log_error(
					title=f"Reconciliation Error: Service Bundle {bundle.name}",
					message=str(e),
				)
			continue

		for bundle in bundles:
			try:
				is_healthy, detail = check_resources_exist(
					cluster_name=bundle.cluster,
					manifest_json=bundle.content,
					namespace=bundle.namespace or "default",
					doctype="Service Bundle",
					docname=bundle.name,
					api_client=api_client,
				)
				next_status = "Deployed" if is_healthy else "Degraded"
				if bundle.status != next_status:
					frappe.db.set_value("Service Bundle", bundle.name, "status", next_status)
				frappe.db.set_value(
					"Service Bundle",
					bundle.name,
					"status_detail",
					"" if is_healthy else _truncate_status_detail(detail),
				)

				if not is_healthy:
					frappe.log_error(
						title=f"State Drift Detected: Service Bundle {bundle.name}",
						message=detail,
					)
			except Exception as e:
				frappe.db.set_value("Service Bundle", bundle.name, "status", "Degraded")
				frappe.db.set_value(
					"Service Bundle",
					bundle.name,
					"status_detail",
					_truncate_status_detail(f"Reconciliation error: {e}"),
				)
				frappe.log_error(
					title=f"Reconciliation Error: Service Bundle {bundle.name}",
					message=str(e),
				)


_SITE_IN_FLIGHT_STATUSES = ("In Progress", "Deleting", "Migrating")
_BACKUP_IN_FLIGHT_STATUSES = ("In Progress", "Restoring")


def _reconcile_frappe_sites():
	"""Poll Kubernetes Job status for all in-flight Frappe Site operations.

	Three lifecycle states are reconciled here:

	- ``In Progress`` (creation): transitions to Active or Failed based on
	  Job exit + ground-truth bench probe.
	- ``Deleting``: on confirmed-missing site, deletes the Frappe Site row
	  itself; on still-present, lands the row in Failed.
	- ``Migrating``: on functional probe, transitions back to Active; on
	  broken or missing, lands Failed.

	All status writes re-check ``operation_token`` against ``operation_job_token``
	so that a concurrent supersession (force-recreate, cancel) cannot be
	overwritten by the previous operation's terminal state.
	"""
	from collections import defaultdict

	from kubernetes import client

	from kubeport.utils.k8s_client import get_k8s_api_client

	in_flight = frappe.get_all(
		"Frappe Site",
		filters={"status": ("in", _SITE_IN_FLIGHT_STATUSES)},
		fields=[
			"name",
			"cluster",
			"namespace",
			"status",
			"operation_job_name",
			"operation_job_token",
			"bench_release",
			"site_name",
		],
	)

	# Group by cluster so we only build one ApiClient per cluster per tick.
	by_cluster: dict[str, list["frappe._dict"]] = defaultdict(list)
	for site in in_flight:
		if site.operation_job_name:
			by_cluster[site.cluster].append(site)

	for cluster_name, sites in by_cluster.items():
		try:
			api_client = get_k8s_api_client(cluster_name)
		except Exception as e:
			for site in sites:
				frappe.log_error(
					title=f"Frappe Site Reconciliation Error: {site.name}",
					message=str(e),
				)
			continue

		batch_v1 = client.BatchV1Api(api_client=api_client)
		core_v1 = client.CoreV1Api(api_client=api_client)

		for site in sites:
			try:
				if site.status == "In Progress":
					_reconcile_site_create(site, batch_v1, core_v1)
				elif site.status == "Deleting":
					_reconcile_site_delete(site, batch_v1, core_v1)
				elif site.status == "Migrating":
					_reconcile_site_migrate(site, batch_v1, core_v1)
			except Exception as e:
				frappe.log_error(
					title=f"Frappe Site Reconciliation Error: {site.name}",
					message=str(e),
				)


def _reconcile_frappe_site_backups():
	"""Poll Kubernetes Job status for in-flight backup and restore operations."""
	from collections import defaultdict

	from kubernetes import client

	from kubeport.utils.k8s_client import get_k8s_api_client

	in_flight = frappe.get_all(
		"Frappe Site Backup",
		filters={"status": ("in", _BACKUP_IN_FLIGHT_STATUSES)},
		fields=[
			"name",
			"frappe_site",
			"cluster",
			"namespace",
			"status",
			"site_name",
			"source_bench_release",
			"operation_job_name",
			"operation_job_token",
			"operation_token",
			"storage_path",
			"size_bytes",
		],
	)

	by_cluster: dict[str, list["frappe._dict"]] = defaultdict(list)
	for backup in in_flight:
		if backup.operation_job_name:
			by_cluster[backup.cluster].append(backup)

	# Per-tick probe budget shared across every cluster in this reconcile pass.
	# Each gone-Job probe can take up to _BACKUP_PROBE_TIMEOUT_SECONDS, and the
	# periodic task lives inside the RQ default 300 s window — bounding the
	# total to _BACKUP_PROBE_PER_TICK_LIMIT keeps a fleet of clusters from
	# multiplying the cost.  Remaining gone-Job rows defer to the next tick:
	# their state is already terminal on disk, so deferring is safe.
	probe_budget = {"remaining": _BACKUP_PROBE_PER_TICK_LIMIT}

	for cluster_name, backups in by_cluster.items():
		try:
			api_client = get_k8s_api_client(cluster_name)
		except Exception as e:
			for backup in backups:
				frappe.log_error(
					title=f"Frappe Site Backup Reconciliation Error: {backup.name}",
					message=str(e),
				)
			continue

		batch_v1 = client.BatchV1Api(api_client=api_client)
		core_v1 = client.CoreV1Api(api_client=api_client)

		for backup in backups:
			try:
				if backup.status == "In Progress":
					_reconcile_site_backup(backup, batch_v1, core_v1, api_client, probe_budget)
				elif backup.status == "Restoring":
					_reconcile_site_restore(backup, batch_v1, core_v1)
			except Exception as e:
				frappe.log_error(
					title=f"Frappe Site Backup Reconciliation Error: {backup.name}",
					message=str(e),
				)


def _reconcile_site_backup(
	backup: "frappe._dict",
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
	api_client: "client.ApiClient",
	probe_budget: dict[str, int] | None = None,
) -> None:
	job, gone = _read_op_job(backup, batch_v1)
	if gone:
		# Job aged out (TTL elapsed) before reconciliation read its status.
		# size_bytes is only ever written on the Available transition itself,
		# so the only authoritative signal here is the PVC ground truth.
		if not _take_probe_budget(probe_budget):
			# Budget exhausted for this tick; the row stays In Progress and
			# the next tick will retry.  This is safe: gone-Job state is
			# already terminal on disk, deferring only delays recovery.
			return
		probe_state, probe_size = _probe_backup_archive_on_pvc(backup, api_client, core_v1)
		if probe_state == SITE_PROBE_EXISTS:
			_finalize_backup_status(
				backup,
				"In Progress",
				"Available",
				"",
				size_bytes=probe_size or 0,
			)
		elif probe_state == SITE_PROBE_MISSING:
			_finalize_backup_status(
				backup,
				"In Progress",
				"Failed",
				"Backup Job disappeared and the archive is not present on the PVC.",
			)
		# SITE_PROBE_UNKNOWN: defer to next tick.
		return

	if not _job_belongs_to_backup(job, backup):
		frappe.logger("kubeport").warning(
			"Skipping reconciliation for Frappe Site Backup '%s': Job '%s' is not labeled for this backup.",
			backup.name,
			backup.operation_job_name,
		)
		return

	succeeded = (job.status.succeeded or 0) if job.status else 0
	failed = (job.status.failed or 0) if job.status else 0

	if succeeded > 0:
		metadata = _extract_backup_success_metadata(core_v1, job, backup.namespace or "default")
		size_bytes = int(metadata.get("size_bytes") or 0)
		if size_bytes <= 0:
			_finalize_backup_status(
				backup,
				"In Progress",
				"Failed",
				"Backup Job succeeded but did not report a non-zero archive size.",
			)
			return
		# Verify the archive is actually present on the PVC before flipping
		# the row to Available — guards against the narrow window where the
		# Job exited zero but the inode was lost between exit and our read.
		# If the budget is exhausted, trust the Job logs and defer the
		# verification rather than risk flipping a good row to Failed.
		if not _take_probe_budget(probe_budget):
			_finalize_backup_status(
				backup,
				"In Progress",
				"Available",
				"",
				size_bytes=size_bytes,
				bench_archive_name=metadata.get("bench_archive_name") or "",
			)
			return
		probe_state, probe_size = _probe_backup_archive_on_pvc(backup, api_client, core_v1)
		if probe_state == SITE_PROBE_MISSING:
			_finalize_backup_status(
				backup,
				"In Progress",
				"Failed",
				"Backup Job reported success but the archive is not on the PVC.",
			)
			return
		# UNKNOWN: trust the Job logs (size_bytes > 0) — probe failure should
		# not flip a likely-good backup to Failed; defer-mode would risk the
		# Job aging out before the next tick can re-verify.
		final_size = probe_size if probe_state == SITE_PROBE_EXISTS and probe_size else size_bytes
		_finalize_backup_status(
			backup,
			"In Progress",
			"Available",
			"",
			size_bytes=final_size,
			bench_archive_name=metadata.get("bench_archive_name") or "",
		)
	elif failed > 0:
		detail = _extract_job_failure_detail(
			core_v1=core_v1,
			job=job,
			namespace=backup.namespace or "default",
			operation_label="bench backup",
		)
		_finalize_backup_status(backup, "In Progress", "Failed", _truncate_status_detail(detail))


def _reconcile_site_restore(
	backup: "frappe._dict",
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
) -> None:
	job, gone = _read_op_job(backup, batch_v1)
	if gone:
		_apply_restore_probe(backup, core_v1, failure_detail=None)
		return

	if not _job_belongs_to_backup(job, backup):
		frappe.logger("kubeport").warning(
			"Skipping reconciliation for Frappe Site Backup '%s': restore Job '%s' is not labeled for this backup.",
			backup.name,
			backup.operation_job_name,
		)
		return

	succeeded = (job.status.succeeded or 0) if job.status else 0
	failed = (job.status.failed or 0) if job.status else 0

	if succeeded > 0:
		_apply_restore_probe(backup, core_v1, failure_detail=None)
	elif failed > 0:
		detail = _extract_job_failure_detail(
			core_v1=core_v1,
			job=job,
			namespace=backup.namespace or "default",
			operation_label="bench restore",
		)
		_apply_restore_probe(backup, core_v1, failure_detail=detail)


def _apply_restore_probe(
	backup: "frappe._dict",
	core_v1: "client.CoreV1Api",
	failure_detail: str | None,
) -> None:
	site = _site_from_backup_for_probe(backup)
	probe = _probe_site_state(site, core_v1)
	if probe == SITE_PROBE_EXISTS:
		_finalize_restore_status(backup, "Active", "")
	elif probe == SITE_PROBE_MISSING:
		detail = failure_detail or "Restore Job ran but the site is not functional on the bench."
		_finalize_restore_status(backup, "Failed", _truncate_status_detail(detail))


def _site_from_backup_for_probe(backup: "frappe._dict") -> Any:
	return frappe._dict(
		{
			"name": backup.frappe_site or backup.name,
			"bench_release": backup.source_bench_release,
			"namespace": backup.namespace or "default",
			"site_name": backup.site_name,
		}
	)


def _read_op_job(
	site: "frappe._dict",
	batch_v1: "client.BatchV1Api",
):
	"""Read the operation Job for ``site``; return (job, status_404) tuple.

	Returns ``(job, False)`` on success, ``(None, True)`` when the Job no
	longer exists (TTL cleanup before we got here), or raises on other API
	errors.
	"""
	from kubernetes.client.rest import ApiException

	try:
		job = batch_v1.read_namespaced_job(
			name=site.operation_job_name,
			namespace=site.namespace or "default",
		)
		return job, False
	except ApiException as e:
		if e.status == 404:
			return None, True
		raise


def _reconcile_site_create(
	site: "frappe._dict",
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
):
	"""Reconcile an ``In Progress`` row: transition to Active or Failed."""
	job, gone = _read_op_job(site, batch_v1)

	if gone:
		# Job was cleaned up (ttlSecondsAfterFinished elapsed) before we read
		# its final status.  Fall back to the bench probe so the row does not
		# sit in "In Progress" forever.
		frappe.logger("kubeport").warning(
			"Creation Job '%s' for Frappe Site '%s' no longer exists "
			"(likely cleaned up by TTL). Falling back to bench probe.",
			site.operation_job_name,
			site.name,
		)
		probe = _probe_site_state(site, core_v1)
		if probe == SITE_PROBE_EXISTS:
			_finalize_site_status(site, "In Progress", "Active", "")
		elif probe == SITE_PROBE_MISSING:
			_finalize_site_status(
				site,
				"In Progress",
				"Failed",
				_truncate_status_detail(
					"Creation Job disappeared before reconciliation could read "
					"its status (TTL expired). The site does not exist on the bench."
				),
			)
		# SITE_PROBE_UNKNOWN: defer to next tick.
		return

	if not _job_belongs_to_site(job, site):
		frappe.logger("kubeport").warning(
			"Skipping reconciliation for Frappe Site '%s': Job '%s' is not labeled for this site.",
			site.name,
			site.operation_job_name,
		)
		return

	succeeded = (job.status.succeeded or 0) if job.status else 0
	failed = (job.status.failed or 0) if job.status else 0

	if succeeded > 0:
		_finalize_site_status(site, "In Progress", "Active", "")
	elif failed > 0:
		# Job exit code is not authoritative — bench new-site can exit non-zero
		# even on success (--install-app warnings, etc.).  Probe the bench.
		probe = _probe_site_state(site, core_v1)
		if probe == SITE_PROBE_EXISTS:
			_finalize_site_status(site, "In Progress", "Active", "")
		elif probe == SITE_PROBE_MISSING:
			detail = _extract_job_failure_detail(
				core_v1=core_v1,
				job=job,
				namespace=site.namespace or "default",
				operation_label="bench new-site",
			)
			if _finalize_site_status(site, "In Progress", "Failed", _truncate_status_detail(detail)):
				frappe.log_error(
					title=f"Frappe Site Creation Failed: {site.name}",
					message=detail,
				)
		# SITE_PROBE_UNKNOWN: defer.
	# Neither succeeded nor failed yet — Job still running, leave row as-is.


def _reconcile_site_delete(
	site: "frappe._dict",
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
):
	"""Reconcile a ``Deleting`` row: delete the doc on confirmed-missing site.

	Job-exit signal alone is not authoritative for drop-site either: an
	exit-zero Job that left the site untouched (mis-pointed bench, DB error
	on a different site) would otherwise get the row removed without the
	site actually being gone.  The probe is the source of truth.
	"""
	job, gone = _read_op_job(site, batch_v1)

	if gone:
		# Job already cleaned up — fall through to probe.
		frappe.logger("kubeport").warning(
			"Drop-site Job '%s' for Frappe Site '%s' no longer exists "
			"(likely cleaned up by TTL). Falling back to bench probe.",
			site.operation_job_name,
			site.name,
		)
		_apply_delete_probe(site, core_v1, failure_detail=None)
		return

	if not _job_belongs_to_site(job, site):
		frappe.logger("kubeport").warning(
			"Skipping reconciliation for Frappe Site '%s': drop-site Job '%s' is not labeled for this site.",
			site.name,
			site.operation_job_name,
		)
		return

	succeeded = (job.status.succeeded or 0) if job.status else 0
	failed = (job.status.failed or 0) if job.status else 0

	if succeeded > 0:
		_apply_delete_probe(site, core_v1, failure_detail=None)
	elif failed > 0:
		# Probe still — drop-site can exit non-zero (e.g. site_config.json
		# already gone from a prior partial run) yet still leave the bench in
		# a consistent state.
		detail = _extract_job_failure_detail(
			core_v1=core_v1,
			job=job,
			namespace=site.namespace or "default",
			operation_label="bench drop-site",
		)
		_apply_delete_probe(site, core_v1, failure_detail=detail)
	# Job still running — leave row as-is.


def _apply_delete_probe(
	site: "frappe._dict",
	core_v1: "client.CoreV1Api",
	failure_detail: str | None,
):
	"""Map a drop-site probe result onto a row decision.

	- MISSING → delete the row (the site is gone, the doc has done its job).
	- EXISTS → mark Failed; surface the Job logs (if we have them) so the
	  operator sees why the drop did not take.
	- UNKNOWN → defer to the next tick.
	"""
	probe = _probe_site_state(site, core_v1)
	if probe == SITE_PROBE_MISSING:
		_finalize_site_deletion(site)
		return
	if probe == SITE_PROBE_EXISTS:
		detail = failure_detail or "Drop-site Job ran but the site is still present on the bench."
		if _finalize_site_status(site, "Deleting", "Failed", _truncate_status_detail(detail)):
			frappe.log_error(
				title=f"Frappe Site Drop Failed: {site.name}",
				message=detail,
			)
	# SITE_PROBE_UNKNOWN: defer.


def _reconcile_site_migrate(
	site: "frappe._dict",
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
):
	"""Reconcile a ``Migrating`` row: Active on functional, Failed on broken."""
	job, gone = _read_op_job(site, batch_v1)

	if gone:
		frappe.logger("kubeport").warning(
			"Migrate Job '%s' for Frappe Site '%s' no longer exists "
			"(likely cleaned up by TTL). Falling back to bench probe.",
			site.operation_job_name,
			site.name,
		)
		probe = _probe_site_state(site, core_v1)
		if probe == SITE_PROBE_EXISTS:
			_finalize_site_status(site, "Migrating", "Active", "")
		elif probe == SITE_PROBE_MISSING:
			_finalize_site_status(
				site,
				"Migrating",
				"Failed",
				_truncate_status_detail(
					"Migrate Job disappeared before reconciliation could read "
					"its status (TTL expired) and the site is not functional."
				),
			)
		return

	if not _job_belongs_to_site(job, site):
		frappe.logger("kubeport").warning(
			"Skipping reconciliation for Frappe Site '%s': migrate Job '%s' is not labeled for this site.",
			site.name,
			site.operation_job_name,
		)
		return

	succeeded = (job.status.succeeded or 0) if job.status else 0
	failed = (job.status.failed or 0) if job.status else 0

	if succeeded > 0:
		probe = _probe_site_state(site, core_v1)
		if probe == SITE_PROBE_EXISTS:
			_finalize_site_status(site, "Migrating", "Active", "")
		elif probe == SITE_PROBE_MISSING:
			detail = _extract_job_failure_detail(
				core_v1=core_v1,
				job=job,
				namespace=site.namespace or "default",
				operation_label="bench migrate",
			)
			if _finalize_site_status(site, "Migrating", "Failed", _truncate_status_detail(detail)):
				frappe.log_error(
					title=f"Frappe Site Migrate Broke Site: {site.name}",
					message=detail,
				)
		# UNKNOWN: defer.
	elif failed > 0:
		# Migrate Job exited non-zero — but if the probe still shows the site
		# functional, treat it as a false negative and recover to Active.
		probe = _probe_site_state(site, core_v1)
		if probe == SITE_PROBE_EXISTS:
			_finalize_site_status(site, "Migrating", "Active", "")
		elif probe == SITE_PROBE_MISSING:
			detail = _extract_job_failure_detail(
				core_v1=core_v1,
				job=job,
				namespace=site.namespace or "default",
				operation_label="bench migrate",
			)
			if _finalize_site_status(site, "Migrating", "Failed", _truncate_status_detail(detail)):
				frappe.log_error(
					title=f"Frappe Site Migrate Failed: {site.name}",
					message=detail,
				)
		# UNKNOWN: defer.


def _finalize_site_status(
	site: "frappe._dict",
	expected_status: str,
	next_status: str,
	detail: str,
) -> bool:
	"""Write a terminal status for a Frappe Site, guarded by the operation token.

	``expected_status`` is the in-flight status the row must currently be in
	for this transition to apply (``In Progress`` for creation, ``Deleting``
	or ``Migrating`` for the lifecycle ops).  Returns True if the transition
	was applied, False if it was skipped because a concurrent operation has
	superseded this Job.
	"""
	current = frappe.db.get_value(
		"Frappe Site",
		site.name,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current or current.get("status") != expected_status:
		return False
	if current.get("operation_token") != site.operation_job_token:
		frappe.logger("kubeport").info(
			"Skipping stale reconciliation for Frappe Site '%s' — current "
			"operation_token does not match the token that launched job '%s'.",
			site.name,
			site.operation_job_name,
		)
		return False

	frappe.db.set_value(
		"Frappe Site",
		site.name,
		{
			"status": next_status,
			"status_detail": detail,
		},
	)
	frappe.publish_realtime(
		"frappe_site_status_update",
		{"site_docname": site.name, "status": next_status},
		doctype="Frappe Site",
		docname=site.name,
	)
	return True


def _finalize_backup_status(
	backup: "frappe._dict",
	expected_status: str,
	next_status: str,
	detail: str,
	*,
	size_bytes: int | None = None,
	bench_archive_name: str = "",
) -> bool:
	current = frappe.db.get_value(
		"Frappe Site Backup",
		backup.name,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current or current.get("status") != expected_status:
		return False
	if current.get("operation_token") != backup.operation_job_token:
		frappe.logger("kubeport").info(
			"Skipping stale reconciliation for Frappe Site Backup '%s' — operation token rotated.",
			backup.name,
		)
		return False

	fields: dict[str, object] = {
		"status": next_status,
		"status_detail": detail,
		"completed_at": frappe.utils.now_datetime(),
		"operation_job_name": "",
		"operation_job_token": "",
	}
	if size_bytes is not None:
		fields["size_bytes"] = size_bytes
	if bench_archive_name:
		fields["bench_archive_name"] = bench_archive_name
	frappe.db.set_value("Frappe Site Backup", backup.name, fields)

	if backup.frappe_site:
		parent_status = "Active" if next_status in ("Available", "Failed") else None
		if parent_status:
			_finalize_site_status(
				frappe._dict(
					{
						"name": backup.frappe_site,
						"operation_job_token": backup.operation_job_token,
						"operation_job_name": backup.operation_job_name,
					}
				),
				"In Progress",
				parent_status,
				"" if next_status == "Available" else detail,
			)

	frappe.publish_realtime(
		"frappe_site_backup_status_update",
		{"site_docname": backup.frappe_site, "backup_docname": backup.name, "status": next_status},
		doctype="Frappe Site Backup",
		docname=backup.name,
	)
	return True


def _finalize_restore_status(
	backup: "frappe._dict",
	site_status: str,
	detail: str,
) -> bool:
	current = frappe.db.get_value(
		"Frappe Site Backup",
		backup.name,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current or current.get("status") != "Restoring":
		return False
	if current.get("operation_token") != backup.operation_job_token:
		return False

	frappe.db.set_value(
		"Frappe Site Backup",
		backup.name,
		{
			"status": "Available",
			"status_detail": detail,
			"completed_at": frappe.utils.now_datetime(),
			"operation_job_name": "",
			"operation_job_token": "",
		},
	)
	if backup.frappe_site:
		_finalize_site_status(
			frappe._dict(
				{
					"name": backup.frappe_site,
					"operation_job_token": backup.operation_job_token,
					"operation_job_name": backup.operation_job_name,
				}
			),
			"Migrating",
			site_status,
			detail,
		)
	frappe.publish_realtime(
		"frappe_site_backup_status_update",
		{"site_docname": backup.frappe_site, "backup_docname": backup.name, "status": "Available"},
		doctype="Frappe Site Backup",
		docname=backup.name,
	)
	return True


def _finalize_site_deletion(site: "frappe._dict") -> bool:
	"""Delete the Frappe Site row after a confirmed-missing probe.

	Token + status guard mirrors ``_finalize_site_status`` so a concurrent
	supersession (cancel, force action) cannot delete a row that has moved
	on to a new operation.

	The ``Active``-refusing branch in ``FrappeSite.on_trash`` does not fire
	for ``Deleting`` rows, so the doc deletion succeeds.  The publish-realtime
	event uses status ``Deleted`` so any open form can route the user away
	from a now-404 doc.
	"""
	current = frappe.db.get_value(
		"Frappe Site",
		site.name,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current or current.get("status") != "Deleting":
		return False
	if current.get("operation_token") != site.operation_job_token:
		frappe.logger("kubeport").info(
			"Skipping stale delete-finalize for Frappe Site '%s' — current "
			"operation_token does not match the token that launched job '%s'.",
			site.name,
			site.operation_job_name,
		)
		return False

	frappe.publish_realtime(
		"frappe_site_status_update",
		{"site_docname": site.name, "status": "Deleted"},
		doctype="Frappe Site",
		docname=site.name,
	)
	frappe.delete_doc(
		"Frappe Site",
		site.name,
		ignore_permissions=True,
		force=True,
		delete_permanently=True,
	)
	return True


def _probe_site_state(site: "frappe._dict", core_v1: "client.CoreV1Api") -> str:
	"""Return one of SITE_PROBE_EXISTS / SITE_PROBE_MISSING / SITE_PROBE_UNKNOWN.

	A presence-only check (``site_config.json`` exists) is insufficient:
	``bench new-site`` writes that file after creating the database but
	**before** running the framework schema install or per-app installs.
	A Job that fails during those later stages would leave a broken site
	that still looks "present" to a directory scan.

	Two-stage ground-truth check:

	1. Fast: the site directory exists (reuses the discovery helper).
	2. Strong: ``bench --site <name> list-apps`` exits 0 inside the bench
	   pod, which requires a reachable DB and populated framework schema.

	Exceptions reaching the bench (pod selection, exec failure) surface as
	``SITE_PROBE_UNKNOWN`` so that a transient bench rollout does not cause
	a terminal "Failed" transition on a site that is really fine — the
	caller simply retries on the next reconcile tick.
	"""
	from kubeport.utils.discovery import _exec_list_sites, _select_site_discovery_pod

	try:
		release = frappe.get_doc("Helm Release", site.bench_release)
		namespace = site.namespace or "default"
		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release.release_name,
		)
		existing_sites = _exec_list_sites(
			core_v1=core_v1,
			namespace=namespace,
			pod=ref_pod,
		)
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Could not probe site '%s' — bench unreachable, treating as unknown: %s",
			site.name,
			e,
		)
		return SITE_PROBE_UNKNOWN

	if site.site_name not in existing_sites:
		return SITE_PROBE_MISSING

	try:
		is_functional = _exec_bench_site_functional(
			core_v1=core_v1,
			namespace=namespace,
			pod=ref_pod,
			site_name=site.site_name,
		)
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Functional probe for site '%s' failed transiently, treating as unknown: %s",
			site.name,
			e,
		)
		return SITE_PROBE_UNKNOWN

	# Directory exists; functional check decides usability.  A non-functional
	# site directory is treated as SITE_PROBE_MISSING so the caller records the
	# Job failure reason instead of prematurely calling it Active.
	return SITE_PROBE_EXISTS if is_functional else SITE_PROBE_MISSING


def _take_probe_budget(probe_budget: dict[str, int] | None) -> bool:
	"""Atomically consume one slot from the per-tick archive-probe budget.

	The budget object is a small mutable dict so the cap survives across
	the per-cluster loop in ``_reconcile_frappe_site_backups`` without
	threading the counter through every call.  Returns True if a slot was
	consumed (caller may proceed to probe), False if the budget is
	exhausted (caller should defer to the next tick).  ``None`` means
	"no caller-imposed cap" — used by tests that want unlimited probing.
	"""
	if probe_budget is None:
		return True
	if probe_budget.get("remaining", 0) <= 0:
		return False
	probe_budget["remaining"] -= 1
	return True


def _probe_backup_archive_on_pvc(
	backup: "frappe._dict",
	api_client: "client.ApiClient",
	core_v1: "client.CoreV1Api",
) -> tuple[str, int | None]:
	"""Verify the backup archive on the namespace-local ``kubeport-backups`` PVC.

	The bench backup script writes ``<archive>.size`` and ``<archive>.name``
	sidecar files only after ``tar`` and ``stat`` both succeed (see
	``_bench_backup_command`` in ``site_tasks``).  Their presence is the
	ground-truth signal that the archive was fully flushed; their absence
	means the archive should be treated as missing even if the Job exited
	zero.

	Bench workload pods do not mount the backup PVC, so we cannot exec on
	an existing pod.  Instead we submit a short-lived ``busybox`` probe
	Job with the PVC mounted (a Job rather than a Pod so the cluster's
	``ttlSecondsAfterFinished`` reaps it even if our worker is hard-killed
	between submission and the explicit delete).  Wait synchronously up
	to ``_BACKUP_PROBE_TIMEOUT_SECONDS``, read the pod's logs, and
	best-effort delete the Job.  Returns one of:
	  - ``("exists", size_bytes)`` when the sidecar reports a positive size
	  - ``("missing", None)`` when the sidecar is absent or zero-size
	  - ``("unknown", None)`` on probe submission / read failures so the
	    caller defers to the next reconcile tick.
	"""
	import secrets
	import time

	from kubernetes.client.rest import ApiException

	from kubeport.tasks.site_tasks import (
		BACKUP_MOUNT_PATH,
		BACKUP_PVC_NAME,
		MANAGED_BY_LABEL,
		MANAGED_BY_VALUE,
		OPERATION_LABEL,
		_safe_label_value,
	)
	from kubeport.utils.k8s_resources import apply_resource

	storage_path = backup.storage_path or ""
	if not storage_path:
		return (SITE_PROBE_UNKNOWN, None)

	namespace = backup.namespace or "default"
	slug = _safe_label_value(backup.name)[:35] or "probe"
	job_name = f"ks-probe-{slug}-{secrets.token_hex(3)}"
	# Tiny shell: print the size sidecar if present, otherwise the literal
	# string MISSING.  We never trust file contents beyond a small integer.
	probe_cmd = f'if [ -f "{storage_path}.size" ]; then cat "{storage_path}.size"; else echo MISSING; fi'
	manifest: dict[str, Any] = {
		"apiVersion": "batch/v1",
		"kind": "Job",
		"metadata": {
			"name": job_name,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
				OPERATION_LABEL: "probe-backup-archive",
			},
		},
		"spec": {
			"backoffLimit": 0,
			"ttlSecondsAfterFinished": _BACKUP_PROBE_TTL_SECONDS,
			"activeDeadlineSeconds": _BACKUP_PROBE_TIMEOUT_SECONDS,
			"template": {
				"metadata": {
					"labels": {
						MANAGED_BY_LABEL: MANAGED_BY_VALUE,
						OPERATION_LABEL: "probe-backup-archive",
					},
				},
				"spec": {
					"restartPolicy": "Never",
					"containers": [
						{
							"name": "probe",
							"image": _BACKUP_PROBE_IMAGE,
							"command": ["sh", "-c", probe_cmd],
							"volumeMounts": [
								{
									"name": "kubeport-backups",
									"mountPath": BACKUP_MOUNT_PATH,
								}
							],
						}
					],
					"volumes": [
						{
							"name": "kubeport-backups",
							"persistentVolumeClaim": {"claimName": BACKUP_PVC_NAME},
						}
					],
				},
			},
		},
	}

	try:
		apply_resource(api_client, manifest, namespace)
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Could not submit archive probe Job for backup '%s': %s",
			backup.name,
			e,
		)
		return (SITE_PROBE_UNKNOWN, None)

	from kubernetes import client

	batch_v1 = client.BatchV1Api(api_client=api_client)
	deadline = time.monotonic() + _BACKUP_PROBE_TIMEOUT_SECONDS
	job_terminal = False
	while time.monotonic() < deadline:
		try:
			job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace, _request_timeout=5)
			succeeded = (job.status.succeeded or 0) if job.status else 0
			failed = (job.status.failed or 0) if job.status else 0
			if succeeded > 0 or failed > 0:
				job_terminal = True
				break
		except ApiException:
			pass
		time.sleep(1)

	logs = ""
	try:
		pods = core_v1.list_namespaced_pod(
			namespace=namespace,
			label_selector=f"job-name={job_name}",
			_request_timeout=5,
		)
		for pod in pods.items or []:
			pod_meta = getattr(pod, "metadata", None)
			pod_name = pod_meta.name if pod_meta else None
			if not pod_name:
				continue
			try:
				logs = (
					core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, _request_timeout=5)
					or ""
				)
				if logs:
					break
			except Exception:
				continue
	except Exception:
		pass

	# Best-effort delete.  ttlSecondsAfterFinished is the safety net for the
	# leak path (worker hard-killed between submit and delete) so this call
	# is purely an optimisation to free cluster resources sooner.
	try:
		batch_v1.delete_namespaced_job(
			name=job_name,
			namespace=namespace,
			propagation_policy="Background",
			_request_timeout=5,
		)
	except Exception:
		pass

	if not job_terminal:
		return (SITE_PROBE_UNKNOWN, None)

	output = (logs or "").strip()
	if output == "MISSING" or not output:
		return (SITE_PROBE_MISSING, None)
	try:
		size = int(output)
	except ValueError:
		return (SITE_PROBE_UNKNOWN, None)
	if size <= 0:
		return (SITE_PROBE_MISSING, None)
	return (SITE_PROBE_EXISTS, size)


def _job_belongs_to_site(job: "client.V1Job", site: "frappe._dict") -> bool:
	"""Return True if the Job's labels match the expected site docname.

	Reconciliation reads the Job by name only, so a hash collision or a
	stale ``operation_job_name`` pointing at an unrelated Job would otherwise
	let us finalize the wrong site's status.  The label has 48 bits of
	entropy at the doc level plus the operator-selected site name, so a
	mismatch is a strong signal to skip.
	"""
	from kubeport.tasks.site_tasks import MANAGED_BY_VALUE, SITE_DOC_LABEL, _safe_label_value

	metadata = getattr(job, "metadata", None)
	labels = (metadata.labels or {}) if metadata and metadata.labels else {}
	if labels.get("app.kubernetes.io/managed-by") != MANAGED_BY_VALUE:
		return False
	return labels.get(SITE_DOC_LABEL) == _safe_label_value(site.name)


def _job_belongs_to_backup(job: "client.V1Job", backup: "frappe._dict") -> bool:
	from kubeport.tasks.site_tasks import MANAGED_BY_VALUE, SITE_BACKUP_DOC_LABEL, _safe_label_value

	metadata = getattr(job, "metadata", None)
	labels = (metadata.labels or {}) if metadata and metadata.labels else {}
	if labels.get("app.kubernetes.io/managed-by") != MANAGED_BY_VALUE:
		return False
	return labels.get(SITE_BACKUP_DOC_LABEL) == _safe_label_value(backup.name)


def _extract_backup_success_metadata(
	core_v1: "client.CoreV1Api",
	job: "client.V1Job",
	namespace: str,
) -> dict[str, str | int]:
	logs = _read_job_logs(core_v1, job, namespace, tail_lines=80)
	metadata: dict[str, str | int] = {}
	for line in (logs or "").splitlines():
		if line.startswith("KUBEPORT_BACKUP_SIZE="):
			try:
				metadata["size_bytes"] = int(line.split("=", 1)[1].strip())
			except ValueError:
				pass
		elif line.startswith("KUBEPORT_BENCH_ARCHIVE="):
			metadata["bench_archive_name"] = line.split("=", 1)[1].strip()
		elif line.startswith("KUBEPORT_BACKUP_ARCHIVE="):
			metadata["storage_path"] = line.split("=", 1)[1].strip()
	return metadata


def _read_job_logs(
	core_v1: "client.CoreV1Api",
	job: "client.V1Job",
	namespace: str,
	*,
	tail_lines: int,
) -> str:
	if not job.metadata or not job.metadata.name:
		return ""
	job_name = job.metadata.name
	try:
		pods = core_v1.list_namespaced_pod(
			namespace=namespace,
			label_selector=f"job-name={job_name}",
			_request_timeout=15,
		)
		for pod in pods.items or []:
			pod_name = pod.metadata.name if pod.metadata else None
			if not pod_name:
				continue
			try:
				return (
					core_v1.read_namespaced_pod_log(
						name=pod_name,
						namespace=namespace,
						tail_lines=tail_lines,
						_request_timeout=15,
					)
					or ""
				)
			except Exception:
				continue
	except Exception:
		return ""
	return ""


def _sweep_orphan_site_jobs():
	"""Delete site-creation Jobs the DocType layer no longer references.

	Covers the narrow failure mode where the background worker applies a
	Job successfully but is hard-killed before it can ``db_set`` the
	``operation_job_name`` on the row.  Nothing else tracks those Jobs:
	``on_trash`` early-returns on empty ``operation_job_name`` and
	``_reconcile_frappe_sites`` filters on the same field.

	The sweep is scoped to (cluster, namespace) pairs that currently have
	at least one ``Frappe Site`` row so we never manufacture a cluster
	connection just to look for orphans.  Jobs are identified by their
	``app.kubernetes.io/managed-by=kubeport`` + ``kubeport.io/frappe-site``
	labels (written by ``_build_op_job_manifest``).
	"""
	from collections import defaultdict

	from kubernetes import client

	from kubeport.tasks.site_tasks import (
		MANAGED_BY_LABEL,
		MANAGED_BY_VALUE,
		OPERATION_LABEL,
		SELF_MANAGED_OPERATION_VALUES,
		SITE_BACKUP_DOC_LABEL,
		SITE_DOC_LABEL,
		_best_effort_delete_job,
	)
	from kubeport.utils.k8s_client import get_k8s_api_client

	all_sites = frappe.get_all(
		"Frappe Site",
		fields=["name", "cluster", "namespace", "operation_job_name"],
	)
	all_backups = frappe.get_all(
		"Frappe Site Backup",
		fields=["name", "cluster", "namespace", "operation_job_name"],
	)

	# (cluster, namespace) -> set of Job names currently referenced by any doc.
	tracked: dict[tuple[str, str], set[str]] = defaultdict(set)
	for site in all_sites:
		if not site.cluster:
			continue
		ns = site.namespace or "default"
		if site.operation_job_name:
			tracked[(site.cluster, ns)].add(site.operation_job_name)
		else:
			# Touch the key so we still sweep the namespace even when every row
			# has an empty operation_job_name (the exact case this sweep targets).
			tracked.setdefault((site.cluster, ns), set())
	for backup in all_backups:
		if not backup.cluster:
			continue
		ns = backup.namespace or "default"
		if backup.operation_job_name:
			tracked[(backup.cluster, ns)].add(backup.operation_job_name)
		else:
			tracked.setdefault((backup.cluster, ns), set())

	for (cluster_name, namespace), known_names in tracked.items():
		try:
			api_client = get_k8s_api_client(cluster_name)
		except Exception as e:
			frappe.log_error(
				title=f"Frappe Site orphan sweep: could not build K8s client for '{cluster_name}'",
				message=str(e),
			)
			continue

		try:
			batch_v1 = client.BatchV1Api(api_client=api_client)
			jobs = batch_v1.list_namespaced_job(
				namespace=namespace,
				label_selector=f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}",
				_request_timeout=15,
			)
		except Exception as e:
			frappe.log_error(
				title=f"Frappe Site orphan sweep: list failed for '{cluster_name}/{namespace}'",
				message=str(e),
			)
			continue

		now = datetime.now(UTC)
		for job in jobs.items or []:
			metadata = getattr(job, "metadata", None)
			job_name = metadata.name if metadata and metadata.name else None
			if not job_name or job_name in known_names:
				continue
			labels = (metadata.labels or {}) if metadata and metadata.labels else {}
			if SITE_DOC_LABEL not in labels and SITE_BACKUP_DOC_LABEL not in labels:
				continue
			# Self-managed lifecycle Jobs (e.g., archive-delete) clean themselves
			# up via ttlSecondsAfterFinished and are intentionally not tracked
			# by any DocType row.  Skipping them here is what keeps the sweep
			# from racing the cleanup it just submitted.
			if labels.get(OPERATION_LABEL) in SELF_MANAGED_OPERATION_VALUES:
				continue
			created = getattr(metadata, "creation_timestamp", None) if metadata else None
			if created is not None:
				if created.tzinfo is None:
					created = created.replace(tzinfo=UTC)
				age_seconds = (now - created).total_seconds()
				if age_seconds < _ORPHAN_SWEEP_GRACE_SECONDS:
					continue
			frappe.logger("kubeport").warning(
				"Sweeping orphan Frappe Site Job '%s' in '%s/%s' — not referenced by any Frappe Site row.",
				job_name,
				cluster_name,
				namespace,
			)
			_best_effort_delete_job(api_client, job_name, namespace)


def _exec_bench_site_functional(
	core_v1: "client.CoreV1Api",
	namespace: str,
	pod: "client.V1Pod",
	site_name: str,
) -> bool:
	"""Return True iff ``bench --site <name> list-apps`` exits 0 inside the pod.

	That bench subcommand opens a DB connection and reads the installed-app
	list from the framework schema. A zero exit is a tight proxy for "the
	site is usable": it requires both a reachable DB and a populated schema.

	We gate success on an ``__OK__`` sentinel instead of parsing the command
	output because the kubernetes stream API does not expose the remote exit
	code without switching to the WebSocket client.
	"""
	from kubernetes.stream import stream

	metadata = getattr(pod, "metadata", None)
	spec = getattr(pod, "spec", None)
	if not metadata or not metadata.name:
		return False

	container_name = ""
	if spec and spec.containers:
		container_name = spec.containers[0].name or ""

	# Positional arg ``$1`` keeps site_name out of any direct shell-expansion
	# context: the value is bound by the exec layer, not interpolated by the
	# caller.  The sentinel lets us distinguish a zero exit from noisy output.
	command = [
		"sh",
		"-lc",
		'bench --site "$1" list-apps >/dev/null 2>&1 && echo __OK__ || echo __FAIL__',
		"sh",
		site_name,
	]
	exec_kwargs: dict[str, Any] = {
		"name": metadata.name,
		"namespace": namespace,
		"command": command,
		"stderr": True,
		"stdin": False,
		"stdout": True,
		"tty": False,
		"_request_timeout": 30.0,
	}
	if container_name:
		exec_kwargs["container"] = container_name

	# Exec transport errors propagate so the caller can distinguish "site is
	# not functional" (False return) from "probe failed transiently" (raises).
	output = stream(core_v1.connect_get_namespaced_pod_exec, **exec_kwargs)
	return "__OK__" in (output or "")


def _extract_job_failure_detail(
	core_v1: "client.CoreV1Api",
	job: "client.V1Job",
	namespace: str,
	*,
	operation_label: str = "bench operation",
) -> str:
	"""Return a useful failure message from the Job pod's stdout log.

	Kubernetes's terminated.reason is always "Error" for any non-zero exit and
	terminated.message is empty unless terminationMessagePath is configured in
	the pod spec (we did not set it).  Fetching the actual pod log gives a far
	more actionable message — it contains the bench command output including the
	real error from the database or app layer.

	``operation_label`` is a human-readable description of the bench command
	that failed (e.g. ``bench new-site``, ``bench drop-site``,
	``bench migrate``), used in the leading sentence of the detail message.
	"""
	if not job.metadata or not job.metadata.name:
		return "Job failed (no metadata available)."

	job_name = job.metadata.name

	try:
		pods = core_v1.list_namespaced_pod(
			namespace=namespace,
			label_selector=f"job-name={job_name}",
			_request_timeout=15,
		)
		for pod in pods.items or []:
			pod_name = pod.metadata.name if pod.metadata else None
			if not pod_name:
				continue
			try:
				logs = core_v1.read_namespaced_pod_log(
					name=pod_name,
					namespace=namespace,
					tail_lines=30,
					_request_timeout=15,
				)
				if logs and logs.strip():
					return f"{operation_label} failed. Last 30 log lines:\n\n{logs.strip()}"
			except Exception:
				pass

			detail = _summarize_unrunnable_pod(pod)
			if detail:
				return detail
	except Exception:
		pass

	return f"Job '{job_name}' reported failure (could not retrieve pod logs)."


def _summarize_unrunnable_pod(pod: Any) -> str | None:
	"""Build a human-readable failure detail from a pod that produced no logs.

	Pods that fail before bench runs (PVC unbound, image pull error,
	scheduling failure) leave the container without a Running phase, so
	``read_namespaced_pod_log`` returns nothing.  Inspect the pod status to
	surface the actual reason (waiting reason + message, scheduling
	condition, or pod-level phase/message) instead of the unhelpful
	"could not retrieve pod logs" fallback.
	"""
	status = getattr(pod, "status", None)
	if not status:
		return None

	for cs in status.container_statuses or []:
		terminated = getattr(getattr(cs, "state", None), "terminated", None)
		if terminated:
			exit_code = getattr(terminated, "exit_code", "unknown")
			reason = getattr(terminated, "reason", "") or ""
			message = (getattr(terminated, "message", "") or "").strip()
			base = f"Job pod exited with code {exit_code}"
			if reason:
				base = f"{base} ({reason})"
			if message:
				return f"{base}: {message}"
			return f"{base} and no readable logs."

	waiting_statuses = list(status.container_statuses or []) + list(
		getattr(status, "init_container_statuses", None) or []
	)
	for cs in waiting_statuses:
		waiting = getattr(getattr(cs, "state", None), "waiting", None)
		if waiting:
			reason = getattr(waiting, "reason", "") or "Unknown"
			message = (getattr(waiting, "message", "") or "").strip()
			base = f"Job pod stuck in {reason}"
			if message:
				return f"{base}: {message}"
			return base

	for cond in status.conditions or []:
		if getattr(cond, "status", "") != "False":
			continue
		cond_reason = getattr(cond, "reason", "") or ""
		cond_message = (getattr(cond, "message", "") or "").strip()
		if cond_reason or cond_message:
			base = f"Job pod could not run ({cond_reason or 'unknown reason'})"
			if cond_message:
				return f"{base}: {cond_message}"
			return base

	phase = getattr(status, "phase", "") or ""
	pod_reason = getattr(status, "reason", "") or ""
	pod_message = (getattr(status, "message", "") or "").strip()
	if phase or pod_reason or pod_message:
		base = f"Job pod phase={phase or 'Unknown'}"
		if pod_reason:
			base = f"{base}, reason={pod_reason}"
		if pod_message:
			return f"{base}: {pod_message}"
		return base

	return None


def _set_helm_reconciliation_state(
	release_docname: str,
	expected_token: str | None,
	next_status: str,
	detail: str,
) -> bool:
	"""Apply a reconciliation-driven status update under the operation-token guard.

	The token guard is what stops a slow ``helm status`` call from clobbering
	a fresh deploy that started after the reconciliation tick read the row.
	If a worker has rotated the token, the row is owned by that operation
	and reconciliation must drop its writes.  Returns True if the write was
	applied, False if it was skipped or no-op.
	"""
	current = frappe.db.get_value(
		"Helm Release",
		release_docname,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current:
		return False

	# Skip rows whose status moved into worker-owned territory between the
	# reconciliation read and now (rapid Deploy click during reconciliation).
	if current.get("status") not in _HEALTHY_RELEASE_STATUSES:
		return False

	# Tolerate ``None == ""`` since rows that have never had a deploy will
	# have a NULL operation_token; matching ``None`` to ``None`` is the right
	# behavior for the first reconciliation pass.
	if (current.get("operation_token") or None) != (expected_token or None):
		frappe.logger("kubeport").info(
			"Skipping stale reconciliation for Helm Release '%s' — operation_token rotated.",
			release_docname,
		)
		return False

	truncated_detail = _truncate_status_detail(detail)
	if current.get("status") == next_status:
		# Status unchanged: still refresh detail so the panel sees current
		# readiness, but skip the realtime event to avoid notification spam.
		frappe.db.set_value(
			"Helm Release",
			release_docname,
			"helm_status_detail",
			truncated_detail,
		)
		return False

	frappe.db.set_value(
		"Helm Release",
		release_docname,
		{
			"status": next_status,
			"helm_status_detail": truncated_detail,
		},
	)
	frappe.publish_realtime(
		"helm_release_status_update",
		{"release_docname": release_docname, "status": next_status},
		doctype="Helm Release",
		docname=release_docname,
	)
	return True


def _helm_operation_is_stale(release) -> bool:
	started_at = release.operation_started_at or release.modified
	if not started_at:
		return False

	try:
		started = frappe.utils.get_datetime(started_at)
		cutoff = frappe.utils.add_to_date(
			frappe.utils.now_datetime(),
			minutes=-_HELM_OPERATION_STALE_MINUTES,
		)
		return started <= cutoff
	except Exception:
		return False


def _runtime_status_from_helm_result(helm_result: dict | None) -> str:
	if not isinstance(helm_result, dict):
		return ""

	info = helm_result.get("info", {})
	if isinstance(info, dict):
		return str(info.get("status") or "")
	return ""


def _set_stale_helm_operation_state(
	release_docname: str,
	expected_token: str | None,
	expected_status: str,
	fields: dict[str, object],
) -> bool:
	current = frappe.db.get_value(
		"Helm Release",
		release_docname,
		["operation_token", "status"],
		as_dict=True,
	)
	if not current:
		return False
	if current.get("status") != expected_status:
		return False
	if (current.get("operation_token") or None) != (expected_token or None):
		frappe.logger("kubeport").info(
			"Skipping stale Helm operation recovery for '%s' because operation_token rotated.",
			release_docname,
		)
		return False

	frappe.db.set_value("Helm Release", release_docname, fields)
	frappe.publish_realtime(
		"helm_release_status_update",
		{
			"release_docname": release_docname,
			"status": fields.get("status"),
		},
		doctype="Helm Release",
		docname=release_docname,
	)
	return True


def _is_helm_release_not_found_error(error: Exception) -> bool:
	message = str(error).lower()
	return "release: not found" in message or "release not found" in message


def _truncate_status_detail(detail: str) -> str:
	return detail[:_HELM_STATUS_DETAIL_LIMIT]
