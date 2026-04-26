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
from typing import Any

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
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "kubeport"
# Duplicated from frappe_site.py on purpose: if a malformed value ever reaches
# the worker (direct DB write, schema import, etc.), we must not interpolate
# shell metacharacters into the bench command string.
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def create_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench new-site``.

	Reads the reference pod from the bench namespace to clone its image and
	sites PVC mount, then submits a per-Job credentials Secret and the Job
	manifest via server-side apply.  Reconciliation polls the Job status and
	transitions the site to Active or Failed once the Job finishes.

	The admin password (and, when the user chose the plaintext path, the DB
	root password) are stored in a per-Job ``Secret`` and referenced by the
	Job via ``secretKeyRef`` instead of being injected as plaintext env
	values.  The Secret is owner-referenced to the Job so it is garbage
	collected along with the Job's ``ttlSecondsAfterFinished`` cleanup.
	"""
	if not _site_operation_matches(site_docname, operation_token, "In Progress"):
		return

	doc = frappe.get_doc("Frappe Site", site_docname)
	creds_secret_name: str | None = None
	job_name_for_cleanup: str | None = None
	job_applied = False
	namespace = ""
	api_client = None

	try:
		release = frappe.get_doc("Helm Release", doc.bench_release)
		cluster = release.cluster
		namespace = release.namespace or "default"
		release_name = release.release_name

		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)

		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release_name,
		)

		ref_spec = _clone_reference_pod_spec(api_client, ref_pod)

		job_name = _job_name(doc.site_name, operation_token)
		job_name_for_cleanup = job_name
		creds_secret_name = f"{job_name}-creds"
		install_apps = _parse_install_apps(doc.install_apps)
		db_type = doc.db_type or "mariadb"
		admin_password = doc.get_password("admin_password") or ""
		db_root_secret = doc.db_root_secret or ""
		db_root_secret_key = doc.db_root_secret_key or "mariadb-root-password"

		# Plaintext DB root password is only needed when the user did not
		# provide a Kubernetes Secret — we route it through our creds Secret
		# so it never lands as a plaintext env var.
		needs_db_root_in_creds = not db_root_secret
		db_root_password = doc.get_password("db_root_password") or ""

		creds_secret_manifest = _build_creds_secret_manifest(
			secret_name=creds_secret_name,
			namespace=namespace,
			site_docname=site_docname,
			admin_password=admin_password,
			db_root_password=db_root_password if needs_db_root_in_creds else None,
		)
		apply_resource(api_client, creds_secret_manifest, namespace)

		bench_cmd = _bench_new_site_command(doc.site_name, install_apps, bool(doc.force_create))
		container_env = _build_env(
			site_name=doc.site_name,
			db_type=db_type,
			creds_secret_name=creds_secret_name,
			db_root_in_creds=needs_db_root_in_creds,
			db_root_secret=db_root_secret,
			db_root_secret_key=db_root_secret_key,
		)
		job_manifest = _build_op_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=site_docname,
			operation_label="create-site",
			container_command=bench_cmd,
			container_env=container_env,
			ref_spec=ref_spec,
		)

		apply_resource(api_client, job_manifest, namespace)
		job_applied = True

		# Now that the Job exists, adopt the Secret via ownerReferences so it
		# gets garbage-collected whenever the Job is deleted (cancellation,
		# TTL-based cleanup after completion, on_trash cascade).  Best-effort:
		# if this fails, the Secret still exists and will be cleaned up by
		# cancel_site_task or by orphan-sweep logic outside this branch.
		try:
			batch_v1 = client.BatchV1Api(api_client=api_client)
			job_read = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
			job_uid = getattr(getattr(job_read, "metadata", None), "uid", None)
			if job_uid:
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
				creds_secret_name,
				owner_err,
			)

		if not _site_operation_matches(site_docname, operation_token, "In Progress"):
			# Doc was cancelled / deleted / force-recreated while we were
			# applying. The Job we just created is now untracked: nothing in the
			# DB references it, so reconciliation and cancel_site_task cannot
			# reach it. Tear it down here so it does not run to completion and
			# create an orphan site on the bench PVC.
			_best_effort_delete_job(api_client, job_name, namespace)
			_best_effort_delete_secret(api_client, creds_secret_name, namespace)
			return

		doc.db_set("creation_job_name", job_name)
		doc.db_set("creation_job_token", operation_token)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "In Progress", "job_name": job_name},
			doctype="Frappe Site",
			docname=site_docname,
		)

	except Exception as e:
		# Best-effort cleanup of anything we created before the exception.
		# Order matters: drop the Job first (which would otherwise GC the
		# Secret via ownerRef once it starts running anyway), then the Secret
		# as a backstop for the case where the ownerRef was never attached.
		if api_client is not None and namespace:
			if job_applied and job_name_for_cleanup:
				_best_effort_delete_job(api_client, job_name_for_cleanup, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)

		if not _site_operation_matches(site_docname, operation_token, "In Progress"):
			return

		doc.db_set("status", "Failed")
		doc.db_set("status_detail", _truncate(f"Job submission failed: {e}"))
		frappe.log_error(
			title=f"Frappe Site Job Submission Failed: {site_docname}",
			message=str(e),
		)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Failed"},
			doctype="Frappe Site",
			docname=site_docname,
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


def delete_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench drop-site``.

	Mirrors ``create_site_task`` for the Job/Secret apply pattern: clone the
	reference pod to inherit image and volumes, optionally build a per-Job
	creds Secret carrying ``DB_ROOT_PASSWORD`` (only when the user did not
	supply an external Secret), apply the Job.  Reconciliation polls the Job
	and probes the bench: a confirmed-missing site causes the row itself to
	be deleted via ``frappe.delete_doc``; a still-present site lands the row
	in ``Failed`` so the operator can retry.
	"""
	if not _site_operation_matches(site_docname, operation_token, "Deleting"):
		return

	doc = frappe.get_doc("Frappe Site", site_docname)
	creds_secret_name: str | None = None
	job_name_for_cleanup: str | None = None
	job_applied = False
	namespace = ""
	api_client = None

	try:
		release = frappe.get_doc("Helm Release", doc.bench_release)
		cluster = release.cluster
		namespace = release.namespace or "default"
		release_name = release.release_name

		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)

		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release_name,
		)

		ref_spec = _clone_reference_pod_spec(api_client, ref_pod)

		job_name = _job_name(doc.site_name, operation_token)
		job_name_for_cleanup = job_name
		db_type = doc.db_type or "mariadb"
		db_root_secret = doc.db_root_secret or ""
		db_root_secret_key = doc.db_root_secret_key or "mariadb-root-password"

		needs_db_root_in_creds = not db_root_secret
		if needs_db_root_in_creds:
			creds_secret_name = f"{job_name}-creds"
			db_root_password = doc.get_password("db_root_password") or ""
			creds_secret_manifest = _build_drop_creds_secret_manifest(
				secret_name=creds_secret_name,
				namespace=namespace,
				site_docname=site_docname,
				db_root_password=db_root_password,
			)
			apply_resource(api_client, creds_secret_manifest, namespace)
		else:
			creds_secret_manifest = None

		bench_cmd = _bench_drop_site_command(doc.site_name)
		container_env = _build_drop_env(
			site_name=doc.site_name,
			db_type=db_type,
			creds_secret_name=creds_secret_name,
			db_root_in_creds=needs_db_root_in_creds,
			db_root_secret=db_root_secret,
			db_root_secret_key=db_root_secret_key,
		)
		job_manifest = _build_op_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=site_docname,
			operation_label="delete-site",
			container_command=bench_cmd,
			container_env=container_env,
			ref_spec=ref_spec,
		)

		apply_resource(api_client, job_manifest, namespace)
		job_applied = True

		# Adopt the creds Secret via ownerRef so K8s GC cascades it with the
		# Job's TTL cleanup.  Best-effort; the cancel/sweep paths cover the
		# rest.
		if creds_secret_manifest is not None:
			try:
				batch_v1 = client.BatchV1Api(api_client=api_client)
				job_read = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
				job_uid = getattr(getattr(job_read, "metadata", None), "uid", None)
				if job_uid:
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
					"Could not attach ownerReference for drop-site creds Secret '%s': %s",
					creds_secret_name,
					owner_err,
				)

		if not _site_operation_matches(site_docname, operation_token, "Deleting"):
			# Doc was cancelled / superseded while we were applying.  Tear down
			# the Job so it does not run untracked and accidentally drop a site
			# the user no longer wants dropped.
			_best_effort_delete_job(api_client, job_name, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)
			return

		doc.db_set("creation_job_name", job_name)
		doc.db_set("creation_job_token", operation_token)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Deleting", "job_name": job_name},
			doctype="Frappe Site",
			docname=site_docname,
		)

	except Exception as e:
		if api_client is not None and namespace:
			if job_applied and job_name_for_cleanup:
				_best_effort_delete_job(api_client, job_name_for_cleanup, namespace)
			if creds_secret_name:
				_best_effort_delete_secret(api_client, creds_secret_name, namespace)

		if not _site_operation_matches(site_docname, operation_token, "Deleting"):
			return

		doc.db_set("status", "Failed")
		doc.db_set("status_detail", _truncate(f"Drop-site Job submission failed: {e}"))
		frappe.log_error(
			title=f"Frappe Site Drop Submission Failed: {site_docname}",
			message=str(e),
		)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Failed"},
			doctype="Frappe Site",
			docname=site_docname,
		)


