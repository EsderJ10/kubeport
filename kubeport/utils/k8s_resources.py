"""
Kubernetes Resource CRUD Helpers

Low-level helpers for reading, deleting, and applying individual Kubernetes
resources via the Python client.  These are consumed by the background task
modules — they should never be called directly from a web request.
"""

import json
from typing import Any

import yaml

import frappe
from kubernetes import client
from kubernetes.client.rest import ApiException


# ---------------------------------------------------------------------------
# Resource Dispatch Table
# ---------------------------------------------------------------------------

# Maps K8s resource kinds to their API class, read method, and delete method.
_RESOURCE_DISPATCH: dict[str, dict[str, Any]] = {
	"Pod": {
		"api": "CoreV1Api",
		"read": "read_namespaced_pod",
		"delete": "delete_namespaced_pod",
	},
	"Service": {
		"api": "CoreV1Api",
		"read": "read_namespaced_service",
		"delete": "delete_namespaced_service",
	},
	"Deployment": {
		"api": "AppsV1Api",
		"read": "read_namespaced_deployment",
		"delete": "delete_namespaced_deployment",
	},
	"ConfigMap": {
		"api": "CoreV1Api",
		"read": "read_namespaced_config_map",
		"delete": "delete_namespaced_config_map",
	},
	"Secret": {
		"api": "CoreV1Api",
		"read": "read_namespaced_secret",
		"delete": "delete_namespaced_secret",
	},
	"Namespace": {
		"api": "CoreV1Api",
		"read": "read_namespace",
		"delete": "delete_namespace",
		"cluster_scoped": True,
	},
	"Ingress": {
		"api": "NetworkingV1Api",
		"read": "read_namespaced_ingress",
		"delete": "delete_namespaced_ingress",
	},
	"PersistentVolumeClaim": {
		"api": "CoreV1Api",
		"read": "read_namespaced_persistent_volume_claim",
		"delete": "delete_namespaced_persistent_volume_claim",
	},
	"StatefulSet": {
		"api": "AppsV1Api",
		"read": "read_namespaced_stateful_set",
		"delete": "delete_namespaced_stateful_set",
	},
	"DaemonSet": {
		"api": "AppsV1Api",
		"read": "read_namespaced_daemon_set",
		"delete": "delete_namespaced_daemon_set",
	},
	"Job": {
		"api": "BatchV1Api",
		"read": "read_namespaced_job",
		"delete": "delete_namespaced_job",
	},
	"CronJob": {
		"api": "BatchV1Api",
		"read": "read_namespaced_cron_job",
		"delete": "delete_namespaced_cron_job",
	},
	"ServiceAccount": {
		"api": "CoreV1Api",
		"read": "read_namespaced_service_account",
		"delete": "delete_namespaced_service_account",
	},
	"ClusterRole": {
		"api": "RbacAuthorizationV1Api",
		"read": "read_cluster_role",
		"delete": "delete_cluster_role",
		"cluster_scoped": True,
	},
	"ClusterRoleBinding": {
		"api": "RbacAuthorizationV1Api",
		"read": "read_cluster_role_binding",
		"delete": "delete_cluster_role_binding",
		"cluster_scoped": True,
	},
	"Role": {
		"api": "RbacAuthorizationV1Api",
		"read": "read_namespaced_role",
		"delete": "delete_namespaced_role",
	},
	"RoleBinding": {
		"api": "RbacAuthorizationV1Api",
		"read": "read_namespaced_role_binding",
		"delete": "delete_namespaced_role_binding",
	},
}


# ---------------------------------------------------------------------------
# Public Helpers
# ---------------------------------------------------------------------------

