"""
Service Bundle background tasks.

Applies and deletes raw Kubernetes resource manifests defined in a
Service Bundle document.
"""

import frappe

from kubeport.utils.k8s_client import get_k8s_api_client
from kubeport.utils.k8s_resources import apply_resource, delete_resource, parse_manifest_objects


def apply_bundle_task(bundle_name: str):
	"""Background task: apply K8s resources defined in a Service Bundle.

	Uses server-side apply for idempotency — re-deploying an already-active
	bundle updates it instead of failing with a 409 Conflict.
	"""
	doc = frappe.get_doc("Service Bundle", bundle_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = parse_manifest_objects(doc.content)
		namespace = doc.namespace or "default"

		for k8s_object in manifest_data:
			apply_resource(api_client, k8s_object, namespace)

		doc.db_set("status", "Deployed")

		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Deployed"},
			doctype="Service Bundle",
			docname=bundle_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Service Bundle Apply Failed: {bundle_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Failed"},
			doctype="Service Bundle",
			docname=bundle_name,
		)


def delete_bundle_task(bundle_name: str):
	"""Background task: delete K8s resources defined in a Service Bundle."""
	doc = frappe.get_doc("Service Bundle", bundle_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = parse_manifest_objects(doc.content)

		for k8s_object in manifest_data:
			delete_resource(api_client, k8s_object, doc.namespace or "default")

		doc.db_set("status", "Draft")

		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Draft"},
			doctype="Service Bundle",
			docname=bundle_name,
		)

	except Exception as e:
		doc.db_set("status", "Failed")
		frappe.log_error(
			title=f"Service Bundle Delete Failed: {bundle_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Failed"},
			doctype="Service Bundle",
			docname=bundle_name,
		)
