# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.helm_release.helm_release import (
	_iter_storage_configs,
	_validate_storage_access_modes,
)


class UnitTestHelmRelease(UnitTestCase):
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


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestHelmRelease(IntegrationTestCase):
	"""Integration tests for HelmRelease."""

	pass
