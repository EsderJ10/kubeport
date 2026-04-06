import frappe
from frappe.model.document import Document
import json


class HelmRelease(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		chart_reference: DF.Data
		cluster: DF.Link
		namespace: DF.Data
		release_name: DF.Data
		status: DF.Literal["Draft", "In Progress", "Deployed", "Degraded", "Failed"]
		values: DF.Code
	# end: auto-generated types

	@frappe.whitelist()
	def deploy_release(self):
		if not self.values:
			frappe.throw("The Values (JSON) field is empty.")

		# Validate JSON syntax before enqueuing
		try:
			json.loads(self.values)
		except json.JSONDecodeError as e:
			frappe.throw(f"JSON syntax error: {str(e)}")

		if not self.cluster:
			frappe.throw("A target cluster is required.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.deploy_release_task",
			release_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			f"Deployment of '{self.release_name}' has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def uninstall_release(self):
		if self.status not in ["Deployed", "Degraded"]:
			frappe.throw("Only deployed or degraded releases can be uninstalled.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.uninstall_release_task",
			release_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Release uninstallation has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)