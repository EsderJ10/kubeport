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

	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.service_bundle.service_bundle.frappe.enqueue")
	def test_apply_bundle_enqueues_service_bundle_task(
		self,
		mock_enqueue,
		_mock_msgprint,
	):
		doc = object.__new__(ServiceBundle)
		doc.name = "bundle-a"
		doc.bundle_name = "bundle-a"
		doc.cluster = "cluster-a"
		doc.content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
		doc.db_set = MagicMock()

		doc.apply_bundle()

		doc.db_set.assert_called_once_with("status", "In Progress")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.service_bundle_tasks.apply_bundle_task",
			bundle_name="bundle-a",
			queue="long",
			enqueue_after_commit=True,
		)


class IntegrationTestServiceBundle(IntegrationTestCase):
	"""Integration tests for ServiceBundle."""

	pass
