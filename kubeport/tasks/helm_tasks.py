"""
Helm Background Tasks

All Helm CLI operations that touch the cluster (install, upgrade, uninstall,
repo sync) run here via ``frappe.enqueue`` on the ``long`` queue.  This
guarantees that ``subprocess.run`` never blocks a web request.

Each task follows the pattern:
1. Load the Frappe document
2. Set status to "In Progress" / "Syncing"
3. Execute the Helm CLI wrapper call
4. Update status + metadata on success
5. On failure: set status to "Failed" / "Error", log the error
6. Push a realtime event so the browser auto-reloads
"""

from collections import defaultdict
import fnmatch
import re
import secrets

import frappe

from kubeport.utils import helm

_HELM_STATUS_DETAIL_LIMIT = 500
_DEPLOYABLE_WORKER_STATUS = "In Progress"
_UNINSTALLING_WORKER_STATUS = "Uninstalling"


# ---------------------------------------------------------------------------
# Repository Tasks
# ---------------------------------------------------------------------------


def add_and_sync_repo(repo_name: str, sync_token: str):
	"""Register a Helm repo and trigger a chart index sync.

	Called automatically when a new Helm Repository document is created
	(via ``after_insert``).
	"""
	if not _repo_sync_token_matches(repo_name, sync_token):
		return

	doc = frappe.get_doc("Helm Repository", repo_name)

	try:
		_ensure_repo_registered(doc)

		# Sync charts into the database
		_sync_charts(doc)
		if not _repo_sync_token_matches(repo_name, sync_token):
			frappe.db.rollback()
			return

		doc.db_set("status", "Synced")
		doc.db_set("last_synced", frappe.utils.now())

		frappe.publish_realtime(
			"helm_repo_sync_update",
			{"repo_name": repo_name, "status": "Synced"},
			doctype="Helm Repository",
			docname=repo_name,
		)

	except Exception as e:
		frappe.db.rollback()
		if not _repo_sync_token_matches(repo_name, sync_token):
			return
		doc.db_set("status", "Error")
		frappe.log_error(
			title=f"Helm Repo Add Failed: {repo_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_repo_sync_update",
			{"repo_name": repo_name, "status": "Error"},
			doctype="Helm Repository",
			docname=repo_name,
		)


def sync_repo_charts(repo_name: str, sync_token: str):
	"""Update a repo's chart index and sync new charts to the database.

	Called by the "Sync Charts" button and the daily scheduler.
	"""
	if not _repo_sync_token_matches(repo_name, sync_token):
		return

	doc = frappe.get_doc("Helm Repository", repo_name)

	try:
		# Re-register the repo before every sync so workers remain correct even if
		# the original add job was superseded or Helm's local registry was reset.
		_ensure_repo_registered(doc)

		# Refresh the local chart index
		helm.repo_update(doc.repo_name)

		# Sync charts
		_sync_charts(doc)
		if not _repo_sync_token_matches(repo_name, sync_token):
			frappe.db.rollback()
			return

		doc.db_set("status", "Synced")
		doc.db_set("last_synced", frappe.utils.now())

		frappe.publish_realtime(
			"helm_repo_sync_update",
			{"repo_name": repo_name, "status": "Synced"},
			doctype="Helm Repository",
			docname=repo_name,
		)

	except Exception as e:
		frappe.db.rollback()
		if not _repo_sync_token_matches(repo_name, sync_token):
			return
		doc.db_set("status", "Error")
		frappe.log_error(
			title=f"Helm Repo Sync Failed: {repo_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_repo_sync_update",
			{"repo_name": repo_name, "status": "Error"},
			doctype="Helm Repository",
			docname=repo_name,
		)


def sync_all_repos():
	"""Daily scheduler task: sync all Helm repositories.

	Referenced by ``hooks.py`` via ``scheduler_events["daily"]``.
	"""
	repos = frappe.get_all(
		"Helm Repository",
		pluck="name",
	)

	for repo_name in repos:
		try:
			sync_token = secrets.token_hex(16)
			frappe.db.set_value("Helm Repository", repo_name, "status", "Syncing")
			frappe.db.set_value("Helm Repository", repo_name, "sync_token", sync_token)
			frappe.enqueue(
				"kubeport.tasks.helm_tasks.sync_repo_charts",
				repo_name=repo_name,
				sync_token=sync_token,
				queue="long",
				enqueue_after_commit=True,
			)
		except Exception as e:
			frappe.log_error(
				title=f"Daily Sync Failed: {repo_name}",
				message=str(e),
			)


# ---------------------------------------------------------------------------
# Release Tasks
# ---------------------------------------------------------------------------


