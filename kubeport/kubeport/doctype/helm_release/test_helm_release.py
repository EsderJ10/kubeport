# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.helm_release.helm_release import (
	HelmRelease,
	build_release_docname,
	_iter_storage_configs,
	_validate_storage_access_modes,
)


class UnitTestHelmRelease(UnitTestCase):
	def test_build_release_docname_scopes_release_identity_to_cluster_and_namespace(self):
		self.assertEqual(
			build_release_docname("cluster-a", "erp", "bench-a"),
			"cluster-a/erp/bench-a",
		)

	def test_iter_storage_configs_finds_nested_persistence_blocks(self):
		configs = _iter_storage_configs({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteMany"],
				},
				"logs": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteOnce"],
				},
			},
		})

		self.assertEqual(configs, [
			("values.persistence.worker", "local-path", ["ReadWriteMany"]),
			("values.persistence.logs", "local-path", ["ReadWriteOnce"]),
		])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_rejects_local_path_rwx(self, mock_throw):
		_validate_storage_access_modes({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteMany"],
				},
			},
		})

		mock_throw.assert_called_once()
		self.assertIn("values.persistence.worker", mock_throw.call_args.args[0])
		self.assertIn("local-path", mock_throw.call_args.args[0])
		self.assertIn("ReadWriteMany", mock_throw.call_args.args[0])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_allows_local_path_rwo(self, mock_throw):
		_validate_storage_access_modes({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteOnce"],
				},
			},
		})

		mock_throw.assert_not_called()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_deploy_release_rejects_duplicate_in_progress_deploy(self, mock_throw):
		doc = object.__new__(HelmRelease)
		doc.chart = "ERPNext"
		doc.cluster = "cluster-a"
		doc.status = "In Progress"
		doc.db_set = MagicMock()
		doc.name = "bench-a"
		doc.release_name = "bench-a"
		mock_throw.side_effect = RuntimeError("Deployment is already in progress for this release.")

		with self.assertRaisesRegex(RuntimeError, "already in progress"):
			doc.deploy_release()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_rejects_identity_changes_after_creation(self, mock_throw):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.cluster = "cluster-b"
		doc.namespace = "default"
		doc.release_name = "bench-a"
		doc.values = ""
		doc.is_new = lambda: False
		mock_throw.side_effect = RuntimeError("immutable after creation")

		with self.assertRaisesRegex(RuntimeError, "immutable after creation"):
			doc.validate()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_on_trash_refuses_active_statuses(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Uninstall the release first")

		for active_status in ("In Progress", "Deployed", "Degraded", "Uninstalling"):
			with self.subTest(status=active_status):
				doc = object.__new__(HelmRelease)
				doc.name = "cluster-a/default/bench-a"
				doc.status = active_status

				with self.assertRaisesRegex(RuntimeError, "Uninstall the release first"):
					doc.on_trash()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_on_trash_allows_draft_and_failed(self, mock_throw):
		for inactive_status in ("Draft", "Failed"):
			with self.subTest(status=inactive_status):
				doc = object.__new__(HelmRelease)
				doc.name = "cluster-a/default/bench-a"
				doc.status = inactive_status

				doc.on_trash()

		mock_throw.assert_not_called()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-1")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	def test_deploy_release_rotates_operation_token_and_enqueues_with_it(
		self,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.chart = "ERPNext"
		doc.cluster = "cluster-a"
		doc.status = "Draft"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.deploy_release()

		doc.db_set.assert_any_call("operation_token", "tok-1")
		doc.db_set.assert_any_call("status", "In Progress")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.helm_tasks.install_or_upgrade_release",
			release_name="cluster-a/default/bench-a",
			operation_token="tok-1",
			queue="long",
			enqueue_after_commit=True,
		)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-2")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	def test_uninstall_release_rotates_operation_token_and_enqueues_with_it(
		self,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.status = "Deployed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.uninstall_release()

		doc.db_set.assert_any_call("operation_token", "tok-2")
		doc.db_set.assert_any_call("status", "Uninstalling")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.helm_tasks.uninstall_release",
			release_name="cluster-a/default/bench-a",
			operation_token="tok-2",
			queue="long",
			enqueue_after_commit=True,
		)


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestHelmRelease(IntegrationTestCase):
	"""Integration tests for HelmRelease."""

	pass
