"""
Frappe Site Background Tasks

Submits a Kubernetes Job that runs ``bench new-site`` inside the bench's
workload container.  The Job is modelled after the ERPNext Helm chart's
``job-create-site.yaml`` template but is submitted directly via the K8s
API — not through a ``helm upgrade`` — to keep site lifecycle separate
from Helm release configuration.

Design rationale (direct Job submission, not Helm upgrade):
- Helm values represent steady-state deployment config; one-off site
  creation operations must not contaminate them.
- Toggling ``jobs.createSite.enabled`` on/off across upgrades is fragile
  and risks re-firing on subsequent upgrades.
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
# Duplicated from frappe_site.py on purpose: if a malformed value ever reaches
# the worker (direct DB write, schema import, etc.), we must not interpolate
# shell metacharacters into the bench command string.
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def create_site_task(site_docname: str, operation_token: str):
	"""Background task: submit a Kubernetes Job that runs ``bench new-site``.

	Reads the reference pod from the bench namespace to clone its image and
	sites PVC mount, then submits a Job manifest via server-side apply.
	Reconciliation polls the Job status and transitions the site to Active or
	Failed once the Job finishes.
	"""
	if not _site_operation_matches(site_docname, operation_token, "In Progress"):
		return

	doc = frappe.get_doc("Frappe Site", site_docname)

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
		install_apps = _parse_install_apps(doc.install_apps)
		db_type = doc.db_type or "mariadb"
		admin_password = doc.get_password("admin_password") or ""
		db_root_password = doc.get_password("db_root_password") or ""

		job_manifest = _build_job_manifest(
			job_name=job_name,
			namespace=namespace,
			site_docname=site_docname,
			site_name=doc.site_name,
			db_type=db_type,
			install_apps=install_apps,
			force_create=bool(doc.force_create),
			admin_password=admin_password,
			db_root_password=db_root_password,
			db_root_secret=doc.db_root_secret or "",
			db_root_secret_key=doc.db_root_secret_key or "mariadb-root-password",
			ref_spec=ref_spec,
		)

		apply_resource(api_client, job_manifest, namespace)

		if not _site_operation_matches(site_docname, operation_token, "In Progress"):
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
	"""Background task: best-effort delete the site-creation Job.

	Called from ``FrappeSite.cancel_site``.  Document status has already
	been rotated to Failed before enqueue, so any outcome here (success,
	404, or error) is purely about cluster cleanup.
	"""
	from kubernetes.client.rest import ApiException

	try:
		api_client = get_k8s_api_client(cluster)
		batch_v1 = client.BatchV1Api(api_client=api_client)
		batch_v1.delete_namespaced_job(
			name=job_name,
			namespace=namespace,
			propagation_policy="Background",
		)
	except ApiException as e:
		if e.status == 404:
			return
		frappe.log_error(
			title=f"Frappe Site Cancel: failed to delete Job '{job_name}'",
			message=str(e),
		)
	except Exception as e:
		frappe.log_error(
			title=f"Frappe Site Cancel: failed to delete Job '{job_name}'",
			message=str(e),
		)


# ---------------------------------------------------------------------------
# Job manifest builders
# ---------------------------------------------------------------------------

def _build_job_manifest(
	job_name: str,
	namespace: str,
	site_docname: str,
	site_name: str,
	db_type: str,
	install_apps: list[str],
	force_create: bool,
	admin_password: str,
	db_root_password: str,
	db_root_secret: str,
	db_root_secret_key: str,
	ref_spec: dict[str, Any],
) -> dict[str, Any]:
	bench_cmd = _bench_new_site_command(site_name, install_apps, force_create)
	site_env = _build_env(
		site_name=site_name,
		db_type=db_type,
		admin_password=admin_password,
		db_root_password=db_root_password,
		db_root_secret=db_root_secret,
		db_root_secret_key=db_root_secret_key,
	)
	# Merge reference-pod env with our site-specific env. Our keys win on
	# collision (explicit values override bench-chart defaults like SITE_NAME).
	env = _merge_env(ref_spec.get("container_env") or [], site_env)

	container: dict[str, Any] = {
		"name": "create-site",
		"image": ref_spec["image"],
		"command": ["bash", "-c"],
		"args": [bench_cmd],
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
				"app.kubernetes.io/managed-by": "kubeport",
				"kubeport.io/frappe-site": _safe_label_value(site_docname),
			},
		},
		"spec": {
			"backoffLimit": 0,
			"ttlSecondsAfterFinished": _JOB_TTL_SECONDS,
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


def _build_env(
	site_name: str,
	db_type: str,
	admin_password: str,
	db_root_password: str,
	db_root_secret: str,
	db_root_secret_key: str,
) -> list[dict[str, Any]]:
	db_root_user = "root" if db_type == "mariadb" else "postgres"

	env: list[dict[str, Any]] = [
		{"name": "SITE_NAME", "value": site_name},
		{"name": "DB_TYPE", "value": db_type},
		{"name": "DB_ROOT_USER", "value": db_root_user},
		{"name": "ADMIN_PASSWORD", "value": admin_password},
	]

	if db_root_secret:
		env.append({
			"name": "DB_ROOT_PASSWORD",
			"valueFrom": {
				"secretKeyRef": {
					"name": db_root_secret,
					"key": db_root_secret_key,
				}
			},
		})
	else:
		env.append({"name": "DB_ROOT_PASSWORD", "value": db_root_password})

	return env


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