def install_or_upgrade_release(release_name: str):
	"""Install or upgrade a Helm release on the target cluster.

	Uses ``helm upgrade --install`` for idempotency.
	"""
	release = frappe.db.get_value(
		"Helm Release",
		release_name,
		["status", "release_name", "chart", "chart_version", "namespace", "cluster", "values"],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_name}' was not found.")
	if release.get("status") != _DEPLOYABLE_WORKER_STATUS:
		frappe.logger("kubeport").info(
			"Skipping stale deploy worker for Helm Release '%s' because status is '%s'.",
			release_name,
			release.get("status"),
		)
		return

	chart_doc = frappe.get_doc("Helm Chart", release["chart"])

	try:
		chart_ref = chart_doc.get_chart_reference()
		version = release["chart_version"] or chart_doc.latest_version

		result = helm.install_or_upgrade(
			release_name=release["release_name"],
			chart_ref=chart_ref,
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
			values_yaml=release.get("values"),
			chart_version=version,
		)

		# Parse result — helm upgrade --install --output json returns the release object
		revision = 0
		status_detail = ""
		if isinstance(result, dict):
			revision = result.get("version", 0)
			info = result.get("info", {})
			if isinstance(info, dict):
				status_detail = info.get("status", "")

		doc_status = _map_helm_runtime_status(status_detail)

		_set_helm_release_fields(release_name, {
			"status": doc_status,
			"helm_revision": revision,
			"helm_status_detail": _truncate_status_detail(status_detail or "unknown"),
		})

		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release["release_name"],
				"status": doc_status,
			},
			doctype="Helm Release",
			docname=release_name,
		)

	except Exception as e:
		_set_helm_release_fields(release_name, {
			"status": "Failed",
			"helm_status_detail": _truncate_status_detail(str(e)),
		})
		frappe.log_error(
			title=f"Helm Install/Upgrade Failed: {release_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release.get("release_name", release_name),
				"status": "Failed",
			},
			doctype="Helm Release",
			docname=release_name,
		)


def uninstall_release(release_name: str):
	"""Uninstall a Helm release from the target cluster."""
	release = frappe.db.get_value(
		"Helm Release",
		release_name,
		["status", "release_name", "namespace", "cluster"],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_name}' was not found.")
	if release.get("status") != _UNINSTALLING_WORKER_STATUS:
		frappe.logger("kubeport").info(
			"Skipping stale uninstall worker for Helm Release '%s' because status is '%s'.",
			release_name,
			release.get("status"),
		)
		return

	try:
		helm.uninstall(
			release_name=release["release_name"],
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
		)

		_set_helm_release_fields(release_name, {
			"status": "Draft",
			"helm_revision": 0,
			"helm_status_detail": "",
		})

		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release["release_name"],
				"status": "Draft",
			},
			doctype="Helm Release",
			docname=release_name,
		)

	except Exception as e:
		_set_helm_release_fields(release_name, {
			"status": "Failed",
			"helm_status_detail": _truncate_status_detail(str(e)),
		})
		frappe.log_error(
			title=f"Helm Uninstall Failed: {release_name}",
			message=str(e),
		)
		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release.get("release_name", release_name),
				"status": "Failed",
			},
			doctype="Helm Release",
			docname=release_name,
		)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _set_helm_release_fields(release_name: str, values: dict[str, object]) -> None:
	for fieldname, value in values.items():
		frappe.db.set_value("Helm Release", release_name, fieldname, value)


def _map_helm_runtime_status(runtime_status: str) -> str:
	return "Deployed" if runtime_status == "deployed" else "Degraded"


def _truncate_status_detail(detail: str) -> str:
	return detail[:_HELM_STATUS_DETAIL_LIMIT]


def _repo_sync_token_matches(repo_name: str, sync_token: str) -> bool:
	current_token = frappe.db.get_value("Helm Repository", repo_name, "sync_token")
	if current_token == sync_token:
		return True

	frappe.logger("kubeport").info(
		"Skipping stale repo sync worker for Helm Repository '%s' because token '%s' "
		"no longer matches current token '%s'.",
		repo_name,
		sync_token,
		current_token,
	)
	return False


def _ensure_repo_registered(repo_doc) -> None:
	password = repo_doc.get_password("repo_password") if repo_doc.repo_password else None
	helm.repo_add(
		repo_doc.repo_name,
		repo_doc.repo_url,
		username=repo_doc.repo_username or None,
		password=password,
	)


