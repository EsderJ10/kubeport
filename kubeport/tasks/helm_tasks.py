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

import fnmatch
import re
import secrets
from collections import defaultdict

import frappe
import yaml

from kubeport.kubeport.doctype.helm_release.helm_release import (
	_get_site_image_digest_for_hash,
	bundled_mariadb_release_name,
	calculate_release_spec_hash,
	is_frappe_site_chart,
	prepare_release_values,
)
from kubeport.utils import helm, metrics
from kubeport.utils.release_health import ResourceHealth, classify_release_state

_HELM_STATUS_DETAIL_LIMIT = 500
_DEPLOYABLE_WORKER_STATUS = "In Progress"
_UNINSTALLING_WORKER_STATUS = "Uninstalling"

# Bitnami's MariaDB OCI chart.  Pinned for reproducibility — bumps go through
# code review since a Bitnami breaking change would silently break every
# bundled-MariaDB Frappe release on the next install/upgrade.  Verify any
# bump exists with: ``helm show chart oci://registry-1.docker.io/bitnamicharts/mariadb --version <X.Y.Z>``.
_BUNDLED_MARIADB_CHART_REF = "oci://registry-1.docker.io/bitnamicharts/mariadb"
_BUNDLED_MARIADB_CHART_VERSION = "25.1.1"

# ---------------------------------------------------------------------------
# Repository Tasks
# ---------------------------------------------------------------------------


def add_and_sync_repo(
	repo_name: str,
	sync_token: str,
	correlation_id: str | None = None,
):
	"""Register a Helm repo and trigger a chart index sync.

	Called automatically when a new Helm Repository document is created
	(via ``after_insert``).
	"""
	with metrics.correlation_scope(correlation_id):
		_add_and_sync_repo_impl(repo_name, sync_token)


def _add_and_sync_repo_impl(repo_name: str, sync_token: str):
	metrics.logger("kubeport.helm").info("worker enter add_and_sync_repo repo=%s", repo_name)
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
			after_commit=True,
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
			after_commit=True,
		)


def sync_repo_charts(
	repo_name: str,
	sync_token: str,
	correlation_id: str | None = None,
):
	"""Update a repo's chart index and sync new charts to the database.

	Called by the "Sync Charts" button and the daily scheduler.
	"""
	with metrics.correlation_scope(correlation_id):
		_sync_repo_charts_impl(repo_name, sync_token)


