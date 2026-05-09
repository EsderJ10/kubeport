"""
Read-only Kubernetes observability helpers for Helm Release drilldowns.

These helpers query observed cluster state on demand and never persist it.
Payload sizes are bounded so form dialogs stay responsive even when pods are
noisy or a workload has a long event history.
"""

from __future__ import annotations

import re
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from kubeport.utils.k8s_client import get_k8s_api_client

LOG_TAIL_LINE_LIMIT = 2000
LOG_TEXT_LIMIT_BYTES = 256 * 1024
EVENT_LIMIT = 50
ROLLOUT_LIMIT = 20
LOG_TIMEOUT_SECONDS = 20.0
LIST_TIMEOUT_SECONDS = 15.0

_LOG_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Pod"})
_ROLLOUT_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})
_EVENT_KINDS = frozenset(
	{
		"Deployment",
		"StatefulSet",
		"DaemonSet",
		"Pod",
		"Job",
		"PersistentVolumeClaim",
		"Service",
		"Ingress",
	}
)
_K8S_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")


def list_pods_for_resource(
	cluster: str,
	namespace: str,
	kind: str,
	name: str,
) -> list[dict[str, Any]]:
	"""Return pods owned by a Deployment, StatefulSet, DaemonSet, or Pod."""
	kind = _validate_kind(kind, _LOG_KINDS)
	name = _validate_name(name)
	namespace = namespace or "default"

	api_client = _get_api_client(cluster)
	core_v1 = client.CoreV1Api(api_client=api_client)
	apps_v1 = client.AppsV1Api(api_client=api_client)

	try:
		if kind == "Pod":
			pod = core_v1.read_namespaced_pod(
				name=name,
				namespace=namespace,
				_request_timeout=LIST_TIMEOUT_SECONDS,
			)
			return [_pod_to_dict(pod)]
		if kind == "Deployment":
			return _list_deployment_pods(core_v1, apps_v1, namespace, name)
		if kind == "StatefulSet":
			obj = apps_v1.read_namespaced_stateful_set(
				name=name,
				namespace=namespace,
				_request_timeout=LIST_TIMEOUT_SECONDS,
			)
			return _list_controller_pods(core_v1, namespace, obj, "StatefulSet", name)
		if kind == "DaemonSet":
			obj = apps_v1.read_namespaced_daemon_set(
				name=name,
				namespace=namespace,
				_request_timeout=LIST_TIMEOUT_SECONDS,
			)
			return _list_controller_pods(core_v1, namespace, obj, "DaemonSet", name)
	except ApiException as e:
		if e.status == 404:
			return []
		raise RuntimeError(f"Kubernetes API error while resolving pods: {e.status} {e.reason or ''}".strip())
	except Exception as e:
		raise RuntimeError(f"Could not resolve pods for {kind}/{name}: {e}")

	return []


def get_pod_logs(
	cluster: str,
	namespace: str,
	pod: str,
	container: str | None = None,
	tail_lines: int = 200,
	previous: bool = False,
) -> str:
	"""Return capped pod logs for one pod/container."""
	pod = _validate_name(pod)
	namespace = namespace or "default"
	tail_lines = _cap_int(tail_lines, default=200, minimum=1, maximum=LOG_TAIL_LINE_LIMIT)

	api_client = _get_api_client(cluster)
	core_v1 = client.CoreV1Api(api_client=api_client)

	try:
		logs = core_v1.read_namespaced_pod_log(
			name=pod,
			namespace=namespace,
			container=str(container).strip() if container else None,
			tail_lines=tail_lines,
			previous=_coerce_bool(previous),
			_request_timeout=LOG_TIMEOUT_SECONDS,
		)
	except ApiException as e:
		raise RuntimeError(f"Kubernetes API error while reading logs: {e.status} {e.reason or ''}".strip())
	except Exception as e:
		raise RuntimeError(f"Could not read logs for Pod/{pod}: {e}")

	return _truncate_text(str(logs or ""), LOG_TEXT_LIMIT_BYTES)


def list_resource_events(
	cluster: str,
	namespace: str,
	kind: str,
	name: str,
	limit: int = 20,
) -> list[dict[str, Any]]:
	"""Return recent Kubernetes events scoped to one resource."""
	kind = _validate_kind(kind, _EVENT_KINDS)
	name = _validate_name(name)
	namespace = namespace or "default"
	limit = _cap_int(limit, default=20, minimum=1, maximum=EVENT_LIMIT)

	api_client = _get_api_client(cluster)
	events = _list_events_v1(api_client, namespace, kind, name)
	if not events:
		events = _list_core_events(api_client, namespace, kind, name)

	events.sort(key=_event_sort_key, reverse=True)
	return events[:limit]


