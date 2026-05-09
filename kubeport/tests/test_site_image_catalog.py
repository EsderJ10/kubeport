# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from kubeport.site_images.catalog import load_catalog, sync_catalog


class UnitTestSiteImageCatalog(UnitTestCase):
	def test_load_catalog_normalizes_image_entries(self):
		path = _write_catalog({
			"images": [{
				"image_title": "Kubeport ERPNext",
				"image_repository": "ghcr.io/losfavs/kubeport-site",
				"image_tag": "v1.0.0-frappe16",
				"frappe_major": 16,
				"is_default": True,
				"apps": [{
					"app_name": "erpnext",
					"source_url": "https://github.com/frappe/erpnext",
					"ref": "version-16",
				}],
			}],
		})

		try:
			rows = load_catalog(path)
		finally:
			path.unlink()

		self.assertEqual(rows[0]["image_repository"], "ghcr.io/losfavs/kubeport-site")
		self.assertEqual(rows[0]["image_tag"], "v1.0.0-frappe16")
		self.assertEqual(rows[0]["status"], "Active")
		self.assertEqual(rows[0]["is_default"], 1)
		self.assertEqual(rows[0]["apps"][0]["app_name"], "erpnext")

	@patch("kubeport.site_images.catalog.frappe.throw")
	def test_load_catalog_rejects_duplicate_repository_tag_pairs(self, mock_throw):
		path = _write_catalog({
			"images": [
				{
					"image_repository": "ghcr.io/losfavs/kubeport-site",
					"image_tag": "v1.0.0-frappe16",
				},
				{
					"image_repository": "ghcr.io/losfavs/kubeport-site",
					"image_tag": "v1.0.0-frappe16",
				},
			],
		})
		mock_throw.side_effect = RuntimeError("Duplicate site image catalog entry")

		try:
			with self.assertRaisesRegex(RuntimeError, "Duplicate"):
				load_catalog(path)
		finally:
			path.unlink()

	@patch("kubeport.site_images.catalog.frappe.throw")
	def test_load_catalog_rejects_deprecated_default(self, mock_throw):
		path = _write_catalog({
			"images": [{
				"image_repository": "ghcr.io/losfavs/kubeport-site",
				"image_tag": "v1.0.0-frappe16",
				"status": "Deprecated",
				"is_default": True,
			}],
		})
		mock_throw.side_effect = RuntimeError("cannot be the default")

		try:
			with self.assertRaisesRegex(RuntimeError, "default"):
				load_catalog(path)
		finally:
			path.unlink()

	@patch("kubeport.site_images.catalog.frappe.db.get_value")
	@patch("kubeport.site_images.catalog.frappe.get_doc")
	@patch("kubeport.site_images.catalog.frappe.db.exists")
	def test_sync_catalog_upserts_existing_rows(self, mock_exists, mock_get_doc, mock_get_value):
		path = _write_catalog({
			"images": [{
				"image_title": "Kubeport ERPNext",
				"image_repository": "ghcr.io/losfavs/kubeport-site",
				"image_tag": "v1.0.0-frappe16",
				"image_digest": "sha256:aaa",
				"frappe_major": 16,
				"erpnext_version": "16.17.0",
				"apps_json_hash": "sha256:bbb",
				"source_revision": "abc123",
				"status": "Active",
				"is_default": True,
				"description": "Runtime",
				"apps": [{"app_name": "erpnext"}],
			}],
		})
		mock_exists.return_value = True
		mock_get_value.return_value = 1
		doc = MagicMock()
		doc.name = "ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16"
		mock_get_doc.return_value = doc

		try:
			docnames = sync_catalog(path)
		finally:
			path.unlink()

		self.assertEqual(docnames, ["ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16"])
		self.assertEqual(doc.image_digest, "sha256:aaa")
		self.assertEqual(doc.erpnext_version, "16.17.0")
		self.assertEqual(doc.is_curated, 1)
		doc.set.assert_called_once_with("apps", [{"app_name": "erpnext", "source_url": "", "ref": ""}])
		doc.save.assert_called_once_with(ignore_permissions=True)

	@patch("kubeport.site_images.catalog.frappe.logger")
	@patch("kubeport.site_images.catalog.frappe.db.get_value")
	@patch("kubeport.site_images.catalog.frappe.get_doc")
	@patch("kubeport.site_images.catalog.frappe.db.exists")
	def test_sync_catalog_preserves_user_row_on_collision(
		self, mock_exists, mock_get_doc, mock_get_value, mock_logger
	):
		path = _write_catalog({
			"images": [{
				"image_title": "Kubeport ERPNext",
				"image_repository": "ghcr.io/losfavs/kubeport-site",
				"image_tag": "v1.0.0-frappe16",
				"frappe_major": 16,
				"status": "Active",
				"is_default": True,
				"apps": [{"app_name": "erpnext"}],
			}],
		})
		mock_exists.return_value = True
		mock_get_value.return_value = 0

		try:
			docnames = sync_catalog(path)
		finally:
			path.unlink()

		self.assertEqual(docnames, [])
		mock_get_doc.assert_not_called()
		mock_logger.return_value.warning.assert_called_once()

	@patch("kubeport.site_images.catalog.frappe.get_doc")
	@patch("kubeport.site_images.catalog.frappe.db.exists")
	def test_sync_catalog_inserts_curated_flag_for_new_rows(self, mock_exists, mock_get_doc):
		path = _write_catalog({
			"images": [{
				"image_title": "Kubeport ERPNext",
				"image_repository": "ghcr.io/losfavs/kubeport-site",
				"image_tag": "v1.0.0-frappe16",
				"frappe_major": 16,
				"status": "Active",
				"is_default": True,
				"apps": [{"app_name": "erpnext"}],
			}],
		})
		mock_exists.return_value = False
		new_doc = MagicMock()
		new_doc.name = "ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16"
		mock_get_doc.return_value = new_doc

		try:
			sync_catalog(path)
		finally:
			path.unlink()

		payload = mock_get_doc.call_args[0][0]
		self.assertEqual(payload["doctype"], "Kubeport Site Image")
		self.assertEqual(payload["is_curated"], 1)
		new_doc.insert.assert_called_once_with(ignore_permissions=True)


def _write_catalog(payload: dict) -> Path:
	handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
	with handle:
		json.dump(payload, handle)
	return Path(handle.name)
