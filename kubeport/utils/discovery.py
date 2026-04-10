"""
Cluster Discovery Helpers

Read-only helpers for live workload discovery against a Kubernetes cluster.
These functions never persist discovered data to MariaDB.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from kubernetes import client
from kubernetes.client import ApiClient
from kubernetes.stream import stream


FRAPPE_BENCH_SITES_PATH = "/home/frappe/frappe-bench/sites"
SITE_DISCOVERY_EXCLUDED_DIRS = {"assets"}
_COMPONENT_RANK = {
	"gunicorn": 500,
	"scheduler": 400,
	"default": 300,
	"worker-default": 300,
	"short": 200,
	"worker-short": 200,
	"long": 100,
	"worker-long": 100,
	"nginx": 50,
	"socketio": 10,
}


def discover_cluster_releases(cluster_name: str) -> list[dict[str, Any]]:
	"""Return normalized Helm release rows for a cluster."""
	from kubeport.utils import helm

	releases = helm.list_releases(cluster_name=cluster_name)
	return [_normalize_release_row(release) for release in releases if isinstance(release, dict)]


def discover_release_sites(
	api_client: ApiClient,
	release: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Discover Frappe sites for a single official-chart release."""
	if not release.get("is_frappe_bench"):
		return []

	core_v1 = client.CoreV1Api(api_client=api_client)
	pod = _select_site_discovery_pod(
		core_v1=core_v1,
		namespace=str(release.get("namespace") or "default"),
		release_name=str(release.get("release_name") or ""),
	)
	sites = _exec_list_sites(
		core_v1=core_v1,
		namespace=str(release.get("namespace") or "default"),
		pod=pod,
	)

	return [{
		"site_name": site_name,
		"bench_release": release.get("release_name", ""),
		"namespace": release.get("namespace", "default"),
		"source": "pod_exec",
		"pod_name": pod.metadata.name if pod.metadata else "",
	} for site_name in sites]


def _normalize_release_row(release: dict[str, Any]) -> dict[str, Any]:
	chart = str(release.get("chart") or "")
	chart_name, chart_version = _split_chart_name_and_version(chart)
	app_version = release.get("app_version")
	if app_version is None:
		app_version = release.get("appVersion")

	return {
		"release_name": str(release.get("name") or ""),
		"namespace": str(release.get("namespace") or "default"),
		"status": str(release.get("status") or ""),
		"chart": chart,
		"chart_name": chart_name,
		"chart_version": chart_version,
		"app_version": str(app_version or ""),
		"updated": str(release.get("updated") or ""),
		"is_frappe_bench": _is_frappe_chart(chart_name),
	}


def _split_chart_name_and_version(chart: str) -> tuple[str, str]:
	if not chart:
		return "", ""

	name, sep, version = chart.rpartition("-")
	if not sep:
		return chart, ""
	if not version or not version[0].isdigit():
		return chart, ""
	return name, version


def _is_frappe_chart(chart_name: str) -> bool:
	return chart_name == "erpnext"


def _select_site_discovery_pod(
	core_v1: client.CoreV1Api,
	namespace: str,
	release_name: str,
) -> client.V1Pod:
	if not release_name:
		raise ValueError("Release name is required for site discovery.")

	pods = core_v1.list_namespaced_pod(
		namespace=namespace,
		label_selector=f"app.kubernetes.io/instance={release_name}",
		_request_timeout=(5, 15),
	)
	candidates = [pod for pod in pods.items if _is_running_pod(pod)]
	if not candidates:
		raise RuntimeError(
			f"No running pods found for Helm release '{release_name}' in namespace '{namespace}'."
		)

	return max(candidates, key=_score_site_pod)


def _is_running_pod(pod: client.V1Pod) -> bool:
	status = getattr(pod, "status", None)
	return bool(status and getattr(status, "phase", "") == "Running")


def _score_site_pod(pod: client.V1Pod) -> int:
	score = 0
	metadata = getattr(pod, "metadata", None)
	status = getattr(pod, "status", None)
	spec = getattr(pod, "spec", None)

	if status and getattr(status, "phase", "") == "Running":
		score += 1000

	if status and status.container_statuses:
		ready_count = sum(1 for container in status.container_statuses if container.ready)
		score += ready_count * 100

	labels = metadata.labels if metadata and metadata.labels else {}
	component = str(labels.get("app.kubernetes.io/component") or "")
	score += _COMPONENT_RANK.get(component, 0)

	name = str(metadata.name if metadata and metadata.name else "")
	for token, token_score in _COMPONENT_RANK.items():
		if token in name:
			score += token_score

	if spec and spec.containers:
		score += len(spec.containers)

	return score


def _exec_list_sites(
	core_v1: client.CoreV1Api,
	namespace: str,
	pod: client.V1Pod,
) -> list[str]:
	metadata = getattr(pod, "metadata", None)
	spec = getattr(pod, "spec", None)
	if not metadata or not metadata.name:
		raise RuntimeError("Cannot exec into a pod without a name.")

	container_name = ""
	if spec and spec.containers:
		container_name = spec.containers[0].name or ""

	command = [
		"sh",
		"-lc",
		(
			f'for d in "{FRAPPE_BENCH_SITES_PATH}"/*; do '
			'[ -d "$d" ] || continue; '
			'[ -f "$d/site_config.json" ] || continue; '
			'basename "$d"; '
			"done"
		),
	]

	exec_kwargs: dict[str, Any] = {
		"name": metadata.name,
		"namespace": namespace,
		"command": command,
		"stderr": True,
		"stdin": False,
		"stdout": True,
		"tty": False,
		"_request_timeout": (5, 20),
	}
	if container_name:
		exec_kwargs["container"] = container_name

	output = stream(core_v1.connect_get_namespaced_pod_exec, **exec_kwargs)
	return _parse_site_names(output.splitlines())


def _parse_site_names(lines: Iterable[str]) -> list[str]:
	sites: set[str] = set()
	for raw_line in lines:
		line = raw_line.strip()
		if not line or line in SITE_DISCOVERY_EXCLUDED_DIRS:
			continue
		sites.add(line)
	return sorted(sites)
