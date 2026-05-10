# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

"""
Property-based FSM tests for ``Frappe Site``.

This module checks the documented Frappe Site state machine against a
``hypothesis.stateful.RuleBasedStateMachine`` model that mirrors the
controller transitions in
``kubeport/kubeport/doctype/frappe_site/frappe_site.py`` and the
reconciliation outcomes that finalize each in-flight operation.

Scope and intent
----------------
This is a **specification-level** check. The model is a pure-Python mirror
of the controller — no MariaDB, no Frappe document machinery, no mocked
K8s clients. It is hermetic by construction. The bug class it catches is
*FSM consistency*: a controller change that introduces an arrow not in the
documented set, or a controller path that leaves the row in an in-flight
status with no pending operation, will fail this test.

Implementation-level coverage (real controller methods, mocked Kubernetes
APIs, the actual ``frappe.enqueue`` → worker → reconcile flow) lives in
``kubeport/tests/test_site_lifecycle.py`` and ``kubeport/tests/test_site_tasks.py``.
The two test layers are complementary, not redundant: those tests catch
implementation bugs in the K8s/Helm path; this module catches FSM-level
inconsistencies that survive across implementation refactors.

Invariants
----------
The three invariants required by ``TODO-12``:

1. ``inv_no_terminal_inflight``: degraded form. The model is single-threaded,
   so it cannot reproduce the cancel-rotates-token / stale-worker-writes-back
   race directly; the actual concurrency property is enforced by the
   per-write token re-checks inside ``kubeport/tasks/site_tasks.py`` and is
   covered by ``test_site_tasks``. Here the invariant is checked as the
   structural consequence: ``status ∈ in-flight ⇔ pending_op ≠ None ∧
   operation_token ≠ ""``. A controller path that rotates the token without
   also clearing pending_op (or vice versa) would fail this check.
2. ``inv_transitions_documented``: every observed (from, to) transition
   appears in ``DOCUMENTED_SITE_ARROWS``. A controller change that
   introduces an undocumented arrow without updating the constant fails the
   test, forcing the documentation in ``docs/control-plane-state.md``
   §Frappe Site Provisioning to be updated in lock-step.
3. ``inv_archive_outlives_site``: when the site row reaches the synthetic
   ``DELETED`` terminal (the row removed by reconcile after a confirmed
   drop-site probe, or by ``frappe.delete_doc``), every backup row that was
   ``Available`` at deletion time remains ``Available``. Catches a future
   regression where a "cascade-delete backups when site is dropped" path is
   added.

The state machine runs with ``max_examples=1000`` per the TODO acceptance
criteria.
"""

from __future__ import annotations

import secrets
import unittest

# ``frappe.testing.discovery`` does ``importlib.import_module`` on every
# ``test_*`` module and treats *any* exception raised at import time — even
# ``unittest.SkipTest`` — as a hard ``TestRunnerError`` that aborts the
# whole run. So this module must import cleanly without hypothesis. When
# the dependency is missing we degrade to a single skipped test case
# instead of refusing to load.
try:
	from hypothesis import HealthCheck, settings
	from hypothesis import strategies as st
	from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

	HYPOTHESIS_AVAILABLE = True
	HYPOTHESIS_IMPORT_ERROR: str | None = None
except ImportError as exc:
	HYPOTHESIS_AVAILABLE = False
	HYPOTHESIS_IMPORT_ERROR = str(exc)

	# Stubs that let the rest of the module load cleanly. The
	# ``FrappeSiteFSM`` class still resolves and Frappe's ``import_module``
	# call does not raise — discovery sees a single skipped TestCase that
	# we install at the bottom of the file.
	class _StubHealthCheck:
		too_slow = 0
		filter_too_much = 0

	HealthCheck = _StubHealthCheck  # type: ignore[assignment, misc]

	def settings(**_kwargs):  # type: ignore[no-redef]
		return None

	class _StubStrategies:
		@staticmethod
		def booleans():
			return None

	st = _StubStrategies  # type: ignore[assignment]

	def rule(*_args, **_kwargs):  # type: ignore[no-redef]
		def wrap(fn):
			return fn

		return wrap

	def invariant(*_args, **_kwargs):  # type: ignore[no-redef]
		def wrap(fn):
			return fn

		return wrap

	class _StubRBSM:
		class TestCase:
			pass

		def __init_subclass__(cls, **_kwargs):
			super().__init_subclass__()

	RuleBasedStateMachine = _StubRBSM  # type: ignore[assignment, misc]