def _sync_repo_charts_impl(repo_name: str, sync_token: str):
	metrics.logger("kubeport.helm").info("worker enter sync_repo_charts repo=%s", repo_name)
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
			after_commit=True,
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
			after_commit=True,
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
			correlation_id = metrics.new_correlation_id()
			frappe.db.set_value("Helm Repository", repo_name, "status", "Syncing")
			frappe.db.set_value("Helm Repository", repo_name, "sync_token", sync_token)
			with metrics.correlation_scope(correlation_id):
				metrics.logger("kubeport.helm").info("enqueue sync_repo_charts (daily) repo=%s", repo_name)
			frappe.enqueue(
				"kubeport.tasks.helm_tasks.sync_repo_charts",
				repo_name=repo_name,
				sync_token=sync_token,
				correlation_id=correlation_id,
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


def install_or_upgrade_release(
	release_name: str,
	operation_token: str,
	correlation_id: str | None = None,
):
	"""Install or upgrade a Helm release on the target cluster.

	Uses ``helm upgrade --install`` for idempotency.  Re-checks
	``operation_token`` before each writeback so a superseded worker (rapid
	double-click on Deploy, mid-flight uninstall) never overwrites the newer
	operation's state.
	"""
	with metrics.correlation_scope(correlation_id):
		_install_or_upgrade_release_impl(release_name, operation_token)


def _install_or_upgrade_release_impl(release_name: str, operation_token: str):
	metrics.logger("kubeport.helm").info("worker enter install_or_upgrade_release release=%s", release_name)
	if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
		return

	release = frappe.db.get_value(
		"Helm Release",
		release_name,
		[
			"release_name",
			"chart",
			"chart_version",
			"namespace",
			"cluster",
			"values",
			"site_image",
			"ingress_enabled",
			"ingress_hostname",
			"ingress_class_name",
			"ingress_cluster_issuer",
			"use_external_database",
		],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_name}' was not found.")

	chart_doc = frappe.get_doc("Helm Chart", release["chart"])

	try:
		chart_ref = chart_doc.get_chart_reference()
		version = release["chart_version"] or chart_doc.latest_version
		values_yaml = prepare_release_values(
			release.get("values"),
			chart_doc,
			release.get("cluster"),
			release.get("site_image"),
			ingress_enabled=release.get("ingress_enabled"),
			ingress_hostname=release.get("ingress_hostname"),
			ingress_class_name=release.get("ingress_class_name"),
			ingress_cluster_issuer=release.get("ingress_cluster_issuer"),
			release_name=release["release_name"],
			use_external_database=release.get("use_external_database"),
		)

		# Provision the sibling MariaDB *before* the parent so the bench's
		# post-install configuration jobs can reach a database (or at least
		# resolve its Service and wait).  Idempotent: re-runs are no-ops.
		_ensure_bundled_mariadb_release(
			parent_release_name=release["release_name"],
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
			chart_doc=chart_doc,
			use_external_database=release.get("use_external_database"),
		)

		result = helm.install_or_upgrade(
			release_name=release["release_name"],
			chart_ref=chart_ref,
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
			values_yaml=values_yaml,
			chart_version=version,
		)

		# Parse result — helm upgrade --install --output json returns the release object
		revision = 0
		runtime_status = ""
		if isinstance(result, dict):
			revision = result.get("version", 0)
			info = result.get("info", {})
			if isinstance(info, dict):
				runtime_status = info.get("status", "")

		# Walker output combined with helm's runtime status decides Deployed
		# vs Degraded.  Errors during the walk surface as Degraded with a
		# clear reason — better than masking them as Deployed.
		walker_results, walker_error = _safe_walk(release_name)

		doc_status, status_detail = classify_release_state(
			runtime_status=runtime_status,
			walker_results=walker_results,
			walker_error=walker_error,
		)

		if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
			return

		fields: dict[str, object] = {
			"status": doc_status,
			"helm_revision": revision,
			"helm_status_detail": _truncate_status_detail(status_detail),
			"operation_type": "",
			"operation_started_at": None,
		}
		if doc_status in ("Deployed", "Degraded"):
			spec_hash = calculate_release_spec_hash(
				chart=release["chart"],
				chart_version=version,
				namespace=release["namespace"],
				release_name=release["release_name"],
				values_yaml=release.get("values"),
				site_image=release.get("site_image"),
				site_image_digest=_get_site_image_digest_for_hash(release.get("site_image")),
				ingress_enabled=release.get("ingress_enabled"),
				ingress_hostname=release.get("ingress_hostname"),
				ingress_class_name=release.get("ingress_class_name"),
				ingress_cluster_issuer=release.get("ingress_cluster_issuer"),
				use_external_database=release.get("use_external_database"),
			)
			fields.update(
				{
					"last_applied_chart_version": version,
					"desired_spec_hash": spec_hash,
					"last_applied_spec_hash": spec_hash,
					"pending_changes": 0,
				}
			)

		_set_helm_release_fields(release_name, fields)

		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release["release_name"],
				"status": doc_status,
			},
			doctype="Helm Release",
			docname=release_name,
			after_commit=True,
		)

	except Exception as e:
		if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
			return

		_set_helm_release_fields(
			release_name,
			{
				"status": "Failed",
				"helm_status_detail": _truncate_status_detail(str(e)),
				"operation_type": "",
				"operation_started_at": None,
			},
		)
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
			after_commit=True,
		)


def rollback_release(
	release_name: str,
	operation_token: str,
	target_revision: int,
	correlation_id: str | None = None,
):
	"""Roll back a Helm release to a previous revision on the target cluster."""
	with metrics.correlation_scope(correlation_id):
		_rollback_release_impl(release_name, operation_token, target_revision)


