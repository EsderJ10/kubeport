"""
Service Bundle background tasks.

Applies and deletes raw Kubernetes resource manifests defined in a
Service Bundle document.
"""

import frappe

from kubeport.utils.k8s_client import get_k8s_api_client
from kubeport.utils.k8s_resources import (
	apply_resource,
	delete_resource,
	load_managed_manifest_objects,
)

_SERVICE_BUNDLE_STATUS_DETAIL_LIMIT = 500


def apply_bundle_task(bundle_name: str, operation_token: str):
	"""Background task: apply K8s resources defined in a Service Bundle.

	Uses server-side apply for idempotency — re-deploying an already-active
	bundle updates it instead of failing with a 409 Conflict.
	"""
	if not _bundle_operation_matches(bundle_name, operation_token, "In Progress"):
		return

	doc = frappe.get_doc("Service Bundle", bundle_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = load_managed_manifest_objects(doc.content)
		namespace = doc.namespace or "default"

		for k8s_object in manifest_data:
			apply_resource(api_client, k8s_object, namespace)

		if not _bundle_operation_matches(bundle_name, operation_token, "In Progress"):
			return

		doc.db_set("status", "Deployed")
		doc.db_set("status_detail", "")

		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Deployed"},
			doctype="Service Bundle",
			docname=bundle_name,
		)

	except Exception as e:
		if not _bundle_operation_matches(bundle_name, operation_token, "In Progress"):
			return

		doc.db_set("status", "Failed")
		doc.db_set("status_detail", _truncate_status_detail(f"Apply failed: {e}"))
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


def delete_bundle_task(bundle_name: str, operation_token: str):
	"""Background task: delete K8s resources defined in a Service Bundle."""
	if not _bundle_operation_matches(bundle_name, operation_token, "Deleting"):
		return

	doc = frappe.get_doc("Service Bundle", bundle_name)

	try:
		api_client = get_k8s_api_client(doc.cluster)
		manifest_data = load_managed_manifest_objects(doc.content)

		for k8s_object in manifest_data:
			delete_resource(api_client, k8s_object, doc.namespace or "default")

		if not _bundle_operation_matches(bundle_name, operation_token, "Deleting"):
			return

		doc.db_set("status", "Draft")
		doc.db_set("status_detail", "")

		frappe.publish_realtime(
			"service_bundle_status_update",
			{"bundle_name": bundle_name, "status": "Draft"},
			doctype="Service Bundle",
			docname=bundle_name,
		)

	except Exception as e:
		if not _bundle_operation_matches(bundle_name, operation_token, "Deleting"):
			return

		doc.db_set("status", "Failed")
		doc.db_set("status_detail", _truncate_status_detail(f"Delete failed: {e}"))
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


def _bundle_operation_matches(
	bundle_name: str,
	operation_token: str,
	expected_status: str,
) -> bool:
	current = frappe.db.get_value(
		"Service Bundle",
		bundle_name,
		["operation_token", "status"],
		as_dict=True,
	)
	if (
		current
		and current.get("operation_token") == operation_token
		and current.get("status") == expected_status
	):
		return True

	frappe.logger("kubeport").info(
		"Skipping stale bundle worker for Service Bundle '%s' because token/status "
		"no longer match the queued operation.",
		bundle_name,
	)
	return False


def _truncate_status_detail(detail: str) -> str:
	return detail[:_SERVICE_BUNDLE_STATUS_DETAIL_LIMIT]
