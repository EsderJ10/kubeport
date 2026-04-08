# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Helm Repository Controller

Manages Helm chart repository registrations.  Adding a new repository
automatically triggers ``helm repo add`` and a chart sync in the background.
"""

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
	# end: auto-generated types

	def validate(self):
		"""Validate the repository URL format."""
		if self.repo_url and not self.repo_url.startswith(("https://", "http://", "oci://")):
			frappe.throw(
				"Repository URL must start with https://, http://, or oci://."
			)

	def after_insert(self):
		"""Auto-register repo in Helm and trigger first sync."""
		self.db_set("status", "Syncing")
		frappe.enqueue(
			"kubeport.tasks.helm_tasks.add_and_sync_repo",
			repo_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			f"Repository '{self.repo_name}' is being registered. "
			"Charts will be synced automatically.",
			alert=True,
			indicator="blue",
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
		self.db_set("status", "Syncing")
		frappe.enqueue(
			"kubeport.tasks.helm_tasks.sync_repo_charts",
			repo_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			"Chart sync has been queued. The chart list will update shortly.",
			alert=True,
			indicator="blue",
		)