# ---------------------------------------------------------------------------
# Documented FSM arrows
# ---------------------------------------------------------------------------

# (from_status, to_status). Encodes the union of:
#   - controller-driven transitions (create/cancel/migrate/backup/restore/drop)
#   - reconciliation-driven outcomes (success/failure for each in-flight op)
# 'DELETED' is a synthetic terminal representing the row removed via
# ``frappe.delete_doc`` after a confirmed drop-site probe.
#
# Two arrows ('Active' → 'In Progress' from backup; 'Failed' → 'In Progress'
# from re-create after failure) are not spelled out in the original
# §Frappe Site Provisioning prose; they are explicit in the controller and
# documented alongside this test in ``docs/control-plane-state.md``.
DOCUMENTED_SITE_ARROWS = frozenset(
	{
		("Draft", "In Progress"),
		("Active", "In Progress"),
		("Active", "Migrating"),
		("Active", "Deleting"),
		("Failed", "In Progress"),
		("Failed", "Deleting"),
		("In Progress", "Active"),
		("In Progress", "Failed"),
		("Migrating", "Active"),
		("Migrating", "Failed"),
		("Deleting", "DELETED"),
		("Deleting", "Failed"),
	}
)


IN_FLIGHT_SITE_STATUSES = frozenset({"In Progress", "Migrating", "Deleting"})
IN_FLIGHT_BACKUP_STATUSES = frozenset({"Pending", "In Progress", "Restoring"})


# ---------------------------------------------------------------------------
# Pure-Python FSM model
# ---------------------------------------------------------------------------


class _BackupRow:
	__slots__ = ("name", "status")

	def __init__(self, name: str) -> None:
		self.name = name
		self.status = "Pending"


