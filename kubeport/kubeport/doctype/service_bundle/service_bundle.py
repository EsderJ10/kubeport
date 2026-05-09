# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Service Bundle Controller

Deploys a set of raw Kubernetes resource manifests to a cluster.
"""

import secrets

import frappe
from frappe.model.document import Document

from kubeport.utils.k8s_resources import load_managed_manifest_objects


class ServiceBundle(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bundle_name: DF.Data
		cluster: DF.Link
		content: DF.Code
		namespace: DF.Data
		operation_token: DF.Data | None
		status: DF.Literal["Draft", "In Progress", "Deleting", "Deployed", "Degraded", "Failed"]
		status_detail: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		"""Validate that the content field contains managed Kubernetes manifests."""
		if self.content:
			try:
				load_managed_manifest_objects(self.content)
			except Exception as e:
				frappe.throw(f"Invalid manifest content: {e}")

	@frappe.whitelist()
	def apply_bundle(self):
		"""Apply the resource manifests to the target cluster."""
		if not self.content:
			frappe.throw("The manifest content is empty.")
		if not self.cluster:
			frappe.throw("A target cluster is required.")
		if self.status in {"In Progress", "Deleting"}:
			frappe.throw("Another bundle operation is already in progress.")

		self._enqueue_operation(
			task_path="kubeport.tasks.service_bundle_tasks.apply_bundle_task",
			status="In Progress",
			message=(
				f"Deployment of '{self.bundle_name}' has been queued. Status will update automatically."
			),
		)

	@frappe.whitelist()
	def delete_bundle(self):
		"""Delete the deployed resources from the cluster."""
		if self.status not in ["Deployed", "Degraded"]:
			frappe.throw("Only deployed or degraded bundles can be deleted.")
		self._enqueue_operation(
			task_path="kubeport.tasks.service_bundle_tasks.delete_bundle_task",
			status="Deleting",
			message="Bundle deletion has been queued. Status will update automatically.",
		)

	def _enqueue_operation(self, task_path: str, status: str, message: str) -> None:
		operation_token = secrets.token_hex(16)
		self.db_set("status", status)
		self.db_set("status_detail", "")
		self.db_set("operation_token", operation_token)
		frappe.enqueue(
			task_path,
			bundle_name=self.name,
			operation_token=operation_token,
			queue="long",
			enqueue_after_commit=True,
		)
		frappe.msgprint(
			message,
			alert=True,
			indicator="blue",
		)
