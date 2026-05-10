"""Host-side driver for the Kubeport baseline-comparison harness.

Mirrors ``eval/harness.py`` but invokes the bash script
``eval/baseline/run.sh`` inside the dev container.  The script drives
the same 10-phase golden path with raw ``kubectl`` / ``helm`` /
``kubectl exec ... -- bench`` (no Kubeport in the loop) and emits a
JSON report between ``RESULT_BEGIN`` / ``RESULT_END`` markers.

The kubeconfig fetched on the host points at ``0.0.0.0:<host-port>``
which is unreachable from inside the dev container; this driver
rewrites the host part to the container's default gateway IP before
shipping it across.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
SCRIPT = EVAL_DIR / "baseline" / "run.sh"

DEFAULT_CONTAINER = "tfg_devcontainer-frappe-1"


def _now_tag() -> str:
	return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _docker_running(container: str) -> bool:
	r = subprocess.run(
		["docker", "inspect", "-f", "{{.State.Running}}", container],
		capture_output=True,
		text=True,
	)
	return r.returncode == 0 and r.stdout.strip() == "true"


def _container_gateway_ip(container: str) -> str:
	# Parse /proc/net/route inside the container (same approach as
	# kubeport.api._get_default_gateway_ip): no dependency on `ip` or
	# `ifconfig`, which are missing from minimal images.
	r = subprocess.run(
		[
			"docker",
			"exec",
			container,
			"sh",
			"-c",
			"awk 'NR>1 && $2==\"00000000\" {print $3; exit}' /proc/net/route",
		],
		capture_output=True,
		text=True,
	)
	hex_le = r.stdout.strip()
	if not re.match(r"^[0-9A-Fa-f]{8}$", hex_le):
		raise RuntimeError(f"Could not read /proc/net/route gateway (got: {hex_le!r})")
	octets = [int(hex_le[i : i + 2], 16) for i in range(0, 8, 2)]
	gw = ".".join(str(o) for o in reversed(octets))
	if not re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
		raise RuntimeError(f"Could not derive gateway IP (got: {gw!r})")
	return gw


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


def _rewrite_localhost_servers(kubeconfig_text: str, replacement_host: str) -> str:
	# k3d kubeconfigs encode the API server as ``https://0.0.0.0:<port>``;
	# the dev container can't reach 0.0.0.0 (that's itself).  Substitute
	# the bridge gateway so kubectl/helm in the container hit the host's
	# published port.
	return re.sub(
		r"(https?://)(?:0\.0\.0\.0|127\.0\.0\.1|localhost)(:\d+)",
		lambda m: f"{m.group(1)}{replacement_host}{m.group(2)}",
		kubeconfig_text,
	)


def _extract_report(stdout: str) -> dict:
	begin = stdout.find("RESULT_BEGIN")
	end = stdout.find("RESULT_END")
	if begin == -1 or end == -1 or end < begin:
		raise RuntimeError("Could not find RESULT_BEGIN / RESULT_END markers in run.sh output")
	body = stdout[begin + len("RESULT_BEGIN") : end].strip()
	return json.loads(body)


def _print_summary(report: dict) -> None:
	summary = report["summary"]
	total_s = report["duration_seconds"]
	print(
		f"\n=== Baseline summary: passed {summary['passed']} / {summary['total']} "
		f"(failed {summary['failed']}, skipped {summary['skipped']}) "
		f"in {total_s:.1f}s — "
		f"{summary['commands_issued_total']} commands, "
		f"{summary['manual_steps_total']} manual steps ===",
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
			f"  {marker:<7} {phase['phase']:<20} "
			f"{phase['duration_seconds']:>7.1f}s  "
			f"cmds={phase['commands_issued']:>2}  "
			f"manual={phase['manual_steps']:>2}  "
			f"{phase['detail']}",
			file=sys.stderr,
		)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--container", default=DEFAULT_CONTAINER)
	parser.add_argument("--kubeconfig-path", default="")
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument("--k3d-cluster", default="")
	parser.add_argument("--helm-repo-name", default="frappe")
	parser.add_argument("--helm-repo-url", default="https://helm.erpnext.com")
	parser.add_argument("--chart-name", default="erpnext")
	parser.add_argument("--release-name", required=True)
	parser.add_argument("--namespace", required=True)
	parser.add_argument("--site-name", default="")
	parser.add_argument("--db-root-user", default="root")
	parser.add_argument("--db-root-password", required=True)
	parser.add_argument("--admin-password", default="")
	parser.add_argument("--install-apps", default="erpnext")
	parser.add_argument("--gunicorn-container", default="gunicorn")
	parser.add_argument(
		"--no-insecure-skip-tls-verify",
		action="store_false",
		dest="insecure_skip_tls_verify",
		default=True,
	)
	parser.add_argument(
		"--output",
		default="",
		help="Override the output JSON path (defaults to eval/results/baseline-<utc>.json)",
	)
	args = parser.parse_args()

	if not _docker_running(args.container):
		print(f"Dev container '{args.container}' is not running.", file=sys.stderr)
		return 2

	tag = _now_tag()
	if not args.site_name:
		args.site_name = f"eval-baseline-{tag.lower()}.localhost"

	kubeconfig_text, kubeconfig_context = _read_kubeconfig(args)
	if not kubeconfig_text:
		print(
			"No kubeconfig source: pass --kubeconfig-path or --k3d-cluster.",
			file=sys.stderr,
		)
		return 2

	gateway_ip = _container_gateway_ip(args.container)
	kubeconfig_text = _rewrite_localhost_servers(kubeconfig_text, gateway_ip)

	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	output_path = Path(args.output) if args.output else (RESULTS_DIR / f"baseline-{tag}.json")

	subprocess.run(
		["docker", "cp", str(SCRIPT), f"{args.container}:/tmp/_baseline_run.sh"],
		check=True,
	)
	subprocess.run(
		["docker", "exec", args.container, "chmod", "+x", "/tmp/_baseline_run.sh"],
		check=True,
	)

	with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig", delete=False) as kc_tmp:
		kc_tmp.write(kubeconfig_text)
		kubeconfig_host_path = kc_tmp.name
	kubeconfig_remote_path = "/tmp/_baseline_kubeconfig"
	try:
		subprocess.run(
			["docker", "cp", kubeconfig_host_path, f"{args.container}:{kubeconfig_remote_path}"],
			check=True,
		)
	finally:
		Path(kubeconfig_host_path).unlink(missing_ok=True)

	cmd = [
		"docker",
		"exec",
		args.container,
		"bash",
		"/tmp/_baseline_run.sh",
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
		"--release-name",
		args.release_name,
		"--namespace",
		args.namespace,
		"--site-name",
		args.site_name,
		"--db-root-user",
		args.db_root_user,
		"--db-root-password",
		args.db_root_password,
		"--admin-password",
		args.admin_password,
		"--install-apps",
		args.install_apps,
		"--gunicorn-container",
		args.gunicorn_container,
		"--insecure-skip-tls-verify",
		"true" if args.insecure_skip_tls_verify else "false",
	]

	t0 = time.monotonic()
	r = subprocess.run(cmd, capture_output=True, text=True)
	wall = time.monotonic() - t0

	if r.stderr:
		print(r.stderr, file=sys.stderr, end="")

	try:
		report = _extract_report(r.stdout)
	except Exception as exc:
		print(f"Failed to parse report: {exc}", file=sys.stderr)
		print("--- raw stdout ---", file=sys.stderr)
		print(r.stdout, file=sys.stderr)
		return 3

	report["host_wall_seconds"] = round(wall, 3)
	report["invocation"] = {
		"container": args.container,
		"helm_repo_name": args.helm_repo_name,
		"helm_repo_url": args.helm_repo_url,
		"chart_name": args.chart_name,
		"release_name": args.release_name,
		"namespace": args.namespace,
		"site_name": args.site_name,
		"install_apps": args.install_apps,
		"kubeconfig_context": kubeconfig_context,
		"gateway_ip": gateway_ip,
	}

	output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
	print(f"\nReport written to {output_path}", file=sys.stderr)
	_print_summary(report)
	return r.returncode


if __name__ == "__main__":
	sys.exit(main())
