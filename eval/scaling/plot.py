"""Render the scaling-tick-latency plot from a scaling JSON report.

Reads a scaling report produced by ``eval/scaling/host_driver.py`` and
writes ``scaling-tick-latency.png`` (or whatever ``--output`` points to)
on a log-log scale with the fitted regression line overlaid.

matplotlib is not installed by default in the dev container or on the
host (per the project's no-local-installs rule).  When the import fails
this script prints a precise, copyable install command and exits 0 so
``make eval-scaling`` does not regress to red — the JSON report is the
acceptance artifact; the PNG is a derived view.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_INSTALL_HINT = (
	"matplotlib is required for PNG generation but is not installed. To "
	"install it into the dev container's bench env (one-time, ~30s):\n\n"
	"    docker exec tfg_devcontainer-frappe-1 \\\n"
	"        /workspace/development/bench-16/env/bin/pip install matplotlib\n\n"
	"Or on the host:\n\n"
	"    pip install --user matplotlib\n\n"
	"Then re-run: make eval-scaling-plot"
)


def _render(report: dict, output_path: Path) -> None:
	import matplotlib

	matplotlib.use("Agg")
	import matplotlib.pyplot as plt

	rows = report.get("tick_latency", [])
	ns = [row["n"] for row in rows]
	medians = [row["median_seconds"] for row in rows]
	mins = [row["min_seconds"] for row in rows]
	maxs = [row["max_seconds"] for row in rows]
	regression = report.get("regression", {})

	fig, ax = plt.subplots(figsize=(7, 4.5))
	ax.set_xscale("log")
	ax.set_yscale("log")
	ax.plot(ns, medians, marker="o", color="#1f77b4", label="median tick latency")
	ax.fill_between(ns, mins, maxs, color="#1f77b4", alpha=0.15, label="min..max range")

	slope = regression.get("slope")
	intercept = regression.get("intercept")
	shape = regression.get("shape", "")
	if isinstance(slope, (int, float)) and isinstance(intercept, (int, float)) and ns:
		import math

		fit_xs = ns
		fit_ys = [math.exp(intercept) * (n**slope) for n in fit_xs]
		ax.plot(
			fit_xs,
			fit_ys,
			linestyle="--",
			color="#d62728",
			label=f"fit: slope={slope:.2f} ({shape})",
		)

	ax.set_xlabel("N synthetic rows (per DocType)")
	ax.set_ylabel("reconcile_all_releases() wall-clock (seconds, log)")
	ax.set_title("Kubeport reconciliation tick latency vs. N")
	ax.grid(True, which="both", linestyle=":", alpha=0.5)
	ax.legend(loc="upper left")
	fig.tight_layout()
	fig.savefig(output_path, dpi=150)
	plt.close(fig)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--input", required=True, help="Path to a scaling JSON report")
	parser.add_argument("--output", required=True, help="Path to write the PNG to")
	args = parser.parse_args()

	try:
		import matplotlib
	except ModuleNotFoundError:
		print(_INSTALL_HINT, file=sys.stderr)
		return 0

	report = json.loads(Path(args.input).read_text(encoding="utf-8"))
	output_path = Path(args.output)
	output_path.parent.mkdir(parents=True, exist_ok=True)
	_render(report, output_path)
	return 0


if __name__ == "__main__":
	sys.exit(main())
