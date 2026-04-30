"""
Frappe Site Background Tasks

Submits Kubernetes Jobs that run ``bench`` lifecycle commands
(``new-site``, ``drop-site``, ``migrate``) inside the bench's workload
container.  Every Job follows the same shape: clone the bench's reference
pod spec to inherit image / volume mounts / pod-level fields, run a single
bench command, exit.  Direct Job submission via the K8s API keeps site
lifecycle independent of Helm upgrade flow.

Design rationale (direct Job submission, not Helm upgrade):
- Helm values represent steady-state deployment config; one-off site
  operations must not contaminate them.
- Toggling chart-bundled Jobs on/off across upgrades is fragile and risks
  re-firing on subsequent upgrades.
- Direct Job submission reuses existing ``apply_resource()`` plumbing,
  gives us a K8s Job artifact with inspectable logs, and auto-cleans
  via ``ttlSecondsAfterFinished``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import frappe
from kubernetes import client

from kubeport.utils.discovery import (
	FRAPPE_BENCH_SITES_PATH,
	_select_site_discovery_pod,
)
from kubeport.utils.k8s_client import get_k8s_api_client
from kubeport.utils.k8s_resources import apply_resource

_STATUS_DETAIL_LIMIT = 500
_JOB_TTL_SECONDS = 7200  # 2 h — enough for reconciliation (5-min cadence) to read result
# Upper bound on how long a single site-creation Job can run.  Without this the
# Job can hang indefinitely (ImagePullBackOff, DB unreachable, etc.) and
# reconciliation's succeeded/failed polling never fires — the site sits in
# "In Progress" forever.  30 min comfortably covers realistic `bench new-site`
# runs including ERPNext app install on modest hardware while still surfacing
# genuine hangs before a human notices.
_JOB_ACTIVE_DEADLINE_SECONDS = 1800
# Label key written on every Job and creds Secret we create.  Used by the
# orphan-sweep in reconciliation to find resources the DocType layer has lost
# track of (e.g. worker hard-killed between Job apply and db_set).
SITE_DOC_LABEL = "kubeport.io/frappe-site"
SITE_BACKUP_DOC_LABEL = "kubeport.io/frappe-site-backup"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "kubeport"
BACKUP_PVC_NAME = "kubeport-backups"
BACKUP_MOUNT_PATH = "/mnt/kubeport-backups"
# Duplicated from frappe_site.py on purpose: if a malformed value ever reaches
# the worker (direct DB write, schema import, etc.), we must not interpolate
# shell metacharacters into the bench command string.
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class SiteOpConfig:
	"""Per-operation configuration for ``_run_site_op``."""

	op_kind: str
	expected_status: str
	container_label: str
	failure_log_title: str
	failure_detail_prefix: str


_CREATE_OP_CONFIG = SiteOpConfig(
	op_kind="create",
	expected_status="In Progress",
	container_label="create-site",
	failure_log_title="Frappe Site Job Submission Failed",
	failure_detail_prefix="Job submission failed",
)


_DELETE_OP_CONFIG = SiteOpConfig(
	op_kind="delete",
	expected_status="Deleting",
	container_label="delete-site",
	failure_log_title="Frappe Site Drop Submission Failed",
	failure_detail_prefix="Drop-site Job submission failed",
)


_MIGRATE_OP_CONFIG = SiteOpConfig(
	op_kind="migrate",
	expected_status="Migrating",
	container_label="migrate-site",
	failure_log_title="Frappe Site Migrate Submission Failed",
	failure_detail_prefix="Migrate Job submission failed",
)


_BACKUP_OP_CONFIG = SiteOpConfig(
	op_kind="backup",
	expected_status="In Progress",
	container_label="backup-site",
	failure_log_title="Frappe Site Backup Submission Failed",
	failure_detail_prefix="Backup Job submission failed",
)


_RESTORE_OP_CONFIG = SiteOpConfig(
	op_kind="restore",
	expected_status="Migrating",
	container_label="restore-site",
	failure_log_title="Frappe Site Restore Submission Failed",
	failure_detail_prefix="Restore Job submission failed",
)


def _create_command(doc: Any) -> str:
	install_apps = _parse_install_apps(doc.install_apps)
	return _bench_new_site_command(doc.site_name, install_apps, bool(doc.force_create))


def _create_env(doc: Any, creds_secret_name: str | None) -> list[dict[str, Any]]:
	if creds_secret_name is None:
		raise RuntimeError("create_site_task requires a creds Secret name.")

	db_root_secret = doc.db_root_secret or ""
	db_root_secret_key = doc.db_root_secret_key or "mariadb-root-password"
	needs_db_root_in_creds = not db_root_secret
	return _build_env(
		site_name=doc.site_name,
		db_type=doc.db_type or "mariadb",
		creds_secret_name=creds_secret_name,
		db_root_in_creds=needs_db_root_in_creds,
		db_root_secret=db_root_secret,
		db_root_secret_key=db_root_secret_key,
	)


def _create_creds_secret(doc: Any, job_name: str) -> dict[str, Any]:
	admin_password = doc.get_password("admin_password") or ""
	db_root_secret = doc.db_root_secret or ""
	needs_db_root_in_creds = not db_root_secret
	db_root_password = doc.get_password("db_root_password") or "" if needs_db_root_in_creds else None
	return _build_creds_secret_manifest(
		secret_name=f"{job_name}-creds",
		namespace=doc.namespace or "default",
		site_docname=doc.name,
		admin_password=admin_password,
		db_root_password=db_root_password,
	)


def create_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench new-site``."""
	_run_site_op(
		site_docname=site_docname,
		operation_token=operation_token,
		config=_CREATE_OP_CONFIG,
		build_command=_create_command,
		build_env=_create_env,
		build_creds_secret=_create_creds_secret,
	)