def migrate_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench migrate``.

	``bench migrate`` reads its DB credentials from ``site_config.json`` on
	the sites PVC, so unlike create/delete this task has no creds Secret —
	the Job only needs the cloned reference pod spec and the site name.
	Reconciliation runs the same functional probe used for creation to
	transition the row back to ``Active`` (or to ``Failed`` if migrations
	broke the site).
	"""
	if not _site_operation_matches(site_docname, operation_token, "Migrating"):
		return

	doc = frappe.get_doc("Frappe Site", site_docname)
	job_name_for_cleanup: str | None = None
	job_applied = False
	namespace = ""
	api_client = None

	try:
		release = frappe.get_doc("Helm Release", doc.bench_release)
		cluster = release.cluster
		namespace = release.namespace or "default"
		release_name = release.release_name

		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)

		ref_pod = _select_site_discovery_pod(
			core_v1=core_v1,
			namespace=namespace,
			release_name=release_name,
		)

		ref_spec = _clone_reference_pod_spec(api_client, ref_pod)

		job_name = _job_name(doc.site_name, operation_token)
		job_name_for_cleanup = job_name

		bench_cmd = _bench_migrate_command(doc.site_name)
		container_env = [{"name": "SITE_NAME", "value": doc.site_name}]
		job_manifest = _build_op_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=site_docname,
			operation_label="migrate-site",
			container_command=bench_cmd,
			container_env=container_env,
			ref_spec=ref_spec,
		)

		apply_resource(api_client, job_manifest, namespace)
		job_applied = True

		if not _site_operation_matches(site_docname, operation_token, "Migrating"):
			# Cancelled / superseded mid-apply.  Stop the Job so it does not
			# alter the site's DB schema after the operator changed their mind.
			_best_effort_delete_job(api_client, job_name, namespace)
			return

		doc.db_set("creation_job_name", job_name)
		doc.db_set("creation_job_token", operation_token)
		frappe.publish_realtime(
			"frappe_site_status_update",
			{"site_docname": site_docname, "status": "Migrating", "job_name": job_name},
			doctype="Frappe Site",
			docname=site_docname,
		)

	except Exception as e:
		if api_client is not None and namespace and job_applied and job_name_for_cleanup:
			_best_effort_delete_job(api_client, job_name_for_cleanup, namespace)

		if not _site_operation_matches(site_docname, operation_token, "Migrating"):
			return

		doc.db_set("status", "Failed")
		doc.db_set("status_detail", _truncate(f"Migrate Job submission failed: {e}"))
		frappe.log_error(
			title=f"Frappe Site Migrate Submission Failed: {site_docname}",
			message=str(e),
		)
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

	return {
		"apiVersion": "batch/v1",
		"kind": "Job",
		"metadata": {
			"name": job_name,
			"namespace": namespace,
			"labels": {
				MANAGED_BY_LABEL: MANAGED_BY_VALUE,
				SITE_DOC_LABEL: _safe_label_value(site_docname),
			},
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
	db_root_user = "root" if db_type == "mariadb" else "postgres"

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
	db_root_user = "root" if db_type == "mariadb" else "postgres"

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
