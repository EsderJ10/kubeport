"""In-container scenario: ``pod_exec_timeout_during_site_probe``.

Forces ``_probe_site_state`` into the ``unknown`` branch by making the
underlying pod-exec call (``_exec_list_sites``) raise a timeout-like
exception, then asserts the reconciler **defers** instead of marking
the row ``Failed``.  After the patch is removed, the next reconcile
tick must successfully probe the bench and transition the row to its
correct terminal state.

Setup mirrors ``_inproc_job_ttl.py``: the row is force-deleted from
its operation Job so the reconciler falls into the probe branch in
``_reconcile_site_migrate``.

Sequence:

1. Trigger a no-op ``bench migrate`` (row → ``Migrating``).
2. Force-delete the operation Job so the reconciler must fall back
   to ``_probe_site_state`` instead of reading Job status.
3. Monkey-patch ``kubeport.utils.discovery._exec_list_sites`` to raise
   a timeout-like exception.
4. Invoke ``_reconcile_frappe_sites`` once — the row must stay
   ``Migrating`` (probe returned ``unknown`` → reconciler deferred).
5. Restore the original ``_exec_list_sites``.
6. Invoke ``_reconcile_frappe_sites`` again — the bench probe now
   succeeds and the row reaches ``Active``.

Acceptance for the scenario: the reconciler defers at least one tick
under exec timeout AND eventually recovers to ``Active`` once the
exec succeeds.

Emits a single JSON object on stdout between ``RESULT_BEGIN`` /
``RESULT_END`` markers; consumed by ``eval/faults/run.py``.
"""

from __future__ import annotations

