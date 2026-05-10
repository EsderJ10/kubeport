"""
Kubernetes Command Tasks

Backs the ``Kubernetes Command`` DocType.  Provides a single entry point
(``run_kubernetes_command``) that the Frappe scheduler invokes for queued
Delete actions, plus an inline executor used for read actions (Get / List).

Read-action results are stored on the command row purely as an audit trail
of what the operator saw at execution time — they are NOT used as a source
of truth for any other DocType (CLAUDE.md "discovery is read-only" still
holds: nothing else reads from this output).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import frappe
from kubernetes import client
from kubernetes.client.rest import ApiException

from kubeport.utils import metrics
from kubeport.utils.k8s_client import get_k8s_api_client

_OUTPUT_LIMIT = 5000
_AUDIT_EXCERPT_LIMIT = 500
_TRUNCATION_MARKER = "\n[... truncated, original {N} chars ...]"

# Maps each supported resource kind to (api_class_name, namespaced_method_suffix).
# The kubernetes-python client exposes uniform method names of the form
# "{verb}_namespaced_{snake_kind}", so we only store the suffix once.
_RESOURCE_DISPATCH: dict[str, tuple[str, str]] = {
	"PersistentVolumeClaim": ("CoreV1Api", "persistent_volume_claim"),
	"Pod": ("CoreV1Api", "pod"),
	"Job": ("BatchV1Api", "job"),
	"Secret": ("CoreV1Api", "secret"),
	"ConfigMap": ("CoreV1Api", "config_map"),
	"Service": ("CoreV1Api", "service"),
	"Deployment": ("AppsV1Api", "deployment"),
	"StatefulSet": ("AppsV1Api", "stateful_set"),
}


def run_kubernetes_command(command_docname: str, correlation_id: str | None = None) -> None:
	"""Background entry point used by the Delete enqueue path."""
	with metrics.correlation_scope(correlation_id):
		metrics.logger("kubeport.k8scmd").info(
			"worker enter run_kubernetes_command command=%s", command_docname
		)
		_execute_command(command_docname)


def _execute_command(command_docname: str) -> None:
	cmd = frappe.get_doc("Kubernetes Command", command_docname)
	dispatch = _RESOURCE_DISPATCH.get(cmd.resource_kind)
	if not dispatch:
		_finalize(cmd, status="Failed", output=f"Unsupported resource kind: {cmd.resource_kind}")
		return

	api_class_name, suffix = dispatch
	try:
		api_client = get_k8s_api_client(cmd.cluster)
		api = getattr(client, api_class_name)(api_client=api_client)

		if cmd.action == "Get":
			method = getattr(api, f"read_namespaced_{suffix}")
			resource = method(name=cmd.resource_name, namespace=cmd.namespace, _request_timeout=15)
			output = _summarize_resource(resource)
		elif cmd.action == "List":
			method = getattr(api, f"list_namespaced_{suffix}")
			kwargs: dict[str, Any] = {"namespace": cmd.namespace, "_request_timeout": 15}
			if cmd.label_selector:
				kwargs["label_selector"] = cmd.label_selector
			result = method(**kwargs)
			items = list(result.items or [])
			lines = [f"{len(items)} {cmd.resource_kind}(s) in namespace '{cmd.namespace}':"]
			lines.extend(_summarize_resource(item) for item in items)
			output = "\n".join(lines)
		elif cmd.action == "Delete":
			method = getattr(api, f"delete_namespaced_{suffix}")
			method(name=cmd.resource_name, namespace=cmd.namespace, _request_timeout=15)
			output = (
				f"Deleted {cmd.resource_kind} '{cmd.resource_name}' "
				f"in namespace '{cmd.namespace}' on cluster '{cmd.cluster}'."
			)
		else:
			_finalize(cmd, status="Failed", output=f"Unsupported action: {cmd.action}")
			return

		_finalize(cmd, status="Completed", output=output)
	except ApiException as e:
		body = (e.body or "").strip()
		message = f"Kubernetes API error ({e.status} {e.reason})"
		if body:
			message = f"{message}: {body}"
		_finalize(cmd, status="Failed", output=message)
	except Exception as e:
		_finalize(cmd, status="Failed", output=f"Error: {e}")


def _finalize(cmd: Any, *, status: str, output: str) -> None:
	original_length = len(output)
	if original_length > _OUTPUT_LIMIT:
		marker = _TRUNCATION_MARKER.format(N=original_length)
		# Reserve space for the marker so the operator always sees it.
		output = output[: _OUTPUT_LIMIT - len(marker)] + marker
	completed_at = datetime.now(UTC)
	cmd.db_set("status", status)
	cmd.db_set("output", output)
	cmd.db_set("completed_at", completed_at)
	_append_audit_log(cmd, status=status, output=output, completed_at=completed_at)


def _append_audit_log(cmd: Any, *, status: str, output: str, completed_at: datetime) -> None:
	"""Insert an append-only audit log row.

	Decoupled from the source ``Kubernetes Command`` so the audit trail
	survives row deletion.  Errors writing the audit log are logged but
	never propagate — the command itself already finalized, and a missing
	audit entry is preferable to spurious task retries.
	"""
	try:
		excerpt = (output or "")[:_AUDIT_EXCERPT_LIMIT]
		audit = frappe.get_doc(
			{
				"doctype": "Kubernetes Command Audit Log",
				"command": cmd.name,
				"action": cmd.action,
				"resource_kind": cmd.resource_kind,
				"resource_name": cmd.resource_name or "",
				"cluster": cmd.cluster,
				"namespace": cmd.namespace or "",
				"label_selector": cmd.label_selector or "",
				"outcome": status,
				"executed_at": completed_at,
				"triggered_by": cmd.triggered_by,
				"output_excerpt": excerpt,
			}
		)
		audit.insert(ignore_permissions=True)
	except Exception as audit_err:
		frappe.logger("kubeport").warning(
			"Could not write Kubernetes Command Audit Log for '%s': %s",
			cmd.name,
			audit_err,
		)


def _summarize_resource(resource: Any) -> str:
	"""One-line human-readable summary of a Kubernetes object."""
	metadata = getattr(resource, "metadata", None)
	name = getattr(metadata, "name", "?") if metadata else "?"
	namespace = getattr(metadata, "namespace", "") if metadata else ""

	parts = [f"name={name}"]
	if namespace:
		parts.append(f"namespace={namespace}")

	status = getattr(resource, "status", None)
	if status is not None:
		phase = getattr(status, "phase", "") or ""
		if phase:
			parts.append(f"phase={phase}")

	spec = getattr(resource, "spec", None)
	if spec is not None:
		access_modes = getattr(spec, "access_modes", None)
		if access_modes:
			parts.append(f"accessModes={list(access_modes)}")

	return " | ".join(parts)
