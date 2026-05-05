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

	def _delete_pod_doc(self, **overrides):
		defaults = {
			"doctype": "Kubernetes Command",
			"cluster": "test-cluster",
			"namespace": "demo",
			"action": "Delete",
			"resource_kind": "Pod",
			"resource_name": "stuck-pod",
			"confirm_destructive": 1,
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
		self.assertEqual(kwargs["queue"], "long")
		set_calls = {call.args[0]: call.args[1] for call in mock_db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Running")

	def test_execute_refuses_when_already_terminal(self):
		doc = self._doc(action="Get")
		doc.name = "KCMD-00003"
		doc.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			doc.execute()

	def test_validate_rejects_delete_for_disallowed_kind(self):
		"""Delete is restricted to Pod / Job / ConfigMap.  Secret, PVC,
		Deployment, StatefulSet, Service must be rejected at save time so
		the dangerous quartet (data loss / outage) cannot be queued via
		this surface — operators go through the proper controllers."""
		for kind in ("Secret", "PersistentVolumeClaim", "Deployment", "StatefulSet", "Service"):
			with self.subTest(kind=kind):
				doc = self._delete_pod_doc(resource_kind=kind, resource_name="anything")
				with self.assertRaises(frappe.ValidationError) as ctx:
					doc.validate()
				self.assertIn("cannot be deleted via Kubernetes Command", str(ctx.exception))

	def test_validate_allows_delete_for_pod_job_configmap(self):
		"""Sanity: the allowlisted kinds pass validation when the rest of
		the form is filled in correctly."""
		for kind in ("Pod", "Job", "ConfigMap"):
			with self.subTest(kind=kind):
				doc = self._delete_pod_doc(resource_kind=kind)
				doc.validate()  # must not raise

	def test_validate_rejects_delete_without_confirm_destructive(self):
		"""validate() — not just execute() — must enforce the destructive
		confirmation so the audit row reflects whether the operator agreed
		to the risk at save time."""
		doc = self._delete_pod_doc(confirm_destructive=0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		self.assertIn("Confirm Destructive", str(ctx.exception))


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


class UnitTestKubernetesCommandFinalize(UnitTestCase):
	"""Covers `_finalize` behaviors that touch the operator-visible audit
	trail: the truncation marker and the append-only audit log row."""

	def _make_cmd(self, **overrides):
		defaults = {
			"name": "KCMD-00050",
			"cluster": "test-cluster",
			"namespace": "demo",
			"action": "List",
			"resource_kind": "Pod",
			"resource_name": "",
			"label_selector": "app=foo",
			"triggered_by": "Administrator",
		}
		defaults.update(overrides)
		mock = MagicMock()
		for key, value in defaults.items():
			setattr(mock, key, value)
		return mock

	@patch("kubeport.tasks.kubernetes_command_tasks._append_audit_log")
	def test_finalize_marks_truncated_output_with_marker(self, _mock_audit):
		"""Output longer than _OUTPUT_LIMIT must be truncated AND carry an
		explicit marker so the operator knows the body is incomplete."""
		from kubeport.tasks.kubernetes_command_tasks import _OUTPUT_LIMIT, _finalize

		cmd = self._make_cmd()
		long_output = "x" * (_OUTPUT_LIMIT + 2000)

		_finalize(cmd, status="Completed", output=long_output)

		written = next(
			call.args[1] for call in cmd.db_set.call_args_list if call.args[0] == "output"
		)
		self.assertLessEqual(len(written), _OUTPUT_LIMIT)
		self.assertIn("truncated", written)
		# The marker must report the original length so the operator can
		# tell how much was dropped.
		self.assertIn(str(len(long_output)), written)

	@patch("kubeport.tasks.kubernetes_command_tasks._append_audit_log")
	def test_finalize_does_not_truncate_short_output(self, _mock_audit):
		"""Outputs under the limit must round-trip unchanged."""
		from kubeport.tasks.kubernetes_command_tasks import _finalize

		cmd = self._make_cmd()
		_finalize(cmd, status="Completed", output="hello world")

		written = next(
			call.args[1] for call in cmd.db_set.call_args_list if call.args[0] == "output"
		)
		self.assertEqual(written, "hello world")

	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_finalize_writes_audit_log_entry(self, mock_get_doc):
		"""Every execute (success OR failure) appends an audit log row.
		Decoupled from the Kubernetes Command itself so the trail survives
		row deletion."""
		from kubeport.tasks.kubernetes_command_tasks import _finalize

		audit_doc = MagicMock()
		mock_get_doc.return_value = audit_doc

		cmd = self._make_cmd(action="Delete", resource_kind="Pod", resource_name="stuck")
		_finalize(cmd, status="Completed", output="Deleted Pod 'stuck' in namespace 'demo'.")

		mock_get_doc.assert_called_once()
		audit_payload = mock_get_doc.call_args.args[0]
		self.assertEqual(audit_payload["doctype"], "Kubernetes Command Audit Log")
		self.assertEqual(audit_payload["command"], "KCMD-00050")
		self.assertEqual(audit_payload["action"], "Delete")
		self.assertEqual(audit_payload["resource_kind"], "Pod")
		self.assertEqual(audit_payload["outcome"], "Completed")
		self.assertEqual(audit_payload["cluster"], "test-cluster")
		audit_doc.insert.assert_called_once_with(ignore_permissions=True)

	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.logger")
	@patch("kubeport.tasks.kubernetes_command_tasks.frappe.get_doc")
	def test_finalize_swallows_audit_log_failure(self, mock_get_doc, mock_logger):
		"""An audit log write failure must NOT propagate — the command
		itself already finalized; spurious task retries would be worse
		than a missing audit entry."""
		from kubeport.tasks.kubernetes_command_tasks import _finalize

		mock_get_doc.side_effect = RuntimeError("audit DB unreachable")

		cmd = self._make_cmd(action="Get", resource_kind="Pod", resource_name="alive")
		# Should not raise.
		_finalize(cmd, status="Completed", output="ok")

		# The original doc still got finalized.
		set_calls = {call.args[0]: call.args[1] for call in cmd.db_set.call_args_list}
		self.assertEqual(set_calls.get("status"), "Completed")
		self.assertEqual(set_calls.get("output"), "ok")
		# And we logged a warning about the audit failure.
		mock_logger.assert_called_with("kubeport")