def parse_manifest_objects(manifest_content: str) -> list[dict]:
	"""Parse a JSON or YAML string into a list of K8s resource dicts.

	Accepts:
	- A single JSON object ``{}`` or a JSON array ``[{}, {}]``.
	- A single YAML document.
	- A multi-document YAML stream separated by ``---``.

	Tries JSON first (fast path) and falls back to YAML parsing.
	"""
	# Fast path: try JSON first (cheaper than YAML for structured data)
	try:
		data = json.loads(manifest_content)
		if isinstance(data, dict):
			return [data]
		if isinstance(data, list):
			return data
	except (json.JSONDecodeError, TypeError):
		pass

	# Slow path: YAML — supports multi-document streams (--- separators)
	try:
		documents = list(yaml.safe_load_all(manifest_content))
	except yaml.YAMLError as e:
		frappe.throw(f"Failed to parse manifest content as JSON or YAML: {e}")

	# Filter out None entries (empty documents between --- markers)
	return [doc for doc in documents if isinstance(doc, dict)]


def apply_resource(api_client: client.ApiClient, k8s_object: dict, namespace: str):
	"""Create or update a single K8s resource using server-side apply.

	Server-side apply (PATCH with ``application/apply-patch+yaml``) is
	idempotent — calling it on an already-existing resource updates it
	instead of erroring with 409 Conflict.  This eliminates the fragility
	of ``create_from_dict`` which only works on the first deploy.

	Falls back to ``create_from_dict`` for resource kinds not yet in the
	dispatch table.
	"""
	kind = k8s_object.get("kind", "")
	api_version = k8s_object.get("apiVersion", "")
	metadata = k8s_object.get("metadata", {})
	name = metadata.get("name", "")
	obj_namespace = metadata.get("namespace", namespace)

	if not kind or not name or not api_version:
		# Not enough info for server-side apply, fall back
		from kubernetes import utils
		utils.create_from_dict(api_client, data=k8s_object, namespace=namespace)
		return

	# Build the URL path for the resource
	resource_path = _build_resource_path(api_version, kind, name, obj_namespace)
	if not resource_path:
		# Unknown kind — fall back to create_from_dict
		from kubernetes import utils
		utils.create_from_dict(api_client, data=k8s_object, namespace=namespace)
		return

	# Ensure metadata has the namespace set for namespaced resources
	dispatch = _RESOURCE_DISPATCH.get(kind)
	if dispatch and not dispatch.get("cluster_scoped") and "namespace" not in metadata:
		k8s_object.setdefault("metadata", {})["namespace"] = obj_namespace

	# Server-side apply via raw PATCH request
	header_params = {
		"Content-Type": "application/apply-patch+yaml",
		"Accept": "application/json",
	}

	try:
		api_client.call_api(
			resource_path,
			"PATCH",
			header_params=header_params,
			query_params=[("fieldManager", "kubeport"), ("force", "true")],
			body=k8s_object,
			response_type="object",
			_preload_content=False,
		)
	except ApiException as e:
		if e.status == 404:
			# Resource doesn't exist yet — create it
			from kubernetes import utils
			utils.create_from_dict(api_client, data=k8s_object, namespace=namespace)
		else:
			raise


def delete_resource(api_client: client.ApiClient, k8s_object: dict, default_namespace: str):
	"""Delete a single K8s resource.  Silently succeeds if already gone (404)."""
	kind = k8s_object.get("kind", "")
	name = k8s_object.get("metadata", {}).get("name", "")
	namespace = k8s_object.get("metadata", {}).get("namespace", default_namespace)

	dispatch = _RESOURCE_DISPATCH.get(kind)
	if not dispatch:
		frappe.log_error(
			title="Unsupported K8s Resource Kind",
			message=f"Cannot delete resource of kind '{kind}'. Add it to _RESOURCE_DISPATCH.",
		)
		return

	api_class = getattr(client, dispatch["api"])
	api_instance = api_class(api_client=api_client)
	delete_method = getattr(api_instance, dispatch["delete"])

	try:
		if dispatch.get("cluster_scoped"):
			delete_method(name=name)
		else:
			delete_method(name=name, namespace=namespace)
	except ApiException as e:
		if e.status == 404:
			pass  # Already gone — goal satisfied
		else:
			raise


