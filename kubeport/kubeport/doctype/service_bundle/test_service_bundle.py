# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.service_bundle.service_bundle import ServiceBundle


class UnitTestServiceBundle(UnitTestCase):
	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.throw")
	def test_validate_rejects_empty_managed_manifest(self, mock_throw):
		doc = object.__new__(ServiceBundle)
		doc.content = "[]"
		mock_throw.side_effect = RuntimeError("Invalid manifest content")

		with self.assertRaisesRegex(RuntimeError, "Invalid manifest content"):
			doc.validate()

		self.assertIn("Invalid manifest content", mock_throw.call_args.args[0])

	@patch(
		"kubeport.kubeport.doctype.service_bundle.service_bundle.secrets.token_hex", return_value="op-token"
	)
	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.enqueue")
	def test_apply_bundle_enqueues_service_bundle_task(
		self,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(ServiceBundle)
		doc.name = "bundle-a"
		doc.bundle_name = "bundle-a"
		doc.cluster = "cluster-a"
		doc.content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
		doc.status = "Draft"
		doc.db_set = MagicMock()

		doc.apply_bundle()

		doc.db_set.assert_any_call("status", "In Progress")
		doc.db_set.assert_any_call("status_detail", "")
		doc.db_set.assert_any_call("operation_token", "op-token")
		mock_enqueue.assert_called_once()
		call_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(
			mock_enqueue.call_args.args,
			("kubeport.tasks.service_bundle_tasks.apply_bundle_task",),
		)
		self.assertEqual(call_kwargs["bundle_name"], "bundle-a")
		self.assertEqual(call_kwargs["operation_token"], "op-token")
		self.assertEqual(call_kwargs["queue"], "long")
		self.assertTrue(call_kwargs["enqueue_after_commit"])
		self.assertIsInstance(call_kwargs.get("correlation_id"), str)
		self.assertEqual(len(call_kwargs["correlation_id"]), 36)

	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.throw")
	def test_apply_bundle_rejects_duplicate_in_progress_operation(self, mock_throw):
		doc = object.__new__(ServiceBundle)
		doc.name = "bundle-a"
		doc.bundle_name = "bundle-a"
		doc.cluster = "cluster-a"
		doc.content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
		doc.status = "Deleting"
		mock_throw.side_effect = RuntimeError("Another bundle operation is already in progress.")

		with self.assertRaisesRegex(RuntimeError, "already in progress"):
			doc.apply_bundle()

		mock_throw.assert_called_once()


class IntegrationTestServiceBundle(IntegrationTestCase):
	"""Integration tests for ServiceBundle."""

	pass