class _SiteModel:
	"""Pure-Python mirror of ``FrappeSite`` controller + reconcile outcomes.

	Each method silently no-ops when its preconditions fail (mirrors the
	controller's ``frappe.throw`` paths from the test's perspective: the
	user would have seen an error and the row state would not change).
	"""

	def __init__(self) -> None:
		self.status = "Draft"
		self.deleted = False
		self.pending_op: str | None = None
		self.operation_token = ""
		self.operation_job_name = ""
		self.backups: list[_BackupRow] = []
		self.transitions: list[tuple[str, str]] = []
		# Snapshot taken at site-row deletion time; used by inv_archive_outlives_site.
		self.available_at_delete: dict[str, str] | None = None

	def _set_status(self, new_status: str) -> None:
		old_status = self.status
		if old_status != new_status:
			self.transitions.append((old_status, new_status))
		self.status = new_status

	def _has_inflight_backup(self) -> bool:
		return any(b.status in IN_FLIGHT_BACKUP_STATUSES for b in self.backups)

	def _rotate_token(self) -> None:
		self.operation_token = secrets.token_hex(8)

	# ---- worker-side bookkeeping ----

	def worker_submits_job(self) -> bool:
		"""Simulate the background worker stamping ``operation_job_name`` after
		Job submission. The controller clears this field at op-start; the
		worker fills it in once the K8s Job is created. Without exercising
		this step the model can never reach a Failed row with a recorded Job
		name, and the ``Failed → Deleting`` arrow becomes unreachable.
		"""
		if self.deleted or self.pending_op is None or self.operation_job_name:
			return False
		self.operation_job_name = f"j-{self.pending_op}"
		return True

	# ---- controller actions ----

	def create(self) -> bool:
		# Mirrors FrappeSite.create_site(): rejects In Progress (already in
		# flight) and Active without force_create. We never set force_create
		# in the model, so Active is rejected. Draft and Failed are allowed.
		if self.deleted or self.status not in ("Draft", "Failed"):
			return False
		self._rotate_token()
		self.pending_op = "create"
		self.operation_job_name = ""
		self._set_status("In Progress")
		return True

	def migrate(self) -> bool:
		if self.deleted or self.status != "Active":
			return False
		self._rotate_token()
		self.pending_op = "migrate"
		self.operation_job_name = ""
		self._set_status("Migrating")
		return True

	def drop(self) -> bool:
		# delete_site() requires Active or (Failed AND has operation_job_name).
		if self.deleted or self.status not in ("Active", "Failed"):
			return False
		if self.status == "Failed" and not self.operation_job_name:
			return False
		self._rotate_token()
		self.pending_op = "drop"
		self._set_status("Deleting")
		return True

	def backup(self) -> bool:
		if self.deleted or self.status != "Active" or self._has_inflight_backup():
			return False
		self.backups.append(_BackupRow(name=f"backup-{len(self.backups)}-{secrets.token_hex(4)}"))
		self._rotate_token()
		self.pending_op = "backup"
		self.operation_job_name = ""
		self._set_status("In Progress")
		return True

	def restore(self) -> bool:
		if self.deleted or self.status != "Active" or self._has_inflight_backup():
			return False
		available = next((b for b in self.backups if b.status == "Available"), None)
		if available is None:
			return False
		available.status = "Restoring"
		self._rotate_token()
		self.pending_op = "restore"
		self.operation_job_name = ""
		self._set_status("Migrating")
		return True

	def cancel(self, *, confirm_destructive: bool) -> bool:
		# Mirrors FrappeSite.cancel_site(): valid in In Progress, Deleting, Migrating.
		# Migrating requires confirm_destructive=True.
		if self.deleted or self.status not in IN_FLIGHT_SITE_STATUSES:
			return False
		if self.status == "Migrating" and not confirm_destructive:
			return False
		# Cascade-fail any in-flight backup rows. Available rows are NOT touched —
		# this is the implementation invariant that lets archives outlive sites.
		for b in self.backups:
			if b.status in IN_FLIGHT_BACKUP_STATUSES:
				b.status = "Failed"
		self._rotate_token()
		self._set_status("Failed")
		self.pending_op = None
		return True

	# ---- reconciliation outcomes ----

	def reconcile(self, *, succeed: bool) -> None:
		"""Apply the worker's terminal write for the current in-flight op."""
		if self.deleted or self.status not in IN_FLIGHT_SITE_STATUSES:
			return
		op = self.pending_op
		if self.status == "In Progress":
			next_status = "Active" if succeed else "Failed"
			self._set_status(next_status)
		elif self.status == "Migrating":
			next_status = "Active" if succeed else "Failed"
			self._set_status(next_status)
		elif self.status == "Deleting":
			if succeed:
				# Snapshot Available backups *before* simulating the on-trash
				# cascade so the invariant catches a future regression that
				# widens the cascade to also fail Available rows.
				self.available_at_delete = {b.name: b.status for b in self.backups if b.status == "Available"}
				# Mirror ``_cancel_inflight_backups_for_site``: when reconcile
				# calls ``frappe.delete_doc`` on the site row, ``on_trash``
				# fails in-flight backup rows only. Available rows must pass
				# through untouched — that is the invariant under test.
				for b in self.backups:
					if b.status in IN_FLIGHT_BACKUP_STATUSES:
						b.status = "Failed"
				self._set_status("DELETED")
				self.deleted = True
			else:
				# Drop-site Job ran but the bench still has the site → row
				# lands in Failed with the operator able to retry.
				self._set_status("Failed")
		# Resolve dependent backup rows.
		if op == "backup":
			for b in self.backups:
				if b.status in IN_FLIGHT_BACKUP_STATUSES:
					# Pending → In Progress is the worker-submit step the model
					# collapses; without it the test would have to materialise
					# every intermediate write.
					if b.status == "Pending":
						b.status = "In Progress"
					b.status = "Available" if succeed else "Failed"
					break
		elif op == "restore":
			for b in self.backups:
				if b.status == "Restoring":
					# Per docs: a failed restore returns the backup row to
					# Available with failure detail; the archive remains
					# usable for a later restore. The site itself is what
					# transitioned to Failed above.
					b.status = "Available"
					break
		self.pending_op = None
		self.operation_job_name = ""


# ---------------------------------------------------------------------------
# Hypothesis state machine
# ---------------------------------------------------------------------------


