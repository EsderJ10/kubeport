"""
Read-only helm-diff preview endpoint for the Helm Release form.

Renders the chart locally with ``helm template`` (no cluster contact) and
compares against the live release manifest from ``helm get manifest``.
Both calls are read-only and safe to issue from the web thread per
``AGENTS.md`` §Async-First Execution — neither mutates cluster state.
"""

from __future__ import annotations

import difflib
from typing import Any

import frappe
import yaml

from kubeport.kubeport.doctype.helm_release.helm_release import prepare_release_values
from kubeport.utils import helm


@frappe.whitelist()
def preview_release(name: str) -> dict[str, Any]:
	"""Return a unified diff of the desired vs live manifest for a Helm Release.

	Args:
		name: ``Helm Release`` document name (``cluster/namespace/release_name``).

	Returns:
		``{
			"diff": str,           # unified diff text, empty when no changes
			"added": int,          # count of resources only in desired
			"removed": int,        # count of resources only on the cluster
			"changed": int,        # count of resources whose YAML differs
			"unchanged": int,      # count of resources matching live state
			"live_present": bool,  # False when no Helm release exists yet
			"error": str,          # human-readable failure, empty on success
		}``
	"""
	frappe.only_for("System Manager")
	name = str(name or "").strip()
	if not name:
		frappe.throw("Helm Release document name is required.")

	release = frappe.get_doc("Helm Release", name)
	release.check_permission("read")

	if not release.chart:
		frappe.throw(f"Helm Release '{name}' has no chart configured.")
	if not release.cluster:
		frappe.throw(f"Helm Release '{name}' has no target cluster configured.")

	chart_doc = frappe.get_doc("Helm Chart", release.chart)
	chart_ref = chart_doc.get_chart_reference()
	version = release.chart_version or chart_doc.latest_version
	namespace = release.namespace or "default"

	try:
		values_yaml = prepare_release_values(
			release.values,
			chart_doc,
			cluster_name=release.cluster,
			site_image=getattr(release, "site_image", None),
			ingress_enabled=getattr(release, "ingress_enabled", 0),
			ingress_hostname=getattr(release, "ingress_hostname", None),
			ingress_class_name=getattr(release, "ingress_class_name", None),
			ingress_cluster_issuer=getattr(release, "ingress_cluster_issuer", None),
			release_name=release.release_name,
		)
	except Exception as e:
		return _error_payload(f"Failed to prepare values: {e}")

	try:
		desired = helm.template(
			release_name=release.release_name,
			chart_ref=chart_ref,
			namespace=namespace,
			values_yaml=values_yaml,
			chart_version=version,
		)
	except Exception as e:
		return _error_payload(f"helm template failed: {e}")

	live: list[dict] = []
	live_present = True
	try:
		live = helm.get_manifest(
			release_name=release.release_name,
			namespace=namespace,
			cluster_name=release.cluster,
		)
	except Exception as e:
		if helm.is_release_not_found_error(e):
			live_present = False
		else:
			return _error_payload(f"helm get manifest failed: {e}")

	return _build_diff(desired, live, live_present, namespace)


def _build_diff(
	desired: list[dict],
	live: list[dict],
	live_present: bool,
	default_namespace: str,
) -> dict[str, Any]:
	desired_index = _index_resources(desired, default_namespace)
	live_index = _index_resources(live, default_namespace)

	added = 0
	removed = 0
	changed = 0
	unchanged = 0
	chunks: list[str] = []

	for key in sorted(set(desired_index) | set(live_index)):
		desired_doc = desired_index.get(key)
		live_doc = live_index.get(key)
		desired_text = _canonicalize(desired_doc)
		live_text = _canonicalize(live_doc)
		label = "/".join(part for part in key if part)

		if desired_doc is None:
			removed += 1
		elif live_doc is None:
			added += 1
		elif desired_text == live_text:
			unchanged += 1
			continue
		else:
			changed += 1

		chunk = "\n".join(
			difflib.unified_diff(
				live_text.splitlines(),
				desired_text.splitlines(),
				fromfile=f"live {label}",
				tofile=f"desired {label}",
				lineterm="",
			)
		)
		if chunk:
			chunks.append(chunk)

	return {
		"diff": "\n".join(chunks),
		"added": added,
		"removed": removed,
		"changed": changed,
		"unchanged": unchanged,
		"live_present": live_present,
		"error": "",
	}


def _index_resources(
	documents: list[dict],
	default_namespace: str,
) -> dict[tuple[str, str, str], dict]:
	"""Index resources by ``(kind, namespace, name)`` for diff matching.

	``helm template`` and ``helm get manifest`` should both inject the
	release namespace, but defensive normalisation keeps cluster-scoped
	resources ("") and namespaced resources aligned across both sides.
	"""
	index: dict[tuple[str, str, str], dict] = {}
	for doc in documents:
		if not isinstance(doc, dict):
			continue
		kind = str(doc.get("kind") or "").strip()
		metadata = doc.get("metadata") or {}
		if not isinstance(metadata, dict):
			continue
		resource_name = str(metadata.get("name") or "").strip()
		if not kind or not resource_name:
			continue
		raw_namespace = str(metadata.get("namespace") or "").strip()
		namespace = raw_namespace or (default_namespace if _is_namespaced_kind(kind) else "")
		index[(kind, namespace, resource_name)] = doc
	return index


def _canonicalize(doc: dict | None) -> str:
	if doc is None:
		return ""
	return yaml.safe_dump(doc, default_flow_style=False, sort_keys=True).rstrip()


def _is_namespaced_kind(kind: str) -> bool:
	return kind not in _CLUSTER_SCOPED_KINDS


def _error_payload(message: str) -> dict[str, Any]:
	return {
		"diff": "",
		"added": 0,
		"removed": 0,
		"changed": 0,
		"unchanged": 0,
		"live_present": False,
		"error": message,
	}


# Subset of cluster-scoped Kubernetes kinds Kubeport may render via Helm.
# Used only to keep namespace normalisation symmetric across desired and
# live indexes — over-listing here is harmless because the same default is
# applied on both sides.
_CLUSTER_SCOPED_KINDS = frozenset(
	{
		"Namespace",
		"ClusterRole",
		"ClusterRoleBinding",
		"PersistentVolume",
		"StorageClass",
		"CustomResourceDefinition",
		"PriorityClass",
	}
)