def _rollback_release_impl(release_name: str, operation_token: str, target_revision: int):
	metrics.logger("kubeport.helm").info(
		"worker enter rollback_release release=%s revision=%s", release_name, target_revision
	)
	if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
		return

	release = frappe.db.get_value(
		"Helm Release",
		release_name,
		[
			"release_name",
			"chart",
			"chart_version",
			"namespace",
			"cluster",
			"values",
			"ingress_enabled",
			"ingress_hostname",
			"ingress_class_name",
			"ingress_cluster_issuer",
			"use_external_database",
		],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_name}' was not found.")

	chart_doc = frappe.get_doc("Helm Chart", release["chart"])

	try:
		namespace = release["namespace"] or "default"
		result = helm.rollback(
			release_name=release["release_name"],
			revision=int(target_revision),
			namespace=namespace,
			cluster_name=release["cluster"],
		)

		revision = 0
		runtime_status = ""
		if isinstance(result, dict):
			revision = result.get("version", 0)
			info = result.get("info", {})
			if isinstance(info, dict):
				runtime_status = info.get("status", "")

		walker_results, walker_error = _safe_walk(release_name)
		doc_status, status_detail = classify_release_state(
			runtime_status=runtime_status,
			walker_results=walker_results,
			walker_error=walker_error,
		)

		if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
			return

		fields: dict[str, object] = {
			"status": doc_status,
			"helm_revision": revision,
			"helm_status_detail": _truncate_status_detail(status_detail),
			"operation_type": "",
			"operation_started_at": None,
		}

		if doc_status in ("Deployed", "Degraded"):
			values_yaml = _normalize_helm_values_output(
				helm.get_values(
					release_name=release["release_name"],
					namespace=namespace,
					cluster_name=release["cluster"],
				)
			)
			chart_version = _extract_chart_version_from_status(
				status_result=result,
				chart_doc=chart_doc,
				fallback=release["chart_version"] or chart_doc.latest_version,
			)
			spec_hash = calculate_release_spec_hash(
				chart=release["chart"],
				chart_version=chart_version,
				namespace=namespace,
				release_name=release["release_name"],
				values_yaml=values_yaml,
				ingress_enabled=release.get("ingress_enabled"),
				ingress_hostname=release.get("ingress_hostname"),
				ingress_class_name=release.get("ingress_class_name"),
				ingress_cluster_issuer=release.get("ingress_cluster_issuer"),
				use_external_database=release.get("use_external_database"),
			)
			fields.update(
				{
					"values": values_yaml,
					"chart_version": chart_version,
					"last_applied_chart_version": chart_version,
					"desired_spec_hash": spec_hash,
					"last_applied_spec_hash": spec_hash,
					"pending_changes": 0,
				}
			)

		_set_helm_release_fields(release_name, fields)

		frappe.publish_realtime(
			"helm_release_status_update",
			{
				"release_docname": release_name,
				"release_name": release["release_name"],
				"status": doc_status,
			},
			doctype="Helm Release",
			docname=release_name,
			after_commit=True,
		)

	except Exception as e:
		if not _release_operation_matches(release_name, operation_token, _DEPLOYABLE_WORKER_STATUS):
			return

		_set_helm_release_fields(
			release_name,
			{
				"status": "Failed",
				"helm_status_detail": _truncate_status_detail(str(e)),
				"operation_type": "",
				"operation_started_at": None,
			},
		)
		frappe.log_error(
			title=f"Helm Rollback Failed: {release_name}",
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
			after_commit=True,
		)


def uninstall_release(
	release_name: str,
	operation_token: str,
	correlation_id: str | None = None,
):
	"""Uninstall a Helm release from the target cluster."""
	with metrics.correlation_scope(correlation_id):
		_uninstall_release_impl(release_name, operation_token)


def _uninstall_release_impl(release_name: str, operation_token: str):
	metrics.logger("kubeport.helm").info("worker enter uninstall_release release=%s", release_name)
	if not _release_operation_matches(release_name, operation_token, _UNINSTALLING_WORKER_STATUS):
		return

	release = frappe.db.get_value(
		"Helm Release",
		release_name,
		["release_name", "namespace", "cluster"],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_name}' was not found.")

	try:
		helm.uninstall(
			release_name=release["release_name"],
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
		)

		# Cascade to the sibling MariaDB if there is one.  We don't gate on
		# use_external_database here: the field may have flipped between
		# deploy and uninstall, and a sibling that was provisioned must still
		# be cleaned up.  The helper is itself idempotent and 404-tolerant,
		# so it's safe to run unconditionally.
		_uninstall_bundled_mariadb_release(
			parent_release_name=release["release_name"],
			namespace=release["namespace"] or "default",
			cluster_name=release["cluster"],
		)

		_finalize_uninstall_success(release_name, release["release_name"], operation_token)

	except Exception as e:
		if not _release_operation_matches(release_name, operation_token, _UNINSTALLING_WORKER_STATUS):
			return

		if _is_release_not_found_error(e):
			_finalize_uninstall_success(release_name, release["release_name"], operation_token)
			return

		_set_helm_release_fields(
			release_name,
			{
				"status": "Failed",
				"helm_status_detail": _truncate_status_detail(str(e)),
				"operation_type": "",
				"operation_started_at": None,
			},
		)
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
			after_commit=True,
		)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _set_helm_release_fields(release_name: str, values: dict[str, object]) -> None:
	for fieldname, value in values.items():
		frappe.db.set_value("Helm Release", release_name, fieldname, value)


