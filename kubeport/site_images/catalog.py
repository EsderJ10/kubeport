# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Import helpers for the shipped Kubeport Site Image catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frappe

from kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image import build_site_image_docname

CATALOG_PATH = Path(__file__).with_name("catalog.json")
_CATALOG_FIELDS = (
	"image_title",
	"image_repository",
	"image_tag",
	"image_digest",
	"frappe_major",
	"erpnext_version",
	"apps_json_hash",
	"source_revision",
	"status",
	"is_default",
	"description",
)


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
	"""Load and validate the shipped catalog JSON."""
	catalog_path = path or CATALOG_PATH
	with catalog_path.open(encoding="utf-8") as f:
		payload = json.load(f)

	images = payload.get("images")
	if not isinstance(images, list):
		frappe.throw("Site image catalog must contain an 'images' list.")

	seen: set[tuple[str, str]] = set()
	default_count = 0
	normalized: list[dict[str, Any]] = []
	for image in images:
		if not isinstance(image, dict):
			frappe.throw("Each site image catalog entry must be a mapping.")

		repository = str(image.get("image_repository") or "").strip()
		tag = str(image.get("image_tag") or "").strip()
		if not repository or not tag:
			frappe.throw("Each site image catalog entry requires image_repository and image_tag.")

		key = (repository, tag)
		if key in seen:
			frappe.throw(f"Duplicate site image catalog entry for '{repository}:{tag}'.")
		seen.add(key)

		status = str(image.get("status") or "Active")
		if status not in ("Active", "Deprecated"):
			frappe.throw(f"Site image '{repository}:{tag}' has invalid status '{status}'.")

		is_default = bool(image.get("is_default"))
		if is_default:
			default_count += 1
			if status == "Deprecated":
				frappe.throw(f"Deprecated site image '{repository}:{tag}' cannot be the default.")

		apps = image.get("apps") or []
		if not isinstance(apps, list):
			frappe.throw(f"Site image '{repository}:{tag}' apps must be a list.")

		normalized.append({
			"image_title": str(image.get("image_title") or f"{repository}:{tag}").strip(),
			"image_repository": repository,
			"image_tag": tag,
			"image_digest": str(image.get("image_digest") or "").strip(),
			"frappe_major": int(image.get("frappe_major") or 16),
			"erpnext_version": str(image.get("erpnext_version") or "").strip(),
			"apps_json_hash": str(image.get("apps_json_hash") or "").strip(),
			"source_revision": str(image.get("source_revision") or "").strip(),
			"status": status,
			"is_default": int(is_default),
			"description": str(image.get("description") or "").strip(),
			"apps": [_normalize_app_row(app) for app in apps],
		})

	if default_count > 1:
		frappe.throw("Only one site image catalog entry can be the default.")

	return normalized


def sync_catalog(path: Path | None = None) -> list[str]:
	"""Upsert curated catalog rows into MariaDB and return the touched docnames.

	User-origin rows that collide on (repository, tag) are preserved untouched.
	"""
	docnames: list[str] = []
	for image in load_catalog(path):
		docname = build_site_image_docname(image["image_repository"], image["image_tag"])
		if frappe.db.exists("Kubeport Site Image", docname):
			existing_origin = frappe.db.get_value("Kubeport Site Image", docname, "origin")
			if existing_origin == "User":
				frappe.logger("kubeport").warning(
					"Skipping curated catalog upsert for '%s'; "
					"a user-registered image already owns this repository:tag.",
					docname,
				)
				continue
			doc = frappe.get_doc("Kubeport Site Image", docname)
			for fieldname in _CATALOG_FIELDS:
				setattr(doc, fieldname, image[fieldname])
			doc.origin = "Kubeport"
			doc.set("apps", image["apps"])
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({
				"doctype": "Kubeport Site Image",
				"origin": "Kubeport",
				**{fieldname: image[fieldname] for fieldname in _CATALOG_FIELDS},
				"apps": image["apps"],
			})
			doc.insert(ignore_permissions=True)
		docnames.append(doc.name)

	return docnames


def _normalize_app_row(app: Any) -> dict[str, str]:
	if not isinstance(app, dict):
		frappe.throw("Each site image app row must be a mapping.")

	app_name = str(app.get("app_name") or "").strip()
	if not app_name:
		frappe.throw("Each site image app row requires app_name.")

	return {
		"app_name": app_name,
		"source_url": str(app.get("source_url") or "").strip(),
		"ref": str(app.get("ref") or "").strip(),
	}
