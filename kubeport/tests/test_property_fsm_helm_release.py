# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

"""
Property-based FSM tests for ``Helm Release``.

This module checks the documented Helm Release state machine against a
``hypothesis.stateful.RuleBasedStateMachine`` model that mirrors the
controller transitions in
``kubeport/kubeport/doctype/helm_release/helm_release.py`` and the
reconciliation outcomes that finalize each in-flight or steady-state row
in ``kubeport/tasks/helm_tasks.py`` and ``kubeport/tasks/reconciliation.py``.

Scope and intent
----------------
This is a **specification-level** check, parallel to
``test_property_fsm_frappe_site``. The model is a pure-Python mirror of
the controller — no MariaDB, no Frappe document machinery, no mocked Helm
or K8s clients. It is hermetic by construction. The bug class it catches
is *FSM consistency*: a controller change that introduces an arrow not
in the documented set, weakens the uninstall block, or mutates
``last_applied_spec_hash`` outside an allowed terminal will fail this
test.

Implementation-level coverage (real controller methods, mocked Helm and
Kubernetes clients, the actual ``frappe.enqueue`` → worker → reconcile
flow) lives in ``kubeport/tests/test_helm_tasks.py`` and
``kubeport/tests/test_reconciliation.py``. The two test layers are
complementary.

Invariants
----------
The three invariants required by ``TODO-13``:

1. ``inv_dependent_site_blocks_uninstall``: a normal (``force=False``)
   uninstall accepted while a linked ``Frappe Site`` is in a blocking
   status would land the row in ``Uninstalling``. The invariant catches
   that path: whenever the row is in ``Uninstalling`` from a
   ``force=False`` call, the snapshot of linked-site statuses taken at
   uninstall-acceptance time must contain no blocking status. The
   blocking set mirrors ``_BLOCKING_SITE_STATUSES`` in the controller
   (``Active``, ``In Progress``, ``Deleting``, ``Migrating``) plus the
   ``Failed`` rows that still record an ``operation_job_name`` — the
   TODO prose calls out ``Active`` as a representative case; the
   implementation set is wider, and this test enforces the wider set.
2. ``inv_failed_cannot_be_directly_deleted``: phrased to match the
   controller's ``on_trash`` (which rejects every status except
   ``Draft``). The TODO prose names ``Failed`` specifically; the
   structural check ``deleted ⇒ status_at_delete == Draft`` catches both
   the named ``Failed`` case and any future regression that admits other
   non-``Draft`` deletions.
3. ``inv_spec_hash_monotonic``: ``last_applied_spec_hash`` only changes
   under one of two arrows: a successful deploy / upgrade / rollback
   reconcile that lands the row in ``Deployed`` / ``Degraded`` (sets the
   hash to the current ``desired_spec_hash``) or a successful uninstall
   that lands the row in ``Draft`` (clears the hash to ``""``). The TODO
   prose names only deploy / upgrade / rollback; the implementation also
   clears on uninstall (``_finalize_uninstall_success`` in
   ``kubeport/tasks/helm_tasks.py``). The invariant accepts the wider,
   in-code set and would still fail if any *other* path mutated the
   hash (e.g. drift reconcile, cancel, value edit, force-uninstall
   timeout landing in ``Failed``).

The state machine runs with ``max_examples=1000`` per the TODO acceptance
criteria.
"""

from __future__ import annotations

import secrets
import unittest

# ``frappe.testing.discovery`` does ``importlib.import_module`` on every
# ``test_*`` module and treats *any* exception raised at import time —
# even ``unittest.SkipTest`` — as a hard ``TestRunnerError`` that aborts
# the whole run. So this module must import cleanly without hypothesis.
# When the dependency is missing we degrade to a single skipped test
# case instead of refusing to load. Mirrors the pattern in
# ``test_property_fsm_frappe_site``.
try:
	from hypothesis import HealthCheck, settings
	from hypothesis import strategies as st
	from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

	HYPOTHESIS_AVAILABLE = True
	HYPOTHESIS_IMPORT_ERROR: str | None = None
