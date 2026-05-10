"""In-container scenario: ``worker_kill_mid_helm_upgrade``.

Triggers a Helm Release upgrade against an already-Deployed row, waits
for the row to flip to ``In Progress`` (the worker has owned the
operation), then ``SIGKILL``s the long-queue worker.  The row is left
stranded with a live ``operation_token`` and stale
``operation_started_at``; reconciliation must recover it within the
documented stale-op window (``STALE_OPERATION_THRESHOLD_MINUTES`` = 30
min, see ``kubeport/utils/constants.py``).

By default the harness backdates ``operation_started_at`` past the
threshold and invokes ``_reconcile_stale_helm_operations`` directly so
the recovery code path is exercised in seconds.  Pass
``--no-fast-forward`` (omit ``--fast-forward``) to wait for the natural
5-minute reconciliation cron tick — useful when measuring real-time
MTTR for thesis evidence.

Emits a single JSON object on stdout between ``RESULT_BEGIN`` /
``RESULT_END`` markers; consumed by ``eval/faults/run.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime


def _bench_setup(site: str, bench_path: str) -> None:
	os.chdir(os.path.join(bench_path, "sites"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "frappe"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "kubeport"))
	import frappe

	frappe.init(site=site, sites_path=".")
	frappe.connect()


def _utc_iso() -> str:
	return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_until(predicate, timeout_s: float, interval_s: float = 1.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(interval_s)
	return predicate()


def _pgrep(pattern: str) -> list[int]:
	r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
	return [int(p) for p in r.stdout.split() if p.strip().isdigit()]


def _descendants(pid: int) -> list[int]:
	out: list[int] = []
	stack = [pid]
	while stack:
		cur = stack.pop()
		r = subprocess.run(["pgrep", "-P", str(cur)], capture_output=True, text=True)
		for token in r.stdout.split():
			if token.strip().isdigit():
				child = int(token)
				out.append(child)
				stack.append(child)
	return out


def _find_long_worker_pids() -> list[int]:
	return _pgrep(r"frappe[ ]worker.*--queue.*long")


def _find_helm_subprocess_pids() -> list[int]:
	return _pgrep(r"helm[ ]upgrade")


def _collect_kill_targets() -> tuple[list[int], list[int]]:
	"""Return ``(parent_pids, all_targets)``.

	``all_targets`` is parents + every descendant + any visible
	``helm upgrade`` subprocess, deduplicated and sorted.
	"""
	parents = _find_long_worker_pids()
	targets: set[int] = set(parents)
	for ppid in parents:
		for d in _descendants(ppid):
			targets.add(d)
	for hp in _find_helm_subprocess_pids():
		targets.add(hp)
	return parents, sorted(targets)


def _kill_pids(pids: list[int]) -> list[int]:
	killed: list[int] = []
	for pid in pids:
		try:
			os.kill(pid, signal.SIGKILL)
			killed.append(pid)
		except ProcessLookupError:
			continue
	return killed


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", default="/workspace/development/bench-16")
	parser.add_argument("--release-doc-name", required=True)
	parser.add_argument("--cluster-doc-name", default="")
	parser.add_argument("--kubeconfig-file", default="")
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument("--mttr-bound-seconds", type=int, default=1800)
	parser.add_argument("--in-progress-poll-seconds", type=int, default=120)
	parser.add_argument("--recovery-poll-seconds", type=int, default=2400)
	parser.add_argument("--fast-forward", action="store_true")
	args = parser.parse_args()

	_bench_setup(args.site, args.bench_path)
	import frappe

	from kubeport.tasks import reconciliation as reconcile_mod
	from kubeport.utils.constants import STALE_OPERATION_THRESHOLD_MINUTES

	scenario_name = "worker_kill_mid_helm_upgrade"
	steps: list[dict] = []
	result = {
		"scenario": scenario_name,
		"defended_invariant": "Stale operation reconciler recovers worker-stranded Helm Release rows",
		"witness": {
			"reconciler": "kubeport/tasks/reconciliation.py:_reconcile_stale_helm_operations",
			"threshold_minutes": STALE_OPERATION_THRESHOLD_MINUTES,
		},
		"injected_at": "",
		"recovered_at": "",
		"mttr_seconds": None,
		"expected_mttr_bound_seconds": args.mttr_bound_seconds,
		"fast_forward_used": args.fast_forward,
		"observed": {},
		"passed": False,
		"detail": "",
		"steps": steps,
	}

	def _step(name: str, **kwargs) -> None:
		steps.append({"step": name, "at": _utc_iso(), **kwargs})

	release_docname = args.release_doc_name
	if not frappe.db.exists("Helm Release", release_docname):
		result["detail"] = f"Helm Release '{release_docname}' does not exist"
		_emit(result)
		return 2

	frappe.db.rollback()
	pre_status, pre_token = frappe.db.get_value(
		"Helm Release", release_docname, ["status", "operation_token"]
	)
	if pre_status not in ("Deployed", "Degraded"):
		result["detail"] = (
			f"Helm Release '{release_docname}' must start from Deployed or Degraded "
			f"to safely run a no-op upgrade; current status: '{pre_status}'."
		)
		_emit(result)
		return 2
	_step("pre_status", status=pre_status, operation_token=pre_token)

	doc = frappe.get_doc("Helm Release", release_docname)
	doc.deploy_release()
	frappe.db.commit()
	_step("deploy_release_enqueued")

	def _in_progress() -> bool:
		frappe.db.rollback()
		return frappe.db.get_value("Helm Release", release_docname, "status") == "In Progress"

	if not _wait_until(_in_progress, args.in_progress_poll_seconds, interval_s=0.2):
		result["detail"] = (
			f"Row never reached 'In Progress' within {args.in_progress_poll_seconds}s — "
			"the long worker may not have picked the job up."
		)
		_emit(result)
		return 1
	frappe.db.rollback()
	mid_token = frappe.db.get_value("Helm Release", release_docname, "operation_token")
	_step("in_progress_observed", operation_token=mid_token)

	# Wait until the helm subprocess is observable; only then is the work
	# actually mid-flight. Without this, an RQ child can finish the job
	# (writing status=Deployed/Degraded) before we kill it, so the row
	# never strands and the stale-op recovery code path is not exercised.
	def _helm_running() -> bool:
		return bool(_find_helm_subprocess_pids())

	if not _wait_until(_helm_running, timeout_s=60.0, interval_s=0.05):
		result["detail"] = (
			"helm upgrade subprocess never appeared on the process table — "
			"cannot guarantee mid-flight kill timing."
		)
		_emit(result)
		return 1
	_step("helm_subprocess_observed")

	parents, targets = _collect_kill_targets()
	if not targets:
		result["detail"] = "No worker / helm processes found at kill time."
		_emit(result)
		return 1
	killed = _kill_pids(targets)
	injected_at = _utc_iso()
	t_inject = time.monotonic()
	result["injected_at"] = injected_at
	_step("worker_killed", parents=parents, targets=targets, killed=killed)

	def _all_gone() -> bool:
		return not (_find_long_worker_pids() or _find_helm_subprocess_pids())

	if not _wait_until(_all_gone, timeout_s=15.0, interval_s=0.2):
		# Re-collect any survivors and SIGKILL once more — fork races can
		# briefly produce a second descendant after the first sweep.
		_, survivors = _collect_kill_targets()
		if survivors:
			_kill_pids(survivors)
			_step("worker_resweep", survivors=survivors)
		if not _wait_until(_all_gone, timeout_s=10.0, interval_s=0.2):
			result["detail"] = "Worker / helm processes did not exit after SIGKILL sweep."
			_emit(result)
			return 1
	_step("worker_exit_confirmed")

	frappe.db.rollback()
	stranded_status, stranded_token = frappe.db.get_value(
		"Helm Release", release_docname, ["status", "operation_token"]
	)
	_step("post_kill_state", status=stranded_status, operation_token=stranded_token)
	if stranded_status != "In Progress":
		result["detail"] = (
			f"Row left in unexpected state '{stranded_status}' after worker kill "
			"(expected 'In Progress'); cannot demonstrate stale-op recovery."
		)
		_emit(result)
		return 1
	if stranded_token != mid_token:
		result["detail"] = "operation_token rotated unexpectedly between in-progress and post-kill."
		_emit(result)
		return 1

	if args.fast_forward:
		backdate_minutes = STALE_OPERATION_THRESHOLD_MINUTES + 5
		backdated = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-backdate_minutes)
		frappe.db.set_value(
			"Helm Release",
			release_docname,
			"operation_started_at",
			backdated,
			update_modified=False,
		)
		frappe.db.commit()
		_step(
			"fast_forward_backdated",
			backdated_to=str(backdated),
			minutes_subtracted=backdate_minutes,
		)
		try:
			reconcile_mod._reconcile_stale_helm_operations()
			frappe.db.commit()
		except Exception as exc:
			import traceback as _tb

			result["detail"] = f"Direct reconciler invocation failed: {type(exc).__name__}: {exc}"
			result["observed"]["reconciler_traceback"] = _tb.format_exc()
			_emit(result)
			return 1
		_step("reconciler_invoked")

	def _terminal() -> bool:
		frappe.db.rollback()
		st = frappe.db.get_value("Helm Release", release_docname, "status")
		return st in ("Deployed", "Degraded", "Failed", "Draft")

	if not _wait_until(_terminal, args.recovery_poll_seconds, interval_s=2.0):
		result["detail"] = (
			f"Row did not reach a terminal status within {args.recovery_poll_seconds}s "
			"after worker kill (no stale-op recovery observed)."
		)
		_emit(result)
		return 1
	recovered_at = _utc_iso()
	t_recover = time.monotonic()
	mttr_seconds = round(t_recover - t_inject, 3)
	frappe.db.rollback()
	final_status, final_token, final_detail = frappe.db.get_value(
		"Helm Release",
		release_docname,
		["status", "operation_token", "helm_status_detail"],
	)
	_step(
		"recovered",
		status=final_status,
		operation_token=final_token,
		helm_status_detail=(final_detail or "")[:240],
	)

	result["recovered_at"] = recovered_at
	result["mttr_seconds"] = mttr_seconds
	result["observed"] = {
		"final_status": final_status,
		"final_helm_status_detail": (final_detail or "")[:240],
		"operation_token_rotated": final_token != mid_token,
	}

	expected_terminal = ("Deployed", "Degraded")
	passed = final_status in expected_terminal and mttr_seconds <= args.mttr_bound_seconds
	result["passed"] = passed
	if not passed:
		if final_status not in expected_terminal:
			result["detail"] = (
				f"Recovered to unexpected status '{final_status}' (expected one of {expected_terminal})."
			)
		else:
			result["detail"] = f"MTTR {mttr_seconds:.1f}s exceeded bound {args.mttr_bound_seconds}s."
	else:
		result["detail"] = (
			f"Stale-op reconciler recovered the stranded row to '{final_status}' "
			f"in {mttr_seconds:.1f}s "
			f"({'fast-forwarded' if args.fast_forward else 'real-time'})."
		)

	_emit(result)
	return 0 if passed else 1


def _emit(result: dict) -> None:
	print("RESULT_BEGIN")
	print(json.dumps(result, indent=2, sort_keys=True))
	print("RESULT_END")


if __name__ == "__main__":
	sys.exit(main())