def cancel_site_task(cluster: str, namespace: str, job_name: str):
	"""Background task: best-effort delete the site-creation Job and its creds Secret.

	Called from ``FrappeSite.cancel_site`` and ``FrappeSite.on_trash``.  The
	doc's status has already been rotated before enqueue, so any outcome
	here (success, 404, or error) is purely about cluster cleanup.

	Deleting the Job with ``propagation_policy="Background"`` cascades to
	owner-referenced objects — including the creds Secret we create in
	``create_site_task`` — so the explicit Secret delete below is only a
	backstop for the narrow window where the Job never existed (e.g. Secret
	got applied, Job submission failed) or ownerReferences were not attached.
	"""
	from kubernetes.client.rest import ApiException

	try:
		api_client = get_k8s_api_client(cluster)
	except Exception as e:
		frappe.log_error(
			title=f"Frappe Site Cancel: failed to build K8s client for '{job_name}'",
			message=str(e),
		)
		return

	try:
		batch_v1 = client.BatchV1Api(api_client=api_client)
		batch_v1.delete_namespaced_job(
			name=job_name,
			namespace=namespace,
			propagation_policy="Background",
		)
	except ApiException as e:
		if e.status != 404:
			frappe.log_error(
				title=f"Frappe Site Cancel: failed to delete Job '{job_name}'",
				message=str(e),
			)
	except Exception as e:
		frappe.log_error(
			title=f"Frappe Site Cancel: failed to delete Job '{job_name}'",
			message=str(e),
		)

	# Backstop for creds Secret cleanup.  When ownerReferences are in place,
	# K8s GC already handles this; but this call guarantees cleanup even in
	# failure-window cases where the Secret might otherwise linger.
	_best_effort_delete_secret(api_client, f"{job_name}-creds", namespace)


def _delete_command(doc: Any) -> str:
	return _bench_drop_site_command(doc.site_name)


def _delete_env(doc: Any, creds_secret_name: str | None) -> list[dict[str, Any]]:
	db_root_secret = doc.db_root_secret or ""
	db_root_secret_key = doc.db_root_secret_key or "mariadb-root-password"
	needs_db_root_in_creds = not db_root_secret
	return _build_drop_env(
		site_name=doc.site_name,
		db_type=doc.db_type or "mariadb",
		creds_secret_name=creds_secret_name,
		db_root_in_creds=needs_db_root_in_creds,
		db_root_secret=db_root_secret,
		db_root_secret_key=db_root_secret_key,
	)


def _delete_creds_secret(doc: Any, job_name: str) -> dict[str, Any] | None:
	if doc.db_root_secret:
		return None

	db_root_password = doc.get_password("db_root_password") or ""
	return _build_drop_creds_secret_manifest(
		secret_name=f"{job_name}-creds",
		namespace=doc.namespace or "default",
		site_docname=doc.name,
		db_root_password=db_root_password,
	)


def delete_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench drop-site``."""
	_run_site_op(
		site_docname=site_docname,
		operation_token=operation_token,
		config=_DELETE_OP_CONFIG,
		build_command=_delete_command,
		build_env=_delete_env,
		build_creds_secret=_delete_creds_secret,
	)


def _migrate_command(doc: Any) -> str:
	return _bench_migrate_command(doc.site_name)


def _migrate_env(doc: Any, creds_secret_name: str | None) -> list[dict[str, Any]]:
	return [{"name": "SITE_NAME", "value": doc.site_name}]


def _backup_env(site_name: str, storage_path: str) -> list[dict[str, Any]]:
	return [
		{"name": "SITE_NAME", "value": site_name},
		{"name": "BACKUP_ARCHIVE_PATH", "value": storage_path},
		{"name": "BACKUP_TARGET_DIR", "value": storage_path.rsplit("/", 1)[0]},
	]


def _restore_env(site_name: str, storage_path: str) -> list[dict[str, Any]]:
	return [
		{"name": "SITE_NAME", "value": site_name},
		{"name": "BACKUP_ARCHIVE_PATH", "value": storage_path},
	]


def _bench_backup_command() -> str:
	return r"""
set -euo pipefail
backup_dir="sites/$SITE_NAME/private/backups"
mkdir -p "$backup_dir" "$BACKUP_TARGET_DIR"
before="$(mktemp)"
after="$(mktemp)"
find "$backup_dir" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort > "$before" || true
bench --site "$SITE_NAME" backup --with-files
find "$backup_dir" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort > "$after"
new_files="$(comm -13 "$before" "$after" || true)"
if [ -z "$new_files" ]; then
	echo "bench backup did not create any files in $backup_dir"
	exit 1
fi
printf '%s\n' "$new_files" > /tmp/kubeport-backup-files.txt
tar -C "$backup_dir" -czf "$BACKUP_ARCHIVE_PATH" -T /tmp/kubeport-backup-files.txt
size="$(stat -c '%s' "$BACKUP_ARCHIVE_PATH")"
first_file="$(head -n 1 /tmp/kubeport-backup-files.txt)"
printf '%s' "$size" > "$BACKUP_ARCHIVE_PATH.size"
printf '%s' "$first_file" > "$BACKUP_ARCHIVE_PATH.name"
echo "KUBEPORT_BACKUP_ARCHIVE=$BACKUP_ARCHIVE_PATH"
echo "KUBEPORT_BACKUP_SIZE=$size"
echo "KUBEPORT_BENCH_ARCHIVE=$first_file"
""".strip()


def _bench_restore_command() -> str:
	return r"""
