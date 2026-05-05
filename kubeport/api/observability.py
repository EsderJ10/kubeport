"""
Read-only observability endpoints for Helm Release form drilldowns.

Every endpoint resolves cluster identity from the Helm Release row.  Client
input is limited to the resource currently shown in the readiness table.
"""

from __future__ import annotations

from typing import Any

import frappe

from kubeport.utils.observability import (
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
	container: str | None = None,
	tail_lines: int = 200,
	previous: bool = False,
) -> dict[str, Any]:
	"""Return capped logs for pods backing one Helm Release resource."""
	release = _get_release_scope(release_docname)
	cluster = str(release["cluster"])
	namespace = str(release.get("namespace") or "default")

	try:
		pods = list_pods_for_resource(cluster, namespace, kind, name)
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return {
			"kind": kind,
			"name": name,
			"namespace": namespace,
			"pods": [],
			"logs_by_pod": {},
			"errors_by_pod": {},
			"error": str(e),
		}

	logs_by_pod: dict[str, str] = {}
	errors_by_pod: dict[str, str] = {}
	for pod in pods:
		pod_name = str(pod.get("name") or "")
		if not pod_name:
			continue
		try:
			logs_by_pod[pod_name] = get_pod_logs(
				cluster=cluster,
				namespace=namespace,
				pod=pod_name,
				container=container,
				tail_lines=tail_lines,
				previous=previous,
			)
		except Exception as e:
			logs_by_pod[pod_name] = ""
			errors_by_pod[pod_name] = str(e)

	return {
		"kind": kind,
		"name": name,
		"namespace": namespace,
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
) -> list[dict[str, Any]]:
	"""Return recent Kubernetes events scoped to one Helm Release resource."""
	release = _get_release_scope(release_docname)
	try:
		return list_resource_events(
			cluster=str(release["cluster"]),
			namespace=str(release.get("namespace") or "default"),
			kind=kind,
			name=name,
			limit=limit,
		)
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return [{
			"type": "Error",
			"reason": "EventLookupFailed",
			"message": str(e),
			"count": 0,
			"first_seen": "",
			"last_seen": "",
			"source": "kubeport",
		}]


@frappe.whitelist()
def get_release_resource_rollout(
	release_docname: str,
	kind: str,
	name: str,
	limit: int = 10,
) -> list[dict[str, Any]]:
	"""Return rollout context for a Helm Release workload resource."""
	release = _get_release_scope(release_docname)
	try:
		return get_rollout_history(
			cluster=str(release["cluster"]),
			namespace=str(release.get("namespace") or "default"),
			kind=kind,
			name=name,
			limit=limit,
		)
	except ValueError as e:
		frappe.throw(str(e))
	except Exception as e:
		return [{
			"kind": "Error",
			"name": "",
			"revision": "",
			"created_at": "",
			"change_cause": str(e),
			"current": False,
			"image_summary": "",
		}]


def _get_release_scope(release_docname: str) -> dict[str, Any]:
	release_docname = str(release_docname or "").strip()
	if not release_docname:
		frappe.throw("Helm Release document name is required.")

	release = frappe.db.get_value(
		"Helm Release",
		release_docname,
		["cluster", "namespace"],
		as_dict=True,
	)
	if not release:
		frappe.throw(f"Helm Release '{release_docname}' was not found.")
	if not release.get("cluster"):
		frappe.throw(f"Helm Release '{release_docname}' is missing cluster information.")
	return release
