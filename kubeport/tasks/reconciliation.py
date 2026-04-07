"""
Reconciliation Sweeper

Periodic task that compares desired state (Frappe DB) with actual state
(Kubernetes cluster) and flags drift.  Runs via ``scheduler_events`` in
``hooks.py`` every 5 minutes.
"""

import frappe

from kubeport.utils.k8s_resources import check_resources_exist


def reconcile_all_releases():
	"""Periodic task: compare desired state (DB) with actual state (K8s cluster).

	Runs every 5 minutes via scheduler_events in hooks.py.
	Covers both Helm Release and Kubernetes Manifest documents.
	"""
	_reconcile_helm_releases()
	_reconcile_kubernetes_manifests()


def _reconcile_helm_releases():
	"""Check all Deployed Helm Releases for drift."""
	deployed_releases = frappe.get_all(
		"Helm Release",
		filters={"status": "Deployed"},
		fields=["name", "cluster", "namespace", "values"],
	)

	for release in deployed_releases:
		try:
			check_resources_exist(
				cluster_name=release.cluster,
				manifest_json=release.values,
				namespace=release.namespace or "default",
				doctype="Helm Release",
				docname=release.name,
			)
		except Exception as e:
			frappe.log_error(
				title=f"Reconciliation Error: Helm Release {release.name}",
				message=str(e),
			)


def _reconcile_kubernetes_manifests():
	"""Check all Applied Kubernetes Manifests for drift."""
	applied_manifests = frappe.get_all(
		"Kubernetes Manifest",
		filters={"status": "Applied"},
		fields=["name", "cluster", "namespace", "content"],
	)

	for manifest in applied_manifests:
		try:
			check_resources_exist(
				cluster_name=manifest.cluster,
				manifest_json=manifest.content,
				namespace=manifest.namespace or "default",
				doctype="Kubernetes Manifest",
				docname=manifest.name,
			)
		except Exception as e:
			frappe.log_error(
				title=f"Reconciliation Error: Kubernetes Manifest {manifest.name}",
				message=str(e),
			)
