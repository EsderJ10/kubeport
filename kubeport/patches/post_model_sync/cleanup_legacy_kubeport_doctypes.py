"""Remove legacy Kubeport doctypes after their data has been migrated."""

from __future__ import annotations

import frappe


_LEGACY_DOCTYPES = (
	"Kubernetes Manifest",
	"Kubernetes Command",
)


def execute() -> None:
	"""Delete legacy doctypes and their metadata tables."""
	for doctype_name in _LEGACY_DOCTYPES:
		_delete_doctype_metadata(doctype_name)
		_drop_table_if_exists(f"tab{doctype_name}")


def _delete_doctype_metadata(doctype_name: str) -> None:
	if not frappe.db.exists("DocType", doctype_name):
		return

	try:
		frappe.delete_doc("DocType", doctype_name, force=True, ignore_permissions=True)
		return
	except Exception as exc:
		frappe.log_error(
			title=f"Legacy DocType Cleanup Fallback: {doctype_name}",
			message=str(exc),
		)

	for table_name, field_name in (
		("DocField", "parent"),
		("DocPerm", "parent"),
		("Property Setter", "doc_type"),
		("Custom Field", "dt"),
		("DocType Action", "parent"),
		("DocType Link", "parent"),
	):
		if frappe.db.exists("DocType", table_name):
			frappe.db.delete(table_name, {field_name: doctype_name})

	frappe.db.delete("DocType", {"name": doctype_name})
	frappe.db.commit()


def _drop_table_if_exists(table_name: str) -> None:
	if not _table_exists(table_name):
		return

	frappe.db.sql(f"DROP TABLE IF EXISTS `{table_name}`")
	frappe.db.commit()


def _table_exists(table_name: str) -> bool:
	return bool(frappe.db.sql("SHOW TABLES LIKE %s", (table_name,)))
