"""In-container half of the Kubeport evaluation harness.

Runs inside the dev container with Frappe initialised against the bench site
that hosts Kubeport.  Drives the 10-step golden path against a target k3d
cluster, polling each DocType row until it reaches its expected terminal
state, and emits one JSON report on stdout between the markers
``RESULT_BEGIN`` / ``RESULT_END``.

Invoked from ``eval/harness.py`` via ``docker exec``; not meant to be run
directly.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timezone


def _bench_setup(site: str, bench_path: str) -> None:
	os.chdir(os.path.join(bench_path, "sites"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "frappe"))
	sys.path.insert(0, os.path.join(bench_path, "apps", "kubeport"))
	import frappe

	frappe.init(site=site, sites_path=".")
	frappe.connect()


def _utc_iso() -> str:
	return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_until(predicate, timeout_s: int, interval_s: float = 2.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(interval_s)
	return predicate()


class Phase:
	def __init__(self, name: str, fn, timeout_s: int) -> None:
		self.name = name
		self.fn = fn
		self.timeout_s = timeout_s
		self.started_at = ""
		self.finished_at = ""
		self.duration_seconds = 0.0
		self.status = "pending"
		self.detail = ""

	def run(self, ctx: dict) -> bool:
		self.started_at = _utc_iso()
		t0 = time.monotonic()
		try:
			self.fn(ctx, self.timeout_s)
			self.status = "passed"
		except Exception as exc:
			self.status = "failed"
			self.detail = f"{type(exc).__name__}: {exc}"
		self.finished_at = _utc_iso()
		self.duration_seconds = round(time.monotonic() - t0, 3)
		return self.status == "passed"

	def to_dict(self) -> dict:
		return {
			"phase": self.name,
			"started_at": self.started_at,
			"finished_at": self.finished_at,
			"duration_seconds": self.duration_seconds,
			"status": self.status,
			"timeout_seconds": self.timeout_s,
			"detail": self.detail,
		}


def setup_cluster_doc(ctx: dict, _timeout_s: int) -> None:
	import frappe

	cluster_name = ctx["cluster_doc_name"]
	if frappe.db.exists("Kubernetes Cluster", cluster_name):
		ctx["cluster_doc_created"] = False
		return
	doc = frappe.get_doc(
		{
			"doctype": "Kubernetes Cluster",
			"cluster_name": cluster_name,
			"auth_method": "Kubeconfig",
			"kubeconfig": ctx["kubeconfig_text"],
			"kubeconfig_context": ctx["kubeconfig_context"] or "",
			"skip_tls_verify": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	ctx["cluster_doc_created"] = True


def setup_helm_repo(ctx: dict, timeout_s: int) -> None:
	import frappe

	repo_name = ctx["helm_repo_name"]
	if not frappe.db.exists("Helm Repository", repo_name):
		doc = frappe.get_doc(
			{
				"doctype": "Helm Repository",
				"repo_name": repo_name,
				"repo_url": ctx["helm_repo_url"],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	doc = frappe.get_doc("Helm Repository", repo_name)
	doc.sync_charts()
	frappe.db.commit()

	def _ready() -> bool:
		frappe.db.rollback()
		last_synced = frappe.db.get_value("Helm Repository", repo_name, "last_synced")
		return bool(last_synced)

	if not _wait_until(_ready, timeout_s, interval_s=3.0):
		raise RuntimeError(f"Helm Repository '{repo_name}' did not finish syncing")


def verify_chart(ctx: dict, _timeout_s: int) -> None:
	import frappe

	chart_name = f"{ctx['helm_repo_name']}/{ctx['chart_name']}"
	frappe.db.rollback()
	if not frappe.db.exists("Helm Chart", chart_name):
		raise RuntimeError(f"Chart '{chart_name}' missing after repo sync")
	ctx["chart_doc_name"] = chart_name


def create_release(ctx: dict, _timeout_s: int) -> None:
	import frappe

	expected_doc_name = f"{ctx['cluster_doc_name']}/{ctx['namespace']}/{ctx['release_name']}"
	if frappe.db.exists("Helm Release", expected_doc_name):
		ctx["release_doc_name"] = expected_doc_name
		ctx["release_doc_created"] = False
		return

	doc = frappe.get_doc(
		{
			"doctype": "Helm Release",
			"release_name": ctx["release_name"],
			"cluster": ctx["cluster_doc_name"],
			"namespace": ctx["namespace"],
			"chart": ctx["chart_doc_name"],
			"chart_version": ctx["chart_version"] or None,
			"site_image": ctx["site_image"] or None,
			"values": ctx["release_values"] or "",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	ctx["release_doc_name"] = doc.name
	ctx["release_doc_created"] = True


def deploy_release(ctx: dict, timeout_s: int) -> None:
	import frappe

	release_doc_name = ctx["release_doc_name"]
	current_status = frappe.db.get_value("Helm Release", release_doc_name, "status")
	if current_status == "Deployed":
		ctx["deploy_skipped_already_deployed"] = True
		return

	doc = frappe.get_doc("Helm Release", release_doc_name)
	doc.deploy_release()
	frappe.db.commit()

	def _done() -> bool:
		frappe.db.rollback()
		status = frappe.db.get_value("Helm Release", release_doc_name, "status")
		return status in ("Deployed", "Failed", "Degraded")

	if not _wait_until(_done, timeout_s, interval_s=5.0):
		raise RuntimeError(f"Helm Release '{release_doc_name}' did not reach a terminal state")
	frappe.db.rollback()
	final = frappe.db.get_value("Helm Release", release_doc_name, "status")
	if final != "Deployed":
		detail = frappe.db.get_value("Helm Release", release_doc_name, "helm_status_detail") or ""
		raise RuntimeError(f"Helm Release ended in '{final}': {detail}")


def create_site(ctx: dict, timeout_s: int) -> None:
	import frappe

	expected_site_doc = f"{ctx['release_doc_name']}/{ctx['site_name']}"
	if frappe.db.exists("Frappe Site", expected_site_doc):
		ctx["site_doc_name"] = expected_site_doc
		ctx["site_doc_created"] = False
		current = frappe.db.get_value("Frappe Site", expected_site_doc, "status")
		if current == "Active":
			ctx["create_site_skipped_already_active"] = True
			return
	else:
		site_payload = {
			"doctype": "Frappe Site",
			"site_name": ctx["site_name"],
			"bench_release": ctx["release_doc_name"],
			"admin_password": ctx["admin_password"],
			"db_root_password": ctx["db_root_password"],
			"db_type": "mariadb",
			"install_apps": ctx["install_apps"],
		}
		doc = frappe.get_doc(site_payload)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		ctx["site_doc_name"] = doc.name
		ctx["site_doc_created"] = True

	doc = frappe.get_doc("Frappe Site", ctx["site_doc_name"])
	doc.create_site()
	frappe.db.commit()

	def _done() -> bool:
		frappe.db.rollback()
		status = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
		return status in ("Active", "Failed")

	if not _wait_until(_done, timeout_s, interval_s=5.0):
		raise RuntimeError(f"Frappe Site '{ctx['site_doc_name']}' did not reach Active or Failed")
	frappe.db.rollback()
	final = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
	if final != "Active":
		detail = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status_detail") or ""
		raise RuntimeError(f"Site provisioning ended in '{final}': {detail}")


def migrate_site(ctx: dict, timeout_s: int) -> None:
	import frappe

	doc = frappe.get_doc("Frappe Site", ctx["site_doc_name"])
	doc.migrate_site()
	frappe.db.commit()

	def _done() -> bool:
		frappe.db.rollback()
		status = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
		return status in ("Active", "Failed")

	if not _wait_until(_done, timeout_s, interval_s=4.0):
		raise RuntimeError("Migrate did not reach a terminal state")
	frappe.db.rollback()
	final = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
	if final != "Active":
		detail = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status_detail") or ""
		raise RuntimeError(f"Migrate ended in '{final}': {detail}")


def backup_site(ctx: dict, timeout_s: int) -> None:
	import frappe

	doc = frappe.get_doc("Frappe Site", ctx["site_doc_name"])
	result = doc.backup_site()
	frappe.db.commit()
	backup_docname = result["backup_docname"]
	ctx["backup_docname"] = backup_docname

	def _done() -> bool:
		frappe.db.rollback()
		status = frappe.db.get_value("Frappe Site Backup", backup_docname, "status")
		return status in ("Available", "Failed")

	if not _wait_until(_done, timeout_s, interval_s=4.0):
		raise RuntimeError("Backup did not reach a terminal state")
	frappe.db.rollback()
	final = frappe.db.get_value("Frappe Site Backup", backup_docname, "status")
	if final != "Available":
		detail = frappe.db.get_value("Frappe Site Backup", backup_docname, "status_detail") or ""
		raise RuntimeError(f"Backup ended in '{final}': {detail}")


def restore_site(ctx: dict, timeout_s: int) -> None:
	import frappe

	doc = frappe.get_doc("Frappe Site", ctx["site_doc_name"])
	doc.restore_site(backup_docname=ctx["backup_docname"], confirm_destructive=True)
	frappe.db.commit()

	def _done() -> bool:
		frappe.db.rollback()
		status = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
		return status in ("Active", "Failed")

	if not _wait_until(_done, timeout_s, interval_s=4.0):
		raise RuntimeError("Restore did not reach a terminal state")
	frappe.db.rollback()
	final = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status")
	if final != "Active":
		detail = frappe.db.get_value("Frappe Site", ctx["site_doc_name"], "status_detail") or ""
		raise RuntimeError(f"Restore ended in '{final}': {detail}")


def drop_site(ctx: dict, timeout_s: int) -> None:
	import frappe

	doc = frappe.get_doc("Frappe Site", ctx["site_doc_name"])
	doc.delete_site()
	frappe.db.commit()

	def _gone() -> bool:
		frappe.db.rollback()
		return not frappe.db.exists("Frappe Site", ctx["site_doc_name"])

	if not _wait_until(_gone, timeout_s, interval_s=5.0):
		raise RuntimeError("Frappe Site row was not removed by reconciliation after delete")


def build_phases() -> list[Phase]:
	return [
		Phase("setup_cluster_doc", setup_cluster_doc, 60),
		Phase("setup_helm_repo", setup_helm_repo, 240),
		Phase("verify_chart", verify_chart, 30),
		Phase("create_release", create_release, 30),
		Phase("deploy_release", deploy_release, 900),
		Phase("create_site", create_site, 900),
		Phase("migrate_site", migrate_site, 600),
		Phase("backup_site", backup_site, 600),
		Phase("restore_site", restore_site, 600),
		Phase("drop_site", drop_site, 600),
	]


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--site", required=True)
	parser.add_argument("--bench-path", default="/workspace/development/bench-16")
	parser.add_argument("--cluster-doc-name", required=True)
	parser.add_argument("--kubeconfig-file", default="")
	parser.add_argument("--kubeconfig-context", default="")
	parser.add_argument("--helm-repo-name", default="frappe")
	parser.add_argument("--helm-repo-url", default="https://helm.erpnext.com")
	parser.add_argument("--chart-name", default="erpnext")
	parser.add_argument("--chart-version", default="")
	parser.add_argument("--release-name", required=True)
	parser.add_argument("--namespace", required=True)
	parser.add_argument("--site-image", default="")
	parser.add_argument("--release-values-file", default="")
	parser.add_argument("--site-name", required=True)
	parser.add_argument("--admin-password", default="")
	parser.add_argument("--db-root-password", default="")
	parser.add_argument("--install-apps", default="erpnext")
	args = parser.parse_args()

	_bench_setup(args.site, args.bench_path)

	release_values = ""
	if args.release_values_file and os.path.exists(args.release_values_file):
		with open(args.release_values_file, encoding="utf-8") as fh:
			release_values = fh.read()

	kubeconfig_text = ""
	if args.kubeconfig_file and os.path.exists(args.kubeconfig_file):
		with open(args.kubeconfig_file, encoding="utf-8") as fh:
			kubeconfig_text = fh.read()

	ctx = {
		"cluster_doc_name": args.cluster_doc_name,
		"kubeconfig_text": kubeconfig_text,
		"kubeconfig_context": args.kubeconfig_context,
		"helm_repo_name": args.helm_repo_name,
		"helm_repo_url": args.helm_repo_url,
		"chart_name": args.chart_name,
		"chart_version": args.chart_version,
		"release_name": args.release_name,
		"namespace": args.namespace,
		"site_image": args.site_image,
		"release_values": release_values,
		"site_name": args.site_name,
		"admin_password": args.admin_password or secrets.token_hex(8),
		"db_root_password": args.db_root_password or secrets.token_hex(8),
		"install_apps": args.install_apps,
	}

	phases = build_phases()
	overall_started = _utc_iso()
	overall_t0 = time.monotonic()
	abort_remaining = False
	for phase in phases:
		if abort_remaining:
			phase.started_at = _utc_iso()
			phase.finished_at = phase.started_at
			phase.status = "skipped"
			phase.detail = "Earlier phase failed"
			continue
		ok = phase.run(ctx)
		if not ok:
			abort_remaining = True

	overall_finished = _utc_iso()
	overall_duration = round(time.monotonic() - overall_t0, 3)
	report = {
		"schema_version": 1,
		"generated_at": overall_finished,
		"started_at": overall_started,
		"finished_at": overall_finished,
		"duration_seconds": overall_duration,
		"context": {
			k: v
			for k, v in ctx.items()
			if k not in ("kubeconfig_text", "release_values", "admin_password", "db_root_password")
		},
		"phases": [p.to_dict() for p in phases],
		"summary": {
			"passed": sum(1 for p in phases if p.status == "passed"),
			"failed": sum(1 for p in phases if p.status == "failed"),
			"skipped": sum(1 for p in phases if p.status == "skipped"),
			"total": len(phases),
		},
	}

	print("RESULT_BEGIN")
	print(json.dumps(report, indent=2, sort_keys=True))
	print("RESULT_END")
	return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
	sys.exit(main())
