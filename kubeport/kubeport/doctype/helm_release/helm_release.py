# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Helm Release Controller

Manages real Helm chart deployments via the Helm CLI.  Each document
represents a single Helm release on a target cluster, backed by
``helm upgrade --install`` and ``helm uninstall`` commands executed
in background tasks.
"""

import frappe
import yaml
from frappe.model.document import Document


class HelmRelease(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		chart: DF.Link
		chart_version: DF.Data | None
		cluster: DF.Link
		helm_revision: DF.Int
		helm_status_detail: DF.SmallText | None
		namespace: DF.Data
		release_name: DF.Data
		status: DF.Literal["Draft", "In Progress", "Deployed", "Degraded", "Uninstalling", "Failed"]
		values: DF.Code | None
	# end: auto-generated types

	def validate(self):
		"""Validate YAML syntax in the values field."""
		if not self.is_new() and self.name != build_release_docname(
			self.cluster,
			self.namespace,
			self.release_name,
		):
			frappe.throw(
				"Cluster, namespace, and release name are immutable after creation. "
				"Create a new Helm Release for a different identity."
			)

		if self.values:
			try:
				parsed = yaml.safe_load(self.values)
				if parsed is not None and not isinstance(parsed, dict):
					frappe.throw(
						"Values must be a YAML mapping (key-value pairs), not a list or scalar."
					)
				_validate_storage_access_modes(parsed)
			except yaml.YAMLError as e:
				frappe.throw(f"Invalid YAML in values: {e}")

	def autoname(self):
		self.name = build_release_docname(
			self.cluster,
			self.namespace,
			self.release_name,
		)

	@frappe.whitelist()
	def deploy_release(self):
		"""Install or upgrade the Helm release via background task.

		Uses ``helm upgrade --install`` for idempotency — creating on first
		deploy, updating on subsequent deploys.
		"""
		if not self.chart:
			frappe.throw("A chart is required.")
		if not self.cluster:
			frappe.throw("A target cluster is required.")
		if self.status == "In Progress":
			frappe.throw("Deployment is already in progress for this release.")
		if self.status == "Uninstalling":
			frappe.throw("Cannot deploy a release while uninstall is in progress.")

		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.helm_tasks.install_or_upgrade_release",
			release_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			f"Deployment of '{self.release_name}' has been queued. "
			"Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def uninstall_release(self):
		"""Uninstall the Helm release via background task."""
		if self.status not in ["Deployed", "Degraded", "Failed"]:
			frappe.throw(
				"Only deployed, degraded, or failed releases can be uninstalled."
			)

		self.db_set("status", "Uninstalling")

		frappe.enqueue(
			"kubeport.tasks.helm_tasks.uninstall_release",
			release_name=self.name,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Release uninstallation has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def load_defaults(self) -> str:
		"""Fetch default values.yaml from the linked chart.

		First checks the cached values on the Helm Chart document.
		Falls back to a live ``helm show values`` call.

		Returns:
			The default values as a YAML string.
		"""
		if not self.chart:
			frappe.throw("Select a chart first.")

		chart_doc = frappe.get_doc("Helm Chart", self.chart)

		# Use cached default values if available
		if chart_doc.default_values:
			return chart_doc.default_values

		# Fall back to a live fetch
		from kubeport.utils.helm import show_values

		chart_ref = chart_doc.get_chart_reference()
		version = self.chart_version or chart_doc.latest_version
		return show_values(chart_ref, version=version)


def _validate_storage_access_modes(values: dict | None) -> None:
	"""Reject storage settings that cannot schedule on common local-path setups."""
	if not values:
		return

	for path, storage_class, access_modes in _iter_storage_configs(values):
		if storage_class != "local-path":
			continue
		if "ReadWriteMany" not in access_modes:
			continue

		frappe.throw(
			"Invalid Helm values: "
			f"{path} uses storageClass 'local-path' with accessModes {access_modes}. "
			"'local-path' only supports ReadWriteOnce on typical K3s/local-path setups."
		)


def build_release_docname(cluster: str | None, namespace: str | None, release_name: str | None) -> str:
	return "/".join([
		str(cluster or "").strip(),
		str(namespace or "default").strip() or "default",
		str(release_name or "").strip(),
	])


def _iter_storage_configs(
	node: dict,
	path: str = "values",
) -> list[tuple[str, str, list[str]]]:
	configs: list[tuple[str, str, list[str]]] = []

	for key, value in node.items():
		current_path = f"{path}.{key}"
		if not isinstance(value, dict):
			continue

		storage_class = value.get("storageClass")
		access_modes = value.get("accessModes")
		if isinstance(storage_class, str) and isinstance(access_modes, list):
			normalized_access_modes = [mode for mode in access_modes if isinstance(mode, str)]
			configs.append((current_path, storage_class, normalized_access_modes))

		configs.extend(_iter_storage_configs(value, current_path))

	return configs
