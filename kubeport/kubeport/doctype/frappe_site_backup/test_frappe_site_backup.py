# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup import (
	FrappeSiteBackup,
	make_backup_name,
)


class UnitTestFrappeSiteBackup(UnitTestCase):
	def _doc(self, **overrides):
		doc = MagicMock(spec=FrappeSiteBackup)
		doc.status = overrides.get("status", "Available")
		doc.storage_path = overrides.get("storage_path", "/mnt/kubeport-backups/c/ns/site/backup.tar.gz")
		doc.cluster = overrides.get("cluster", "cluster-a")
		doc.namespace = overrides.get("namespace", "ns")
		doc.source_release_name = overrides.get("source_release_name", "bench-a")
		doc.on_trash = FrappeSiteBackup.on_trash.__get__(doc, FrappeSiteBackup)
		return doc

	def test_make_backup_name_uses_site_and_timestamp(self):
		with patch("kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup.frappe") as mock_frappe:
			mock_frappe.utils.now_datetime.return_value.strftime.return_value = "20260430120000"
			self.assertEqual(make_backup_name("demo.example.com"), "demo.example.com-20260430120000")

	@patch("kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup.frappe.enqueue")
	def test_on_trash_available_backup_enqueues_archive_delete(self, mock_enqueue):
		doc = self._doc(status="Available")
		doc.on_trash()
		mock_enqueue.assert_called_once()
		_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(_kwargs["cluster"], "cluster-a")
		self.assertEqual(_kwargs["namespace"], "ns")
		self.assertEqual(_kwargs["storage_path"], "/mnt/kubeport-backups/c/ns/site/backup.tar.gz")

	@patch("kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup.frappe.enqueue")
	def test_on_trash_failed_backup_with_storage_path_enqueues_archive_delete(self, mock_enqueue):
		"""A backup that partially wrote an archive then failed (e.g., tar
		corruption mid-flush) still has a ``storage_path`` stamped on the
		row.  Trashing it must clean the orphan file off the PVC; otherwise
		the disk slowly leaks every time a backup fails."""
		doc = self._doc(status="Failed")
		doc.on_trash()
		mock_enqueue.assert_called_once()
		_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(_kwargs["storage_path"], "/mnt/kubeport-backups/c/ns/site/backup.tar.gz")

	@patch("kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup.frappe.enqueue")
	def test_on_trash_failed_backup_without_storage_path_skips_cleanup(self, mock_enqueue):
		"""A Failed row that never made it as far as stamping a path has
		no archive to clean up."""
		doc = self._doc(status="Failed", storage_path="")
		doc.on_trash()
		mock_enqueue.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site_backup.frappe_site_backup.frappe.enqueue")
	def test_on_trash_refuses_in_flight_backup(self, mock_enqueue):
		doc = self._doc(status="In Progress")
		with self.assertRaises(frappe.ValidationError):
			doc.on_trash()
		mock_enqueue.assert_not_called()