set -euo pipefail
restore_dir="$(mktemp -d)"
tar -xzf "$BACKUP_ARCHIVE_PATH" -C "$restore_dir"
db_file="$(find "$restore_dir" -maxdepth 1 -type f -name '*database.sql.gz' | sort | head -n 1)"
if [ -z "$db_file" ]; then
	echo "Backup archive does not contain a database.sql.gz file"
	exit 1
fi
public_file="$(find "$restore_dir" -maxdepth 1 -type f -name '*public-files.tar' | sort | head -n 1)"
private_file="$(find "$restore_dir" -maxdepth 1 -type f -name '*private-files.tar' | sort | head -n 1)"
cmd=(bench --site "$SITE_NAME" restore "$db_file" --force)
if [ -n "$public_file" ]; then
	cmd+=(--with-public-files "$public_file")
fi
if [ -n "$private_file" ]; then
	cmd+=(--with-private-files "$private_file")
fi
"${cmd[@]}"
echo "KUBEPORT_RESTORE_ARCHIVE=$BACKUP_ARCHIVE_PATH"
""".strip()


def _prepare_backup_ref_spec(
	doc: Any,
	release: Any,
	namespace: str,
	api_client: client.ApiClient,
	ref_spec: dict[str, Any],
) -> None:
	_ensure_backup_pvc(api_client, release.cluster, namespace)
	volumes = ref_spec.setdefault("volumes", [])
	mounts = ref_spec.setdefault("volume_mounts", [])
	if not any(volume.get("name") == "kubeport-backups" for volume in volumes):
		volumes.append({
			"name": "kubeport-backups",
			"persistentVolumeClaim": {"claimName": BACKUP_PVC_NAME},
		})
	if not any(mount.get("name") == "kubeport-backups" for mount in mounts):
		mounts.append({"name": "kubeport-backups", "mountPath": BACKUP_MOUNT_PATH})


def _ensure_backup_pvc(api_client: client.ApiClient, cluster_name: str, namespace: str) -> None:
	cluster = frappe.get_doc("Kubernetes Cluster", cluster_name)
	manifest: dict[str, Any] = {
		"apiVersion": "v1",
		"kind": "PersistentVolumeClaim",
		"metadata": {
			"name": BACKUP_PVC_NAME,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
			},
		},
		"spec": {
			"accessModes": ["ReadWriteMany"],
			"resources": {"requests": {"storage": "10Gi"}},
		},
	}
	if getattr(cluster, "backup_storage_class", None):
		manifest["spec"]["storageClassName"] = cluster.backup_storage_class
	apply_resource(api_client, manifest, namespace)


def _backup_storage_path(cluster: str, namespace: str, site_name: str, backup_name: str) -> str:
	return "/".join([
		BACKUP_MOUNT_PATH,
		_safe_path_segment(cluster),
		_safe_path_segment(namespace),
		_safe_path_segment(site_name),
		f"{_safe_path_segment(backup_name)}.tar.gz",
	])


def _safe_path_segment(value: str) -> str:
	sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", value or "unknown")
	sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
	return sanitized or "unknown"


def _archive_delete_job_name(storage_path: str) -> str:
	slug = _safe_path_segment(storage_path.rsplit("/", 1)[-1]).lower()
	return f"ks-delete-backup-{slug[:30]}"


def migrate_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench migrate``."""
	_run_site_op(
		site_docname=site_docname,
		operation_token=operation_token,
		config=_MIGRATE_OP_CONFIG,
		build_command=_migrate_command,
		build_env=_migrate_env,
		build_creds_secret=None,
	)


