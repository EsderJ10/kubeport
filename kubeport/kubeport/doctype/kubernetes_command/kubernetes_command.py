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

from kubeport.utils import metrics

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
# Tighter allowlist for Delete: read access to all eight kinds is fine, but
# destructive ops are restricted to ephemeral/restartable resources.  Deleting
# a Secret breaks live workloads, a PVC destroys data, and a Deployment /
# StatefulSet / Service causes outages — those want the operator to go through
# the proper controllers (Helm Release, Service Bundle, Frappe Site).  Pods
# and Jobs are restartable; ConfigMap is recoverable.
_DELETABLE_KINDS = {"Pod", "Job", "ConfigMap"}
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
		if self.action == "Delete":
			if self.resource_kind not in _DELETABLE_KINDS:
				frappe.throw(
					f"{self.resource_kind} cannot be deleted via Kubernetes Command. "
					f"Allowed kinds for Delete: {', '.join(sorted(_DELETABLE_KINDS))}. "
					"Use the appropriate controller (Helm Release, Service Bundle, "
					"or Frappe Site) for destructive changes to other kinds."
				)
			if not self.confirm_destructive:
				frappe.throw(
					"Confirm Destructive must be checked before saving a Delete "
					"command.  This keeps the audit row honest about whether the "
					"destructive op was approved at save time, not just at execute."
				)

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
			correlation_id = metrics.new_correlation_id()
			self.db_set("status", "Running")
			self.db_set("started_at", now_datetime())
			with metrics.correlation_scope(correlation_id):
				metrics.logger("kubeport.k8scmd").info("enqueue run_kubernetes_command command=%s", self.name)
			frappe.enqueue(
				"kubeport.tasks.kubernetes_command_tasks.run_kubernetes_command",
				command_docname=self.name,
				correlation_id=correlation_id,
				queue="long",
				enqueue_after_commit=True,
			)
			return {"queued": True, "docname": self.name}

		# Read action — run inline so the result is available immediately
		from kubeport.tasks.kubernetes_command_tasks import _execute_command

		self.db_set("status", "Running")
		self.db_set("started_at", now_datetime())
		_execute_command(self.name)
		status = frappe.db.get_value("Kubernetes Command", self.name, "status")
		return {"queued": False, "docname": self.name, "status": status}