def get_rollout_history(
	cluster: str,
	namespace: str,
	kind: str,
	name: str,
	limit: int = 10,
) -> list[dict[str, Any]]:
	"""Return a bounded rollout context for workload controllers."""
	kind = _validate_kind(kind, _ROLLOUT_KINDS)
	name = _validate_name(name)
	namespace = namespace or "default"
	limit = _cap_int(limit, default=10, minimum=1, maximum=ROLLOUT_LIMIT)

	api_client = _get_api_client(cluster)
	apps_v1 = client.AppsV1Api(api_client=api_client)

	try:
		if kind == "Deployment":
			rows = _deployment_rollout_rows(apps_v1, namespace, name)
		elif kind == "StatefulSet":
			obj = apps_v1.read_namespaced_stateful_set(
				name=name,
				namespace=namespace,
				_request_timeout=LIST_TIMEOUT_SECONDS,
			)
			rows = _controller_revision_rows(apps_v1, namespace, obj, "StatefulSet", name)
		else:
			obj = apps_v1.read_namespaced_daemon_set(
				name=name,
				namespace=namespace,
				_request_timeout=LIST_TIMEOUT_SECONDS,
			)
			rows = _controller_revision_rows(apps_v1, namespace, obj, "DaemonSet", name)
	except ApiException as e:
		if e.status == 404:
			return []
		raise RuntimeError(
			f"Kubernetes API error while reading rollout context: {e.status} {e.reason or ''}".strip()
		)
	except Exception as e:
		raise RuntimeError(f"Could not read rollout context for {kind}/{name}: {e}")

	rows.sort(key=_rollout_sort_key, reverse=True)
	return rows[:limit]


