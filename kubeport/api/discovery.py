"""
Cluster Discovery API

Whitelisted endpoints for live, read-only discovery of Helm releases and
Frappe sites in a Kubernetes cluster.
"""

from __future__ import annotations

from typing import Any

import frappe

from kubeport.utils.discovery import discover_cluster_releases, discover_release_sites
from kubeport.utils.k8s_client import get_k8s_api_client


@frappe.whitelist()
def get_cluster_discovery(cluster_name: str) -> dict[str, Any]:
	"""Return live discovery data for a Kubernetes Cluster form view."""
	response: dict[str, Any] = {
		"cluster": cluster_name or "",
		"generated_at": frappe.utils.now(),
		"benches": [],
		"sites": [],
		"errors": [],
	}

	if not cluster_name:
		response["errors"].append({
			"scope": "cluster",
			"message": "Cluster name is required.",
		})
		return response

	try:
		releases = discover_cluster_releases(cluster_name)
		response["benches"] = releases
	except Exception as e:
		frappe.log_error(
			title=f"Cluster Discovery Failed: {cluster_name}",
			message=str(e),
		)
		response["errors"].append({
			"scope": "cluster",
			"message": f"Failed to list Helm releases: {e}",
		})
		return response

	frappe_releases = [release for release in releases if release.get("is_frappe_bench")]
	if not frappe_releases:
		return response

	try:
		api_client = get_k8s_api_client(cluster_name)
	except Exception as e:
		frappe.log_error(
			title=f"Site Discovery Auth Failed: {cluster_name}",
			message=str(e),
		)
		response["errors"].append({
			"scope": "cluster",
			"message": f"Failed to initialize Kubernetes site discovery: {e}",
		})
		return response

	for release in frappe_releases:
		try:
			response["sites"].extend(discover_release_sites(api_client, release))
		except Exception as e:
			frappe.log_error(
				title=f"Site Discovery Failed: {cluster_name}/{release.get('release_name', '')}",
				message=str(e),
			)
			response["errors"].append({
				"scope": "release",
				"release_name": release.get("release_name", ""),
				"namespace": release.get("namespace", "default"),
				"message": f"Failed to discover sites for release '{release.get('release_name', '')}': {e}",
			})

	return response
