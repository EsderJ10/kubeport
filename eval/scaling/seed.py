"""Bulk seeders for the Kubeport scaling harness.

Inserts synthetic ``Helm Release`` / ``Service Bundle`` / ``Frappe Site`` rows
in their healthy terminal states (``Deployed`` / ``Active``) using
``frappe.db.bulk_insert`` so DocType controllers, validation, and lifecycle
hooks are bypassed — only the database-write cost is paid.

All synthetic rows share the prefix ``scalebench-`` so cleanup is idempotent
and never touches operator data.  Cluster / Helm Chart / Helm Repository
parent rows are inserted once per run via the regular ORM path so the FK
references are satisfied; seeded child rows then point at them.

Designed to be invoked from ``eval/scaling/_inproc.py`` inside the dev
container.  Not safe to run on a production bench.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
from collections.abc import Iterator
from typing import Any

PREFIX = "scalebench-"
CLUSTER_NAME = f"{PREFIX}cluster"
NAMESPACE = f"{PREFIX}ns"
HELM_REPO_NAME = f"{PREFIX}repo"
HELM_REPO_URL = "https://example.invalid/charts"
HELM_CHART_NAME = f"{PREFIX}chart"
HELM_CHART_DOCNAME = f"{HELM_REPO_NAME}/{HELM_CHART_NAME}"
HELM_CHART_VERSION = "0.0.0-scalebench"

_SYNTHETIC_SPEC_HASH = "scalebench-spec-hash"
_DEFAULT_SERVICE_BUNDLE_CONTENT = json.dumps(
	{
		"apiVersion": "v1",
		"kind": "ConfigMap",
		"metadata": {"name": "scalebench-cm", "namespace": NAMESPACE},
		"data": {"flag": "true"},
	},
	sort_keys=True,
)


def _utc_now_str() -> str:
	return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def ensure_parents() -> None:
	"""Insert the shared Kubernetes Cluster / Helm Repository / Helm Chart rows.

	Uses the regular ORM path (``frappe.get_doc(...).insert``) so validation,
	autoname, and password-field handling all run.  Idempotent: existing rows
	are reused.  These three rows alone are insufficient to talk to a real
	cluster — the kubeconfig is a placeholder — but the scaling benchmark
	monkey-patches every cluster-touching helper before any reconciliation
	tick runs, so the placeholders are never dereferenced.
	"""
	import frappe

	if not frappe.db.exists("Kubernetes Cluster", CLUSTER_NAME):
		frappe.get_doc(
			{
				"doctype": "Kubernetes Cluster",
				"cluster_name": CLUSTER_NAME,
				"auth_method": "Kubeconfig",
				"kubeconfig": "apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n",
				"skip_tls_verify": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Helm Repository", HELM_REPO_NAME):
		frappe.get_doc(
			{
				"doctype": "Helm Repository",
				"repo_name": HELM_REPO_NAME,
				"repo_url": HELM_REPO_URL,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Helm Chart", HELM_CHART_DOCNAME):
		frappe.get_doc(
			{
				"doctype": "Helm Chart",
				"repository": HELM_REPO_NAME,
				"chart_name": HELM_CHART_NAME,
				"latest_version": HELM_CHART_VERSION,
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()


def set_synthetic_status(status: str) -> dict[str, int]:
	"""Set ``status`` on every synthetic Helm Release / Service Bundle row.

	Used by the benchmark harness to flip seeded rows between ``"Draft"`` (the
	insert default — invisible to reconciliation) and ``"Deployed"`` (so the
	timed reconciliation tick has work to do).  A SQL-level update keeps the
	flip cheap on large N and skips DocType ``validate`` hooks that would
	probe the placeholder cluster.

	Returns a per-DocType count of touched rows so callers can log the flip.
	"""
	import frappe

	counts: dict[str, int] = {}
	for doctype in ("Helm Release", "Service Bundle"):
		rows = frappe.get_all(doctype, filters={"name": ("like", f"%{PREFIX}%")}, pluck="name")
		if rows:
			frappe.db.set_value(doctype, {"name": ("in", rows)}, "status", status)
		counts[doctype] = len(rows)
	frappe.db.commit()
	return counts


@contextlib.contextmanager
def synthetic_rows_active(status: str = "Deployed") -> Iterator[None]:
	"""Flip seeded rows to ``status`` for the duration of the block.

	On entry, sets every synthetic Helm Release / Service Bundle to ``status``
	(default ``"Deployed"``).  On exit — including the abnormal exit paths a
	scaling benchmark cares about (KeyboardInterrupt, OOM, exception inside
	the timed loop) — flips them back to ``"Draft"`` so the live Frappe
	scheduler never observes seeded rows as ``Deployed``.

	Pair with :func:`seed_helm_releases` and :func:`seed_service_bundles`,
	whose default status is ``"Draft"``: the rows are inert outside this
	context and reconciliation-visible only inside it.
	"""
	set_synthetic_status(status)
	try:
		yield
	finally:
		set_synthetic_status("Draft")


def cleanup_synthetic_rows() -> dict[str, int]:
	"""Delete every synthetic row produced by previous seed calls.

	Uses raw ``frappe.db.delete`` so DocType ``on_trash`` hooks never run —
	the live ``Frappe Site`` / ``Helm Release`` ``on_trash`` validators
	would otherwise probe the (unreachable, placeholder) cluster and abort
	the cleanup.  Synthetic rows have no child tables, so a SQL-only delete
	is sufficient.

	Returns a per-DocType count of removed rows so callers can log the sweep.
	The parent rows (cluster / repo / chart) are kept so re-seeding does not
	pay the ORM-insert cost on every run.
	"""
	import frappe

	counts: dict[str, int] = {}
	for doctype in (
		"Frappe Site",
		"Helm Release",
		"Service Bundle",
	):
		rows = frappe.get_all(doctype, filters={"name": ("like", f"%{PREFIX}%")}, pluck="name")
		if rows:
			frappe.db.delete(doctype, {"name": ("in", rows)})
		counts[doctype] = len(rows)
	frappe.db.commit()
	return counts


def _helm_release_row(idx: int, now: str, status: str) -> tuple[Any, ...]:
	release_name = f"{PREFIX}rel-{idx:06d}"
	docname = f"{CLUSTER_NAME}/{NAMESPACE}/{release_name}"
	return (
		docname,
		now,
		now,
		"Administrator",
		"Administrator",
		release_name,
		CLUSTER_NAME,
		NAMESPACE,
		HELM_CHART_DOCNAME,
		HELM_CHART_VERSION,
		status,
		_SYNTHETIC_SPEC_HASH,
		_SYNTHETIC_SPEC_HASH,
		HELM_CHART_VERSION,
		f"scalebench-token-{idx:06d}",
	)


def _service_bundle_row(idx: int, now: str, status: str) -> tuple[Any, ...]:
	bundle_name = f"{PREFIX}bundle-{idx:06d}"
	return (
		bundle_name,
		now,
		now,
		"Administrator",
		"Administrator",
		bundle_name,
		CLUSTER_NAME,
		NAMESPACE,
		status,
		_DEFAULT_SERVICE_BUNDLE_CONTENT,
		f"scalebench-token-{idx:06d}",
	)


def _frappe_site_row(idx: int, release_docname: str, now: str) -> tuple[Any, ...]:
	site_name = f"{PREFIX}site-{idx:06d}.localhost"
	docname = f"{release_docname}/{site_name}"
	return (
		docname,
		now,
		now,
		"Administrator",
		"Administrator",
		site_name,
		release_docname,
		CLUSTER_NAME,
		NAMESPACE,
		"Active",
		"mariadb",
	)


def seed_helm_releases(n: int, status: str = "Draft") -> int:
	"""Bulk-insert ``n`` synthetic Helm Release rows.

	``status`` defaults to ``"Draft"`` so that a benchmark crash between seed
	and the timed block leaves rows in a state reconciliation skips
	(``_HEALTHY_RELEASE_STATUSES`` only iterates ``Deployed``/``Degraded``).
	The benchmark harness flips them to ``"Deployed"`` inside the
	``_mock_cluster_reads`` context to measure the reconciliation hot path
	without leaking unreachable ``Deployed`` rows into the live scheduler.
	"""
	import frappe

	if n <= 0:
		return 0
	now = _utc_now_str()
	rows = [_helm_release_row(i, now, status) for i in range(n)]
	frappe.db.bulk_insert(
		"Helm Release",
		fields=[
			"name",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"release_name",
			"cluster",
			"namespace",
			"chart",
			"chart_version",
			"status",
			"desired_spec_hash",
			"last_applied_spec_hash",
			"last_applied_chart_version",
			"operation_token",
		],
		values=rows,
		ignore_duplicates=True,
	)
	frappe.db.commit()
	return n


def seed_service_bundles(n: int, status: str = "Draft") -> int:
	"""Bulk-insert ``n`` synthetic Service Bundle rows.

	``status`` defaults to ``"Draft"``; see :func:`seed_helm_releases` for the
	rationale.  Reconciliation only iterates ``Deployed``/``Degraded`` bundles
	(see ``_reconcile_service_bundles``), so Draft seeded rows are inert.
	"""
	import frappe

	if n <= 0:
		return 0
	now = _utc_now_str()
	rows = [_service_bundle_row(i, now, status) for i in range(n)]
	frappe.db.bulk_insert(
		"Service Bundle",
		fields=[
			"name",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"bundle_name",
			"cluster",
			"namespace",
			"status",
			"content",
			"operation_token",
		],
		values=rows,
		ignore_duplicates=True,
	)
	frappe.db.commit()
	return n


def seed_frappe_sites(n: int) -> int:
	"""Bulk-insert ``n`` synthetic Frappe Site rows in ``Active`` state.

	Sites are linked to the first synthetic Helm Release.  ``Active`` rows
	are not iterated by ``_reconcile_frappe_sites`` (which filters on
	``In Progress``/``Deleting``/``Migrating``), so this characterises the
	background DB-filter cost — i.e., the floor on per-tick latency from a
	large Frappe Site backlog that is healthy.  Document this in the report.
	"""
	import frappe

	if n <= 0:
		return 0
	release_docname = f"{CLUSTER_NAME}/{NAMESPACE}/{PREFIX}rel-000000"
	if not frappe.db.exists("Helm Release", release_docname):
		seed_helm_releases(1)
	now = _utc_now_str()
	rows = [_frappe_site_row(i, release_docname, now) for i in range(n)]
	frappe.db.bulk_insert(
		"Frappe Site",
		fields=[
			"name",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"site_name",
			"bench_release",
			"cluster",
			"namespace",
			"status",
			"db_type",
		],
		values=rows,
		ignore_duplicates=True,
	)
	frappe.db.commit()
	return n
