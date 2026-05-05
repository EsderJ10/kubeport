# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Kubernetes Command Audit Log

Append-only record of every Kubernetes Command execution.  Decoupled from
the source ``Kubernetes Command`` row so the audit trail survives row
deletion.  System Manager has read-only access; the row is created via
``frappe.get_doc(...).insert(ignore_permissions=True)`` from the command
worker, never from the UI.
"""

from __future__ import annotations

from frappe.model.document import Document


class KubernetesCommandAuditLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Data
		cluster: DF.Data
		command: DF.Data | None
		executed_at: DF.Datetime
		label_selector: DF.Data | None
		namespace: DF.Data | None
		outcome: DF.Literal["Completed", "Failed"]
		output_excerpt: DF.SmallText | None
		resource_kind: DF.Data
		resource_name: DF.Data | None
		triggered_by: DF.Link | None
	# end: auto-generated types
	pass
