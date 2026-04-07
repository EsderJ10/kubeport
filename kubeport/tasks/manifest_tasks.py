"""
Kubernetes Manifest background tasks.
It is used to apply and delete Kubernetes resources defined in a manifest.
"""

import frappe

from kubeport.utils.k8s_client import get_k8s_api_client
from kubeport.utils.k8s_resources import apply_resource, delete_resource, parse_manifest_objects


def apply_manifest_task(manifest_name: str):
	"""Background task: apply a Kubernetes manifest to the cluster.

	Uses server-side apply for idempotency — re-applying an already-deployed
	manifest updates it instead of failing with a 409 Conflict.
	"""
	doc = frappe.get_doc("Kubernetes Manifest", manifest_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = parse_manifest_objects(doc.content)
		namespace = doc.namespace or "default"

		for k8s_object in manifest_data:
			apply_resource(api_client, k8s_object, namespace)

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
		manifest_data = parse_manifest_objects(doc.content)

		for k8s_object in manifest_data:
			delete_resource(api_client, k8s_object, doc.namespace or "default")

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