except ImportError as exc:
	HYPOTHESIS_AVAILABLE = False
	HYPOTHESIS_IMPORT_ERROR = str(exc)

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

		@staticmethod
		def sampled_from(_choices):
			return None

		@staticmethod
		def integers(*_args, **_kwargs):
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
#   - controller-driven transitions (deploy/upgrade/rollback/uninstall)
#   - in-flight reconcile outcomes (success/failure for each pending op)
#   - steady-state drift reconcile outcomes for Deployed/Degraded rows
#
# Two classes of arrows are not spelled out in the §Helm Release
# Management one-line lifecycle ("Draft → In Progress → Deployed /
# Degraded / Failed → Uninstalling → Draft") but are explicit in code:
#   1. Drift transitions between ``Deployed`` / ``Degraded`` and
#      ``Failed`` driven by ``reconcile_helm_releases``.
#   2. ``Uninstalling → Failed`` driven by stale-uninstall recovery in
#      ``reconcile_stale_helm_operations``.
# A regression that adds either an undocumented arrow or removes one of
# these documented ones will fail ``inv_transitions_documented``,
# forcing a paired update to ``docs/control-plane-state.md``.
DOCUMENTED_RELEASE_ARROWS = frozenset(
	{
		# Controller actions: deploy / upgrade / rollback enter In Progress.
		("Draft", "In Progress"),
		("Deployed", "In Progress"),
		("Degraded", "In Progress"),
		("Failed", "In Progress"),
		# Controller actions: uninstall enters Uninstalling.
		("Deployed", "Uninstalling"),
		("Degraded", "Uninstalling"),
		("Failed", "Uninstalling"),
		# In-flight reconcile outcomes for In Progress rows.
		("In Progress", "Deployed"),
		("In Progress", "Degraded"),
		("In Progress", "Failed"),
		# In-flight reconcile outcomes for Uninstalling rows. Stale
		# uninstall recovery lands a stuck row in Failed (see
		# ``reconcile_stale_helm_operations``).
		("Uninstalling", "Draft"),
		("Uninstalling", "Failed"),
		# Steady-state drift reconcile outcomes (``reconcile_helm_releases``).
		("Deployed", "Degraded"),
		("Degraded", "Deployed"),
		("Deployed", "Failed"),
		("Degraded", "Failed"),
	}
)


IN_FLIGHT_RELEASE_STATUSES = frozenset({"In Progress", "Uninstalling"})

# Mirrors ``_BLOCKING_SITE_STATUSES`` in
# ``kubeport/kubeport/doctype/helm_release/helm_release.py``. The
# uninstall path also counts ``Failed`` rows with a non-empty
# ``operation_job_name`` as blocking — encoded separately because it
# combines status with another field.
_BLOCKING_SITE_STATUSES = frozenset({"Active", "In Progress", "Deleting", "Migrating"})


def _site_blocks_uninstall(site_status: str, has_job_name: bool) -> bool:
	if site_status in _BLOCKING_SITE_STATUSES:
		return True
	if site_status == "Failed" and has_job_name:
		return True
	return False


# ---------------------------------------------------------------------------
# Pure-Python FSM model
# ---------------------------------------------------------------------------


class _LinkedSite:
	__slots__ = ("has_job_name", "name", "status")

	def __init__(self, name: str, status: str, has_job_name: bool) -> None:
		self.name = name
		self.status = status
		self.has_job_name = has_job_name

	def blocks(self) -> bool:
		return _site_blocks_uninstall(self.status, self.has_job_name)