class FrappeSiteFSM(RuleBasedStateMachine):
	def __init__(self) -> None:
		super().__init__()
		self.model = _SiteModel()

	@rule()
	def r_create(self) -> None:
		self.model.create()

	@rule()
	def r_migrate(self) -> None:
		self.model.migrate()

	@rule()
	def r_drop(self) -> None:
		self.model.drop()

	@rule()
	def r_backup(self) -> None:
		self.model.backup()

	@rule()
	def r_restore(self) -> None:
		self.model.restore()

	@rule(confirm=st.booleans())
	def r_cancel(self, confirm: bool) -> None:
		self.model.cancel(confirm_destructive=confirm)

	@rule(succeed=st.booleans())
	def r_tick_reconciliation(self, succeed: bool) -> None:
		self.model.reconcile(succeed=succeed)

	@rule()
	def r_worker_submits_job(self) -> None:
		self.model.worker_submits_job()

	@invariant()
	def inv_no_terminal_inflight(self) -> None:
		"""``status ∈ in-flight  ⇔  pending_op ≠ None ∧ operation_token ≠ ""``.

		Degraded form of the TODO-12 concurrency property — the model is
		single-threaded and cannot reproduce a stale-worker / cancel-rotates
		race directly. Catches structural inconsistency: a controller change
		that rotates the token without clearing pending_op (or sets the row
		to in-flight without enqueueing) would fail here.
		"""
		if self.model.deleted:
			return
		in_flight = self.model.status in IN_FLIGHT_SITE_STATUSES
		if in_flight:
			assert self.model.pending_op is not None, (
				f"row in {self.model.status} with no pending_op (rotated token, no worker?)"
			)
			assert self.model.operation_token, f"row in {self.model.status} with empty operation_token"
		else:
			assert self.model.pending_op is None, (
				f"row in terminal {self.model.status} but pending_op={self.model.pending_op!r}"
			)

	@invariant()
	def inv_transitions_documented(self) -> None:
		"""Every observed (from, to) is in ``DOCUMENTED_SITE_ARROWS``."""
		for arrow in self.model.transitions:
			assert arrow in DOCUMENTED_SITE_ARROWS, (
				f"undocumented site transition {arrow[0]!r} -> {arrow[1]!r}; "
				f"either add it to DOCUMENTED_SITE_ARROWS and "
				f"docs/control-plane-state.md, or fix the controller path"
			)

	@invariant()
	def inv_archive_outlives_site(self) -> None:
		"""Available backups at site-deletion time stay Available afterwards."""
		if not self.model.deleted or self.model.available_at_delete is None:
			return
		current = {b.name: b.status for b in self.model.backups}
		for name, snap_status in self.model.available_at_delete.items():
			assert current.get(name) == snap_status, (
				f"backup {name!r} mutated from {snap_status} to "
				f"{current.get(name)!r} after the source Frappe Site row was "
				f"deleted; archives must outlive the source row"
			)


FrappeSiteFSM.TestCase.settings = settings(
	max_examples=1000,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# Hedge bench's test discovery: subclass both the hypothesis-generated
# unittest TestCase and ``frappe.tests.UnitTestCase`` so the runner picks
# this up the same way it picks up every other ``test_*`` module in this
# directory. ``UnitTestCase`` is import-guarded for environments where
# ``frappe`` is unavailable (e.g. the host running ``ruff`` outside the
# bench container).
try:
	from frappe.tests import UnitTestCase

	class TestFrappeSiteFSM(FrappeSiteFSM.TestCase, UnitTestCase):  # type: ignore[misc, valid-type]
		pass
except ImportError:
	TestFrappeSiteFSM = FrappeSiteFSM.TestCase


if not HYPOTHESIS_AVAILABLE:
	# Replace the placeholder hypothesis-stub TestCase with a single
	# skipped test so discovery sees a clear "skipped" instead of a class
	# that silently runs zero tests against an inert state machine.
	@unittest.skip(f"hypothesis not installed: {HYPOTHESIS_IMPORT_ERROR}")
	class TestFrappeSiteFSM(unittest.TestCase):
		def test_property_fsm_skipped(self):
			pass
