# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase


class UnitTestKubernetesCommand(UnitTestCase):
	def _doc(self, **overrides):
		defaults = {
			"doctype": "Kubernetes Command",
			"cluster": "test-cluster",
			"namespace": "demo",
			"action": "Get",
			"resource_kind": "PersistentVolumeClaim",
			"resource_name": "kubeport-backups",
			"confirm_destructive": 0,
		}
		defaults.update(overrides)
		return frappe.get_doc(defaults)

	def test_validate_requires_namespace_for_namespaced_kind(self):
		doc = self._doc(namespace=None)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_validate_requires_resource_name_for_get(self):
		doc = self._doc(resource_name=None, action="Get")
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_validate_requires_resource_name_for_delete(self):
		doc = self._doc(resource_name=None, action="Delete", confirm_destructive=1)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_validate_allows_list_without_resource_name(self):
		doc = self._doc(resource_name=None, action="List")
		doc.validate()

	def test_execute_delete_requires_confirm_destructive(self):
		doc = self._doc(action="Delete", confirm_destructive=0)
		doc.name = "KCMD-00001"
		doc.status = "Pending"
		with self.assertRaises(frappe.ValidationError):
			doc.execute()

	@patch("frappe.enqueue")
	def test_execute_delete_enqueues_background_job(self, mock_enqueue):
		doc = self._doc(action="Delete", confirm_destructive=1)
		doc.name = "KCMD-00002"
		doc.status = "Pending"
		with patch.object(doc, "db_set") as mock_db_set:
			result = doc.execute()
		self.assertTrue(result["queued"])
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "kubeport.tasks.kubernetes_command_tasks.run_kubernetes_command")
		self.assertEqual(kwargs["command_docname"], "KCMD-00002")
		set_calls = {call.args[0]: call.args[1] for call in mock_db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Running")

	def test_execute_refuses_when_already_terminal(self):
		doc = self._doc(action="Get")
		doc.name = "KCMD-00003"
		doc.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			doc.execute()


class UnitTestKubernetesCommandTasks(UnitTestCase):
	def _make_cmd(self, **overrides):
		defaults = {
			"name": "KCMD-00010",
			"cluster": "test-cluster",
			"namespace": "demo",
			"action": "Delete",
			"resource_kind": "PersistentVolumeClaim",
			"resource_name": "kubeport-backups",
			"label_selector": None,
		}
		defaults.update(overrides)
		mock = MagicMock()
		for key, value in defaults.items():
			setattr(mock, key, value)
		return mock

	@patch("kubeport.tasks.kubernetes_command_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.kubernetes_command_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_execute_delete_calls_k8s_delete(
		self,
		mock_get_doc,
		mock_core_v1,
		mock_get_api,
	):
		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		cmd = self._make_cmd()
		mock_get_doc.return_value = cmd

		_execute_command("KCMD-00010")

		mock_core_v1.return_value.delete_namespaced_persistent_volume_claim.assert_called_once_with(
			name="kubeport-backups",
			namespace="demo",
			_request_timeout=15,
		)
		set_calls = {call.args[0]: call.args[1] for call in cmd.db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Completed")
		self.assertIn("Deleted PersistentVolumeClaim", set_calls.get("output", ""))

	@patch("kubeport.tasks.kubernetes_command_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.kubernetes_command_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_execute_get_records_resource_summary(
		self,
		mock_get_doc,
		mock_core_v1,
		mock_get_api,
	):
		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		cmd = self._make_cmd(action="Get")
		mock_get_doc.return_value = cmd
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.return_value = (
			SimpleNamespace(
				metadata=SimpleNamespace(name="kubeport-backups", namespace="demo"),
				status=SimpleNamespace(phase="Bound"),
				spec=SimpleNamespace(access_modes=["ReadWriteMany"]),
			)
		)

		_execute_command("KCMD-00010")

		set_calls = {call.args[0]: call.args[1] for call in cmd.db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Completed")
		output = set_calls.get("output", "")
		self.assertIn("name=kubeport-backups", output)
		self.assertIn("phase=Bound", output)
		self.assertIn("ReadWriteMany", output)

	@patch("kubeport.tasks.kubernetes_command_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.kubernetes_command_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_execute_records_api_exception_as_failed(
		self,
		mock_get_doc,
		mock_core_v1,
		mock_get_api,
	):
		from kubernetes.client.rest import ApiException

		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		cmd = self._make_cmd()
		mock_get_doc.return_value = cmd
		mock_core_v1.return_value.delete_namespaced_persistent_volume_claim.side_effect = ApiException(
			status=404,
			reason="Not Found",
		)

		_execute_command("KCMD-00010")

		set_calls = {call.args[0]: call.args[1] for call in cmd.db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Failed")
		self.assertIn("404", set_calls.get("output", ""))

	@patch("kubeport.tasks.kubernetes_command_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.kubernetes_command_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_execute_list_passes_label_selector(
		self,
		mock_get_doc,
		mock_core_v1,
		mock_get_api,
	):
		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		cmd = self._make_cmd(action="List", label_selector="app=foo")
		mock_get_doc.return_value = cmd
		mock_core_v1.return_value.list_namespaced_persistent_volume_claim.return_value = (
			SimpleNamespace(items=[])
		)

		_execute_command("KCMD-00010")

		mock_core_v1.return_value.list_namespaced_persistent_volume_claim.assert_called_once_with(
			namespace="demo",
			_request_timeout=15,
			label_selector="app=foo",
		)