def _bundled_mariadb_values(parent_release_name: str) -> str:
	"""Return values YAML for the sibling Bitnami MariaDB release.

	``fullnameOverride`` is required so the rendered Service and Secret are
	named exactly ``<parent>-mariadb`` (Bitnami's default would prepend the
	sibling release name and produce ``<parent>-mariadb-mariadb``).  That
	canonical name is what the parent's ``dbHost`` and Kubeport's auto-wire
	both look for.
	"""
	sibling = bundled_mariadb_release_name(parent_release_name)
	return yaml.safe_dump(
		{
			"fullnameOverride": sibling,
			# Bitnami auto-generates a root password into the
			# ``<sibling>-mariadb`` Secret with key ``mariadb-root-password``.
			# Kubeport's auto-wire on the Frappe Site doc finds it by
			# Bitnami convention, so the operator never has to copy it.
			"auth": {},
		},
		default_flow_style=False,
		sort_keys=False,
	)


def _ensure_bundled_mariadb_release(
	parent_release_name: str,
	namespace: str,
	cluster_name: str,
	chart_doc: object,
	use_external_database: bool | int | None,
) -> None:
	"""Idempotently ensure the sibling Bitnami MariaDB release exists.

	No-op when the chart is not a Frappe site chart or the operator opted
	into external-DB topology.  ``helm upgrade --install`` is itself
	idempotent: re-running with the same values is a no-op, the StatefulSet
	keeps its PVC, no data is lost.
	"""
	if not is_frappe_site_chart(chart_doc):
		return
	if use_external_database:
		return

	sibling = bundled_mariadb_release_name(parent_release_name)
	values_yaml = _bundled_mariadb_values(parent_release_name)
	helm.install_or_upgrade(
		release_name=sibling,
		chart_ref=_BUNDLED_MARIADB_CHART_REF,
		namespace=namespace,
		cluster_name=cluster_name,
		values_yaml=values_yaml,
		chart_version=_BUNDLED_MARIADB_CHART_VERSION,
	)


def _uninstall_bundled_mariadb_release(
	parent_release_name: str,
	namespace: str,
	cluster_name: str,
) -> None:
	"""Cascade-uninstall the sibling.  Best-effort: a missing sibling is fine
	(operator may have removed it manually, or it was never bundled), and a
	non-404 failure is logged but not propagated — we don't block the parent's
	uninstall on sibling cleanup.  The PVC behind the StatefulSet is deleted
	as part of the helm uninstall.
	"""
	sibling = bundled_mariadb_release_name(parent_release_name)
	try:
		helm.uninstall(release_name=sibling, namespace=namespace, cluster_name=cluster_name)
	except Exception as exc:
		if helm.is_release_not_found_error(exc):
			return
		frappe.log_error(
			title=f"Bundled MariaDB cascade uninstall failed: {sibling}",
			message=str(exc),
		)


def _safe_walk(release_name: str) -> tuple[list[ResourceHealth], str | None]:
	"""Run the readiness walker, swallowing exceptions into a string error.

	Returns ``(results, None)`` on success and ``([], error_message)`` on
	failure.  Walker errors must not poison the helm-side outcome — the
	deploy succeeded, only the post-deploy readiness check failed.
	"""
	from kubeport.utils.release_health import walk

	try:
		return walk(release_name), None
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Readiness walker failed for Helm Release '%s': %s",
			release_name,
			e,
		)
		return [], str(e)


