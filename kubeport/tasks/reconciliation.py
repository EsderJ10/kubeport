"""
Reconciliation Sweeper

Periodic task that compares desired state (Frappe DB) with actual state
(Kubernetes cluster) and flags drift.  Runs via ``scheduler_events`` in
``hooks.py`` every 5 minutes.

Covers three DocTypes:
- **Helm Release** — uses ``helm status`` to check Helm-managed releases
- **Service Bundle** — uses K8s API to check raw manifest resources
- **Frappe Site** — polls Kubernetes Job status for in-progress site creations
"""

import frappe

from kubeport.utils.k8s_resources import check_resources_exist

_HEALTHY_RELEASE_STATUSES = ["Deployed", "Degraded"]
_HELM_STATUS_DETAIL_LIMIT = 500


def reconcile_all_releases():
	"""Periodic task: compare desired state (DB) with actual state (K8s cluster).

	Runs every 5 minutes via scheduler_events in hooks.py.
	"""
	_reconcile_helm_releases()
	_reconcile_service_bundles()
	_reconcile_frappe_sites()


def _reconcile_helm_releases():
	"""Check all Deployed Helm Releases via ``helm status``.

	Uses the Helm CLI to query the real Helm release state, rather than
	checking individual K8s resources.  This is more accurate because Helm
	tracks release state in its own metadata (secrets in the release namespace).
	"""
	from kubeport.utils import helm

	deployed_releases = frappe.get_all(
		"Helm Release",
		filters={"status": ["in", _HEALTHY_RELEASE_STATUSES]},
		fields=["name", "cluster", "namespace", "release_name", "status"],
	)

	for release in deployed_releases:
		try:
			result = helm.status(
				release_name=release.release_name,
				namespace=release.namespace or "default",
				cluster_name=release.cluster,
			)

			# helm status JSON has info.status = "deployed" | "failed" | "pending-*" | etc.
			actual_status = ""
			if isinstance(result, dict):
				info = result.get("info", {})
				if isinstance(info, dict):
					actual_status = info.get("status", "")

			if actual_status == "deployed":
				_set_helm_reconciliation_state(release.name, "Deployed", actual_status)
			else:
				_set_helm_reconciliation_state(
					release.name,
					"Degraded",
					f"Helm reports: {actual_status or 'unknown'}",
				)
				frappe.log_error(
					title=f"Helm Drift Detected: {release.name}",
					message=f"Expected 'deployed', got '{actual_status or 'unknown'}'.",
				)

		except Exception as e:
			# If helm status fails entirely, mark as degraded
			_set_helm_reconciliation_state(
				release.name,
				"Degraded",
				f"Reconciliation error: {_truncate_status_detail(str(e))}",
			)
			frappe.log_error(
				title=f"Helm Reconciliation Error: {release.name}",
				message=str(e),
			)


