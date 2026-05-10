"""In-bench bootstrap for the chaos CI workflow.

Inserts the minimal DocType rows the ``worker_kill_mid_helm_upgrade``
scenario needs: a ``Kubernetes Cluster`` pointing at a local k3d
kubeconfig, a ``Helm Repository`` pointing at a local HTTP-served
chart index, and a ``Helm Release`` for the in-repo
``eval/faults/fixtures/chaos-chart``.  Drives the release to
``Deployed`` and prints the release docname between
``RELEASE_BEGIN`` / ``RELEASE_END`` markers so the workflow can hand
it off to ``_inproc_worker_kill.py``.

Reuses the already-tested phase functions from ``eval/_inproc.py``
instead of re-implementing the wait/idempotency logic.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _bench_setup(site: str, bench_path: str) -> None:
	os.chdir(os.path.join(bench_path, "sites"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "frappe"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "kubeport"))
	import frappe

	frappe.init(site=site, sites_path=".")
	frappe.connect()


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", required=True)
	parser.add_argument("--cluster-doc-name", default="chaos-k3d")
	parser.add_argument("--kubeconfig-file", required=True)
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument("--helm-repo-name", default="chaos")
	parser.add_argument("--helm-repo-url", required=True)
	parser.add_argument("--chart-name", default="chaos-chart")
	parser.add_argument("--release-name", default="chaos-release")
	parser.add_argument("--namespace", default="chaos")
	parser.add_argument("--repo-sync-timeout", type=int, default=180)
	parser.add_argument("--deploy-timeout", type=int, default=300)
	args = parser.parse_args()

	_bench_setup(args.site, args.bench_path)

	repo_root = Path(__file__).resolve().parents[2]
	sys.path.insert(0, str(repo_root / "eval"))
	import _inproc  # type: ignore[import-not-found]

	kubeconfig_text = Path(args.kubeconfig_file).read_text(encoding="utf-8")
	ctx = {
		"cluster_doc_name": args.cluster_doc_name,
		"kubeconfig_text": kubeconfig_text,
		"kubeconfig_context": args.kubeconfig_context,
		"helm_repo_name": args.helm_repo_name,
		"helm_repo_url": args.helm_repo_url,
		"chart_name": args.chart_name,
		"namespace": args.namespace,
		"release_name": args.release_name,
		"chart_version": "",
		"site_image": "",
		"release_values": "",
	}

	_inproc.setup_cluster_doc(ctx, 60)
	_inproc.setup_helm_repo(ctx, args.repo_sync_timeout)
	_inproc.verify_chart(ctx, 30)
	_inproc.create_release(ctx, 30)
	_inproc.deploy_release(ctx, args.deploy_timeout)

	print("RELEASE_BEGIN")
	print(ctx["release_doc_name"])
	print("RELEASE_END")
	return 0


if __name__ == "__main__":
	sys.exit(main())
