# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Read-only aggregations powering the Kubeport Operations Workspace.

Each function here is invoked by a Number Card or Dashboard Chart on the
``kubeport_operations`` Workspace.  All endpoints read already-reconciled
DocType fields from MariaDB; none make live cluster calls.  Live cluster
data on the dashboard surface (e.g. Untracked Cluster Releases) goes
through the existing on-demand Script Reports, not through these
auto-refreshed cards.
"""

from __future__ import annotations

import frappe

from kubeport.utils.constants import STALE_OPERATION_THRESHOLD_MINUTES

# DocTypes that own an ``operation_token`` field.  ``modified_field`` is the
# column to use for staleness comparison: prefer ``operation_started_at`` when
# the DocType has it, fall back to ``modified``.  Frappe Site has no
# ``operation_started_at`` (verified in plan); Helm Release and Frappe Site
# Backup do.
_STALE_SOURCES: tuple[tuple[str, str, str], ...] = (
	("Helm Release", "helm_release", "operation_started_at"),
	("Frappe Site", "frappe_site", "modified"),
	("Frappe Site Backup", "frappe_site_backup", "operation_started_at"),
)


@frappe.whitelist()
def count_stale_operations() -> dict[str, int]:
	"""Return the count of stale in-flight operations broken down per DocType.

	A row is "stale" when its ``operation_token`` is set (i.e. an operation
	is in flight) but its start time (``operation_started_at`` where
	available, otherwise ``modified``) is older than
	``STALE_OPERATION_THRESHOLD_MINUTES`` ago.  This mirrors the same
	threshold reconciliation uses to trigger its stale-recovery branch.

	Returned shape: ``{"helm_release": int, "frappe_site": int,
	"frappe_site_backup": int, "total": int}``.
	"""
	cutoff = frappe.utils.add_to_date(
		frappe.utils.now_datetime(),
		minutes=-STALE_OPERATION_THRESHOLD_MINUTES,
	)

	counts: dict[str, int] = {}
	for doctype, key, time_field in _STALE_SOURCES:
		counts[key] = frappe.db.count(
			doctype,
			filters={
				"operation_token": ["is", "set"],
				time_field: ["<=", cutoff],
			},
		)

	counts["total"] = sum(counts.values())
	return counts


@frappe.whitelist()
def stale_operations_card_value() -> dict[str, int]:
	"""Number Card-shaped wrapper around :func:`count_stale_operations`.

	Frappe's "Custom" Number Card invokes a server method and reads the
	``value`` key for display.  This wrapper returns the total only;
	the per-DocType breakdown is exposed by :func:`count_stale_operations`
	for the Script Report drilldown that lands in Phase 2.
	"""
	return {"value": count_stale_operations()["total"]}
