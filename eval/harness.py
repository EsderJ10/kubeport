"""Host-side driver for the Kubeport evaluation harness.

Validates that the dev container is up, optionally reads a kubeconfig for
the target k3d cluster, copies ``_inproc.py`` into the container, and runs
it inside the bench environment.  The in-container script emits a JSON
report between ``RESULT_BEGIN`` / ``RESULT_END`` markers; this driver
extracts that block and writes ``eval/results/<utc-timestamp>.json``.

Idempotent: each invocation produces a new timestamped report.  Existing
DocType rows are reused if their names match; pass distinct
``--release-name`` / ``--site-name`` to force a fresh end-to-end run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
INPROC = EVAL_DIR / "_inproc.py"

DEFAULT_CONTAINER = "tfg_devcontainer-frappe-1"
DEFAULT_BENCH_PATH = "/workspace/development/bench-16"
DEFAULT_BENCH_SITE = "frappe-k8s.localhost"


def _now_tag() -> str:
	return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _docker_running(container: str) -> bool:
	r = subprocess.run(
		["docker", "inspect", "-f", "{{.State.Running}}", container],
		capture_output=True,
		text=True,
	)
	return r.returncode == 0 and r.stdout.strip() == "true"


def _bench_workers_running(container: str) -> dict:
	"""Return ``{"long": bool, "scheduler": bool}`` for the running workers."""
	probes = {
		"long": r"frappe[ ]worker.*--queue.*long",
		"scheduler": r"frappe[ ]schedule(\s|$)",
	}
	out = {}
	for label, pattern in probes.items():
		r = subprocess.run(
			["docker", "exec", container, "bash", "-lc", f"pgrep -fa '{pattern}' || true"],
			capture_output=True,
			text=True,
		)
		out[label] = bool(r.stdout.strip())
	return out


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
	summary = report["summary"]
	total_s = report["duration_seconds"]
	print(
		f"\n=== Eval summary: passed {summary['passed']} / {summary['total']} "
		f"(failed {summary['failed']}, skipped {summary['skipped']}) "
		f"in {total_s:.1f}s ===",
		file=sys.stderr,
	)
	for phase in report["phases"]:
		marker = {
			"passed": "[OK]",
			"failed": "[FAIL]",
			"skipped": "[SKIP]",
			"pending": "[--]",
		}.get(phase["status"], "[??]")
		print(
			f"  {marker:<7} {phase['phase']:<20} {phase['duration_seconds']:>7.1f}s  {phase['detail']}",
			file=sys.stderr,
		)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--container", default=DEFAULT_CONTAINER)
	parser.add_argument("--bench-path", default=DEFAULT_BENCH_PATH)
	parser.add_argument("--bench-site", default=DEFAULT_BENCH_SITE)
	parser.add_argument("--cluster-doc-name", default="eval-k3d")
	parser.add_argument("--kubeconfig-path", default="", help="Path to a kubeconfig file on the host")
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument(
		"--k3d-cluster",
		default="",
		help="Name of an existing k3d cluster to read a kubeconfig from on the host",
	)
	parser.add_argument("--helm-repo-name", default="frappe")
	parser.add_argument("--helm-repo-url", default="https://helm.erpnext.com")
	parser.add_argument("--chart-name", default="erpnext")
	parser.add_argument("--chart-version", default="")
	parser.add_argument("--release-name", default="")
	parser.add_argument("--namespace", default="")
	parser.add_argument("--site-image", default="")
	parser.add_argument("--release-values-file", default="")
	parser.add_argument("--site-name", default="")
	parser.add_argument("--admin-password", default="")
	parser.add_argument("--db-root-password", default="")
	parser.add_argument("--install-apps", default="erpnext")
	parser.add_argument(
		"--output",
		default="",
		help="Override the output JSON path (defaults to eval/results/<utc>.json)",
	)
	parser.add_argument(
		"--require-workers",
		action="store_true",
		default=True,
		help="Fail fast if the bench long-queue worker or scheduler is not running (default: on)",
	)
	parser.add_argument(
		"--no-require-workers",
		action="store_false",
		dest="require_workers",
		help="Skip the worker/scheduler precondition check",
	)
	args = parser.parse_args()

	if not _docker_running(args.container):
		print(
			f"Dev container '{args.container}' is not running. Start it first.",
			file=sys.stderr,
		)
		return 2

	if args.require_workers:
		workers = _bench_workers_running(args.container)
		missing = [name for name, up in workers.items() if not up]
		if missing:
			print(
				"Bench workers not running inside the dev container: "
				f"{', '.join(missing)}.\n"
				"Start them in the background, e.g.:\n"
				f"  docker exec -d {args.container} bash -lc "
				f"'cd {DEFAULT_BENCH_PATH} && bench worker --queue long &> /tmp/worker-long.log'\n"
				f"  docker exec -d {args.container} bash -lc "
				f"'cd {DEFAULT_BENCH_PATH} && bench schedule &> /tmp/scheduler.log'\n"
				"Or pass --no-require-workers to skip this check.",
				file=sys.stderr,
			)
			return 2

	tag = _now_tag()
	if not args.release_name:
		args.release_name = f"eval-{tag.lower()}"
	if not args.namespace:
		args.namespace = f"eval-{tag.lower()}"
	if not args.site_name:
		args.site_name = f"eval-{tag.lower()}.localhost"

	kubeconfig_text, kubeconfig_context = _read_kubeconfig(args)
	if not kubeconfig_text:
		print(
			"No kubeconfig source supplied: pass --kubeconfig-path or --k3d-cluster.",
			file=sys.stderr,
		)
		return 2

	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	output_path = Path(args.output) if args.output else (RESULTS_DIR / f"{tag}.json")

	subprocess.run(
		["docker", "cp", str(INPROC), f"{args.container}:/tmp/_inproc.py"],
		check=True,
	)

	with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig", delete=False) as kc_tmp:
		kc_tmp.write(kubeconfig_text)
		kubeconfig_host_path = kc_tmp.name
	kubeconfig_remote_path = "/tmp/_eval_kubeconfig"
	try:
		subprocess.run(
			["docker", "cp", kubeconfig_host_path, f"{args.container}:{kubeconfig_remote_path}"],
			check=True,
		)
	finally:
		Path(kubeconfig_host_path).unlink(missing_ok=True)

	values_remote_path = ""
	if args.release_values_file:
		src = Path(args.release_values_file)
		if not src.exists():
			print(f"Values file not found: {src}", file=sys.stderr)
			return 2
		values_remote_path = "/tmp/_eval_values.yaml"
		subprocess.run(
			["docker", "cp", str(src), f"{args.container}:{values_remote_path}"],
			check=True,
		)

	cmd = [
		"docker",
		"exec",
		args.container,
		f"{args.bench_path}/env/bin/python",
		"/tmp/_inproc.py",
		"--site",
		args.bench_site,
		"--bench-path",
		args.bench_path,
		"--cluster-doc-name",
		args.cluster_doc_name,
		"--kubeconfig-file",
		kubeconfig_remote_path,
		"--kubeconfig-context",
		kubeconfig_context,
		"--helm-repo-name",
		args.helm_repo_name,
		"--helm-repo-url",
		args.helm_repo_url,
		"--chart-name",
		args.chart_name,
		"--chart-version",
		args.chart_version,
		"--release-name",
		args.release_name,
		"--namespace",
		args.namespace,
		"--site-image",
		args.site_image,
		"--release-values-file",
		values_remote_path,
		"--site-name",
		args.site_name,
		"--admin-password",
		args.admin_password,
		"--db-root-password",
		args.db_root_password,
		"--install-apps",
		args.install_apps,
	]

	t0 = time.monotonic()
	r = subprocess.run(cmd, capture_output=True, text=True)
	wall = time.monotonic() - t0

	stdout = r.stdout
	stderr = r.stderr
	if stderr:
		print(stderr, file=sys.stderr, end="")

	try:
		report = _extract_report(stdout)
	except Exception as exc:
		print(f"Failed to parse report: {exc}", file=sys.stderr)
		print("--- raw stdout ---", file=sys.stderr)
		print(stdout, file=sys.stderr)
		return 3

	report["host_wall_seconds"] = round(wall, 3)
	report["invocation"] = {
		"cluster_doc_name": args.cluster_doc_name,
		"helm_repo_name": args.helm_repo_name,
		"helm_repo_url": args.helm_repo_url,
		"chart_name": args.chart_name,
		"chart_version": args.chart_version,
		"release_name": args.release_name,
		"namespace": args.namespace,
		"site_image": args.site_image,
		"site_name": args.site_name,
		"install_apps": args.install_apps,
		"bench_site": args.bench_site,
	}

	output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
	print(f"\nReport written to {output_path}", file=sys.stderr)
	_print_summary(report)
	return r.returncode


if __name__ == "__main__":
	sys.exit(main())
