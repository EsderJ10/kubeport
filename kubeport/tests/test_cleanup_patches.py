# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles import (
	_next_available_bundle_name,
	execute,
)


class UnitTestCleanupPatches(UnitTestCase):
	@patch("kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles.frappe.logger")
	@patch("kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles.frappe.get_doc")
	@patch("kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles.frappe.get_all")
	@patch("kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles.frappe.db")
	def test_manifest_migration_copies_rows_into_service_bundles(
		self,
		mock_db,
		mock_get_all,
		mock_get_doc,
		mock_logger,
	):
		mock_doc = MagicMock()
		mock_get_doc.return_value = mock_doc
		mock_db.exists.side_effect = lambda doctype, name=None: (
			doctype == "DocType" and name == "Kubernetes Manifest"
		)

		def get_all_side_effect(doctype, **kwargs):
			if doctype == "Kubernetes Manifest":
				return [
					{
						"name": "legacy-a",
						"manifest_name": "bundle-a",
						"cluster": "cluster-a",
						"namespace": "",
						"content": "apiVersion: v1",
						"status": "Applied",
					}
				]
			if doctype == "Service Bundle":
				return []
			return []

		mock_get_all.side_effect = get_all_side_effect

		execute()

		mock_get_doc.assert_called_once_with(
			{
				"doctype": "Service Bundle",
				"bundle_name": "bundle-a",
				"cluster": "cluster-a",
				"namespace": "default",
				"content": "apiVersion: v1",
				"status": "Deployed",
			}
		)
		mock_doc.insert.assert_called_once_with(ignore_permissions=True)
		mock_db.commit.assert_called_once()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.patches.pre_model_sync.migrate_kubernetes_manifests_to_service_bundles.frappe.db")
	def test_next_available_bundle_name_uses_migrated_suffix_sequence(self, mock_db):
		existing_names = {
			("Service Bundle", "bundle-a"): True,
			("Service Bundle", "bundle-a-migrated"): True,
			("Service Bundle", "bundle-a-migrated-2"): True,
			("Service Bundle", "bundle-a-migrated-3"): False,
		}
		mock_db.exists.side_effect = lambda doctype, name=None: existing_names.get((doctype, name), False)

		self.assertEqual(_next_available_bundle_name("bundle-a"), "bundle-a-migrated-3")
