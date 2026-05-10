"""Host-side driver for the Kubeport scaling harness.

Mirrors ``eval/harness.py`` (golden-path) and
``eval/baseline/host_driver.py`` (baseline) but targets the scaling
benchmark in ``eval/scaling/_inproc.py``.

Validates the dev container is up, copies the in-container scripts,
runs the benchmark, parses the JSON report between the
``RESULT_BEGIN`` / ``RESULT_END`` markers, and writes
``eval/results/scaling-<utc>.json``.

After the JSON is written it tries to invoke ``eval/scaling/plot.py``
on the host.  matplotlib is not preinstalled in the dev container or on
the host (per the project's no-local-installs rule); the plot step
degrades to a clear warning if matplotlib is unavailable so the JSON
report is always produced.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
SCALING_DIR = EVAL_DIR / "scaling"

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


def _extract_report(stdout: str) -> dict:
	begin = stdout.find("RESULT_BEGIN")
	end = stdout.find("RESULT_END")
	if begin == -1 or end == -1 or end < begin:
		raise RuntimeError("Could not find RESULT_BEGIN / RESULT_END markers in inproc output")
	body = stdout[begin + len("RESULT_BEGIN") : end].strip()
	return json.loads(body)


def _print_summary(report: dict) -> None:
	tick_rows = report.get("tick_latency", [])
	regression = report.get("regression", {})
	helm_summary = report.get("helm_subprocess_latency", {}).get("summary", {})
	print("\n=== Scaling tick latency ===", file=sys.stderr)
	print(f"  {'N':>6}  {'median(s)':>10}  {'mean(s)':>10}  {'max(s)':>10}", file=sys.stderr)
	for row in tick_rows:
		print(
			f"  {row['n']:>6}  "
			f"{row['median_seconds']:>10.4f}  "
			f"{row['mean_seconds']:>10.4f}  "
			f"{row['max_seconds']:>10.4f}",
			file=sys.stderr,
		)
	print(
		f"  Regression: shape={regression.get('shape')} slope={regression.get('slope')}",
		file=sys.stderr,
	)
	print(
		f"  Helm samples: count={helm_summary.get('sample_count')} "
		f"median={helm_summary.get('median_seconds')}s "
		f"p95={helm_summary.get('p95_seconds')}s",
		file=sys.stderr,
	)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--container", default=DEFAULT_CONTAINER)
	parser.add_argument("--bench-path", default=DEFAULT_BENCH_PATH)
	parser.add_argument("--bench-site", default=DEFAULT_BENCH_SITE)
	parser.add_argument(
		"--ns",
		default="1,10,100,1000",
		help="Comma-separated N values to characterise (default 1,10,100,1000)",
	)
	parser.add_argument("--repeats", type=int, default=5)
	parser.add_argument(
		"--output",
		default="",
		help="Override the output JSON path (defaults to eval/results/scaling-<utc>.json)",
	)
	parser.add_argument(
		"--png",
		default="",
		help="Override the output PNG path (defaults to eval/results/scaling-tick-latency.png)",
	)
	parser.add_argument(
		"--skip-plot",
		action="store_true",
		help="Skip invoking eval/scaling/plot.py after the JSON is written",
	)
	args = parser.parse_args()

	if not _docker_running(args.container):
		print(
			f"Dev container '{args.container}' is not running. Start it first.",
			file=sys.stderr,
		)
		return 2

	tag = _now_tag()
	RESULTS_DIR.mkdir(parents=True, exist_ok=True)
	output_path = Path(args.output) if args.output else (RESULTS_DIR / f"scaling-{tag}.json")
	png_path = Path(args.png) if args.png else (RESULTS_DIR / "scaling-tick-latency.png")

	# Ship the in-container scripts to a self-contained /tmp directory so
	# the imports inside _inproc.py resolve.
	subprocess.run(
		["docker", "exec", args.container, "mkdir", "-p", "/tmp/_scaling"],
		check=True,
	)
	for filename in ("_inproc.py", "seed.py", "extract_latencies.py"):
		subprocess.run(
			[
				"docker",
				"cp",
				str(SCALING_DIR / filename),
				f"{args.container}:/tmp/_scaling/{filename}",
			],
			check=True,
		)

	cmd = [
		"docker",
		"exec",
		args.container,
		f"{args.bench_path}/env/bin/python",
		"/tmp/_scaling/_inproc.py",
		"--site",
		args.bench_site,
		"--bench-path",
		args.bench_path,
		"--ns",
		args.ns,
		"--repeats",
		str(args.repeats),
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
	output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
	print(f"\nReport written to {output_path}", file=sys.stderr)
	_print_summary(report)

	if args.skip_plot:
		return r.returncode

	plot_script = SCALING_DIR / "plot.py"
	# Render the plot via the dev container's bench python, which is where
	# matplotlib lives (per eval/README.md §Scaling).  Falling back to the
	# host python would silently skip when matplotlib isn't on the host.
	subprocess.run(
		["docker", "cp", str(plot_script), f"{args.container}:/tmp/_scaling/plot.py"],
		check=True,
	)
	subprocess.run(
		["docker", "cp", str(output_path), f"{args.container}:/tmp/_scaling/scaling.json"],
		check=True,
	)
	png_existed_before = png_path.exists()
	png_mtime_before = png_path.stat().st_mtime if png_existed_before else 0.0
	plot = subprocess.run(
		[
			"docker",
			"exec",
			args.container,
			f"{args.bench_path}/env/bin/python",
			"/tmp/_scaling/plot.py",
			"--input",
			"/tmp/_scaling/scaling.json",
			"--output",
			"/tmp/_scaling/scaling-tick-latency.png",
		],
		capture_output=True,
		text=True,
	)
	if plot.returncode == 0:
		copy_back = subprocess.run(
			["docker", "cp", f"{args.container}:/tmp/_scaling/scaling-tick-latency.png", str(png_path)],
			capture_output=True,
			text=True,
		)
		if copy_back.returncode != 0 and copy_back.stderr:
			print(copy_back.stderr, file=sys.stderr, end="")
	if plot.stdout:
		print(plot.stdout, file=sys.stderr, end="")
	if plot.stderr:
		print(plot.stderr, file=sys.stderr, end="")
	png_written = png_path.exists() and (
		not png_existed_before or png_path.stat().st_mtime > png_mtime_before
	)
	if plot.returncode != 0:
		print(
			f"Plot step exited {plot.returncode}; JSON report is still complete.",
			file=sys.stderr,
		)
	elif png_written:
		print(f"Plot written to {png_path}", file=sys.stderr)
	else:
		print(
			"Plot step skipped (matplotlib unavailable). JSON report is still "
			"complete; see eval/README.md §Scaling for the install hint.",
			file=sys.stderr,
		)

	return r.returncode


if __name__ == "__main__":
	sys.exit(main())
