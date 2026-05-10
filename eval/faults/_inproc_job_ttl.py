"""In-container scenario: ``job_ttl_expired_before_reconcile``.

Triggers ``bench migrate`` against an Active Frappe Site (a no-op
migration on an already-up-to-date site finishes in seconds), then
force-deletes the operation Job from Kubernetes before reconciliation
has read its status.  This simulates the K8s ``ttlSecondsAfterFinished``
cleanup firing on a Job whose final state was never sampled.

The reconciler must fall back to ``_probe_site_state`` (pod-exec
``bench list-apps`` against the bench pod) instead of marking the row
``Failed`` by default.  A site that exists on the bench should reach
``Active``; only a genuinely missing site should reach ``Failed``.
This is the "ground-truth" defence in
``docs/control-plane-state.md`` §Robustness Properties.

By default the harness invokes ``_reconcile_frappe_sites`` directly
after the deletion so the recovery path is exercised in seconds rather
than waiting up to 5 minutes for the next cron tick.  Pass
``--no-fast-forward`` to wait for the natural reconciliation cycle.

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
	from kubeport.utils.k8s_client import get_k8s_api_client

	scenario_name = "job_ttl_expired_before_reconcile"
	steps: list[dict] = []
	result = {
		"scenario": scenario_name,
		"defended_invariant": (
			"Reconciliation falls back to ground-truth bench probe when the "
			"operation Job has been removed before the tick reads it"
		),
		"witness": {
			"reconciler": "kubeport/tasks/reconciliation.py:_reconcile_site_migrate",
			"probe": "kubeport/tasks/reconciliation.py:_probe_site_state",
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
		["status", "cluster", "namespace", "operation_token"],
		as_dict=True,
	)
	if not pre or pre.get("status") != "Active":
		result["detail"] = (
			f"Frappe Site '{site_docname}' must start from Active "
			f"to safely run a no-op migrate; current status: '{pre.get('status') if pre else None}'."
		)
		_emit(result)
		return 2
	cluster = pre["cluster"]
	namespace = pre["namespace"] or "default"
	_step("pre_status", status=pre["status"], cluster=cluster, namespace=namespace)

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
	op = frappe.db.get_value(
		"Frappe Site",
		site_docname,
		["operation_job_name", "operation_job_token"],
		as_dict=True,
	)
	job_name = op["operation_job_name"]
	_step("job_recorded", job_name=job_name, operation_job_token=op["operation_job_token"])

	api_client = get_k8s_api_client(cluster)
	batch = k8s_client.BatchV1Api(api_client=api_client)

	try:
		batch.read_namespaced_job(name=job_name, namespace=namespace)
	except ApiException as exc:
		if exc.status == 404:
			result["detail"] = (
				f"Operation Job '{job_name}' was already gone before injection — "
				"cluster TTL fired earlier than expected."
			)
		else:
			result["detail"] = f"Could not read Job before injection: {exc}"
		_emit(result)
		return 1
	_step("job_visible_in_cluster")

	# Inject: force-delete the Job (cascade to Pods). This simulates the
	# operation Job being garbage-collected by ``ttlSecondsAfterFinished``
	# before the next reconciliation tick reads it.
	try:
		# Background propagation: the Job is removed from the API server
		# immediately, dependent Pods are reaped async by the GC. This
		# matches the steady-state semantics of ``ttlSecondsAfterFinished``
		# clean-up — Job gone first, Pods catch up.
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
	_step("job_force_deleted", job_name=job_name)

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

	if args.fast_forward:
		try:
			reconcile_mod._reconcile_frappe_sites()
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
		st = frappe.db.get_value("Frappe Site", site_docname, "status")
		return st in ("Active", "Failed")

	if not _wait_until(_terminal, args.recovery_poll_seconds, interval_s=2.0):
		result["detail"] = (
			f"Site row did not reach a terminal state within {args.recovery_poll_seconds}s "
			"after Job deletion."
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
		["status", "status_detail", "operation_job_name"],
		as_dict=True,
	)
	_step(
		"recovered",
		status=final["status"],
		status_detail=(final.get("status_detail") or "")[:240],
		operation_job_name=final.get("operation_job_name") or "",
	)

	result["recovered_at"] = recovered_at
	result["mttr_seconds"] = mttr_seconds
	result["observed"] = {
		"final_status": final["status"],
		"final_status_detail": (final.get("status_detail") or "")[:240],
	}

	# Acceptance: site row must reach Active (the bench probe should have
	# found the site present), within the documented bound. A Failed
	# terminal would mean the reconciler defaulted to Failed without
	# trusting the probe — that is exactly the bug this scenario guards.
	passed = final["status"] == "Active" and mttr_seconds <= args.mttr_bound_seconds
	result["passed"] = passed
	if not passed:
		if final["status"] != "Active":
			result["detail"] = (
				f"Recovered to '{final['status']}' (expected 'Active'). "
				"Reconciler may have defaulted to Failed instead of trusting the bench probe."
			)
		else:
			result["detail"] = (
				f"Time-to-terminal {mttr_seconds:.1f}s exceeded bound {args.mttr_bound_seconds}s."
			)
	else:
		result["detail"] = (
			f"Bench probe restored row to '{final['status']}' "
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
