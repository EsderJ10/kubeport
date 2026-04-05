"""Kubeport Background Tasks

All long-running Kubernetes operations are executed here via Frappe's RQ
background workers, not in the web process. Each function is invoked by
``frappe.enqueue()`` from the corresponding DocType controller.
"""

import json

import frappe
from kubernetes import client, utils
from kubernetes.client.rest import ApiException

from kubeport.utils.k8s_client import get_k8s_api_client


# ---------------------------------------------------------------------------
# Kubernetes Manifest Tasks
# ---------------------------------------------------------------------------

def apply_manifest_task(manifest_name: str):
	"""Background task: apply a Kubernetes manifest to the cluster."""
	doc = frappe.get_doc("Kubernetes Manifest", manifest_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = json.loads(doc.content)

		# Allow the JSON content to be a single object or a list of objects
		if isinstance(manifest_data, dict):
			manifest_data = [manifest_data]

		namespace = doc.namespace or "default"
		for k8s_object in manifest_data:
			utils.create_from_dict(api_client, data=k8s_object, namespace=namespace)

		doc.db_set("status", "Applied")

		frappe.publish_realtime(
			"manifest_status_update",
			{"manifest_name": manifest_name, "status": "Applied"},
			doctype="Kubernetes Manifest",
			docname=manifest_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Manifest Apply Failed: {manifest_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"manifest_status_update",
			{"manifest_name": manifest_name, "status": "Failed"},
			doctype="Kubernetes Manifest",
			docname=manifest_name,
		)


def delete_manifest_task(manifest_name: str):
	"""Background task: delete Kubernetes resources defined in a manifest."""
	doc = frappe.get_doc("Kubernetes Manifest", manifest_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = json.loads(doc.content)

		if isinstance(manifest_data, dict):
			manifest_data = [manifest_data]

		for k8s_object in manifest_data:
			_delete_k8s_resource(api_client, k8s_object, doc.namespace or "default")

		doc.db_set("status", "Draft")

		frappe.publish_realtime(
			"manifest_status_update",
			{"manifest_name": manifest_name, "status": "Draft"},
			doctype="Kubernetes Manifest",
			docname=manifest_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Manifest Delete Failed: {manifest_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"manifest_status_update",
			{"manifest_name": manifest_name, "status": "Failed"},
			doctype="Kubernetes Manifest",
			docname=manifest_name,
		)


# ---------------------------------------------------------------------------
# Helm Release Tasks (now using raw K8s manifests via Python client)
# ---------------------------------------------------------------------------

def deploy_release_task(release_name: str):
	"""Background task: deploy K8s resources defined in a Helm Release."""
	doc = frappe.get_doc("Helm Release", release_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = json.loads(doc.values)

		if isinstance(manifest_data, dict):
			manifest_data = [manifest_data]

		namespace = doc.namespace or "default"
		for k8s_object in manifest_data:
			utils.create_from_dict(api_client, data=k8s_object, namespace=namespace)

		doc.db_set("status", "Deployed")

		frappe.publish_realtime(
			"helm_status_update",
			{"release_name": release_name, "status": "Deployed"},
			doctype="Helm Release",
			docname=release_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Release Deploy Failed: {release_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_status_update",
			{"release_name": release_name, "status": "Failed"},
			doctype="Helm Release",
			docname=release_name,
		)


def uninstall_release_task(release_name: str):
	"""Background task: delete K8s resources defined in a Helm Release."""
	doc = frappe.get_doc("Helm Release", release_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = json.loads(doc.values)

		if isinstance(manifest_data, dict):
			manifest_data = [manifest_data]

		for k8s_object in manifest_data:
			_delete_k8s_resource(api_client, k8s_object, doc.namespace or "default")

		doc.db_set("status", "Draft")

		frappe.publish_realtime(
			"helm_status_update",
			{"release_name": release_name, "status": "Draft"},
			doctype="Helm Release",
			docname=release_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Release Uninstall Failed: {release_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_status_update",
			{"release_name": release_name, "status": "Failed"},
			doctype="Helm Release",
			docname=release_name,
		)


# ---------------------------------------------------------------------------
# Reconciliation Sweeper
# ---------------------------------------------------------------------------

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
			_check_resources_exist(
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
			_check_resources_exist(
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


def _check_resources_exist(
	cluster_name: str,
	manifest_json: str,
	namespace: str,
	doctype: str,
	docname: str,
):
	"""Verify that K8s resources defined in the manifest JSON actually exist on the cluster.

	If any resource is missing (404), the DocType status is downgraded to 'Degraded'.
	"""
	api_client = get_k8s_api_client(cluster_name)
	manifest_data = json.loads(manifest_json)

	if isinstance(manifest_data, dict):
		manifest_data = [manifest_data]

	for k8s_object in manifest_data:
		kind = k8s_object.get("kind", "")
		name = k8s_object.get("metadata", {}).get("name", "")
		obj_namespace = k8s_object.get("metadata", {}).get("namespace", namespace)

		if not kind or not name:
			continue

		try:
			_read_k8s_resource(api_client, kind, name, obj_namespace)
		except ApiException as e:
			if e.status == 404:
				# Resource is missing — state drift detected
				frappe.db.set_value(doctype, docname, "status", "Degraded")
				frappe.log_error(
					title=f"State Drift Detected: {doctype} {docname}",
					message=f"{kind}/{name} not found in namespace {obj_namespace}.",
				)
				return
			raise


# ---------------------------------------------------------------------------
# Private Helpers — K8s Resource CRUD
# ---------------------------------------------------------------------------

# Maps K8s resource kinds to their API class, read method, and delete method.
_RESOURCE_DISPATCH = {
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


def _delete_k8s_resource(api_client, k8s_object: dict, default_namespace: str):
	"""Delete a single K8s resource using the Python client."""
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
			# Resource is already gone, which perfectly satisfies our goal of deleting it.
			pass
		else:
			raise


def _read_k8s_resource(api_client, kind: str, name: str, namespace: str):
	"""Read (GET) a single K8s resource. Raises ApiException on 404."""
	dispatch = _RESOURCE_DISPATCH.get(kind)
	if not dispatch:
		return  # Skip unknown resource types during reconciliation

	api_class = getattr(client, dispatch["api"])
	api_instance = api_class(api_client=api_client)
	read_method = getattr(api_instance, dispatch["read"])

	if dispatch.get("cluster_scoped"):
		read_method(name=name)
	else:
		read_method(name=name, namespace=namespace)
