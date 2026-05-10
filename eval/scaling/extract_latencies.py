"""Extract helm-subprocess latency samples from harness JSON reports.

The Kubeport helm wrapper at ``kubeport/utils/helm.py`` does not emit
per-call structured timing, but the TODO-04 harness records wall-clock per
phase in ``eval/results/<utc>.json``.  Three phases dominate helm
subprocess time and are treated here as latency samples:

- ``setup_helm_repo`` — ``helm repo add`` + ``helm repo update`` + chart
  catalog rebuild (one ``helm search`` per chart).
- ``deploy_release``  — ``helm upgrade --install`` (in fresh-mode runs)
  or ``helm status`` (in reuse-mode runs).
- ``verify_chart``    — DB lookup only, no helm call; included for
  reference and tagged as ``helm_call=False``.

The extractor does not require live container access — it works against
any directory of harness reports.  Produces a list of per-sample dicts and
a small percentile summary.  Both are folded into the scaling report.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# Phases in eval/_inproc.py whose duration_seconds is dominated by helm.
HELM_PHASES = {"setup_helm_repo", "deploy_release"}
# Phases that exercise the helm code path but are typically pure DB work.
HELM_REFERENCE_PHASES = {"verify_chart"}


def _percentile(sorted_values: list[float], pct: float) -> float:
	if not sorted_values:
		return 0.0
	if len(sorted_values) == 1:
		return sorted_values[0]
	k = (len(sorted_values) - 1) * pct
	f = math.floor(k)
	c = math.ceil(k)
	if f == c:
		return sorted_values[int(k)]
	return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def collect_samples(results_dir: Path) -> list[dict[str, Any]]:
	"""Walk ``results_dir`` and return one sample dict per helm-touching phase."""
	samples: list[dict[str, Any]] = []
	if not results_dir.exists():
		return samples
	for report_path in sorted(results_dir.glob("*.json")):
		# Skip the scaling report itself and any baseline-derived reports —
		# baseline phases use raw helm CLI directly, which is a different
		# population (operator latency, not Kubeport-mediated latency).
		if report_path.name.startswith(("scaling", "sample-baseline", "baseline")):
			continue
		try:
			report = json.loads(report_path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		phases = report.get("phases", [])
		if not isinstance(phases, list):
			continue
		for phase in phases:
			name = phase.get("phase")
			if name not in (HELM_PHASES | HELM_REFERENCE_PHASES):
				continue
			if phase.get("status") != "passed":
				continue
			samples.append(
				{
					"source": report_path.name,
					"phase": name,
					"duration_seconds": float(phase.get("duration_seconds", 0.0)),
					"helm_call": name in HELM_PHASES,
				}
			)
	return samples


def summarise(samples: list[dict[str, Any]]) -> dict[str, Any]:
	"""Return percentile + count summary for the helm-call samples only."""
	helm_durations = sorted(s["duration_seconds"] for s in samples if s["helm_call"])
	if not helm_durations:
		return {
			"sample_count": 0,
			"min_seconds": None,
			"median_seconds": None,
			"p95_seconds": None,
			"max_seconds": None,
			"mean_seconds": None,
		}
	mean = sum(helm_durations) / len(helm_durations)
	return {
		"sample_count": len(helm_durations),
		"min_seconds": round(helm_durations[0], 3),
		"median_seconds": round(_percentile(helm_durations, 0.50), 3),
		"p95_seconds": round(_percentile(helm_durations, 0.95), 3),
		"max_seconds": round(helm_durations[-1], 3),
		"mean_seconds": round(mean, 3),
	}


def main() -> int:
	import argparse
	import sys

	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--results-dir",
		default="eval/results",
		help="Directory containing harness JSON reports (default: eval/results)",
	)
	parser.add_argument(
		"--output",
		default="",
		help="Write the JSON output here instead of stdout",
	)
	args = parser.parse_args()

	results_dir = Path(args.results_dir)
	samples = collect_samples(results_dir)
	payload = {
		"results_dir": str(results_dir),
		"samples": samples,
		"summary": summarise(samples),
	}
	text = json.dumps(payload, indent=2, sort_keys=True)
	if args.output:
		Path(args.output).write_text(text, encoding="utf-8")
	else:
		sys.stdout.write(text + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
