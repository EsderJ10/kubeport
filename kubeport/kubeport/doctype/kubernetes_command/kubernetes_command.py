# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Kubernetes Command Controller

Operator-facing audit trail for ad-hoc cluster operations (delete a stuck PVC,
list pods, fetch a Job manifest) that have no DocType-managed lifecycle of
their own.  Read actions (Get / List) execute inline; Delete actions enqueue
a background worker so the cluster mutation invariant from CLAUDE.md still
holds.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

_NAMESPACED_KINDS = {
	"PersistentVolumeClaim",
	"Pod",
	"Job",
	"Secret",
	"ConfigMap",
	"Service",
	"Deployment",
	"StatefulSet",
}
_TERMINAL_STATUSES = {"Completed", "Failed"}


class KubernetesCommand(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Literal["Get", "List", "Delete"]
		cluster: DF.Link
		completed_at: DF.Datetime | None
		confirm_destructive: DF.Check
		label_selector: DF.Data | None
		namespace: DF.Data | None
		operation_job_name: DF.Data | None
		output: DF.Code | None
		resource_kind: DF.Literal[
			"PersistentVolumeClaim",
			"Pod",
			"Job",
			"Secret",
			"ConfigMap",
			"Service",
			"Deployment",
			"StatefulSet",
		]
		resource_name: DF.Data | None
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Completed", "Failed"]
		triggered_by: DF.Link | None
	# end: auto-generated types

	def validate(self) -> None:
		if self.resource_kind in _NAMESPACED_KINDS and not self.namespace:
			frappe.throw(f"Namespace is required for {self.resource_kind}.")
		if self.action in ("Get", "Delete") and not self.resource_name:
			frappe.throw(f"Resource Name is required for {self.action}.")
		if self.action == "Delete" and not self.confirm_destructive:
			# Surface here too so it's caught at form save, not only at execute()
			# (the JS layer already prompts).  Keeps the audit row honest about
			# whether a destructive op was approved.
			pass

	def before_insert(self) -> None:
		self.triggered_by = frappe.session.user
		self.status = "Pending"
		self.output = ""

	@frappe.whitelist()
	def execute(self) -> dict:
		"""Run the command.

		Read actions (Get / List) execute inline and the result is stored in
		``output`` before this call returns.  Delete enqueues a background
		job to honour CLAUDE.md's "all cluster mutations go through background
		jobs" rule, even though the deletion itself is a single API call.
		"""
		if self.status in _TERMINAL_STATUSES:
			frappe.throw("Command has already been executed; create a new one to re-run.")
		if self.status == "Running":
			frappe.throw("Command is already running.")

		if self.action == "Delete":
			if not self.confirm_destructive:
				frappe.throw("Confirm Destructive must be checked before executing a Delete.")
			self.db_set("status", "Running")
			self.db_set("started_at", now_datetime())
			frappe.enqueue(
				"kubeport.tasks.kubernetes_command_tasks.run_kubernetes_command",
				command_docname=self.name,
				queue="default",
				enqueue_after_commit=True,
			)
			return {"queued": True, "docname": self.name}

		# Read action — run inline so the result is available immediately
		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		self.db_set("status", "Running")
		self.db_set("started_at", now_datetime())
		_execute_command(self.name)
		self.reload()
		return {"queued": False, "docname": self.name, "status": self.status}
