"""
Reconciliation Sweeper

Periodic task that compares desired state (Frappe DB) with actual state
(Kubernetes cluster) and flags drift.  Runs via ``scheduler_events`` in
``hooks.py`` every 5 minutes.

Covers two DocTypes:
- **Helm Release** — uses ``helm status`` to check Helm-managed releases
- **Service Bundle** — uses K8s API to check raw manifest resources
"""

import frappe

from kubeport.utils.k8s_resources import check_resources_exist


def reconcile_all_releases():
	"""Periodic task: compare desired state (DB) with actual state (K8s cluster).

	Runs every 5 minutes via scheduler_events in hooks.py.
	"""
	_reconcile_helm_releases()
	_reconcile_service_bundles()


def _reconcile_helm_releases():
	"""Check all Deployed Helm Releases via ``helm status``.

	Uses the Helm CLI to query the real Helm release state, rather than
	checking individual K8s resources.  This is more accurate because Helm
	tracks release state in its own metadata (secrets in the release namespace).
	"""
	from kubeport.utils import helm

	deployed_releases = frappe.get_all(
		"Helm Release",
		filters={"status": "Deployed"},
		fields=["name", "cluster", "namespace", "release_name"],
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

			if actual_status and actual_status != "deployed":
				frappe.db.set_value("Helm Release", release.name, "status", "Degraded")
				frappe.db.set_value(
					"Helm Release", release.name,
					"helm_status_detail", f"Helm reports: {actual_status}",
				)
				frappe.log_error(
					title=f"Helm Drift Detected: {release.name}",
					message=f"Expected 'deployed', got '{actual_status}'.",
				)

		except Exception as e:
			# If helm status fails entirely, mark as degraded
			frappe.db.set_value("Helm Release", release.name, "status", "Degraded")
			frappe.db.set_value(
				"Helm Release", release.name,
				"helm_status_detail", f"Reconciliation error: {str(e)[:500]}",
			)
			frappe.log_error(
				title=f"Helm Reconciliation Error: {release.name}",
				message=str(e),
			)


def _reconcile_service_bundles():
	"""Check all Deployed Service Bundles for resource drift."""
	deployed_bundles = frappe.get_all(
		"Service Bundle",
		filters={"status": "Deployed"},
		fields=["name", "cluster", "namespace", "content"],
	)

	for bundle in deployed_bundles:
		try:
			check_resources_exist(
				cluster_name=bundle.cluster,
				manifest_json=bundle.content,
				namespace=bundle.namespace or "default",
				doctype="Service Bundle",
				docname=bundle.name,
			)
		except Exception as e:
			frappe.log_error(
				title=f"Reconciliation Error: Service Bundle {bundle.name}",
				message=str(e),
			)