import argparse
import json
import os
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


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", default="/workspace/development/bench-16")
	parser.add_argument("--site-doc-name", required=True)
	parser.add_argument("--mttr-bound-seconds", type=int, default=600)
	parser.add_argument("--migrate-job-poll-seconds", type=int, default=120)
	parser.add_argument("--recovery-poll-seconds", type=int, default=600)
	parser.add_argument("--fast-forward", action="store_true")
	args = parser.parse_args()

	_bench_setup(args.site, args.bench_path)
	import frappe
	from kubernetes import client as k8s_client
	from kubernetes.client.rest import ApiException

	from kubeport.tasks import reconciliation as reconcile_mod
	from kubeport.utils import discovery as discovery_mod
	from kubeport.utils.k8s_client import get_k8s_api_client

	scenario_name = "pod_exec_timeout_during_site_probe"
	steps: list[dict] = []
	result = {
		"scenario": scenario_name,
		"defended_invariant": (
			"Three-state probe returns unknown on transient pod-exec failure; "
			"the reconciler defers the row instead of defaulting to Failed"
		),
		"witness": {
			"reconciler": "kubeport/tasks/reconciliation.py:_reconcile_site_migrate",
			"probe": "kubeport/tasks/reconciliation.py:_probe_site_state",
			"exec": "kubeport/utils/discovery.py:_exec_list_sites",
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

	site_docname = args.site_doc_name
	if not frappe.db.exists("Frappe Site", site_docname):
		result["detail"] = f"Frappe Site '{site_docname}' does not exist"
		_emit(result)
		return 2

	frappe.db.rollback()
	pre = frappe.db.get_value(
		"Frappe Site",
		site_docname,
		["status", "cluster", "namespace"],
		as_dict=True,
	)
	if not pre or pre.get("status") != "Active":
		result["detail"] = (
			f"Frappe Site '{site_docname}' must start from Active; "
			f"current status: '{pre.get('status') if pre else None}'."
		)
		_emit(result)
		return 2
	cluster = pre["cluster"]
	namespace = pre["namespace"] or "default"
	_step("pre_status", status=pre["status"])

	doc = frappe.get_doc("Frappe Site", site_docname)
	doc.migrate_site()
	frappe.db.commit()
	_step("migrate_enqueued")

	def _job_recorded() -> bool:
		frappe.db.rollback()
		row = frappe.db.get_value(
			"Frappe Site",
			site_docname,
			["status", "operation_job_name"],
			as_dict=True,
		)
		return (row or {}).get("status") == "Migrating" and bool((row or {}).get("operation_job_name"))

	if not _wait_until(_job_recorded, args.migrate_job_poll_seconds, interval_s=0.5):
		result["detail"] = (
			f"Operation Job name not recorded within {args.migrate_job_poll_seconds}s — "
			"the worker may not have created the Job."
		)
		_emit(result)
		return 1
	frappe.db.rollback()
	job_name = frappe.db.get_value("Frappe Site", site_docname, "operation_job_name")
	_step("job_recorded", job_name=job_name)

	api_client = get_k8s_api_client(cluster)
	batch = k8s_client.BatchV1Api(api_client=api_client)

	# Force-delete the Job so the reconciler must fall into the probe branch.
	try:
		batch.delete_namespaced_job(
			name=job_name,
			namespace=namespace,
			body=k8s_client.V1DeleteOptions(
				propagation_policy="Background",
				grace_period_seconds=0,
			),
		)
	except ApiException as exc:
		result["detail"] = f"Failed to delete Job '{job_name}': {exc}"
		_emit(result)
		return 1
	injected_at = _utc_iso()
	t_inject = time.monotonic()
	result["injected_at"] = injected_at
	_step("job_force_deleted")

	def _job_gone() -> bool:
		try:
			batch.read_namespaced_job(name=job_name, namespace=namespace)
			return False
		except ApiException as exc:
			return exc.status == 404

	if not _wait_until(_job_gone, timeout_s=30.0, interval_s=0.5):
		result["detail"] = "Operation Job did not 404 within 30s after delete."
		_emit(result)
		return 1
	_step("job_404_confirmed")

	# Inject the exec timeout: replace _exec_list_sites with a stub that
	# raises a timeout-like exception. The reconciler's _probe_site_state
	# catches Exception and returns SITE_PROBE_UNKNOWN.
	original_exec_list = discovery_mod._exec_list_sites

	def _timing_out_exec(**kwargs):
		from urllib3.exceptions import ReadTimeoutError

		raise ReadTimeoutError(
			pool=None,
			url="https://injected/exec",
			message="simulated pod-exec timeout (eval/faults injection)",
		)

	discovery_mod._exec_list_sites = _timing_out_exec
	_step("exec_patched_to_timeout")

	deferred_ticks = 0
	try:
		# Tick 1: probe must return unknown → reconciler defers, status unchanged.
		reconcile_mod._reconcile_frappe_sites()
		frappe.db.commit()
		frappe.db.rollback()
		status_after_tick1 = frappe.db.get_value("Frappe Site", site_docname, "status")
		_step("tick_1_under_timeout", status=status_after_tick1)
		if status_after_tick1 == "Migrating":
			deferred_ticks += 1
		else:
			result["detail"] = (
				f"Reconciler did not defer under exec timeout — row moved to "
				f"'{status_after_tick1}' on the first tick (expected 'Migrating')."
			)
			result["observed"] = {"tick_1_status": status_after_tick1, "deferred_ticks": 0}
			_emit(result)
			return 1
	finally:
		# Always restore the original to avoid wedging subsequent reconciles.
		discovery_mod._exec_list_sites = original_exec_list
	_step("exec_restored")

	# Tick 2: with exec restored, the probe sees the bench, finds the site,
	# and the reconciler transitions the row to its terminal state.
	reconcile_mod._reconcile_frappe_sites()
	frappe.db.commit()

	def _terminal() -> bool:
		frappe.db.rollback()
		st = frappe.db.get_value("Frappe Site", site_docname, "status")
		return st in ("Active", "Failed")

	if not _wait_until(_terminal, args.recovery_poll_seconds, interval_s=2.0):
		result["detail"] = (
			f"Site row did not reach a terminal state on tick 2 within "
			f"{args.recovery_poll_seconds}s after exec was restored."
		)
		_emit(result)
		return 1
	recovered_at = _utc_iso()
	t_recover = time.monotonic()
	mttr_seconds = round(t_recover - t_inject, 3)
	frappe.db.rollback()
	final = frappe.db.get_value(
		"Frappe Site",
		site_docname,
		["status", "status_detail"],
		as_dict=True,
	)
	_step(
		"recovered",
		status=final["status"],
		status_detail=(final.get("status_detail") or "")[:240],
		deferred_ticks=deferred_ticks,
	)

	result["recovered_at"] = recovered_at
	result["mttr_seconds"] = mttr_seconds
	result["observed"] = {
		"final_status": final["status"],
		"final_status_detail": (final.get("status_detail") or "")[:240],
		"deferred_ticks": deferred_ticks,
	}

	passed = final["status"] == "Active" and deferred_ticks >= 1 and mttr_seconds <= args.mttr_bound_seconds
	result["passed"] = passed
	if not passed:
		if final["status"] != "Active":
			result["detail"] = f"Recovered to '{final['status']}' (expected 'Active')."
		elif deferred_ticks < 1:
			result["detail"] = "Reconciler did not defer at least one tick under exec timeout."
		else:
			result["detail"] = (
				f"Time-to-terminal {mttr_seconds:.1f}s exceeded bound {args.mttr_bound_seconds}s."
			)
	else:
		result["detail"] = (
			f"Probe deferred for {deferred_ticks} tick under exec timeout; "
			f"row recovered to '{final['status']}' once exec was restored "
			f"(MTTR {mttr_seconds:.1f}s, fast-forwarded)."
		)

	_emit(result)
	return 0 if passed else 1


def _emit(result: dict) -> None:
	print("RESULT_BEGIN")
	print(json.dumps(result, indent=2, sort_keys=True))
	print("RESULT_END")


if __name__ == "__main__":
	sys.exit(main())