def _finalize_uninstall_success(
	release_docname: str,
	release_name: str,
	operation_token: str,
) -> None:
	if not _release_operation_matches(release_docname, operation_token, _UNINSTALLING_WORKER_STATUS):
		return

	_set_helm_release_fields(
		release_docname,
		{
			"status": "Draft",
			"helm_revision": 0,
			"helm_status_detail": "",
			"last_applied_chart_version": "",
			"last_applied_spec_hash": "",
			"pending_changes": 0,
			"operation_type": "",
			"operation_started_at": None,
		},
	)

	frappe.publish_realtime(
		"helm_release_status_update",
		{
			"release_docname": release_docname,
			"release_name": release_name,
			"status": "Draft",
		},
		doctype="Helm Release",
		docname=release_docname,
		after_commit=True,
	)


def _is_release_not_found_error(error: Exception) -> bool:
	message = str(error).lower()
	return "release: not found" in message or "release not found" in message


def _normalize_helm_values_output(values_yaml: str | None) -> str:
	if not values_yaml:
		return ""

	try:
		parsed = yaml.safe_load(values_yaml)
	except yaml.YAMLError:
		return values_yaml

	if parsed in (None, {}):
		return ""
	if not isinstance(parsed, dict):
		return values_yaml
	return yaml.safe_dump(parsed, default_flow_style=False, sort_keys=True)


def _extract_chart_version_from_status(
	status_result: dict,
	chart_doc,
	fallback: str | None = "",
) -> str:
	chart_obj = status_result.get("chart") if isinstance(status_result, dict) else None
	if isinstance(chart_obj, dict):
		metadata = chart_obj.get("metadata", {})
		if isinstance(metadata, dict) and metadata.get("version"):
			return str(metadata["version"])

	chart_text = str(chart_obj or status_result.get("chart_name") or "")
	chart_name = str(getattr(chart_doc, "chart_name", "") or "")
	prefix = f"{chart_name}-"
	if chart_name and chart_text.startswith(prefix):
		return chart_text[len(prefix) :]

	return str(fallback or "")


def _release_operation_matches(
	release_name: str,
	operation_token: str,
	expected_status: str,
) -> bool:
	"""Return True iff this worker's operation is still the row's current op.

	Mirrors ``_site_operation_matches`` from ``site_tasks.py``: the worker
	must re-read both the token AND the status before acting, because either
	can move out from under it (rapid double-click on Deploy → token rotates
	with status pinned at ``In Progress``; a destructive cancel → status
	moves while token may match).
	"""
	current = frappe.db.get_value(
		"Helm Release",
		release_name,
		["operation_token", "status"],
		as_dict=True,
	)
	if (
		current
		and current.get("operation_token") == operation_token
		and current.get("status") == expected_status
	):
		return True

	frappe.logger("kubeport").info(
		"Skipping stale helm worker for Helm Release '%s' because token/status "
		"no longer match the queued operation.",
		release_name,
	)
	return False


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
		version_payload = [
			{
				"version": entry["version"],
				"app_version": entry["app_version"],
				"description": entry["description"],
			}
			for entry in version_rows
		]

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
			new_chart = frappe.get_doc(
				{
					"doctype": "Helm Chart",
					"chart_name": chart_name,
					"repository": repo_doc.name,
					"latest_version": latest_entry["version"],
					"latest_app_version": latest_entry["app_version"],
					"description": latest_entry["description"],
					"default_values": "",
					"versions": version_payload,
				}
			)
			new_chart.insert(ignore_permissions=True)

	existing_chart_doc_names = set(
		frappe.get_all(
			"Helm Chart",
			filters={"repository": repo_doc.name},
			pluck="name",
		)
	)
	for stale_chart_doc_name in existing_chart_doc_names - desired_chart_doc_names:
		frappe.delete_doc("Helm Chart", stale_chart_doc_name, ignore_permissions=True)


def _parse_include_patterns(patterns_text: str | None) -> list[str]:
	"""Parse the comma-separated include patterns into a list of globs.

	Returns an empty list if no patterns are configured (sync all).
	"""
	if not patterns_text or not patterns_text.strip():
		return []

	# Split by comma, strip whitespace, remove empty entries
	return [p.strip() for p in re.split(r"[,\n]", patterns_text) if p.strip()]


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

		grouped[chart_name].append(
			{
				"version": chart_version,
				"app_version": str(chart_entry.get("app_version") or ""),
				"description": str(chart_entry.get("description") or ""),
			}
		)

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

	return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in parts)
