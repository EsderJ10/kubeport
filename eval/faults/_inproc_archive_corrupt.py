"""In-container scenario: ``corrupt_archive_size_sidecar``.

Triggers ``bench backup`` against an Active Frappe Site, waits for the
backup Job to succeed (so the archive plus its ``<archive>.size`` and
``<archive>.name`` sidecars are flushed to the
``kubeport-backups`` PVC), then submits a short-lived busybox Job that
truncates the ``.size`` sidecar to zero bytes — simulating a half-write
or a sidecar corruption that the bench backup script's atomic-write
guard did not catch.

Reconciliation then runs against the still-In-Progress backup row.
``_probe_backup_archive_on_pvc`` reads the sidecar via its own probe
Job, sees an empty payload, and returns ``SITE_PROBE_MISSING``.  The
row must be marked ``Failed`` (not flipped to ``Available`` by
trusting the Job logs) and, on row delete, the
``delete_backup_archive_task`` must enqueue an archive-delete Job and
remove the archive plus sidecars from the PVC.

Acceptance:
- Row reaches ``Failed`` after corruption + reconcile.
- After ``frappe.delete_doc`` of the backup row, the archive file is
  no longer present on the PVC (verified by a busybox check Job).

Emits a single JSON object on stdout between ``RESULT_BEGIN`` /
``RESULT_END`` markers; consumed by ``eval/faults/run.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
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


def _submit_pvc_one_shot(
	api_client,
	namespace: str,
	command: str,
	label: str,
	timeout_s: int = 60,
) -> tuple[bool, str]:
	"""Submit a busybox Job that mounts ``kubeport-backups`` and runs
	``command``.  Wait for terminal state, return ``(succeeded, log)``.

	Used both for the corruption injection and for the post-cleanup
	verification, which need to read/write files on the PVC and have no
	host-side path into it.
	"""
	from kubernetes import client as k8s_client
	from kubernetes.client.rest import ApiException

	from kubeport.tasks.site_tasks import (
		BACKUP_MOUNT_PATH,
		BACKUP_PVC_NAME,
		MANAGED_BY_LABEL,
		MANAGED_BY_VALUE,
		OPERATION_LABEL,
	)

	job_name = f"ks-eval-{label}-{secrets.token_hex(3)}"
	manifest = {
		"apiVersion": "batch/v1",
		"kind": "Job",
		"metadata": {
			"name": job_name,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
				OPERATION_LABEL: f"eval-{label}",
			},
		},
		"spec": {
			"backoffLimit": 0,
			"ttlSecondsAfterFinished": 60,
			"activeDeadlineSeconds": timeout_s,
			"template": {
				"metadata": {
					"labels": {
						MANAGED_BY_LABEL: MANAGED_BY_VALUE,
						OPERATION_LABEL: f"eval-{label}",
					},
				},
				"spec": {
					"restartPolicy": "Never",
					"containers": [
						{
							"name": "eval",
							"image": "busybox:1.36",
							"command": ["sh", "-c", command],
							"volumeMounts": [{"name": "kubeport-backups", "mountPath": BACKUP_MOUNT_PATH}],
						}
					],
					"volumes": [
						{
							"name": "kubeport-backups",
							"persistentVolumeClaim": {"claimName": BACKUP_PVC_NAME},
						}
					],
				},
			},
		},
	}

	from kubeport.utils.k8s_resources import apply_resource

	apply_resource(api_client, manifest, namespace)
	batch_v1 = k8s_client.BatchV1Api(api_client=api_client)
	core_v1 = k8s_client.CoreV1Api(api_client=api_client)

	deadline = time.monotonic() + timeout_s + 5
	succeeded = False
	failed = False
	while time.monotonic() < deadline:
		try:
			j = batch_v1.read_namespaced_job(name=job_name, namespace=namespace, _request_timeout=5)
			s = (j.status.succeeded or 0) if j.status else 0
			f = (j.status.failed or 0) if j.status else 0
			if s > 0:
				succeeded = True
				break
			if f > 0:
				failed = True
				break
		except ApiException:
			pass
		time.sleep(1.0)

	logs = ""
	try:
		pods = core_v1.list_namespaced_pod(
			namespace=namespace, label_selector=f"job-name={job_name}", _request_timeout=5
		)
		for p in pods.items or []:
			if not p.metadata or not p.metadata.name:
				continue
			try:
				logs = (
					core_v1.read_namespaced_pod_log(
						name=p.metadata.name, namespace=namespace, _request_timeout=5
					)
					or ""
				)
				if logs:
					break
			except Exception:
				continue
	except Exception:
		pass

	try:
		batch_v1.delete_namespaced_job(
			name=job_name,
			namespace=namespace,
			propagation_policy="Background",
			_request_timeout=5,
		)
	except Exception:
		pass

	return (succeeded and not failed, logs)


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", default="/workspace/development/bench-16")
	parser.add_argument("--site-doc-name", required=True)
	parser.add_argument("--mttr-bound-seconds", type=int, default=600)
	parser.add_argument("--backup-job-poll-seconds", type=int, default=600)
	parser.add_argument("--cleanup-poll-seconds", type=int, default=180)
	# Accepted for harness uniformity even though this scenario uses
	# scenario-specific timeouts (backup-job, cleanup) instead.
	parser.add_argument("--recovery-poll-seconds", type=int, default=0)
	parser.add_argument("--fast-forward", action="store_true")
	args = parser.parse_args()

	_bench_setup(args.site, args.bench_path)
	import frappe
	from kubernetes import client as k8s_client

	from kubeport.tasks import reconciliation as reconcile_mod
	from kubeport.utils.k8s_client import get_k8s_api_client

	scenario_name = "corrupt_archive_size_sidecar"
	steps: list[dict] = []
	result = {
		"scenario": scenario_name,
		"defended_invariant": (
			"PVC sidecar probe marks Failed when the archive size sidecar is "
			"truncated/missing; row delete enqueues archive trash cleanup that "
			"removes the archive from the PVC"
		),
		"witness": {
			"reconciler": "kubeport/tasks/reconciliation.py:_reconcile_site_backup",
			"probe": "kubeport/tasks/reconciliation.py:_probe_backup_archive_on_pvc",
			"cleanup": "kubeport/tasks/site_tasks.py:delete_backup_archive_task",
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
	_step("pre_status", status=pre["status"], cluster=cluster, namespace=namespace)

	doc = frappe.get_doc("Frappe Site", site_docname)
	backup_result = doc.backup_site()
	frappe.db.commit()
	backup_docname = backup_result["backup_docname"]
	_step("backup_enqueued", backup_docname=backup_docname)

	def _backup_job_recorded() -> bool:
		frappe.db.rollback()
		row = frappe.db.get_value(
			"Frappe Site Backup",
			backup_docname,
			["status", "operation_job_name", "storage_path"],
			as_dict=True,
		)
		return (
			(row or {}).get("status") == "In Progress"
			and bool((row or {}).get("operation_job_name"))
			and bool((row or {}).get("storage_path"))
		)

	if not _wait_until(_backup_job_recorded, args.backup_job_poll_seconds, interval_s=1.0):
		result["detail"] = (
			f"Backup operation_job_name / storage_path not recorded within "
			f"{args.backup_job_poll_seconds}s — bench backup may not have started."
		)
		_emit(result)
		return 1

	frappe.db.rollback()
	op_row = frappe.db.get_value(
		"Frappe Site Backup",
		backup_docname,
		["operation_job_name", "storage_path"],
		as_dict=True,
	)
	op_job_name = op_row["operation_job_name"]
	storage_path = op_row["storage_path"]
	_step("backup_job_recorded", job_name=op_job_name, storage_path=storage_path)

	# Wait for the bench backup Job to succeed so the .size sidecar is on
	# the PVC. Until then there is nothing to corrupt.
	api_client = get_k8s_api_client(cluster)
	batch_v1 = k8s_client.BatchV1Api(api_client=api_client)

	def _backup_job_succeeded() -> bool:
		try:
			j = batch_v1.read_namespaced_job(name=op_job_name, namespace=namespace, _request_timeout=5)
			return bool(j.status and (j.status.succeeded or 0) > 0)
		except Exception:
			return False

	if not _wait_until(_backup_job_succeeded, args.backup_job_poll_seconds, interval_s=2.0):
		result["detail"] = f"bench backup Job did not succeed within {args.backup_job_poll_seconds}s."
		_emit(result)
		return 1
	_step("backup_job_succeeded")

	# Truncate the .size sidecar to zero bytes via a busybox Job. Use ``:`` to
	# overwrite-truncate without invoking ``truncate(1)`` (busybox has it but
	# the redirect form is portable across all images we might use).
	corrupt_cmd = (
		f': > "{storage_path}.size" && '
		f'echo SIZE_AFTER=$(stat -c %s "{storage_path}.size" 2>/dev/null || echo missing)'
	)
	ok, corrupt_log = _submit_pvc_one_shot(
		api_client=api_client,
		namespace=namespace,
		command=corrupt_cmd,
		label="corrupt-sidecar",
	)
	if not ok:
		result["detail"] = f"Sidecar truncation Job did not succeed. Log: {corrupt_log[:200]}"
		_emit(result)
		return 1
	injected_at = _utc_iso()
	t_inject = time.monotonic()
	result["injected_at"] = injected_at
	_step("size_sidecar_truncated", log=corrupt_log.strip()[:200])

	# Trigger backup reconciliation. Without the corruption, this would
	# flip the row to Available with the Job-reported size_bytes; with the
	# corruption the post-success PVC verification probe should return
	# MISSING and the reconciler must mark Failed.
	if args.fast_forward:
		try:
			reconcile_mod.reconcile_site_backups()
			frappe.db.commit()
		except Exception as exc:
			import traceback as _tb

			result["detail"] = f"Direct reconciler invocation failed: {type(exc).__name__}: {exc}"
			result["observed"]["reconciler_traceback"] = _tb.format_exc()
			_emit(result)
			return 1
		_step("reconciler_invoked")

	def _backup_terminal() -> bool:
		frappe.db.rollback()
		st = frappe.db.get_value("Frappe Site Backup", backup_docname, "status")
		return st in ("Available", "Failed")

	if not _wait_until(_backup_terminal, args.cleanup_poll_seconds, interval_s=2.0):
		result["detail"] = f"Backup row did not reach a terminal state within {args.cleanup_poll_seconds}s."
		_emit(result)
		return 1
	frappe.db.rollback()
	post_recon = frappe.db.get_value(
		"Frappe Site Backup",
		backup_docname,
		["status", "status_detail", "size_bytes"],
		as_dict=True,
	)
	_step(
		"reconcile_terminal",
		status=post_recon["status"],
		status_detail=(post_recon.get("status_detail") or "")[:240],
		size_bytes=post_recon.get("size_bytes"),
	)
	if post_recon["status"] != "Failed":
		result["detail"] = (
			f"After sidecar corruption the row reached '{post_recon['status']}' (expected 'Failed'). "
			"The PVC verification probe did not catch the corruption."
		)
		result["observed"] = {
			"final_status": post_recon["status"],
			"final_status_detail": (post_recon.get("status_detail") or "")[:240],
		}
		_emit(result)
		return 1

	# Now exercise the trash cleanup: deleting the row enqueues
	# delete_backup_archive_task, which submits a Job that ``rm -f``s the
	# archive plus its sidecars from the PVC.
	frappe.delete_doc("Frappe Site Backup", backup_docname, ignore_permissions=True)
	frappe.db.commit()
	_step("backup_row_deleted")

	# Verify the archive is gone on the PVC. The cleanup Job runs on the
	# bench's long worker; wait for the underlying file to disappear.
	check_cmd = (
		f'if [ -f "{storage_path}" ]; then echo PRESENT; else echo ABSENT; fi; '
		f'if [ -f "{storage_path}.size" ]; then echo SIZE_PRESENT; else echo SIZE_ABSENT; fi'
	)

	archive_absent = False
	cleanup_deadline = time.monotonic() + args.cleanup_poll_seconds
	last_log = ""
	while time.monotonic() < cleanup_deadline:
		ok, log = _submit_pvc_one_shot(
			api_client=api_client,
			namespace=namespace,
			command=check_cmd,
			label="check-cleanup",
		)
		last_log = log
		if ok and "ABSENT" in log:
			archive_absent = True
			break
		time.sleep(5.0)

	recovered_at = _utc_iso()
	t_recover = time.monotonic()
	mttr_seconds = round(t_recover - t_inject, 3)
	_step("cleanup_check", archive_absent=archive_absent, log=last_log.strip()[:240])

	result["recovered_at"] = recovered_at
	result["mttr_seconds"] = mttr_seconds
	result["observed"] = {
		"final_status": "Failed",
		"final_status_detail": (post_recon.get("status_detail") or "")[:240],
		"archive_absent_after_cleanup": archive_absent,
		"cleanup_check_log": last_log.strip()[:240],
	}

	passed = archive_absent and mttr_seconds <= args.mttr_bound_seconds
	result["passed"] = passed
	if not passed:
		if not archive_absent:
			result["detail"] = (
				f"Archive trash cleanup did not remove the file from the PVC within "
				f"{args.cleanup_poll_seconds}s. Last check: {last_log.strip()[:120]}"
			)
		else:
			result["detail"] = (
				f"Time-to-cleanup {mttr_seconds:.1f}s exceeded bound {args.mttr_bound_seconds}s."
			)
	else:
		result["detail"] = (
			f"Sidecar corruption marked row Failed; trash cleanup removed the archive "
			f"from the PVC in {mttr_seconds:.1f}s "
			f"({'fast-forwarded reconcile' if args.fast_forward else 'real-time reconcile'})."
		)

	_emit(result)
	return 0 if passed else 1


def _emit(result: dict) -> None:
	print("RESULT_BEGIN")
	print(json.dumps(result, indent=2, sort_keys=True))
	print("RESULT_END")


if __name__ == "__main__":
	sys.exit(main())