def backup_site_task(site_docname: str, backup_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench backup``."""
	if not _backup_operation_matches(backup_docname, operation_token, ("Pending",)):
		return

	backup = frappe.get_doc("Frappe Site Backup", backup_docname)
	storage_path = backup.storage_path or _backup_storage_path(
		cluster=backup.cluster,
		namespace=backup.namespace or "default",
		site_name=backup.site_name,
		backup_name=backup.backup_name,
	)
	backup.db_set("storage_path", storage_path)

	def _record_backup_job(doc: Any, release: Any, job_name: str, token: str, namespace: str) -> bool:
		if not _backup_operation_matches(backup_docname, token, ("Pending",)):
			return False
		now = frappe.utils.now_datetime()
		frappe.db.set_value("Frappe Site Backup", backup_docname, {
			"status": "In Progress",
			"started_at": now,
			"operation_started_at": now,
			"operation_job_name": job_name,
			"operation_job_token": token,
			"status_detail": "",
		})
		frappe.publish_realtime(
			"frappe_site_backup_status_update",
			{"site_docname": site_docname, "backup_docname": backup_docname, "status": "In Progress"},
			doctype="Frappe Site Backup",
			docname=backup_docname,
		)
		return True

	def _fail_backup_submission(doc: Any, error: Exception) -> None:
		_detail = _truncate(f"Backup Job submission failed: {error}")
		doc.db_set("status", "Active")
		doc.db_set("status_detail", _detail)
		_fail_backup_row(backup_docname, operation_token, _detail)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Active"},
			doctype="Frappe Site",
			docname=site_docname,
		)

	_run_site_op(
		site_docname=site_docname,
		operation_token=operation_token,
		config=_BACKUP_OP_CONFIG,
		build_command=lambda doc: _bench_backup_command(),
		build_env=lambda doc, secret: _backup_env(doc.site_name, storage_path),
		build_creds_secret=None,
		prepare_ref_spec=_prepare_backup_ref_spec,
		record_job=_record_backup_job,
		handle_submission_failure=_fail_backup_submission,
		extra_job_labels={SITE_BACKUP_DOC_LABEL: _safe_label_value(backup_docname)},
	)


def restore_site_task(site_docname: str, backup_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench restore``."""
	if not _backup_operation_matches(backup_docname, operation_token, ("Restoring",)):
		return

	backup = frappe.get_doc("Frappe Site Backup", backup_docname)
	if not backup.storage_path:
		_fail_restore_submission(site_docname, backup_docname, operation_token, "Backup storage path is empty.")
		return

	def _record_restore_job(doc: Any, release: Any, job_name: str, token: str, namespace: str) -> bool:
		if not _backup_operation_matches(backup_docname, token, ("Restoring",)):
			return False
		frappe.db.set_value("Frappe Site Backup", backup_docname, {
			"operation_job_name": job_name,
			"operation_job_token": token,
			"operation_started_at": frappe.utils.now_datetime(),
			"status_detail": "",
		})
		frappe.publish_realtime(
			"frappe_site_backup_status_update",
			{"site_docname": site_docname, "backup_docname": backup_docname, "status": "Restoring"},
			doctype="Frappe Site Backup",
			docname=backup_docname,
		)
		return True

	def _fail_restore_submit(doc: Any, error: Exception) -> None:
		_fail_restore_submission(site_docname, backup_docname, operation_token, f"Restore Job submission failed: {error}")

	_run_site_op(
		site_docname=site_docname,
		operation_token=operation_token,
		config=_RESTORE_OP_CONFIG,
		build_command=lambda doc: _bench_restore_command(),
		build_env=lambda doc, secret: _restore_env(doc.site_name, backup.storage_path),
		build_creds_secret=None,
		prepare_ref_spec=_prepare_backup_ref_spec,
		record_job=_record_restore_job,
		handle_submission_failure=_fail_restore_submit,
		extra_job_labels={SITE_BACKUP_DOC_LABEL: _safe_label_value(backup_docname)},
	)


def delete_backup_archive_task(cluster: str, namespace: str, release_name: str, storage_path: str):
	"""Best-effort archive delete for an Available backup row being trashed."""
	if not storage_path:
		return

	try:
		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)
		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release_name,
		)
		ref_spec = _clone_reference_pod_spec(api_client, ref_pod)
		_prepare_backup_ref_spec(SimpleNamespace(), SimpleNamespace(cluster=cluster), namespace, api_client, ref_spec)
		job_name = _archive_delete_job_name(storage_path)
		job_manifest = _build_op_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=f"backup-archive/{job_name}",
			operation_label="delete-backup",
			container_command='rm -f "$BACKUP_ARCHIVE_PATH" "$BACKUP_ARCHIVE_PATH.size" "$BACKUP_ARCHIVE_PATH.name"',
			container_env=[{"name": "BACKUP_ARCHIVE_PATH", "value": storage_path}],
			ref_spec=ref_spec,
			extra_labels={SITE_BACKUP_DOC_LABEL: _safe_label_value(job_name)},
		)
		apply_resource(api_client, job_manifest, namespace)
	except Exception as e:
		frappe.log_error(
			title="Frappe Site Backup Archive Delete Failed",
			message=str(e),
		)


def _attach_creds_secret_owner_ref(
	api_client: "client.ApiClient",
	creds_secret_manifest: dict[str, Any],
	job_name: str,
	namespace: str,
) -> None:
	"""Re-apply a creds Secret with an ownerReference pointing at the Job."""
	try:
		batch_v1 = client.BatchV1Api(api_client=api_client)
		job_read = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
		job_uid = getattr(getattr(job_read, "metadata", None), "uid", None)
		if not job_uid:
			return
		creds_secret_manifest["metadata"]["ownerReferences"] = [{
			"apiVersion": "batch/v1",
			"kind": "Job",
			"name": job_name,
			"uid": job_uid,
			"controller": True,
			"blockOwnerDeletion": True,
		}]
		apply_resource(api_client, creds_secret_manifest, namespace)
	except Exception as owner_err:
		frappe.logger("kubeport").warning(
			"Could not attach ownerReference for creds Secret '%s': %s",
			creds_secret_manifest.get("metadata", {}).get("name", "?"),
			owner_err,
		)