def read_resource(api_client: client.ApiClient, kind: str, name: str, namespace: str):
	"""Read (GET) a single K8s resource. Raises ApiException on 404."""
	dispatch = _RESOURCE_DISPATCH.get(kind)
	if not dispatch:
		frappe.logger("kubeport").warning(
			f"Skipping reconciliation for unsupported resource kind '{kind}/{name}' "
			f"in namespace '{namespace}'. Add it to _RESOURCE_DISPATCH to enable tracking."
		)
		return  # Skip unknown resource types during reconciliation

	api_class = getattr(client, dispatch["api"])
	api_instance = api_class(api_client=api_client)
	read_method = getattr(api_instance, dispatch["read"])

	if dispatch.get("cluster_scoped"):
		read_method(name=name)
	else:
		read_method(name=name, namespace=namespace)


def check_resources_exist(
	cluster_name: str,
	manifest_json: str,
	namespace: str,
	doctype: str,
	docname: str,
):
	"""Verify that K8s resources from a manifest JSON exist on the cluster.

	If any resource is missing (404), the DocType status is set to ``Degraded``.
	"""
	from kubeport.utils.k8s_client import get_k8s_api_client

	api_client = get_k8s_api_client(cluster_name)
	manifest_data = parse_manifest_objects(manifest_json)

	for k8s_object in manifest_data:
		kind = k8s_object.get("kind", "")
		name = k8s_object.get("metadata", {}).get("name", "")
		obj_namespace = k8s_object.get("metadata", {}).get("namespace", namespace)

		if not kind or not name:
			continue

		try:
			read_resource(api_client, kind, name, obj_namespace)
		except ApiException as e:
			if e.status == 404:
				frappe.db.set_value(doctype, docname, "status", "Degraded")
				frappe.log_error(
					title=f"State Drift Detected: {doctype} {docname}",
					message=f"{kind}/{name} not found in namespace {obj_namespace}.",
				)
				return
			raise


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------

# Maps apiVersion prefixes to their URL path segments.
_API_VERSION_PATHS = {
	"v1": "/api/v1",
	"apps/v1": "/apis/apps/v1",
	"batch/v1": "/apis/batch/v1",
	"networking.k8s.io/v1": "/apis/networking.k8s.io/v1",
	"rbac.authorization.k8s.io/v1": "/apis/rbac.authorization.k8s.io/v1",
}

# Maps kind to its plural resource name for URL building.
_KIND_TO_PLURAL: dict[str, str] = {
	"Pod": "pods",
	"Service": "services",
	"Deployment": "deployments",
	"ConfigMap": "configmaps",
	"Secret": "secrets",
	"Namespace": "namespaces",
	"Ingress": "ingresses",
	"PersistentVolumeClaim": "persistentvolumeclaims",
	"StatefulSet": "statefulsets",
	"DaemonSet": "daemonsets",
	"Job": "jobs",
	"CronJob": "cronjobs",
	"ServiceAccount": "serviceaccounts",
	"ClusterRole": "clusterroles",
	"ClusterRoleBinding": "clusterrolebindings",
	"Role": "roles",
	"RoleBinding": "rolebindings",
}


def _build_resource_path(
	api_version: str, kind: str, name: str, namespace: str
) -> str | None:
	"""Build the REST path for a specific K8s resource.

	Returns ``None`` if the apiVersion or kind is not in our lookup tables.
	"""
	base = _API_VERSION_PATHS.get(api_version)
	plural = _KIND_TO_PLURAL.get(kind)
	if not base or not plural:
		return None

	dispatch = _RESOURCE_DISPATCH.get(kind)
	if dispatch and dispatch.get("cluster_scoped"):
		return f"{base}/{plural}/{name}"

	return f"{base}/namespaces/{namespace}/{plural}/{name}"
