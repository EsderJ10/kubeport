# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.service_bundle_tasks import apply_bundle_task, delete_bundle_task


class UnitTestServiceBundleTasks(UnitTestCase):
	@patch("kubeport.tasks.service_bundle_tasks.frappe.logger")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.get_doc")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.db.get_value")
	def test_apply_bundle_task_skips_stale_worker_execution(
		self,
		mock_get_value,
		mock_get_doc,
		mock_logger,
	):
		mock_get_value.return_value = {
			"operation_token": "active-token",
			"status": "In Progress",
		}

		apply_bundle_task("bundle-a", "stale-token")

		mock_get_doc.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.tasks.service_bundle_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.service_bundle_tasks.load_managed_manifest_objects")
	@patch("kubeport.tasks.service_bundle_tasks.apply_resource")
	@patch("kubeport.tasks.service_bundle_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.get_doc")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.db.get_value")
	def test_apply_bundle_task_marks_bundle_deployed_when_token_is_current(
		self,
		mock_get_value,
		mock_get_doc,
		mock_get_k8s_api_client,
		mock_apply_resource,
		mock_load_manifest_objects,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			{"operation_token": "bundle-token", "status": "In Progress"},
			{"operation_token": "bundle-token", "status": "In Progress"},
		]
		doc = MagicMock()
		doc.cluster = "cluster-a"
		doc.content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
		doc.namespace = "demo"
		mock_get_doc.return_value = doc
		mock_get_k8s_api_client.return_value = object()
		mock_load_manifest_objects.return_value = [{"kind": "ConfigMap", "metadata": {"name": "demo"}}]

		apply_bundle_task("bundle-a", "bundle-token")

		mock_apply_resource.assert_called_once()
		doc.db_set.assert_any_call("status", "Deployed")
		doc.db_set.assert_any_call("status_detail", "")
		mock_publish_realtime.assert_called_once_with(
			"service_bundle_status_update",
			{"bundle_name": "bundle-a", "status": "Deployed"},
			doctype="Service Bundle",
			docname="bundle-a",
			after_commit=True,
		)

	@patch("kubeport.tasks.service_bundle_tasks.frappe.logger")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.log_error")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.service_bundle_tasks.delete_resource")
	@patch("kubeport.tasks.service_bundle_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.get_doc")
	@patch("kubeport.tasks.service_bundle_tasks.frappe.db.get_value")
	def test_delete_bundle_task_does_not_mark_error_when_worker_becomes_stale(
		self,
		mock_get_value,
		mock_get_doc,
		mock_get_k8s_api_client,
		mock_delete_resource,
		mock_publish_realtime,
		mock_log_error,
		mock_logger,
	):
		mock_get_value.side_effect = [
			{"operation_token": "bundle-token", "status": "Deleting"},
			{"operation_token": "newer-token", "status": "In Progress"},
		]
		doc = MagicMock()
		doc.cluster = "cluster-a"
		doc.content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
		doc.namespace = "demo"
		mock_get_doc.return_value = doc
		mock_get_k8s_api_client.return_value = object()
		mock_delete_resource.side_effect = RuntimeError("delete failed")

		with patch(
			"kubeport.tasks.service_bundle_tasks.load_managed_manifest_objects",
			return_value=[{"kind": "ConfigMap", "metadata": {"name": "demo"}}],
		):
			delete_bundle_task("bundle-a", "bundle-token")

		doc.db_set.assert_not_called()
		mock_publish_realtime.assert_not_called()
		mock_log_error.assert_not_called()
		mock_logger.return_value.info.assert_called_once()