def _run_site_op(
	site_docname: str,
	operation_token: str,
	config: SiteOpConfig,
	build_command: Callable[[Any], str],
	build_env: Callable[[Any, str | None], list[dict[str, Any]]],
	build_creds_secret: Callable[[Any, str], dict[str, Any] | None] | None = None,
	prepare_ref_spec: Callable[[Any, Any, str, client.ApiClient, dict[str, Any]], None] | None = None,
	record_job: Callable[[Any, Any, str, str, str], bool] | None = None,
	handle_submission_failure: Callable[[Any, Exception], None] | None = None,
	extra_job_labels: dict[str, str] | None = None,
) -> None:
	"""Run a one-shot bench operation Job under the shared lifecycle scaffolding."""
	if not _site_operation_matches(site_docname, operation_token, config.expected_status):
		return

	doc = frappe.get_doc("Frappe Site", site_docname)
	creds_secret_name: str | None = None
	creds_secret_manifest: dict[str, Any] | None = None
	job_name_for_cleanup: str | None = None
	job_applied = False
	namespace = ""
	api_client = None

	try:
		release = frappe.get_doc("Helm Release", doc.bench_release)
		cluster = release.cluster
		namespace = release.namespace or "default"
		release_name = release.release_name
		doc.namespace = namespace

		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)

		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release_name,
		)
		ref_spec = _clone_reference_pod_spec(api_client, ref_pod)
		if prepare_ref_spec is not None:
			prepare_ref_spec(doc, release, namespace, api_client, ref_spec)

		job_name = _job_name(doc.site_name, operation_token)
		job_name_for_cleanup = job_name

		if build_creds_secret is not None:
			creds_secret_manifest = build_creds_secret(doc, job_name)
			if creds_secret_manifest is not None:
				creds_secret_name = creds_secret_manifest["metadata"]["name"]
				apply_resource(api_client, creds_secret_manifest, namespace)

		bench_cmd = build_command(doc)
		container_env = build_env(doc, creds_secret_name)
		job_manifest = _build_op_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=site_docname,
			operation_label=config.container_label,
			container_command=bench_cmd,
			container_env=container_env,
			ref_spec=ref_spec,
			extra_labels=extra_job_labels,
		)

		apply_resource(api_client, job_manifest, namespace)
		job_applied = True

		if creds_secret_manifest is not None:
			_attach_creds_secret_owner_ref(
				api_client=api_client,
				creds_secret_manifest=creds_secret_manifest,
				job_name=job_name,
				namespace=namespace,
			)

		if not _site_operation_matches(site_docname, operation_token, config.expected_status):
			_best_effort_delete_job(api_client, job_name, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)
			return

		if record_job is None:
			doc.db_set("operation_job_name", job_name)
			doc.db_set("operation_job_token", operation_token)
			frappe.publish_realtime(
				"frappe_site_status_update",
				{"site_docname": site_docname, "status": config.expected_status, "job_name": job_name},
				doctype="Frappe Site",
				docname=site_docname,
			)
		elif not record_job(doc, release, job_name, operation_token, namespace):
			_best_effort_delete_job(api_client, job_name, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)

	except Exception as e:
		if api_client is not None and namespace:
			if job_applied and job_name_for_cleanup:
				_best_effort_delete_job(api_client, job_name_for_cleanup, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)

		if not _site_operation_matches(site_docname, operation_token, config.expected_status):
			return

		if handle_submission_failure is not None:
			handle_submission_failure(doc, e)
		else:
			doc.db_set("status", "Failed")
			doc.db_set("status_detail", _truncate(f"{config.failure_detail_prefix}: {e}"))
		frappe.log_error(
			title=f"{config.failure_log_title}: {site_docname}",
			message=str(e),
		)
		if handle_submission_failure is None:
			frappe.publish_realtime(
				"frappe_site_status_update",
				{"site_docname": site_docname, "status": "Failed"},
				doctype="Frappe Site",
				docname=site_docname,
			)


# ---------------------------------------------------------------------------
# Job manifest builders
# ---------------------------------------------------------------------------

