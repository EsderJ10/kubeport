# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Kubeport-owned Frappe/ERPNext runtime image catalog rows."""

import re

import frappe
from frappe.model.document import Document

_GHCR_REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*[a-z0-9]$")
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class KubeportSiteImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from kubeport.kubeport.doctype.kubeport_site_image_app.kubeport_site_image_app import (
			KubeportSiteImageApp,
		)

		apps: DF.Table[KubeportSiteImageApp]
		apps_json_hash: DF.Data | None
		description: DF.SmallText | None
		erpnext_version: DF.Data | None
		frappe_major: DF.Int
		image_digest: DF.Data | None
		image_repository: DF.Data
		image_tag: DF.Data
		image_title: DF.Data
		is_curated: DF.Check
		is_default: DF.Check
		source_revision: DF.Data | None
		status: DF.Literal["Active", "Deprecated"]
	# end: auto-generated types

	def autoname(self) -> None:
		self.name = build_site_image_docname(self.image_repository, self.image_tag)

	def validate(self) -> None:
		self.image_repository = str(self.image_repository or "").strip()
		self.image_tag = str(self.image_tag or "").strip()
		self.image_digest = str(self.image_digest or "").strip().lower()
		self.status = self.status or "Active"

		if not self.image_repository:
			frappe.throw("Image repository is required.")
		if not self.image_tag:
			frappe.throw("Image tag is required.")
		if self.status not in ("Active", "Deprecated"):
			frappe.throw("Status must be Active or Deprecated.")
		validate_site_image_reference(
			image_repository=self.image_repository,
			image_tag=self.image_tag,
			image_digest=self.image_digest,
			status=self.status,
			is_curated=bool(self.is_curated),
			is_default=bool(self.is_default),
			label=self.name or self.image_title or self.image_repository,
		)

		duplicate = frappe.db.get_value(
			"Kubeport Site Image",
			{
				"image_repository": self.image_repository,
				"image_tag": self.image_tag,
				"name": ["!=", self.name],
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				"Another Kubeport Site Image already uses repository "
				f"'{self.image_repository}' and tag '{self.image_tag}'."
			)

		if self.is_default and self.status == "Deprecated":
			frappe.throw("A deprecated image cannot be the default.")
		if self.is_default and not self.is_curated:
			frappe.throw("Defaults are reserved for curated Kubeport images.")

	def on_update(self) -> None:
		if self.is_default and self.status == "Active" and self.is_curated:
			_clear_other_defaults(self.name)

	def on_trash(self) -> None:
		if self.is_curated:
			frappe.throw(
				f"Curated Kubeport Site Image '{self.name}' cannot be deleted; mark it Deprecated instead."
			)

		linked = frappe.get_all(
			"Helm Release",
			filters={"site_image": self.name},
			fields=["name"],
			limit=5,
		)
		if linked:
			names = ", ".join(row["name"] for row in linked)
			frappe.throw(
				f"Cannot delete Kubeport Site Image '{self.name}'; it is linked to Helm Release(s): {names}."
			)


def build_site_image_docname(image_repository: str | None, image_tag: str | None) -> str:
	return f"{str(image_repository or '').strip()}:{str(image_tag or '').strip()}"


def validate_site_image_reference(
	*,
	image_repository: str | None,
	image_tag: str | None,
	image_digest: str | None,
	status: str | None,
	is_curated: bool,
	is_default: bool,
	label: str | None = None,
) -> None:
	"""Validate the public GHCR image reference Kubeport can safely deploy."""
	repository = str(image_repository or "").strip()
	tag = str(image_tag or "").strip()
	digest = str(image_digest or "").strip().lower()
	status = str(status or "Active").strip()
	label = str(label or repository or "Site Image").strip()

	if not _GHCR_REPOSITORY_RE.fullmatch(repository):
		frappe.throw(f"Site image '{label}' must use a public GHCR repository such as 'ghcr.io/owner/image'.")
	if ":" in repository or "@" in repository:
		frappe.throw(
			f"Site image '{label}' repository must not include a tag or digest; "
			"use the Tag and Digest fields instead."
		)
	if not _IMAGE_TAG_RE.fullmatch(tag):
		frappe.throw(f"Site image '{label}' has invalid image tag '{tag}'.")
	if digest and not _SHA256_DIGEST_RE.fullmatch(digest):
		frappe.throw(
			f"Site image '{label}' digest must be a sha256 digest in the form "
			"'sha256:<64 lowercase hex characters>'."
		)
	if is_default and not is_curated:
		frappe.throw("Defaults are reserved for curated Kubeport images.")
	if is_default and status == "Deprecated":
		frappe.throw("A deprecated image cannot be the default.")
	if is_curated and status == "Active" and not digest:
		frappe.throw(f"Curated active site image '{label}' must record the pushed GHCR image digest.")


def _clear_other_defaults(current_name: str) -> None:
	for row in frappe.get_all(
		"Kubeport Site Image",
		filters={"is_default": 1, "name": ["!=", current_name]},
		pluck="name",
	):
		frappe.db.set_value("Kubeport Site Image", row, "is_default", 0)
