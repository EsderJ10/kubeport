# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Read-only Kubeport Site Image catalog API."""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def list_site_images(include_deprecated: bool = False) -> list[dict[str, Any]]:
	"""Return catalog images for Helm Release form selection."""
	if isinstance(include_deprecated, str):
		include_deprecated = include_deprecated.lower() in ("1", "true", "yes")

	filters = {}
	if not include_deprecated:
		filters["status"] = "Active"

	rows = frappe.get_all(
		"Kubeport Site Image",
		filters=filters,
		fields=[
			"name",
			"image_title",
			"image_repository",
			"image_tag",
			"image_digest",
			"frappe_major",
			"erpnext_version",
			"apps_json_hash",
			"source_revision",
			"status",
			"origin",
			"is_default",
			"description",
		],
		order_by="is_default desc, origin asc, status asc, image_title asc",
	)

	return [_with_apps(_as_dict(row)) for row in rows]


def _with_apps(row: dict[str, Any]) -> dict[str, Any]:
	row["apps"] = frappe.get_all(
		"Kubeport Site Image App",
		filters={
			"parenttype": "Kubeport Site Image",
			"parent": row["name"],
		},
		fields=["app_name", "source_url", "ref"],
		order_by="idx asc",
	)
	return row


def _as_dict(row: Any) -> dict[str, Any]:
	if isinstance(row, dict):
		return dict(row)
	return dict(row.as_dict())
