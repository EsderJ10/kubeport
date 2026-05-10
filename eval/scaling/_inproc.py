"""In-container half of the Kubeport scaling harness.

For each N in ``--ns`` (default ``1,10,100,1000``):

1. Cleans up synthetic rows from prior runs (idempotent).
2. Bulk-inserts N synthetic ``Helm Release``, ``Service Bundle``, and
   ``Frappe Site`` rows via ``eval/scaling/seed.py`` — controllers and
   hooks are bypassed, so only the database-write cost is paid.
3. Patches every cluster-touching helper used by the periodic
   reconciliation tasks to a deterministic in-memory stub so tick latency
   reflects framework + DB cost, not cluster-side wait time.
4. Runs ``reconcile_all_releases()`` ``--repeats`` times and records the
   per-tick wall-clock.

Then it walks ``eval/results/<utc>.json`` reports via
``eval/scaling/extract_latencies.py`` and folds helm-call latency samples
into the same payload.

Finally it fits ``log(median tick latency) ~ slope * log(N) + intercept``
and labels the regression shape (``constant`` / ``sublinear`` / ``linear``
/ ``superlinear``).  No numpy dependency: stdlib least-squares.

Emits one JSON document on stdout between ``RESULT_BEGIN`` / ``RESULT_END``,
matching the harness convention so ``host_driver.py`` can parse it the
same way ``eval/harness.py`` parses ``eval/_inproc.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock


def _bench_setup(site: str, bench_path: str) -> None:
	os.chdir(os.path.join(bench_path, "sites"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "frappe"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "kubeport"))
	import frappe

	frappe.init(site=site, sites_path=".")
	frappe.connect()


def _utc_iso() -> str:
	return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def _mock_cluster_reads():
	"""Stub every cluster-touching helper invoked by reconcile_all_releases.

	The reconciliation paths exercised by the seeded rows reach four helpers:

	- ``kubeport.utils.helm.status``                — returns {"info": {"status": "deployed"}}
	- ``kubeport.utils.k8s_client.get_k8s_api_client`` — returns a placeholder
	- ``kubeport.utils.release_health.walk``        — returns ([], "ok")
	- ``kubeport.utils.k8s_resources.check_resources_exist`` — returns (True, "")

	The orphan-Job sweep also calls into Kubernetes; it short-circuits when
	no rows have ``operation_job_name`` set, which is the case for our
	seeded healthy rows.  Stale-Helm-operation reconciliation walks
	in-flight statuses, also empty for our seeds.
	"""
	patches = [
		mock.patch(
			"kubeport.utils.helm.status",
			return_value={"info": {"status": "deployed"}},
		),
		mock.patch(
			"kubeport.utils.k8s_client.get_k8s_api_client",
			return_value=object(),
		),
		mock.patch(
			"kubeport.utils.release_health.walk",
			return_value=([], "ok"),
		),
		mock.patch(
			"kubeport.utils.k8s_resources.check_resources_exist",
			return_value=(True, ""),
		),
	]
	# Reconciliation also imports walk inside _reconcile_helm_releases via a
	# local import; patch the symbol on the source module so the local import
	# binding picks up the stub.
	with contextlib.ExitStack() as stack:
		for patch in patches:
			stack.enter_context(patch)
		yield


def _time_one_tick() -> float:
	import frappe

	from kubeport.tasks.reconciliation import reconcile_all_releases

	frappe.db.rollback()
	t0 = time.monotonic()
	reconcile_all_releases()
	elapsed = time.monotonic() - t0
	frappe.db.rollback()
	return elapsed


def _regression(ns: list[int], medians: list[float]) -> dict[str, float | str]:
	"""Fit log(median_latency) ~ slope * log(N) + intercept via stdlib least-squares.

	Slope interpretation:
	- |slope| <= 0.10 → ``constant``
	- 0.10 <  slope  <= 0.85 → ``sublinear``
	- 0.85 <  slope  <  1.15 → ``linear``
	- slope >= 1.15 → ``superlinear``
	"""
	pairs = [(math.log(n), math.log(t)) for n, t in zip(ns, medians, strict=True) if n > 0 and t > 0]
	if len(pairs) < 2:
		return {"slope": 0.0, "intercept": 0.0, "shape": "insufficient-data"}
	xs = [p[0] for p in pairs]
	ys = [p[1] for p in pairs]
	mean_x = statistics.fmean(xs)
	mean_y = statistics.fmean(ys)
	num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
	den = sum((x - mean_x) ** 2 for x in xs)
	slope = num / den if den else 0.0
	intercept = mean_y - slope * mean_x
	shape = "constant"
	if abs(slope) > 0.10:
		shape = "sublinear"
	if slope > 0.85:
		shape = "linear"
	if slope >= 1.15:
		shape = "superlinear"
	return {"slope": round(slope, 3), "intercept": round(intercept, 3), "shape": shape}


def run_benchmark(ns: list[int], repeats: int, results_dir: Path) -> dict:
	import seed  # type: ignore[import-not-found]
	from extract_latencies import collect_samples, summarise  # type: ignore[import-not-found]

	overall_started = _utc_iso()
	overall_t0 = time.monotonic()

	pre_sweep = seed.cleanup_synthetic_rows()
	seed.ensure_parents()

	per_n: list[dict] = []
	for n in ns:
		# Reset to a clean slate before each N so leftover rows from the
		# previous (smaller / larger) run do not skew the next measurement.
		seed.cleanup_synthetic_rows()
		seed.seed_helm_releases(n)
		seed.seed_service_bundles(n)
		seed.seed_frappe_sites(n)
		samples = []
		# ``synthetic_rows_active`` flips the Draft seeds to Deployed for the
		# duration of the timed block and back to Draft on exit (including
		# abnormal exits).  Pairing it with the seed default makes it
		# impossible for a benchmark crash to leave unreachable Deployed rows
		# behind, where the live scheduler would otherwise see them and emit
		# one Error Log row per release per 5-minute tick.
		with _mock_cluster_reads(), seed.synthetic_rows_active():
			# One throw-away tick to warm caches (query plan, autocommit
			# state) so the first measured tick isn't a JIT outlier.
			_time_one_tick()
			for _ in range(repeats):
				samples.append(_time_one_tick())
		per_n.append(
			{
				"n": n,
				"repeats": repeats,
				"tick_seconds": [round(s, 4) for s in samples],
				"min_seconds": round(min(samples), 4),
				"median_seconds": round(statistics.median(samples), 4),
				"mean_seconds": round(statistics.fmean(samples), 4),
				"max_seconds": round(max(samples), 4),
			}
		)

	# Final sweep so the bench is clean even if the harness aborts mid-run.
	post_sweep = seed.cleanup_synthetic_rows()

	medians = [row["median_seconds"] for row in per_n]
	regression = _regression(ns, medians)

	helm_samples = collect_samples(results_dir)
	helm_summary = summarise(helm_samples)

	overall_finished = _utc_iso()
	overall_duration = round(time.monotonic() - overall_t0, 3)
	return {
		"schema_version": 1,
		"generated_at": overall_finished,
		"started_at": overall_started,
		"finished_at": overall_finished,
		"duration_seconds": overall_duration,
		"context": {
			"ns": ns,
			"repeats": repeats,
			"results_dir": str(results_dir),
			"reconciliation_entrypoint": "kubeport.tasks.reconciliation.reconcile_all_releases",
		},
		"sweep": {
			"pre_run": pre_sweep,
			"post_run": post_sweep,
		},
		"tick_latency": per_n,
		"regression": regression,
		"helm_subprocess_latency": {
			"samples": helm_samples,
			"summary": helm_summary,
		},
	}


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", default="/workspace/development/bench-16")
	parser.add_argument(
		"--ns",
		default="1,10,100,1000",
		help="Comma-separated list of N values (default: 1,10,100,1000)",
	)
	parser.add_argument("--repeats", type=int, default=5)
	parser.add_argument("--results-dir", default="apps/kubeport/eval/results")
	args = parser.parse_args()

	# Make seed.py / extract_latencies.py importable when this file is
	# docker-cp'd to /tmp without its sibling files.  When run from the
	# repo (host or container with bind-mount), the sibling files are
	# alongside this one.  Prepend the script directory either way.
	sys.path.insert(0, str(Path(__file__).resolve().parent))

	_bench_setup(args.site, args.bench_path)

	ns = [int(x) for x in args.ns.split(",") if x.strip()]
	results_dir = Path(args.results_dir)
	if not results_dir.is_absolute():
		results_dir = Path(args.bench_path) / results_dir

	report = run_benchmark(ns=ns, repeats=args.repeats, results_dir=results_dir)

	print("RESULT_BEGIN")
	print(json.dumps(report, indent=2, sort_keys=True))
	print("RESULT_END")
	return 0


if __name__ == "__main__":
	sys.exit(main())
