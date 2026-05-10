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

from kubeport.utils import metrics
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


@frappe.whitelist(allow_guest=True)
def public_helm_release_count() -> dict[str, int]:
	"""Return the total number of Helm Release records ever created.

	This endpoint is intentionally guest-accessible so that the public
	landing page can display a live deployment counter without requiring
	an authenticated session.  Only a single aggregate integer is exposed;
	no sensitive release data is returned.
	"""
	count: int = frappe.db.count("Helm Release")
	return {"count": count}


# ---------------------------------------------------------------------------
# Internal observability — TODO-14
# ---------------------------------------------------------------------------


@frappe.whitelist()
def internal_metrics_summary() -> dict[str, object]:
	"""Return every Kubeport-internal counter and the helm-latency histogram.

	The values are *Kubeport's* observed state of itself (reconcile ticks
	processed, stale operations recovered, orphan jobs swept, helm subprocess
	latency).  They are cluster-agnostic, so this does not violate the
	desired-vs-observed-state invariant in AGENTS.md — nothing about cluster
	state is read or persisted here.
	"""
	return {
		"counters": metrics.get_all_counters(),
		"histograms": {
			metrics.HISTOGRAM_HELM_LATENCY: metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY),
		},
	}


@frappe.whitelist()
def reconcile_ticks_card_value() -> dict[str, int]:
	"""Number-Card-shaped wrapper around the reconcile-ticks counter."""
	return {"value": metrics.get_counter(metrics.COUNTER_RECONCILE_TICKS)}


@frappe.whitelist()
def stale_ops_recovered_card_value() -> dict[str, int]:
	"""Number-Card-shaped wrapper around the stale-op-recovery counter."""
	return {"value": metrics.get_counter(metrics.COUNTER_STALE_OPS_RECOVERED)}


@frappe.whitelist()
def orphan_jobs_swept_card_value() -> dict[str, int]:
	"""Number-Card-shaped wrapper around the orphan-job-sweep counter."""
	return {"value": metrics.get_counter(metrics.COUNTER_ORPHAN_JOBS_SWEPT)}


@frappe.whitelist()
def helm_p95_latency_card_value() -> dict[str, float]:
	"""Number-Card-shaped wrapper around the p95 of helm subprocess latency.

	Reports zero when the histogram window has no samples (cluster idle since
	the last process restart or last :func:`metrics.reset_all`).
	"""
	summary = metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)
	return {"value": float(summary.get("p95", 0.0))}
