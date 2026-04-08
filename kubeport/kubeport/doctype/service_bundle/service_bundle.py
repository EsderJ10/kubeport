# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Service Bundle Controller

Deploys a set of raw Kubernetes resource manifests to a cluster.
This is the honest successor to the original "Helm Release" DocType,
which applied raw manifests despite its name.  Actual Helm integration
is handled by the new ``Helm Release`` DocType.
"""

import frappe
from frappe.model.document import Document

from kubeport.utils.k8s_resources import parse_manifest_objects


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
		status: DF.Literal["Draft", "In Progress", "Deployed", "Degraded", "Failed"]
	# end: auto-generated types

	def validate(self):
		"""Validate that the content field contains parsable manifests."""
		if self.content:
			try:
				parse_manifest_objects(self.content)
			except Exception as e:
				frappe.throw(f"Invalid manifest content: {e}")

	@frappe.whitelist()
	def apply_bundle(self):
		"""Apply the resource manifests to the target cluster."""
		if not self.content:
			frappe.throw("The manifest content is empty.")
		if not self.cluster:
			frappe.throw("A target cluster is required.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.service_bundle_tasks.apply_bundle_task",
			bundle_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			f"Deployment of '{self.bundle_name}' has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def delete_bundle(self):
		"""Delete the deployed resources from the cluster."""
		if self.status not in ["Deployed", "Degraded"]:
			frappe.throw("Only deployed or degraded bundles can be deleted.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.service_bundle_tasks.delete_bundle_task",
			bundle_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Bundle deletion has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)