def _build_op_job_manifest(
	job_name: str,
	namespace: str,
	site_docname: str,
	operation_label: str,
	container_command: str,
	container_env: list[dict[str, Any]],
	ref_spec: dict[str, Any],
	extra_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
	"""Build a Job manifest for any single-shot bench operation.

	``operation_label`` becomes the container name (e.g. ``create-site``,
	``delete-site``, ``migrate-site``) and is purely cosmetic — Job identity
	for reconciliation/sweep purposes is the ``SITE_DOC_LABEL`` on the Job
	itself, not the container name.
	"""
	# Merge reference-pod env with our op-specific env. Our keys win on
	# collision (explicit values override bench-chart defaults like SITE_NAME).
	env = _merge_env(ref_spec.get("container_env") or [], container_env)

	container: dict[str, Any] = {
		"name": operation_label,
		"image": ref_spec["image"],
		"command": ["bash", "-c"],
		"args": [container_command],
		"env": env,
		"volumeMounts": ref_spec.get("volume_mounts") or [],
	}
	if ref_spec.get("container_env_from"):
		container["envFrom"] = ref_spec["container_env_from"]
	if ref_spec.get("container_resources"):
		container["resources"] = ref_spec["container_resources"]
	if ref_spec.get("container_security_context"):
		container["securityContext"] = ref_spec["container_security_context"]

	pod_spec: dict[str, Any] = {
		"restartPolicy": "Never",
		"containers": [container],
		"volumes": ref_spec.get("volumes") or [],
	}
	pod_spec.update(ref_spec.get("pod_level") or {})

	labels = {
		MANAGED_BY_LABEL: MANAGED_BY_VALUE,
		SITE_DOC_LABEL: _safe_label_value(site_docname),
	}
	if extra_labels:
		labels.update(extra_labels)

	return {
		"apiVersion": "batch/v1",
		"kind": "Job",
		"metadata": {
			"name": job_name,
			"namespace": namespace,
			"labels": labels,
		},
		"spec": {
			"backoffLimit": 0,
			"ttlSecondsAfterFinished": _JOB_TTL_SECONDS,
			"activeDeadlineSeconds": _JOB_ACTIVE_DEADLINE_SECONDS,
			"template": {"spec": pod_spec},
		},
	}


def _merge_env(
	base: list[dict[str, Any]],
	overrides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	override_names = {entry.get("name") for entry in overrides if entry.get("name")}
	merged = [entry for entry in base if entry.get("name") not in override_names]
	merged.extend(overrides)
	return merged


def _bench_new_site_command(site_name: str, install_apps: list[str], force: bool) -> str:
	"""Build the ``bench new-site`` shell command string."""
	parts = [
		"bench",
		"new-site",
		'"$SITE_NAME"',
		"--no-mariadb-socket",
		'--db-type="$DB_TYPE"',
		'--mariadb-root-username="$DB_ROOT_USER"',
		'--mariadb-root-password="$DB_ROOT_PASSWORD"',
		'--admin-password="$ADMIN_PASSWORD"',
	]
	for app in install_apps:
		parts.append(f'--install-app="{app}"')
	if force:
		parts.append("--force")
	return " ".join(parts)


def _bench_drop_site_command(site_name: str) -> str:
	"""Build the ``bench drop-site`` shell command string.

	``--no-backup`` keeps the bench PVC clean (we don't have a backup-export
	story yet, so leaving an archived dump on the PVC is just wasted space
	with no way to retrieve it).  ``--force`` skips the interactive confirm
	since this Job runs non-interactively.
	"""
	return " ".join([
		"bench",
		"drop-site",
		'"$SITE_NAME"',
		'--root-login="$DB_ROOT_USER"',
		'--root-password="$DB_ROOT_PASSWORD"',
		"--no-backup",
		"--force",
	])


def _bench_migrate_command(site_name: str) -> str:
	"""Build the ``bench --site <name> migrate`` shell command string.

	Site name is passed via the validated ``$SITE_NAME`` env var, not
	interpolated into the string, so a malformed value cannot inject shell
	metacharacters.
	"""
	return 'bench --site "$SITE_NAME" migrate'


def _build_env(
	site_name: str,
	db_type: str,
	creds_secret_name: str,
	db_root_in_creds: bool,
	db_root_secret: str,
	db_root_secret_key: str,
) -> list[dict[str, Any]]:
	"""Build the Job container env list.

	Credentials are **never** materialized as plaintext env values — both
	``ADMIN_PASSWORD`` and ``DB_ROOT_PASSWORD`` flow in via ``secretKeyRef``.
	The DB root password can come from two sources: the user-supplied
	``db_root_secret`` (preferred), or our own per-Job creds Secret when the
	user chose the plaintext field on the DocType.
	"""
	db_root_user = "root"

	env: list[dict[str, Any]] = [
		{"name": "SITE_NAME", "value": site_name},
		{"name": "DB_TYPE", "value": db_type},
		{"name": "DB_ROOT_USER", "value": db_root_user},
		{
			"name": "ADMIN_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": creds_secret_name,
					"key": "ADMIN_PASSWORD",
				}
			},
		},
	]

	if db_root_in_creds:
		env.append({
			"name": "DB_ROOT_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": creds_secret_name,
					"key": "DB_ROOT_PASSWORD",
				}
			},
		})
	else:
		env.append({
			"name": "DB_ROOT_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": db_root_secret,
					"key": db_root_secret_key,
				}
			},
		})

	return env


def _build_drop_env(
	site_name: str,
	db_type: str,
	creds_secret_name: str | None,
	db_root_in_creds: bool,
	db_root_secret: str,
	db_root_secret_key: str,
) -> list[dict[str, Any]]:
	"""Build the env list for a ``bench drop-site`` Job.

	Drop-site only needs DB root credentials (no admin password).  The DB
	root password flows in via ``secretKeyRef`` from either our per-Job
	creds Secret (when the user chose the plaintext field on the DocType)
	or the user-supplied ``db_root_secret``.
	"""
	db_root_user = "root"

	env: list[dict[str, Any]] = [
		{"name": "SITE_NAME", "value": site_name},
		{"name": "DB_TYPE", "value": db_type},
		{"name": "DB_ROOT_USER", "value": db_root_user},
	]

	if db_root_in_creds:
		if not creds_secret_name:
			raise ValueError("creds_secret_name is required when db_root_in_creds is True.")
		env.append({
			"name": "DB_ROOT_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": creds_secret_name,
					"key": "DB_ROOT_PASSWORD",
				}
			},
		})
	else:
		env.append({
			"name": "DB_ROOT_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": db_root_secret,
					"key": db_root_secret_key,
				}
			},
		})

	return env


def _build_creds_secret_manifest(
	secret_name: str,
	namespace: str,
	site_docname: str,
	admin_password: str,
	db_root_password: str | None,
) -> dict[str, Any]:
	"""Build the per-Job credentials Secret that the Job reads via secretKeyRef.

	Always carries ``ADMIN_PASSWORD``.  Also carries ``DB_ROOT_PASSWORD`` when
	the user chose the plaintext path on the DocType (i.e. no external
	``db_root_secret`` was provided).  ``stringData`` lets us hand values as
	plain strings; the API server base64-encodes them at rest in etcd.
	"""
	string_data: dict[str, str] = {"ADMIN_PASSWORD": admin_password}
	if db_root_password is not None:
		string_data["DB_ROOT_PASSWORD"] = db_root_password

	return {
		"apiVersion": "v1",
		"kind": "Secret",
		"type": "Opaque",
		"metadata": {
			"name": secret_name,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
				SITE_DOC_LABEL: _safe_label_value(site_docname),
			},
		},
		"stringData": string_data,
	}