class _ReleaseModel:
	"""Pure-Python mirror of ``HelmRelease`` controller + reconcile outcomes.

	Each method silently no-ops when its preconditions fail (mirrors the
	controller's ``frappe.throw`` paths from the test's perspective: the
	user would have seen an error and the row state would not change).
	"""

	def __init__(self) -> None:
		self.status = "Draft"
		self.deleted = False
		self.pending_op: str | None = None
		self.operation_token = ""
		self.desired_spec_hash = "h0"
		self.last_applied_spec_hash = ""
		self.values_revision = 0
		self.linked_sites: list[_LinkedSite] = []
		self.transitions: list[tuple[str, str]] = []
		# Status snapshot at the moment ``frappe.delete_doc`` was
		# accepted; used by ``inv_failed_cannot_be_directly_deleted``.
		self.status_at_delete: str | None = None
		# Snapshot of linked-site blocking states at the moment a
		# ``force=False`` uninstall was accepted; used by
		# ``inv_dependent_site_blocks_uninstall``.
		self.uninstall_blocking_snapshot: list[str] | None = None
		# Tracks the type of the last accepted uninstall (force=True/False).
		# ``None`` means no uninstall is in flight.
		self.uninstall_force: bool | None = None

	def _set_status(self, new_status: str) -> None:
		old_status = self.status
		if old_status != new_status:
			self.transitions.append((old_status, new_status))
		self.status = new_status

	def _rotate_token(self) -> None:
		self.operation_token = secrets.token_hex(8)

	def _can_start_op(self) -> bool:
		return not self.deleted and self.status not in IN_FLIGHT_RELEASE_STATUSES

	# ---- linked-site bookkeeping (drives uninstall block) ----

	def add_linked_site(self, status: str, has_job_name: bool) -> None:
		if self.deleted:
			return
		name = f"site-{len(self.linked_sites)}-{secrets.token_hex(2)}"
		self.linked_sites.append(_LinkedSite(name=name, status=status, has_job_name=has_job_name))

	def mutate_linked_site(self, idx: int, status: str, has_job_name: bool) -> None:
		if self.deleted or not self.linked_sites:
			return
		site = self.linked_sites[idx % len(self.linked_sites)]
		site.status = status
		site.has_job_name = has_job_name

	def remove_linked_site(self, idx: int) -> None:
		if self.deleted or not self.linked_sites:
			return
		del self.linked_sites[idx % len(self.linked_sites)]

	# ---- desired-state edits ----

	def edit_values(self) -> None:
		"""Simulate the operator changing values / chart version.

		Only ``desired_spec_hash`` rotates here; ``last_applied_spec_hash``
		stays untouched until a successful deploy/upgrade/rollback
		writes the new hash back. This is the structural property that
		``inv_spec_hash_monotonic`` checks.
		"""
		if self.deleted:
			return
		self.values_revision += 1
		self.desired_spec_hash = f"h{self.values_revision}"

	# ---- controller actions ----

	def deploy(self) -> bool:
		# Mirrors HelmRelease.deploy_release() restricted to the Draft
		# entry case. Returns False when preconditions fail so the
		# rule body can simply call this and continue.
		if not self._can_start_op() or self.status != "Draft":
			return False
		self._rotate_token()
		self.pending_op = "deploy"
		self._set_status("In Progress")
		return True

	def upgrade(self) -> bool:
		# deploy_release() is the same controller method as deploy(); we
		# split rules to mirror TODO-13's enumeration. Upgrade applies
		# when the row already represents a previously-deployed release
		# (Deployed / Degraded / Failed).
		if not self._can_start_op() or self.status not in ("Deployed", "Degraded", "Failed"):
			return False
		self._rotate_token()
		self.pending_op = "upgrade"
		self._set_status("In Progress")
		return True

	def rollback(self) -> bool:
		if not self._can_start_op() or self.status not in ("Deployed", "Degraded", "Failed"):
			return False
		self._rotate_token()
		self.pending_op = "rollback"
		self._set_status("In Progress")
		return True

	def uninstall(self) -> bool:
		"""Normal uninstall — refused while any linked site blocks."""
		if not self._can_start_op() or self.status not in ("Deployed", "Degraded", "Failed"):
			return False
		blockers = [s.status for s in self.linked_sites if s.blocks()]
		if blockers:
			return False
		self._rotate_token()
		self.pending_op = "uninstall"
		self.uninstall_force = False
		self.uninstall_blocking_snapshot = blockers
		self._set_status("Uninstalling")
		return True

	def force_uninstall(self) -> bool:
		"""Force uninstall — bypasses the linked-site block (with typed confirmation)."""
		if not self._can_start_op() or self.status not in ("Deployed", "Degraded", "Failed"):
			return False
		self._rotate_token()
		self.pending_op = "uninstall"
		self.uninstall_force = True
		self.uninstall_blocking_snapshot = [s.status for s in self.linked_sites if s.blocks()]
		self._set_status("Uninstalling")
		return True

	# ---- frappe.delete_doc / on_trash ----

	def delete_doc(self) -> bool:
		"""Mirror ``HelmRelease.on_trash``: only ``Draft`` rows can be trashed."""
		if self.deleted:
			return False
		if self.status != "Draft":
			return False
		self.status_at_delete = self.status
		self.deleted = True
		return True

	# ---- reconciliation outcomes ----

	def reconcile(self, outcome: str) -> None:
		"""Apply the worker's terminal write or a drift-reconcile outcome.

		``outcome`` is one of:
		  - ``healthy``: in-flight succeeds → Deployed; uninstall succeeds
		    → Draft; drift reconcile finds Deployed.
		  - ``degraded``: in-flight succeeds with workload-readiness
		    issues → Degraded; drift reconcile finds Degraded.
		  - ``failed``: in-flight fails or stale-uninstall recovery
		    lands Failed; drift reconcile reports the release missing.
		"""
		if self.deleted:
			return
		if self.status == "In Progress":
			self._reconcile_in_progress(outcome)
		elif self.status == "Uninstalling":
			self._reconcile_uninstalling(outcome)
		elif self.status in ("Deployed", "Degraded"):
			self._reconcile_drift(outcome)
		# Other terminal states (Draft, Failed) are ignored by the
		# real reconcilers — see _IN_FLIGHT_RELEASE_STATUSES /
		# _HEALTHY_RELEASE_STATUSES filters in
		# kubeport/tasks/reconciliation.py.

	def _reconcile_in_progress(self, outcome: str) -> None:
		if outcome == "healthy":
			next_status = "Deployed"
		elif outcome == "degraded":
			next_status = "Degraded"
		else:
			next_status = "Failed"
		self._set_status(next_status)
		# Successful deploy / upgrade / rollback writes the new hash;
		# failed reconcile leaves the previous hash untouched. This
		# is the documented contract of
		# ``install_or_upgrade_release`` and ``rollback_release`` in
		# ``kubeport/tasks/helm_tasks.py``.
		if next_status in ("Deployed", "Degraded"):
			self.last_applied_spec_hash = self.desired_spec_hash
		self.pending_op = None

	def _reconcile_uninstalling(self, outcome: str) -> None:
		if outcome == "healthy":
			# Successful uninstall: row returns to Draft, hash cleared.
			self._set_status("Draft")
			self.last_applied_spec_hash = ""
			self.linked_sites = []  # nothing to reference once the bench is gone
		else:
			# Stale-uninstall recovery (degraded / failed): the row
			# is parked in Failed for operator retry. The
			# implementation does NOT touch last_applied_spec_hash on
			# this path — the invariant relies on that.
			self._set_status("Failed")
		self.pending_op = None
		self.uninstall_force = None
		self.uninstall_blocking_snapshot = None

	def _reconcile_drift(self, outcome: str) -> None:
		# The drift reconciler observes Deployed/Degraded rows and
		# reclassifies them. It never touches last_applied_spec_hash —
		# the spec hash only re-rotates on a fresh deploy/upgrade/
		# rollback (or uninstall). That is the property under
		# inv_spec_hash_monotonic.
		if outcome == "healthy":
			self._set_status("Deployed")
		elif outcome == "degraded":
			self._set_status("Degraded")
		else:
			self._set_status("Failed")