def _sync_charts(repo_doc):
	"""Parse ``helm search repo`` output and upsert Helm Chart documents.

	Applies the ``include_patterns`` filter from the repository document and
	fully rebuilds the per-chart version inventory from the current Helm repo
	index, removing stale chart rows that no longer exist upstream.
	"""
	charts_data = helm.search_repo_with_options(
		repo_doc.repo_name,
		all_versions=True,
		timeout=600,
	)

	# Parse include patterns (comma-separated, with optional glob wildcards)
	patterns = _parse_include_patterns(repo_doc.include_patterns)
	chart_groups = _group_chart_inventory(charts_data, patterns)
	desired_chart_doc_names: set[str] = set()

	for chart_name, version_rows in chart_groups.items():
		chart_doc_name = f"{repo_doc.name}/{chart_name}"
		desired_chart_doc_names.add(chart_doc_name)
		latest_entry = version_rows[0]
		version_payload = [{
			"version": entry["version"],
			"app_version": entry["app_version"],
			"description": entry["description"],
		} for entry in version_rows]

		if frappe.db.exists("Helm Chart", chart_doc_name):
			chart_doc = frappe.get_doc("Helm Chart", chart_doc_name)
			previous_latest_version = chart_doc.latest_version or ""
			chart_doc.chart_name = chart_name
			chart_doc.repository = repo_doc.name
			chart_doc.latest_version = latest_entry["version"]
			chart_doc.latest_app_version = latest_entry["app_version"]
			chart_doc.description = latest_entry["description"]
			if previous_latest_version and previous_latest_version != latest_entry["version"]:
				chart_doc.default_values = ""
			chart_doc.set("versions", version_payload)
			chart_doc.save(ignore_permissions=True)
		else:
			new_chart = frappe.get_doc({
				"doctype": "Helm Chart",
				"chart_name": chart_name,
				"repository": repo_doc.name,
				"latest_version": latest_entry["version"],
				"latest_app_version": latest_entry["app_version"],
				"description": latest_entry["description"],
				"default_values": "",
				"versions": version_payload,
			})
			new_chart.insert(ignore_permissions=True)

	existing_chart_doc_names = set(frappe.get_all(
		"Helm Chart",
		filters={"repository": repo_doc.name},
		pluck="name",
	))
	for stale_chart_doc_name in existing_chart_doc_names - desired_chart_doc_names:
		frappe.delete_doc("Helm Chart", stale_chart_doc_name, ignore_permissions=True)



def _parse_include_patterns(patterns_text: str | None) -> list[str]:
	"""Parse the comma-separated include patterns into a list of globs.

	Returns an empty list if no patterns are configured (sync all).
	"""
	if not patterns_text or not patterns_text.strip():
		return []

	# Split by comma, strip whitespace, remove empty entries
	return [
		p.strip()
		for p in re.split(r"[,\n]", patterns_text)
		if p.strip()
	]


def _matches_any_pattern(chart_name: str, patterns: list[str]) -> bool:
	"""Check if a chart name matches any of the include patterns.

	Supports glob-style wildcards via ``fnmatch``.
	"""
	return any(fnmatch.fnmatch(chart_name, pattern) for pattern in patterns)


def _group_chart_inventory(
	charts_data: list[dict],
	patterns: list[str],
) -> dict[str, list[dict[str, str]]]:
	grouped: dict[str, list[dict[str, str]]] = defaultdict(list)

	for chart_entry in charts_data:
		full_name = str(chart_entry.get("name") or "")
		chart_name = full_name.split("/")[-1] if "/" in full_name else full_name
		chart_version = str(chart_entry.get("version") or "")
		if not chart_name or not chart_version:
			continue
		if patterns and not _matches_any_pattern(chart_name, patterns):
			continue

		grouped[chart_name].append({
			"version": chart_version,
			"app_version": str(chart_entry.get("app_version") or ""),
			"description": str(chart_entry.get("description") or ""),
		})

	for chart_name, version_rows in grouped.items():
		grouped[chart_name] = sorted(
			_version_rows_deduplicated(version_rows),
			key=lambda row: _version_sort_key(row["version"]),
			reverse=True,
		)

	return dict(grouped)


def _version_rows_deduplicated(version_rows: list[dict[str, str]]) -> list[dict[str, str]]:
	seen_versions: set[str] = set()
	deduplicated: list[dict[str, str]] = []

	for row in version_rows:
		version = row["version"]
		if version in seen_versions:
			continue
		seen_versions.add(version)
		deduplicated.append(row)

	return deduplicated


def _version_sort_key(version: str) -> tuple[tuple[int, object], ...]:
	parts = re.findall(r"\d+|[A-Za-z]+", version)
	if not parts:
		return ((1, version.lower()),)

	return tuple(
		(0, int(part)) if part.isdigit() else (1, part.lower())
		for part in parts
	)