def _build_drop_creds_secret_manifest(
	secret_name: str,
	namespace: str,
	site_docname: str,
	db_root_password: str,
) -> dict[str, Any]:
	"""Build the per-Job credentials Secret for a ``bench drop-site`` Job.

	Drop-site only needs ``DB_ROOT_PASSWORD`` — there is no admin password
	involved, unlike ``new-site``.  Kept as a sibling to
	``_build_creds_secret_manifest`` (rather than a multi-purpose helper) so
	each manifest builder reads as a single-purpose unit and tests can
	assert the shape exactly.
	"""
	return {
		"apiVersion": "v1",
		"kind": "Secret",
		"type": "Opaque",
		"metadata": {
			"name": secret_name,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
				SITE_DOC_LABEL: _safe_label_value(site_docname),
			},
		},
		"stringData": {"DB_ROOT_PASSWORD": db_root_password},
	}


def _best_effort_delete_secret(
	api_client: "client.ApiClient",
	name: str,
	namespace: str,
) -> None:
	"""Delete a credentials Secret, ignoring 404s and logging everything else."""
	from kubernetes.client.rest import ApiException

	try:
		core_v1 = client.CoreV1Api(api_client=api_client)
		core_v1.delete_namespaced_secret(name=name, namespace=namespace)
	except ApiException as e:
		if e.status == 404:
			return
		frappe.logger("kubeport").warning(
			"Could not delete orphan creds Secret '%s' in '%s': %s",
			name,
			namespace,
			e,
		)
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Could not delete orphan creds Secret '%s' in '%s': %s",
			name,
			namespace,
			e,
		)


def _best_effort_delete_job(
	api_client: "client.ApiClient",
	name: str,
	namespace: str,
) -> None:
	"""Delete a site-creation Job, ignoring 404s and logging everything else.

	Used to tear down a Job that became orphaned mid-flight: the doc was
	cancelled, deleted, or force-recreated after we already applied the Job
	but before we could record its name on the DocType.  Without this, the
	Job would run to completion untracked, potentially creating a site on
	the bench PVC that has no row in MariaDB pointing at it.

	Background propagation cascades to the owner-referenced creds Secret
	and to the Job's pods.
	"""
	from kubernetes.client.rest import ApiException

	try:
		batch_v1 = client.BatchV1Api(api_client=api_client)
		batch_v1.delete_namespaced_job(
			name=name,
			namespace=namespace,
			propagation_policy="Background",
		)
	except ApiException as e:
		if e.status == 404:
			return
		frappe.logger("kubeport").warning(
			"Could not delete orphan site-creation Job '%s' in '%s': %s",
			name,
			namespace,
			e,
		)
	except Exception as e:
		frappe.logger("kubeport").warning(
			"Could not delete orphan site-creation Job '%s' in '%s': %s",
			name,
			namespace,
			e,
		)


# ---------------------------------------------------------------------------
# Pod inspection helpers
# ---------------------------------------------------------------------------

# Pod-level fields we lift from the reference bench pod onto the Job's pod
# template.  These are essential for the Job to schedule and run the same way
# the bench workload does (auth to DB via ServiceAccount, pull private images,
# land on the same node as a RWO sites PVC, etc.).  Field names are Python
# attribute names on V1PodSpec; the manifest keys are derived via
# _to_camel_case so they match the JSON K8s API contract.
_POD_LEVEL_FIELDS = (
	"service_account_name",
	"image_pull_secrets",
	"security_context",
	"node_selector",
	"tolerations",
	"affinity",
	"priority_class_name",
	"runtime_class_name",
)


def _clone_reference_pod_spec(
	api_client: client.ApiClient,
	pod: client.V1Pod,
) -> dict[str, Any]:
	"""Lift the fields needed to reproduce the bench's runtime context on the Job.

	Returns a dict describing the image, the container the sites volume is
	mounted on, its env/envFrom/resources/securityContext, the subset of
	volumes the container actually references, and whitelisted pod-level
	fields (serviceAccountName, imagePullSecrets, securityContext, etc.).

	Raises ``RuntimeError`` if the reference pod has no spec or the sites
	volume is not mounted on any container — without the sites volume the
	Job cannot run ``bench new-site``.
	"""
	spec = getattr(pod, "spec", None)
	if not spec:
		raise RuntimeError("Reference pod has no spec.")

	ref_container = _pick_sites_container(spec)

	image = ref_container.image
	if not image:
		raise RuntimeError("Could not determine container image from reference pod.")

	volume_mounts = _sanitize_list(api_client, ref_container.volume_mounts)
	referenced_vol_names = {vm.get("name") for vm in volume_mounts if vm.get("name")}
	volumes = [
		vol for vol in _sanitize_list(api_client, spec.volumes)
		if vol.get("name") in referenced_vol_names
	]

	pod_level: dict[str, Any] = {}
	for attr in _POD_LEVEL_FIELDS:
		value = getattr(spec, attr, None)
		if value in (None, [], {}):
			continue
		serialized = api_client.sanitize_for_serialization(value)
		if serialized in (None, [], {}):
			continue
		pod_level[_to_camel_case(attr)] = serialized

	return {
		"image": image,
		"pod_level": pod_level,
		"container_env": _sanitize_list(api_client, ref_container.env),
		"container_env_from": _sanitize_list(api_client, ref_container.env_from),
		"container_resources": (
			api_client.sanitize_for_serialization(ref_container.resources)
			if ref_container.resources else None
		),
		"container_security_context": (
			api_client.sanitize_for_serialization(ref_container.security_context)
			if ref_container.security_context else None
		),
		"volume_mounts": volume_mounts,
		"volumes": volumes,
	}


