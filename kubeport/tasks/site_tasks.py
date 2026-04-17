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

		image = _extract_image(ref_pod)
		sites_volume, sites_mount = _extract_sites_volume(api_client, ref_pod)

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
			image=image,
			db_type=db_type,
			install_apps=install_apps,
			force_create=bool(doc.force_create),
			admin_password=admin_password,
			db_root_password=db_root_password,
			db_root_secret=doc.db_root_secret or "",
			db_root_secret_key=doc.db_root_secret_key or "mariadb-root-password",
			sites_volume=sites_volume,
			sites_mount=sites_mount,
		)

		apply_resource(api_client, job_manifest, namespace)

		if not _site_operation_matches(site_docname, operation_token, "In Progress"):
			return

		doc.db_set("creation_job_name", job_name)
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


# ---------------------------------------------------------------------------
# Job manifest builders
# ---------------------------------------------------------------------------

def _build_job_manifest(
	job_name: str,
	namespace: str,
	site_docname: str,
	site_name: str,
	image: str,
	db_type: str,
	install_apps: list[str],
	force_create: bool,
	admin_password: str,
	db_root_password: str,
	db_root_secret: str,
	db_root_secret_key: str,
	sites_volume: dict[str, Any],
	sites_mount: dict[str, Any],
) -> dict[str, Any]:
	bench_cmd = _bench_new_site_command(site_name, install_apps, force_create)
	env = _build_env(
		site_name=site_name,
		db_type=db_type,
		admin_password=admin_password,
		db_root_password=db_root_password,
		db_root_secret=db_root_secret,
		db_root_secret_key=db_root_secret_key,
	)

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
			"template": {
				"spec": {
					"restartPolicy": "Never",
					"containers": [
						{
							"name": "create-site",
							"image": image,
							"command": ["bash", "-c"],
							"args": [bench_cmd],
							"env": env,
							"volumeMounts": [sites_mount],
						}
					],
					"volumes": [sites_volume],
				}
			},
		},
	}


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

def _extract_image(pod: client.V1Pod) -> str:
	spec = getattr(pod, "spec", None)
	if spec and spec.containers:
		image = spec.containers[0].image
		if image:
			return image
	raise RuntimeError("Could not determine container image from reference pod.")


def _extract_sites_volume(
	api_client: client.ApiClient,
	pod: client.V1Pod,
) -> tuple[dict[str, Any], dict[str, Any]]:
	"""Return (volume_dict, volume_mount_dict) for the sites directory."""
	spec = getattr(pod, "spec", None)
	if not spec:
		raise RuntimeError("Reference pod has no spec.")

	sites_mount_obj = None
	for container in (spec.containers or []):
		for vm in (container.volume_mounts or []):
			if vm.mount_path == FRAPPE_BENCH_SITES_PATH:
				sites_mount_obj = vm
				break
		if sites_mount_obj:
			break

	if not sites_mount_obj:
		raise RuntimeError(
			f"No volume mount for '{FRAPPE_BENCH_SITES_PATH}' found on reference pod. "
			"Cannot construct site creation job without the sites volume."
		)

	sites_volume_obj = None
	for vol in (spec.volumes or []):
		if vol.name == sites_mount_obj.name:
			sites_volume_obj = vol
			break

	if not sites_volume_obj:
		raise RuntimeError(
			f"Volume '{sites_mount_obj.name}' referenced by sites mount not found in pod spec."
		)

	volume_dict = api_client.sanitize_for_serialization(sites_volume_obj)
	mount_dict = api_client.sanitize_for_serialization(sites_mount_obj)
	return volume_dict, mount_dict


# ---------------------------------------------------------------------------
# Naming and parsing helpers
# ---------------------------------------------------------------------------

def _job_name(site_name: str, token: str) -> str:
	slug = re.sub(r"[^a-z0-9-]", "-", site_name.lower())
	slug = re.sub(r"-+", "-", slug).strip("-")[:40]
	return f"ks-{slug}-{token[:8]}"


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
	return [app.strip() for app in raw.splitlines() if app.strip()]


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
