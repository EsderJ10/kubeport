import frappe
from frappe.model.document import Document
import json


class KubernetesManifest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cluster: DF.Link
		content: DF.Code
		manifest_name: DF.Data
		namespace: DF.Data | None
		status: DF.Literal["Draft", "In Progress", "Applied", "Degraded", "Failed"]
	# end: auto-generated types

	@frappe.whitelist()
	def apply_manifest(self):
		if not self.content:
			frappe.throw("The JSON content is empty.")

		# Validate JSON syntax before enqueuing
		try:
			json.loads(self.content)
		except json.JSONDecodeError as e:
			frappe.throw(f"JSON syntax error: {str(e)}")

		if not self.cluster:
			frappe.throw("You must select a target cluster.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.apply_manifest_task",
			manifest_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Manifest application has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def delete_manifest(self):
		if self.status not in ["Applied", "Degraded"]:
			frappe.throw("Only applied or degraded manifests can be deleted.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.delete_manifest_task",
			manifest_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Manifest deletion has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)