def _reconcile_service_bundles():
	"""Check all Deployed Service Bundles for resource drift."""
	deployed_bundles = frappe.get_all(
		"Service Bundle",
		filters={"status": ["in", _HEALTHY_RELEASE_STATUSES]},
		fields=["name", "cluster", "namespace", "content", "status"],
	)

	for bundle in deployed_bundles:
		try:
			is_healthy, detail = check_resources_exist(
				cluster_name=bundle.cluster,
				manifest_json=bundle.content,
				namespace=bundle.namespace or "default",
				doctype="Service Bundle",
				docname=bundle.name,
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


def _reconcile_frappe_sites():
	"""Poll Kubernetes Job status for all in-progress Frappe Site creations.

	Transitions sites to Active (job succeeded) or Failed (job failed).
	Sites whose Job has already been cleaned up by ttlSecondsAfterFinished
	are left as-is so a human can investigate; a warning is logged.
	"""
	from kubernetes import client
	from kubernetes.client.rest import ApiException

	from kubeport.utils.k8s_client import get_k8s_api_client

	in_progress = frappe.get_all(
		"Frappe Site",
		filters={"status": "In Progress"},
		fields=["name", "cluster", "namespace", "creation_job_name"],
	)

	for site in in_progress:
		if not site.creation_job_name:
			continue

		try:
			api_client = get_k8s_api_client(site.cluster)
			batch_v1 = client.BatchV1Api(api_client=api_client)
			job = batch_v1.read_namespaced_job(
				name=site.creation_job_name,
				namespace=site.namespace or "default",
			)

			succeeded = (job.status.succeeded or 0) if job.status else 0
			failed = (job.status.failed or 0) if job.status else 0

			if succeeded > 0:
				frappe.db.set_value("Frappe Site", site.name, "status", "Active")
				frappe.db.set_value("Frappe Site", site.name, "status_detail", "")
				frappe.publish_realtime(
					"frappe_site_status_update",
					{"site_docname": site.name, "status": "Active"},
					doctype="Frappe Site",
					docname=site.name,
				)
			elif failed > 0:
				detail = _extract_job_failure_detail(
					batch_v1=batch_v1,
					core_v1=client.CoreV1Api(api_client=api_client),
					job=job,
					namespace=site.namespace or "default",
				)
				frappe.db.set_value("Frappe Site", site.name, "status", "Failed")
				frappe.db.set_value(
					"Frappe Site",
					site.name,
					"status_detail",
					_truncate_status_detail(detail),
				)
				frappe.log_error(
					title=f"Frappe Site Creation Failed: {site.name}",
					message=detail,
				)
				frappe.publish_realtime(
					"frappe_site_status_update",
					{"site_docname": site.name, "status": "Failed"},
					doctype="Frappe Site",
					docname=site.name,
				)
			# If neither succeeded nor failed, the Job is still running — leave status as-is.

		except ApiException as e:
			if e.status == 404:
				# Job was cleaned up (ttlSecondsAfterFinished elapsed) before we read it.
				frappe.logger("kubeport").warning(
					"Creation Job '%s' for Frappe Site '%s' no longer exists "
					"(likely cleaned up by TTL). Site status left as 'In Progress'.",
					site.creation_job_name,
					site.name,
				)
			else:
				frappe.log_error(
					title=f"Frappe Site Reconciliation Error: {site.name}",
					message=str(e),
				)
		except Exception as e:
			frappe.log_error(
				title=f"Frappe Site Reconciliation Error: {site.name}",
				message=str(e),
			)


def _extract_job_failure_detail(
	batch_v1: "client.BatchV1Api",
	core_v1: "client.CoreV1Api",
	job: "client.V1Job",
	namespace: str,
) -> str:
	"""Extract a human-readable failure reason from the Job's pod(s)."""
	if not job.metadata or not job.metadata.name:
		return "Job failed (no metadata available)."

	try:
		pods = core_v1.list_namespaced_pod(
			namespace=namespace,
			label_selector=f"job-name={job.metadata.name}",
			_request_timeout=15,
		)
		for pod in (pods.items or []):
			status = getattr(pod, "status", None)
			if not status:
				continue
			for cs in (status.container_statuses or []):
				terminated = getattr(getattr(cs, "state", None), "terminated", None)
				if terminated:
					msg = getattr(terminated, "message", "") or ""
					reason = getattr(terminated, "reason", "") or ""
					exit_code = getattr(terminated, "exit_code", "")
					parts = [p for p in [reason, msg] if p]
					detail = ": ".join(parts) if parts else f"exit code {exit_code}"
					return f"Job pod failed — {detail}"
	except Exception:
		pass

	return f"Job '{job.metadata.name}' reported failure (could not extract pod detail)."


def _set_helm_reconciliation_state(release_name: str, status: str, detail: str) -> None:
	frappe.db.set_value("Helm Release", release_name, "status", status)
	frappe.db.set_value(
		"Helm Release",
		release_name,
		"helm_status_detail",
		_truncate_status_detail(detail),
	)


def _truncate_status_detail(detail: str) -> str:
	return detail[:_HELM_STATUS_DETAIL_LIMIT]