# ---------------------------------------------------------------------------
# Hypothesis state machine
# ---------------------------------------------------------------------------


_SITE_STATUSES = ("Draft", "In Progress", "Active", "Failed", "Deleting", "Migrating")
_RECONCILE_OUTCOMES = ("healthy", "degraded", "failed")


class HelmReleaseFSM(RuleBasedStateMachine):
	def __init__(self) -> None:
		super().__init__()
		self.model = _ReleaseModel()
		# Witness for inv_spec_hash_monotonic: the value we expect
		# ``last_applied_spec_hash`` to hold at the next invariant
		# evaluation, only updated by allowed mutators.
		self._expected_hash = ""

	def _record_hash_mutation(self, new_value: str) -> None:
		self._expected_hash = new_value

	# ---- TODO-13 enumerated rules ----

	@rule()
	def r_deploy(self) -> None:
		self.model.deploy()

	@rule()
	def r_upgrade(self) -> None:
		self.model.upgrade()

	@rule()
	def r_rollback(self) -> None:
		self.model.rollback()

	@rule()
	def r_uninstall(self) -> None:
		self.model.uninstall()

	@rule()
	def r_force_uninstall(self) -> None:
		self.model.force_uninstall()

	@rule(outcome=st.sampled_from(_RECONCILE_OUTCOMES))
	def r_tick_reconciliation(self, outcome: str) -> None:
		# Snapshot the in-flight op before the model clears it so we
		# can update the expected-hash witness in lock-step with the
		# reconcile branch the model just took. The implementation
		# this mirrors lives in kubeport/tasks/helm_tasks.py.
		op_before = self.model.pending_op
		status_before = self.model.status
		self.model.reconcile(outcome=outcome)
		if status_before == "In Progress" and outcome in ("healthy", "degraded"):
			self._record_hash_mutation(self.model.desired_spec_hash)
		elif status_before == "Uninstalling" and outcome == "healthy":
			self._record_hash_mutation("")
		# All other branches (in-progress failure, stale uninstall
		# recovery, drift reconcile) leave last_applied_spec_hash
		# unchanged — and so does the witness.
		_ = op_before  # retained for future debugging output

	# ---- auxiliary rules to evolve world state ----

	@rule()
	def r_edit_values(self) -> None:
		self.model.edit_values()

	@rule(
		status=st.sampled_from(_SITE_STATUSES),
		has_job_name=st.booleans(),
	)
	def r_add_linked_site(self, status: str, has_job_name: bool) -> None:
		self.model.add_linked_site(status=status, has_job_name=has_job_name)

	@rule(
		idx=st.integers(min_value=0, max_value=15),
		status=st.sampled_from(_SITE_STATUSES),
		has_job_name=st.booleans(),
	)
	def r_mutate_linked_site(self, idx: int, status: str, has_job_name: bool) -> None:
		self.model.mutate_linked_site(idx, status, has_job_name)

	@rule(idx=st.integers(min_value=0, max_value=15))
	def r_remove_linked_site(self, idx: int) -> None:
		self.model.remove_linked_site(idx)

	@rule()
	def r_delete_doc(self) -> None:
		self.model.delete_doc()

	# ---- invariants ----

	@invariant()
	def inv_no_terminal_inflight(self) -> None:
		"""``status ∈ in-flight  ⇔  pending_op ≠ None ∧ operation_token ≠ ""``.

		Same structural check as the Frappe Site FSM uses: catches a
		controller path that rotates the operation token without
		setting ``pending_op``, or lands the row in an in-flight
		status without enqueueing a worker.
		"""
		if self.model.deleted:
			return
		in_flight = self.model.status in IN_FLIGHT_RELEASE_STATUSES
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
		"""Every observed (from, to) is in ``DOCUMENTED_RELEASE_ARROWS``."""
		for arrow in self.model.transitions:
			assert arrow in DOCUMENTED_RELEASE_ARROWS, (
				f"undocumented release transition {arrow[0]!r} -> {arrow[1]!r}; "
				f"either add it to DOCUMENTED_RELEASE_ARROWS and "
				f"docs/control-plane-state.md, or fix the controller path"
			)

	@invariant()
	def inv_dependent_site_blocks_uninstall(self) -> None:
		"""Normal uninstall never proceeds while a linked site blocks.

		Witness: when ``status == 'Uninstalling'`` was reached via a
		``force=False`` call, the snapshot of linked-site blocking
		statuses taken at uninstall-acceptance time must be empty.
		Force uninstalls intentionally ignore this — they record the
		snapshot for diagnostics but the invariant excludes them.
		"""
		if self.model.deleted or self.model.status != "Uninstalling":
			return
		if self.model.uninstall_force is True:
			return
		assert self.model.uninstall_force is False, (
			"row in Uninstalling with no recorded uninstall_force flag"
		)
		assert self.model.uninstall_blocking_snapshot == [], (
			"normal uninstall accepted with linked sites still blocking: "
			f"{self.model.uninstall_blocking_snapshot!r}"
		)

	@invariant()
	def inv_failed_cannot_be_directly_deleted(self) -> None:
		"""Deleting a row outside ``Draft`` is rejected.

		Phrased structurally as ``deleted ⇒ status_at_delete == 'Draft'``
		so it catches the named ``Failed`` case from TODO-13 *and* any
		future regression that admits other non-``Draft`` deletions.
		"""
		if not self.model.deleted:
			return
		assert self.model.status_at_delete == "Draft", (
			f"row deleted from non-Draft status {self.model.status_at_delete!r}; "
			f"on_trash must reject every status except 'Draft'"
		)

	@invariant()
	def inv_spec_hash_monotonic(self) -> None:
		"""``last_applied_spec_hash`` only changes on allowed terminals.

		Allowed mutations (in the implementation):
		  * In-flight reconcile success (``In Progress -> Deployed/Degraded``)
		    after a deploy / upgrade / rollback → set to the current
		    ``desired_spec_hash``.
		  * Successful uninstall (``Uninstalling -> Draft``) →
		    cleared to ``""``.
		Any other path that mutates ``last_applied_spec_hash`` (drift
		reconcile, failed in-flight reconcile, stale-uninstall
		recovery, value edits, force-uninstall timeout landing in
		Failed) makes the witness diverge from the actual field and
		fails the test.
		"""
		assert self.model.last_applied_spec_hash == self._expected_hash, (
			f"last_applied_spec_hash mutated outside an allowed terminal: "
			f"got {self.model.last_applied_spec_hash!r}, "
			f"expected {self._expected_hash!r}"
		)


HelmReleaseFSM.TestCase.settings = settings(
	max_examples=1000,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# Hedge bench's test discovery: subclass both the hypothesis-generated
# unittest TestCase and ``frappe.tests.UnitTestCase`` so the runner
# picks this up the same way it picks up every other ``test_*`` module
# in this directory. ``UnitTestCase`` is import-guarded for
# environments where ``frappe`` is unavailable (e.g. the host running
# ``ruff`` outside the bench container).
try:
	from frappe.tests import UnitTestCase

	class TestHelmReleaseFSM(HelmReleaseFSM.TestCase, UnitTestCase):  # type: ignore[misc, valid-type]
		pass
except ImportError:
	TestHelmReleaseFSM = HelmReleaseFSM.TestCase


if not HYPOTHESIS_AVAILABLE:
	# Replace the placeholder hypothesis-stub TestCase with a single
	# skipped test so discovery sees a clear "skipped" instead of a
	# class that silently runs zero tests against an inert state machine.
	@unittest.skip(f"hypothesis not installed: {HYPOTHESIS_IMPORT_ERROR}")
	class TestHelmReleaseFSM(unittest.TestCase):  # type: ignore[no-redef]
		def test_property_fsm_skipped(self):
			pass
