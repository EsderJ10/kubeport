# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Frappe Site Backup Controller

Stores metadata for backup archives created from a Frappe Site.  The archive
itself lives outside MariaDB on the configured backup storage backend.
"""

from __future__ import annotations

import re

import frappe
from frappe.model.document import Document

from kubeport.utils import metrics

_STATUS_DETAIL_LIMIT = 500
_BACKUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")


class FrappeSiteBackup(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		backup_name: DF.Data
		bench_archive_name: DF.Data | None
		cluster: DF.Data
		completed_at: DF.Datetime | None
		frappe_site: DF.Data | None
		namespace: DF.Data
		operation_job_name: DF.Data | None
		operation_job_token: DF.Data | None
		operation_started_at: DF.Datetime | None
		operation_token: DF.Data | None
		site_name: DF.Data
		source_bench_release: DF.Data
		source_release_name: DF.Data | None
		size_bytes: DF.Int | None
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "In Progress", "Available", "Restoring", "Failed"]
		status_detail: DF.SmallText | None
		storage_backend: DF.Literal["pvc"]
		storage_path: DF.Data | None
		triggered_by: DF.Link | None
	# end: auto-generated types

	def autoname(self) -> None:
		if not self.backup_name:
			self.backup_name = make_backup_name(self.site_name)
		self.name = f"{self.site_name}::{self.backup_name}"

	def validate(self) -> None:
		if not self.is_new():
			for fieldname in ("frappe_site", "site_name", "source_bench_release", "cluster", "namespace"):
				if self.has_value_changed(fieldname):
					frappe.throw("Backup source metadata is immutable after creation.")
		if not self.site_name:
			frappe.throw("Site Name is required.")
		if not self.cluster:
			frappe.throw("Cluster is required.")
		if not self.namespace:
			self.namespace = "default"
		if not self.storage_backend:
			self.storage_backend = "pvc"
		if self.storage_backend != "pvc":
			frappe.throw("Only pvc backup storage is supported in this release.")
		if self.backup_name and not _BACKUP_NAME_RE.match(self.backup_name):
			frappe.throw(
				"Backup Name must contain only lowercase letters, digits, dots, hyphens, "
				"or underscores, and must start and end with a letter or digit."
			)

	def on_trash(self) -> None:
		if self.status in ("In Progress", "Restoring"):
			frappe.throw("Cannot delete a backup while an operation is in progress.")
		# Failed rows can stamp ``storage_path`` (e.g., a partial-write archive
		# left behind by a crashed Job).  Cleaning these up requires the same
		# enqueued task as Available rows so the PVC does not slowly leak.
		if not self.storage_path:
			return

		correlation_id = metrics.new_correlation_id()
		with metrics.correlation_scope(correlation_id):
			metrics.logger("kubeport.backup").info(
				"enqueue delete_backup_archive_task path=%s",
				self.storage_path,
			)
		frappe.enqueue(
			"kubeport.tasks.site_tasks.delete_backup_archive_task",
			cluster=self.cluster,
			namespace=self.namespace or "default",
			release_name=self.source_release_name or "",
			storage_path=self.storage_path,
			correlation_id=correlation_id,
			queue="long",
			enqueue_after_commit=True,
		)


def make_backup_name(site_name: str) -> str:
	"""Return a stable human-readable backup name for ``site_name``."""
	timestamp = frappe.utils.now_datetime().strftime("%Y%m%d%H%M%S")
	slug = re.sub(r"[^a-z0-9._-]", "-", (site_name or "site").lower())
	slug = re.sub(r"-+", "-", slug).strip("-.")
	if not slug:
		slug = "site"
	return f"{slug}-{timestamp}"


def truncate_status_detail(detail: str) -> str:
	return detail[:_STATUS_DETAIL_LIMIT]