def _list_deployment_pods(
	core_v1: "client.CoreV1Api",
	apps_v1: "client.AppsV1Api",
	namespace: str,
	name: str,
) -> list[dict[str, Any]]:
	deployment = apps_v1.read_namespaced_deployment(
		name=name,
		namespace=namespace,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	selector = _selector_to_string(getattr(getattr(deployment, "spec", None), "selector", None))
	replica_sets = apps_v1.list_namespaced_replica_set(
		namespace=namespace,
		label_selector=selector or None,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	replica_set_names = {
		_meta_name(replica_set)
		for replica_set in getattr(replica_sets, "items", []) or []
		if _has_owner(replica_set, "Deployment", name)
	}
	pods = core_v1.list_namespaced_pod(
		namespace=namespace,
		label_selector=selector or None,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	matched = [
		pod
		for pod in getattr(pods, "items", []) or []
		if _has_any_owner(pod, "ReplicaSet", replica_set_names)
	]
	return [_pod_to_dict(pod) for pod in matched]


def _get_api_client(cluster: str) -> "client.ApiClient":
	try:
		return get_k8s_api_client(cluster)
	except Exception as e:
		raise RuntimeError(f"Could not initialize Kubernetes client for cluster '{cluster}': {e}")


def _list_controller_pods(
	core_v1: "client.CoreV1Api",
	namespace: str,
	obj: Any,
	kind: str,
	name: str,
) -> list[dict[str, Any]]:
	selector = _selector_to_string(getattr(getattr(obj, "spec", None), "selector", None))
	pods = core_v1.list_namespaced_pod(
		namespace=namespace,
		label_selector=selector or None,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	matched = [pod for pod in getattr(pods, "items", []) or [] if _has_owner(pod, kind, name)]
	return [_pod_to_dict(pod) for pod in matched]


def _deployment_rollout_rows(
	apps_v1: "client.AppsV1Api",
	namespace: str,
	name: str,
) -> list[dict[str, Any]]:
	deployment = apps_v1.read_namespaced_deployment(
		name=name,
		namespace=namespace,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	selector = _selector_to_string(getattr(getattr(deployment, "spec", None), "selector", None))
	current_revision = _annotation(deployment, "deployment.kubernetes.io/revision")
	replica_sets = apps_v1.list_namespaced_replica_set(
		namespace=namespace,
		label_selector=selector or None,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	rows: list[dict[str, Any]] = []
	for replica_set in getattr(replica_sets, "items", []) or []:
		if not _has_owner(replica_set, "Deployment", name):
			continue
		revision = _annotation(replica_set, "deployment.kubernetes.io/revision")
		rows.append(
			{
				"kind": "ReplicaSet",
				"name": _meta_name(replica_set),
				"revision": revision,
				"created_at": _timestamp(replica_set),
				"change_cause": _annotation(replica_set, "kubernetes.io/change-cause"),
				"current": bool(current_revision and revision == current_revision),
				"image_summary": _image_summary_from_template(
					getattr(getattr(replica_set, "spec", None), "template", None)
				),
			}
		)
	return rows


def _controller_revision_rows(
	apps_v1: "client.AppsV1Api",
	namespace: str,
	obj: Any,
	kind: str,
	name: str,
) -> list[dict[str, Any]]:
	selector = _selector_to_string(getattr(getattr(obj, "spec", None), "selector", None))
	status = getattr(obj, "status", None)
	current_revision_name = str(getattr(status, "current_revision", "") or "")
	update_revision_name = str(getattr(status, "update_revision", "") or "")
	revisions = apps_v1.list_namespaced_controller_revision(
		namespace=namespace,
		label_selector=selector or None,
		_request_timeout=LIST_TIMEOUT_SECONDS,
	)
	rows: list[dict[str, Any]] = []
	for revision in getattr(revisions, "items", []) or []:
		if not _has_owner(revision, kind, name):
			continue
		revision_name = _meta_name(revision)
		rows.append(
			{
				"kind": "ControllerRevision",
				"name": revision_name,
				"revision": getattr(revision, "revision", None) or "",
				"created_at": _timestamp(revision),
				"change_cause": _annotation(revision, "kubernetes.io/change-cause"),
				"current": bool(revision_name and revision_name == current_revision_name),
				"update": bool(revision_name and revision_name == update_revision_name),
				"image_summary": _image_summary_from_controller_revision(revision),
			}
		)
	return rows


def _list_events_v1(
	api_client: "client.ApiClient",
	namespace: str,
	kind: str,
	name: str,
) -> list[dict[str, Any]]:
	try:
		events_v1 = client.EventsV1Api(api_client=api_client)
		response = events_v1.list_namespaced_event(
			namespace=namespace,
			field_selector=f"regarding.kind={kind},regarding.name={name}",
			_request_timeout=LIST_TIMEOUT_SECONDS,
		)
	except Exception:
		return []

	return [_event_v1_to_dict(event) for event in getattr(response, "items", []) or []]


def _list_core_events(
	api_client: "client.ApiClient",
	namespace: str,
	kind: str,
	name: str,
) -> list[dict[str, Any]]:
	try:
		core_v1 = client.CoreV1Api(api_client=api_client)
		response = core_v1.list_namespaced_event(
			namespace=namespace,
			field_selector=f"involvedObject.kind={kind},involvedObject.name={name}",
			_request_timeout=LIST_TIMEOUT_SECONDS,
		)
	except Exception as e:
		raise RuntimeError(f"Could not read Kubernetes events for {kind}/{name}: {e}")

	return [_core_event_to_dict(event) for event in getattr(response, "items", []) or []]


def _pod_to_dict(pod: Any) -> dict[str, Any]:
	status = getattr(pod, "status", None)
	spec = getattr(pod, "spec", None)
	container_statuses = list(getattr(status, "container_statuses", None) or [])
	init_statuses = list(getattr(status, "init_container_statuses", None) or [])
	return {
		"name": _meta_name(pod),
		"phase": str(getattr(status, "phase", "") or ""),
		"container_names": [
			str(getattr(container, "name", "") or "")
			for container in list(getattr(spec, "containers", None) or [])
			if getattr(container, "name", None)
		],
		"restart_count": sum(int(getattr(row, "restart_count", 0) or 0) for row in container_statuses),
		"container_statuses": [_container_status_to_dict(row) for row in container_statuses + init_statuses],
	}


def _container_status_to_dict(row: Any) -> dict[str, Any]:
	state = getattr(row, "state", None)
	waiting = getattr(state, "waiting", None) if state else None
	terminated = getattr(state, "terminated", None) if state else None
	running = getattr(state, "running", None) if state else None
	state_name = "unknown"
	reason = ""
	if waiting:
		state_name = "waiting"
		reason = str(getattr(waiting, "reason", "") or "")
	elif terminated:
		state_name = "terminated"
		reason = str(getattr(terminated, "reason", "") or "")
	elif running:
		state_name = "running"
	return {
		"name": str(getattr(row, "name", "") or ""),
		"ready": bool(getattr(row, "ready", False)),
		"restart_count": int(getattr(row, "restart_count", 0) or 0),
		"state": state_name,
		"reason": reason,
	}


def _event_v1_to_dict(event: Any) -> dict[str, Any]:
	return {
		"type": str(getattr(event, "type", "") or ""),
		"reason": str(getattr(event, "reason", "") or ""),
		"message": str(getattr(event, "note", "") or ""),
		"count": int(getattr(event, "deprecated_count", 0) or 0),
		"first_seen": _stringify_time(getattr(event, "deprecated_first_timestamp", None)),
		"last_seen": _stringify_time(
			getattr(event, "event_time", None) or getattr(event, "deprecated_last_timestamp", None)
		),
		"source": str(getattr(event, "reporting_controller", "") or ""),
	}


def _core_event_to_dict(event: Any) -> dict[str, Any]:
	source = getattr(event, "source", None)
	return {
		"type": str(getattr(event, "type", "") or ""),
		"reason": str(getattr(event, "reason", "") or ""),
		"message": str(getattr(event, "message", "") or ""),
		"count": int(getattr(event, "count", 0) or 0),
		"first_seen": _stringify_time(getattr(event, "first_timestamp", None)),
		"last_seen": _stringify_time(getattr(event, "last_timestamp", None)),
		"source": str(getattr(source, "component", "") or ""),
	}


def _event_sort_key(event: dict[str, Any]) -> str:
	return str(event.get("last_seen") or event.get("first_seen") or "")


def _rollout_sort_key(row: dict[str, Any]) -> int:
	try:
		return int(row.get("revision") or 0)
	except (TypeError, ValueError):
		return 0


def _selector_to_string(selector: Any) -> str:
	if selector is None:
		return ""

	parts: list[str] = []
	match_labels = getattr(selector, "match_labels", None) or {}
	for key in sorted(match_labels):
		parts.append(f"{key}={match_labels[key]}")

	for expression in getattr(selector, "match_expressions", None) or []:
		key = str(getattr(expression, "key", "") or "")
		operator = str(getattr(expression, "operator", "") or "")
		values = [str(value) for value in (getattr(expression, "values", None) or [])]
		if not key:
			continue
		if operator == "In":
			parts.append(f"{key} in ({','.join(values)})")
		elif operator == "NotIn":
			parts.append(f"{key} notin ({','.join(values)})")
		elif operator == "Exists":
			parts.append(key)
		elif operator == "DoesNotExist":
			parts.append(f"!{key}")

	return ",".join(parts)


def _has_owner(obj: Any, kind: str, name: str) -> bool:
	for owner in _owner_references(obj):
		if getattr(owner, "kind", None) == kind and getattr(owner, "name", None) == name:
			return True
	return False


def _has_any_owner(obj: Any, kind: str, names: set[str]) -> bool:
	if not names:
		return False
	for owner in _owner_references(obj):
		if getattr(owner, "kind", None) == kind and getattr(owner, "name", None) in names:
			return True
	return False


def _owner_references(obj: Any) -> list[Any]:
	return list(getattr(getattr(obj, "metadata", None), "owner_references", None) or [])


def _meta_name(obj: Any) -> str:
	return str(getattr(getattr(obj, "metadata", None), "name", "") or "")


def _annotation(obj: Any, key: str) -> str:
	annotations = getattr(getattr(obj, "metadata", None), "annotations", None) or {}
	return str(annotations.get(key) or "")


def _timestamp(obj: Any) -> str:
	return _stringify_time(getattr(getattr(obj, "metadata", None), "creation_timestamp", None))


def _stringify_time(value: Any) -> str:
	if value is None:
		return ""
	if hasattr(value, "isoformat"):
		return str(value.isoformat())
	return str(value)


def _image_summary_from_template(template: Any) -> str:
	spec = getattr(template, "spec", None)
	containers = list(getattr(spec, "containers", None) or [])
	images = [
		str(getattr(container, "image", "") or "")
		for container in containers
		if getattr(container, "image", None)
	]
	return ", ".join(images)


def _image_summary_from_controller_revision(revision: Any) -> str:
	data = getattr(revision, "data", None)
	if not isinstance(data, dict):
		return ""
	template = data.get("spec", {}).get("template", {})
	containers = template.get("spec", {}).get("containers", [])
	if not isinstance(containers, list):
		return ""
	images = [
		str(container.get("image") or "")
		for container in containers
		if isinstance(container, dict) and container.get("image")
	]
	return ", ".join(images)


def _validate_kind(kind: str, allowed: frozenset[str]) -> str:
	kind = str(kind or "").strip()
	if kind not in allowed:
		raise ValueError(f"Unsupported resource kind: {kind or '<empty>'}")
	return kind


def _validate_name(name: str) -> str:
	name = str(name or "").strip()
	if not name or not _K8S_NAME_PATTERN.match(name):
		raise ValueError("Resource name is required and must be a valid Kubernetes object name.")
	return name


def _cap_int(value: int, default: int, minimum: int, maximum: int) -> int:
	try:
		number = int(value)
	except (TypeError, ValueError):
		number = default
	if number < minimum:
		return minimum
	if number > maximum:
		return maximum
	return number


def _coerce_bool(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() in {"1", "true", "yes", "on"}
	return bool(value)


def _truncate_text(text: str, max_bytes: int) -> str:
	encoded = text.encode("utf-8")
	if len(encoded) <= max_bytes:
		return text
	truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
	return f"{truncated}\n\n[truncated to {max_bytes} bytes]"