def _pick_sites_container(spec: client.V1PodSpec) -> client.V1Container:
	for container in (spec.containers or []):
		for vm in (container.volume_mounts or []):
			if vm.mount_path == FRAPPE_BENCH_SITES_PATH:
				return container
	raise RuntimeError(
		f"No volume mount for '{FRAPPE_BENCH_SITES_PATH}' found on reference pod. "
		"Cannot construct site creation job without the sites volume."
	)


def _sanitize_list(
	api_client: client.ApiClient,
	items: Any,
) -> list[dict[str, Any]]:
	if not items:
		return []
	serialized = api_client.sanitize_for_serialization(items) or []
	return [entry for entry in serialized if isinstance(entry, dict)]


def _to_camel_case(snake: str) -> str:
	head, *tail = snake.split("_")
	return head + "".join(part.capitalize() for part in tail)


# ---------------------------------------------------------------------------
# Naming and parsing helpers
# ---------------------------------------------------------------------------

def _job_name(site_name: str, token: str) -> str:
	"""Build a K8s-safe Job name from the site name and operation token.

	The token suffix makes the name per-operation unique so a retry on a
	new operation_token produces a fresh Job instead of server-side-applying
	over a previous one.  12 hex chars ≈ 48 bits of entropy — more than
	enough to avoid accidental collisions across the token prefix.
	"""
	slug = re.sub(r"[^a-z0-9-]", "-", site_name.lower())
	slug = re.sub(r"-+", "-", slug).strip("-")[:40]
	return f"ks-{slug}-{token[:12]}"


def _safe_label_value(value: str) -> str:
	"""Truncate and sanitize a string for use as a K8s label value (max 63 chars).

	K8s label values allow only alphanumerics, '-', '_', and '.' — slashes and
	other characters (including the '/' that appear in Frappe docnames) are
	replaced with '-'.
	"""
	sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", value)
	sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
	return sanitized[:63]


def _parse_install_apps(raw: str | None) -> list[str]:
	if not raw:
		return []
	apps: list[str] = []
	for line in raw.splitlines():
		app = line.strip()
		if not app:
			continue
		if not _APP_NAME_RE.match(app):
			raise ValueError(f"Invalid app name '{app}' in install_apps.")
		apps.append(app)
	return apps


def _truncate(detail: str) -> str:
	return detail[:_STATUS_DETAIL_LIMIT]


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------

def _site_operation_matches(
	site_docname: str,
	operation_token: str,
	expected_status: str,
) -> bool:
	current = frappe.db.get_value(
		"Frappe Site",
		site_docname,
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
		"Skipping stale site worker for Frappe Site '%s' because token/status "
		"no longer match the queued operation.",
		site_docname,
	)
	return False


def _backup_operation_matches(
	backup_docname: str,
	operation_token: str,
	expected_statuses: tuple[str, ...],
) -> bool:
	current = frappe.db.get_value(
		"Frappe Site Backup",
		backup_docname,
		["operation_token", "status"],
		as_dict=True,
	)
	if (
		current
		and current.get("operation_token") == operation_token
		and current.get("status") in expected_statuses
	):
		return True

	frappe.logger("kubeport").info(
		"Skipping stale backup worker for Frappe Site Backup '%s' because token/status "
		"no longer match the queued operation.",
		backup_docname,
	)
	return False


def _fail_backup_row(backup_docname: str, operation_token: str, detail: str) -> bool:
	if not _backup_operation_matches(backup_docname, operation_token, ("Pending", "In Progress")):
		return False
	frappe.db.set_value("Frappe Site Backup", backup_docname, {
		"status": "Failed",
		"status_detail": _truncate(detail),
		"completed_at": frappe.utils.now_datetime(),
		"operation_job_name": "",
		"operation_job_token": "",
	})
	frappe.publish_realtime(
		"frappe_site_backup_status_update",
		{"backup_docname": backup_docname, "status": "Failed"},
		doctype="Frappe Site Backup",
		docname=backup_docname,
	)
	return True


def _fail_restore_submission(
	site_docname: str,
	backup_docname: str,
	operation_token: str,
	detail: str,
) -> None:
	if _site_operation_matches(site_docname, operation_token, "Migrating"):
		frappe.db.set_value("Frappe Site", site_docname, {
			"status": "Failed",
			"status_detail": _truncate(detail),
		})
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Failed"},
			doctype="Frappe Site",
			docname=site_docname,
		)
	if _backup_operation_matches(backup_docname, operation_token, ("Restoring",)):
		frappe.db.set_value("Frappe Site Backup", backup_docname, {
			"status": "Available",
			"status_detail": _truncate(detail),
			"operation_job_name": "",
			"operation_job_token": "",
		})
	frappe.publish_realtime(
		"frappe_site_backup_status_update",
		{"site_docname": site_docname, "backup_docname": backup_docname, "status": "Available"},
		doctype="Frappe Site Backup",
		docname=backup_docname,
	)
