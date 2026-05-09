# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Helm Repository Controller

Manages Helm chart repository registrations.  Adding a new repository
automatically triggers ``helm repo add`` and a chart sync in the background.
"""

import secrets

import frappe
from frappe.model.document import Document


class HelmRepository(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		include_patterns: DF.SmallText | None
		last_synced: DF.Datetime | None
		repo_name: DF.Data
		repo_password: DF.Password | None
		repo_url: DF.Data
		repo_username: DF.Data | None
		status: DF.Literal["Pending", "Syncing", "Synced", "Error"]
		sync_token: DF.Data | None
	# end: auto-generated types

	def validate(self):
		"""Validate the repository URL format."""
		if self.repo_url and not self.repo_url.startswith(("https://", "http://", "oci://")):
			frappe.throw("Repository URL must start with https://, http://, or oci://.")

	def after_insert(self):
		"""Auto-register repo in Helm and trigger first sync."""
		self._enqueue_sync_job(
			task_path="kubeport.tasks.helm_tasks.add_and_sync_repo",
			message=(
				f"Repository '{self.repo_name}' is being registered. Charts will be synced automatically."
			),
		)

	def on_trash(self):
		"""Remove repo from Helm when the document is deleted."""
		from kubeport.utils.helm import repo_remove

		try:
			repo_remove(self.repo_name)
		except Exception:
			pass  # Best-effort cleanup — repo may already be gone

	@frappe.whitelist()
	def sync_charts(self):
		"""Manual sync trigger — re-fetches chart index and upserts Helm Chart docs."""
		self._enqueue_sync_job(
			task_path="kubeport.tasks.helm_tasks.sync_repo_charts",
			message="Chart sync has been queued. The chart list will update shortly.",
		)

	def _enqueue_sync_job(self, task_path: str, message: str) -> None:
		sync_token = secrets.token_hex(16)
		self.db_set("status", "Syncing")
		self.db_set("sync_token", sync_token)
		frappe.enqueue(
			task_path,
			repo_name=self.name,
			sync_token=sync_token,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			message,
			alert=True,
			indicator="blue",
		)
