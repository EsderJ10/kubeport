"""Migrate legacy Kubernetes Manifest rows into Service Bundle rows."""

from __future__ import annotations

import frappe


_STATUS_MAP = {
	"Applied": "Deployed",
	"Draft": "Draft",
	"In Progress": "In Progress",
	"Degraded": "Degraded",
	"Failed": "Failed",
}


def execute() -> None:
	"""Copy legacy Kubernetes Manifest rows into Service Bundle."""
	if not frappe.db.exists("DocType", "Kubernetes Manifest"):
		return

	manifests = frappe.get_all(
		"Kubernetes Manifest",
		fields=["name", "manifest_name", "cluster", "namespace", "content", "status"],
		order_by="creation asc",
	)

	for manifest in manifests:
		source_name = str(manifest.get("name") or "")
		base_name = str(manifest.get("manifest_name") or source_name)
		if not base_name:
			continue

		existing_target = _find_existing_migration_target(base_name, manifest)
		if existing_target:
			frappe.logger("kubeport").info(
				"Skipping Kubernetes Manifest '%s'; already migrated to Service Bundle '%s'.",
				source_name,
				existing_target,
			)
			continue

		target_name = _next_available_bundle_name(base_name)
		bundle_doc = frappe.get_doc({
			"doctype": "Service Bundle",
			"bundle_name": target_name,
			"cluster": manifest.get("cluster"),
			"namespace": manifest.get("namespace") or "default",
			"content": manifest.get("content") or "",
			"status": _STATUS_MAP.get(str(manifest.get("status") or ""), "Draft"),
		})
		bundle_doc.insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.logger("kubeport").info(
			"Migrated Kubernetes Manifest '%s' to Service Bundle '%s'.",
			source_name,
			target_name,
		)


def _find_existing_migration_target(base_name: str, manifest: dict[str, object]) -> str | None:
	namespace = str(manifest.get("namespace") or "default")
	content = manifest.get("content") or ""
	status = _STATUS_MAP.get(str(manifest.get("status") or ""), "Draft")
	cluster = manifest.get("cluster")

	bundles = frappe.get_all(
		"Service Bundle",
		filters={"cluster": cluster, "namespace": namespace},
		fields=["name", "content", "status"],
	)

	for bundle in bundles:
		bundle_name = str(bundle.get("name") or "")
		if not _is_candidate_bundle_name(bundle_name, base_name):
			continue
		if bundle.get("content") != content:
			continue
		if bundle.get("status") != status:
			continue
		return bundle_name

	return None


def _next_available_bundle_name(base_name: str) -> str:
	if not frappe.db.exists("Service Bundle", base_name):
		return base_name

	suffix = "-migrated"
	candidate = f"{base_name}{suffix}"
	if not frappe.db.exists("Service Bundle", candidate):
		return candidate

	index = 2
	while frappe.db.exists("Service Bundle", f"{candidate}-{index}"):
		index += 1

	return f"{candidate}-{index}"


def _is_candidate_bundle_name(bundle_name: str, base_name: str) -> bool:
	return bundle_name == base_name or bundle_name.startswith(f"{base_name}-migrated")
