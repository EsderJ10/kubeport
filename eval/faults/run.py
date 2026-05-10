"""Host-side driver for the Kubeport fault-injection evaluation harness.

Drives one or more fault scenarios against the bench site running inside
the dev container.  Each scenario validates a documented robustness
defence in ``docs/control-plane-state.md`` §Robustness Properties; this
driver is the empirical counterpart referenced from upcoming TODO-08
``docs/evaluation.md``.

Idempotent: each invocation produces a new timestamped report under
``eval/results/faults-<utc-timestamp>.json``.  Existing reports are
never overwritten.  After a scenario kills the long-queue worker, the
driver restarts it before exiting so the dev container is left in the
same state it found it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
FAULTS_DIR = EVAL_DIR / "faults"
RESULTS_DIR = EVAL_DIR / "results"

DEFAULT_CONTAINER = "tfg_devcontainer-frappe-1"
DEFAULT_BENCH_PATH = "/workspace/development/bench-16"
DEFAULT_BENCH_SITE = "frappe-k8s.localhost"

SCENARIO_INPROC: dict[str, str] = {
	"worker_kill_mid_helm_upgrade": "_inproc_worker_kill.py",
	"job_ttl_expired_before_reconcile": "_inproc_job_ttl.py",
	"pod_exec_timeout_during_site_probe": "_inproc_pod_exec_timeout.py",
	"corrupt_archive_size_sidecar": "_inproc_archive_corrupt.py",
}


def _now_tag() -> str:
	return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _docker_running(container: str) -> bool:
	r = subprocess.run(
		["docker", "inspect", "-f", "{{.State.Running}}", container],
		capture_output=True,
		text=True,
	)
	return r.returncode == 0 and r.stdout.strip() == "true"


def _long_worker_running(container: str) -> bool:
	r = subprocess.run(
		["docker", "exec", container, "bash", "-lc", "pgrep -fa 'frappe[ ]worker.*--queue.*long' || true"],
		capture_output=True,
		text=True,
	)
	return bool(r.stdout.strip())


def _scheduler_running(container: str) -> bool:
	r = subprocess.run(
		["docker", "exec", container, "bash", "-lc", "pgrep -fa 'frappe[ ]schedule(\\s|$)' || true"],
		capture_output=True,
		text=True,
	)
	return bool(r.stdout.strip())


def _start_long_worker(container: str, bench_path: str) -> None:
	subprocess.run(
		[
			"docker",
			"exec",
			"-d",
			container,
			"bash",
			"-lc",
			f"cd {bench_path} && bench worker --queue long >> /tmp/worker-long.log 2>&1",
		],
		check=False,
	)


def _wait_until(predicate, timeout_s: float, interval_s: float = 1.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(interval_s)
	return predicate()


def _read_kubeconfig(args: argparse.Namespace) -> tuple[str, str]:
	if args.kubeconfig_path:
		text = Path(args.kubeconfig_path).read_text(encoding="utf-8")
		return text, args.kubeconfig_context or ""
	if args.k3d_cluster:
		r = subprocess.run(
			["k3d", "kubeconfig", "get", args.k3d_cluster],
			capture_output=True,
			text=True,
			check=True,
		)
		return r.stdout, args.kubeconfig_context or f"k3d-{args.k3d_cluster}"
	return "", ""


def _extract_report(stdout: str) -> dict:
	begin = stdout.find("RESULT_BEGIN")
	end = stdout.find("RESULT_END")
	if begin == -1 or end == -1 or end < begin:
		raise RuntimeError("Could not find RESULT_BEGIN / RESULT_END markers in inproc output")
	body = stdout[begin + len("RESULT_BEGIN") : end].strip()
	return json.loads(body)


def _print_summary(report: dict) -> None:
	results = report["scenarios"]
	total = len(results)
	passed = sum(1 for s in results if s["passed"])
	print(
		f"\n=== Fault summary: passed {passed} / {total} (failed {total - passed}) ===",
		file=sys.stderr,
	)
	for s in results:
		marker = "[OK]" if s["passed"] else "[FAIL]"
		mttr = s.get("mttr_seconds")
		mttr_s = f"{mttr:.1f}s" if isinstance(mttr, (int, float)) else "n/a"
		print(
			f"  {marker:<7} {s['scenario']:<32} mttr={mttr_s:<8} {s.get('detail', '')}",
			file=sys.stderr,
		)


def _run_scenario(
	args: argparse.Namespace,
	scenario: str,
	kubeconfig_remote: str,
	kubeconfig_context: str,
) -> dict:
	inproc_basename = SCENARIO_INPROC.get(scenario)
	if not inproc_basename:
		return {
			"scenario": scenario,
			"passed": False,
			"detail": f"Unknown scenario '{scenario}'",
		}
	inproc_local = FAULTS_DIR / inproc_basename
	inproc_remote = f"/tmp/{inproc_basename}"
	subprocess.run(
		["docker", "cp", str(inproc_local), f"{args.container}:{inproc_remote}"],
		check=True,
	)

	common = [
		"docker",
		"exec",
		args.container,
		f"{args.bench_path}/env/bin/python",
		inproc_remote,
		"--site",
		args.bench_site,
		"--bench-path",
		args.bench_path,
		"--mttr-bound-seconds",
		str(args.mttr_bound_seconds),
		"--recovery-poll-seconds",
		str(args.recovery_poll_seconds),
	]

	if scenario == "worker_kill_mid_helm_upgrade":
		cmd = [
			*common,
			"--release-doc-name",
			args.release_doc_name,
			"--cluster-doc-name",
			args.cluster_doc_name,
			"--kubeconfig-file",
			kubeconfig_remote,
			"--kubeconfig-context",
			kubeconfig_context,
			"--in-progress-poll-seconds",
			str(args.in_progress_poll_seconds),
		]
	elif scenario in ("job_ttl_expired_before_reconcile", "pod_exec_timeout_during_site_probe"):
		cmd = [
			*common,
			"--site-doc-name",
			args.site_doc_name,
			"--migrate-job-poll-seconds",
			str(args.migrate_job_poll_seconds),
		]
	elif scenario == "corrupt_archive_size_sidecar":
		cmd = [
			*common,
			"--site-doc-name",
			args.site_doc_name,
			"--backup-job-poll-seconds",
			str(args.backup_job_poll_seconds),
			"--cleanup-poll-seconds",
			str(args.cleanup_poll_seconds),
		]
	else:
		return {
			"scenario": scenario,
			"passed": False,
			"detail": f"No dispatch wiring for scenario '{scenario}'",
		}

	if args.fast_forward:
		cmd.append("--fast-forward")

	r = subprocess.run(cmd, capture_output=True, text=True)
	if r.stderr:
		print(r.stderr, file=sys.stderr, end="")
	try:
		return _extract_report(r.stdout)
	except Exception as exc:
		return {
			"scenario": scenario,
			"passed": False,
			"detail": f"Failed to parse report: {exc}",
			"raw_stdout": r.stdout[-2000:],
		}


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--container", default=DEFAULT_CONTAINER)
	parser.add_argument("--bench-path", default=DEFAULT_BENCH_PATH)
	parser.add_argument("--bench-site", default=DEFAULT_BENCH_SITE)
	parser.add_argument(
		"--scenarios",
		default="worker_kill_mid_helm_upgrade",
		help="Comma-separated scenario names to run (default: scenario 1 only)",
	)
	parser.add_argument(
		"--release-doc-name",
		default="demo-k3d/demo/demo-bench",
		help="Helm Release docname to drive (must already be Deployed)",
	)
	parser.add_argument(
		"--site-doc-name",
		default="demo-k3d/demo/demo-bench/pacopepe.localhost",
		help="Frappe Site docname to drive (must already be Active)",
	)
	parser.add_argument("--cluster-doc-name", default="demo-k3d")
	parser.add_argument(
		"--k3d-cluster",
		default="",
		help="Reserved for future scenarios that need cluster access from the harness",
	)
	parser.add_argument("--kubeconfig-path", default="")
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument(
		"--mttr-bound-seconds",
		type=int,
		default=1800,
		help="Documented stale-op recovery window (default 30 min, matches STALE_OPERATION_THRESHOLD_MINUTES)",
	)
	parser.add_argument(
		"--in-progress-poll-seconds",
		type=int,
		default=120,
		help="Maximum time to wait for the row to flip to In Progress after deploy_release (default 120s)",
	)
	parser.add_argument(
		"--migrate-job-poll-seconds",
		type=int,
		default=120,
		help="Maximum time to wait for operation_job_name to be recorded after migrate_site (default 120s)",
	)
	parser.add_argument(
		"--backup-job-poll-seconds",
		type=int,
		default=600,
		help="Maximum time to wait for the bench backup Job to record + succeed (default 600s)",
	)
	parser.add_argument(
		"--cleanup-poll-seconds",
		type=int,
		default=180,
		help="Maximum time to wait for archive trash cleanup to remove the archive from the PVC (default 180s)",
	)
	parser.add_argument(
		"--recovery-poll-seconds",
		type=int,
		default=2400,
		help="Maximum time to wait for stale-op recovery (default 40 min — covers MTTR ceiling plus margin)",
	)
	parser.add_argument(
		"--fast-forward",
		action="store_true",
		help=(
			"Backdate operation_started_at past the staleness threshold and invoke "
			"the reconciler directly so the recovery code path is exercised in seconds, "
			"not minutes. Recorded as fast_forward_used=true in the report."
		),
	)
	parser.add_argument(
		"--no-restart-worker",
		action="store_true",
		help="Skip restarting the long-queue worker after the scenario.",
	)
	parser.add_argument(
		"--output",
		default="",
		help="Override the output JSON path (defaults to eval/results/faults-<utc>.json)",
	)
	args = parser.parse_args()

	if not _docker_running(args.container):
		print(f"Dev container '{args.container}' is not running.", file=sys.stderr)
		return 2
	if not _long_worker_running(args.container):
		print(
			"Long-queue worker is not running inside the dev container. "
			f"Start it with: docker exec -d {args.container} bash -lc "
			f"'cd {args.bench_path} && bench worker --queue long &> /tmp/worker-long.log'",
			file=sys.stderr,
		)
		return 2
	if not _scheduler_running(args.container):
		print(
			"Bench scheduler is not running inside the dev container. "
			f"Start it with: docker exec -d {args.container} bash -lc "
			f"'cd {args.bench_path} && bench schedule &> /tmp/scheduler.log'",
			file=sys.stderr,
		)
		return 2

	tag = _now_tag()
	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	output_path = Path(args.output) if args.output else (RESULTS_DIR / f"faults-{tag}.json")

	kubeconfig_text, kubeconfig_context = _read_kubeconfig(args)
	kubeconfig_remote = "/tmp/_eval_faults_kubeconfig"
	if kubeconfig_text:
		with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig", delete=False) as kc_tmp:
			kc_tmp.write(kubeconfig_text)
			kubeconfig_host_path = kc_tmp.name
		try:
			subprocess.run(
				["docker", "cp", kubeconfig_host_path, f"{args.container}:{kubeconfig_remote}"],
				check=True,
			)
		finally:
			Path(kubeconfig_host_path).unlink(missing_ok=True)
	else:
		kubeconfig_remote = ""

	scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
	overall_started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
	overall_t0 = time.monotonic()
	results: list[dict] = []
	for scenario in scenarios:
		result = _run_scenario(args, scenario, kubeconfig_remote, kubeconfig_context)
		results.append(result)
		# After every scenario, ensure the long-queue worker is back up.
		# scenario 1 kills it directly; later scenarios depend on it. Skip
		# the restart entirely if the operator opted out via flag.
		if not args.no_restart_worker and not _long_worker_running(args.container):
			_start_long_worker(args.container, args.bench_path)
			if not _wait_until(
				lambda: _long_worker_running(args.container),
				timeout_s=15.0,
				interval_s=1.0,
			):
				print(
					"Warning: long-queue worker did not come back up automatically. "
					"Restart it manually before re-running.",
					file=sys.stderr,
				)

	overall_finished_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
	report = {
		"schema_version": 1,
		"generated_at": overall_finished_at,
		"started_at": overall_started_at,
		"finished_at": overall_finished_at,
		"duration_seconds": round(time.monotonic() - overall_t0, 3),
		"context": {
			"container": args.container,
			"bench_site": args.bench_site,
			"release_doc_name": args.release_doc_name,
			"cluster_doc_name": args.cluster_doc_name,
			"fast_forward": args.fast_forward,
			"mttr_bound_seconds": args.mttr_bound_seconds,
		},
		"scenarios": results,
		"summary": {
			"total": len(results),
			"passed": sum(1 for s in results if s.get("passed")),
			"failed": sum(1 for s in results if not s.get("passed")),
		},
	}
	output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
	print(f"\nReport written to {output_path}", file=sys.stderr)
	_print_summary(report)
	return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
	sys.exit(main())
