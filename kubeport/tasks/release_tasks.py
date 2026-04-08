"""
Helm Release background tasks.
It is used to apply and delete Kubernetes resources defined in a Helm Release.
"""

import frappe

from kubeport.utils.k8s_client import get_k8s_api_client
from kubeport.utils.k8s_resources import apply_resource, delete_resource, parse_manifest_objects


def deploy_release_task(release_name: str):
	"""Background task: deploy K8s resources defined in a Helm Release.

	Uses server-side apply for idempotency — re-deploying an already-active
	release updates it instead of failing with a 409 Conflict.

	Note: Despite the DocType name, this currently applies raw K8s manifests
	from the ``values`` field — actual Helm chart integration is planned for
	Phase 2.
	"""
	doc = frappe.get_doc("Helm Release", release_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = parse_manifest_objects(doc.values)
		namespace = doc.namespace or "default"

		for k8s_object in manifest_data:
			apply_resource(api_client, k8s_object, namespace)

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
		manifest_data = parse_manifest_objects(doc.values)

		for k8s_object in manifest_data:
			delete_resource(api_client, k8s_object, doc.namespace or "default")

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
