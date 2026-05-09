"""
Read-only observability endpoints for Helm Release form drilldowns.

Every endpoint resolves cluster identity from the Helm Release row.  Client
input is limited to the resource currently shown in the readiness table.
"""

from __future__ import annotations

from typing import Any

import frappe

from kubeport.utils import helm
from kubeport.utils.observability import (
	_EVENT_KINDS,
	_LOG_KINDS,
	_ROLLOUT_KINDS,
	_validate_kind,
	_validate_name,
	get_pod_logs,
	get_rollout_history,
	list_pods_for_resource,
	list_resource_events,
)


@frappe.whitelist()
def get_release_resource_logs(
	release_docname: str,
	kind: str,
	name: str,
	pod_name: str | None = None,
	container: str | None = None,
	tail_lines: int = 200,
	previous: bool = False,
	namespace: str | None = None,
) -> dict[str, Any]:
	"""Return capped logs for one selected pod backing a Helm Release resource."""
	release = _get_release_scope(release_docname)
	cluster = str(release["cluster"])
	resource_namespace = str(namespace or release.get("namespace") or "default")

	try:
		_assert_release_resource_member(
			release=release,
			kind=kind,
			name=name,
			namespace=resource_namespace,
			allowed_kinds=_LOG_KINDS,
		)
		pods = list_pods_for_resource(cluster, resource_namespace, kind, name)
		selected_pod = _select_pod_name(pods, pod_name)
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return {
			"kind": kind,
			"name": name,
			"namespace": resource_namespace,
			"selected_pod": "",
			"pods": [],
			"logs_by_pod": {},
			"errors_by_pod": {},
			"error": str(e),
		}

	logs_by_pod: dict[str, str] = {}
	errors_by_pod: dict[str, str] = {}
	if selected_pod:
		try:
			logs_by_pod[selected_pod] = get_pod_logs(
				cluster=cluster,
				namespace=resource_namespace,
				pod=selected_pod,
				container=container,
				tail_lines=tail_lines,
				previous=previous,
			)
		except Exception as e:
			logs_by_pod[selected_pod] = ""
			errors_by_pod[selected_pod] = str(e)

	return {
		"kind": kind,
		"name": name,
		"namespace": resource_namespace,
		"selected_pod": selected_pod,
		"pods": pods,
		"logs_by_pod": logs_by_pod,
		"errors_by_pod": errors_by_pod,
		"error": "" if pods else "No pods were found for this resource.",
	}


@frappe.whitelist()
def get_release_resource_events(
	release_docname: str,
	kind: str,
	name: str,
	limit: int = 20,
	namespace: str | None = None,
) -> dict[str, Any]:
	"""Return recent Kubernetes events scoped to one Helm Release resource."""
	release = _get_release_scope(release_docname)
	resource_namespace = str(namespace or release.get("namespace") or "default")
	try:
		_assert_release_resource_member(
			release=release,
			kind=kind,
			name=name,
			namespace=resource_namespace,
			allowed_kinds=_EVENT_KINDS,
		)
		rows = list_resource_events(
			cluster=str(release["cluster"]),
			namespace=resource_namespace,
			kind=kind,
			name=name,
			limit=limit,
		)
		return {"rows": rows, "error": ""}
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return {"rows": [], "error": str(e)}


@frappe.whitelist()
def get_release_resource_rollout(
	release_docname: str,
	kind: str,
	name: str,
	limit: int = 10,
	namespace: str | None = None,
) -> dict[str, Any]:
	"""Return rollout context for a Helm Release workload resource."""
	release = _get_release_scope(release_docname)
	resource_namespace = str(namespace or release.get("namespace") or "default")
	try:
		_assert_release_resource_member(
			release=release,
			kind=kind,
			name=name,
			namespace=resource_namespace,
			allowed_kinds=_ROLLOUT_KINDS,
		)
		rows = get_rollout_history(
			cluster=str(release["cluster"]),
			namespace=resource_namespace,
			kind=kind,
			name=name,
			limit=limit,
		)
		return {"rows": rows, "error": ""}
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return {"rows": [], "error": str(e)}


def _get_release_scope(release_docname: str) -> dict[str, Any]:
	frappe.only_for("System Manager")
	release_docname = str(release_docname or "").strip()
	if not release_docname:
		frappe.throw("Helm Release document name is required.")

	release = frappe.get_doc("Helm Release", release_docname)
	if not release:
		frappe.throw(f"Helm Release '{release_docname}' was not found.")
	release.check_permission("read")

	scope = {
		"cluster": getattr(release, "cluster", None),
		"namespace": getattr(release, "namespace", None),
		"release_name": getattr(release, "release_name", None),
	}
	if not scope.get("cluster"):
		frappe.throw(f"Helm Release '{release_docname}' is missing cluster information.")
	if not scope.get("release_name"):
		frappe.throw(f"Helm Release '{release_docname}' is missing release name information.")
	return scope


def _select_pod_name(pods: list[dict[str, Any]], pod_name: str | None) -> str:
	available_names = [str(pod.get("name") or "") for pod in pods if pod.get("name")]
	if not available_names:
		return ""

	if pod_name is None or str(pod_name).strip() == "":
		return available_names[0]

	selected = _validate_name(pod_name)
	if selected not in available_names:
		raise ValueError("Selected pod is not part of this resource.")
	return selected


def _assert_release_resource_member(
	release: dict[str, Any],
	kind: str,
	name: str,
	namespace: str,
	allowed_kinds: frozenset[str],
) -> None:
	kind = _validate_kind(kind, allowed_kinds)
	name = _validate_name(name)
	namespace = str(namespace or release.get("namespace") or "default")
	release_namespace = str(release.get("namespace") or "default")

	manifest = helm.get_manifest(
		release_name=str(release["release_name"]),
		namespace=release_namespace,
		cluster_name=str(release["cluster"]),
	)
	for resource in manifest:
		metadata = resource.get("metadata") or {}
		resource_kind = str(resource.get("kind") or "")
		resource_name = str(metadata.get("name") or "")
		resource_namespace = str(metadata.get("namespace") or release_namespace)
		if resource_kind == kind and resource_name == name and resource_namespace == namespace:
			return

	raise ValueError("Resource is not part of this Helm Release.")
