"""
Kubeport Cluster API

Whitelisted endpoints for querying live Kubernetes cluster data.
These are called from the frontend to populate dynamic dropdowns
(e.g., namespace autocomplete when the user selects a cluster).
"""

import frappe
from kubernetes import client

from kubeport.utils.k8s_client import get_k8s_api_client


@frappe.whitelist()
def get_cluster_namespaces(cluster_name: str) -> list[str]:
	"""Return the list of namespace names from a live Kubernetes cluster.

	Called by client-side scripts to populate the namespace field with
	real options fetched from the selected cluster.

	Args:
		cluster_name: The name (primary key) of the Kubernetes Cluster DocType.

	Returns:
		A sorted list of namespace name strings.
	"""
	if not cluster_name:
		return []

	try:
		api_client = get_k8s_api_client(cluster_name)
		v1 = client.CoreV1Api(api_client=api_client)
		namespaces = v1.list_namespace()
		return sorted(ns.metadata.name for ns in namespaces.items)
	except Exception as e:
		frappe.log_error(
			title=f"Namespace Discovery Failed: {cluster_name}",
			message=str(e),
		)
		# Return empty list rather than crashing the form — the user can
		# still type a namespace manually.
		return []
