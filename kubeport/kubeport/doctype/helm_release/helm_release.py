# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Helm Release Controller.

Manages real Helm chart deployments via the Helm CLI.  Each document
represents a single Helm release on a target cluster, backed by
``helm upgrade --install``, ``helm rollback``, and ``helm uninstall``
commands executed in background tasks.
"""

import hashlib
import json
import secrets
from typing import Any

import frappe
import yaml
from frappe.model.document import Document

# ``Draft`` is the only state that is known not to own cluster resources.
# ``Failed`` releases may still have Helm-owned objects and must go through
# uninstall before direct row deletion.
_DELETE_ALLOWED_STATUS = "Draft"
_BLOCKING_SITE_STATUSES = {"Active", "In Progress", "Deleting", "Migrating"}
_FORCE_UNINSTALL_PREFIX = "UNINSTALL "


class HelmRelease(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		chart: DF.Link
		chart_version: DF.Data | None
		cluster: DF.Link
		desired_spec_hash: DF.Data | None
		helm_revision: DF.Int
		helm_status_detail: DF.SmallText | None
		last_applied_chart_version: DF.Data | None
		last_applied_spec_hash: DF.Data | None
		namespace: DF.Data
		operation_started_at: DF.Datetime | None
		operation_token: DF.Data | None
		operation_type: DF.Literal["", "Deploy", "Upgrade", "Rollback", "Uninstall"]
		pending_changes: DF.Check
		release_name: DF.Data
		status: DF.Literal["Draft", "In Progress", "Deployed", "Degraded", "Uninstalling", "Failed"]
		values: DF.Code | None
	# end: auto-generated types

	def validate(self) -> None:
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

		self.desired_spec_hash = calculate_release_spec_hash(
			chart=self.chart,
			chart_version=self._resolve_chart_version(),
			namespace=self.namespace,
			release_name=self.release_name,
			values_yaml=self.values,
		)
		self.pending_changes = int(bool(
			self.last_applied_spec_hash
			and self.desired_spec_hash != self.last_applied_spec_hash
		))

	def autoname(self) -> None:
		self.name = build_release_docname(
			self.cluster,
			self.namespace,
			self.release_name,
		)

	def on_trash(self) -> None:
		"""Refuse to delete a row that still owns cluster resources.

		The desired-vs-observed split means the row IS the only authority that
		knows a release was created by Kubeport.  Deleting it without first
		uninstalling would leave the helm release running indefinitely with
		nothing tracking it on the control plane side.  Operator must call
		``uninstall_release`` (lands the row in ``Draft``) before trash.
		"""
		if self.status != _DELETE_ALLOWED_STATUS:
			frappe.throw(
				f"Cannot delete '{self.name}' while status is '{self.status}'. "
				"Uninstall the release first to release its cluster resources, "
				"then delete the row."
			)

	@frappe.whitelist()
	def deploy_release(self) -> None:
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

		operation_token = secrets.token_hex(16)
		self.db_set("operation_token", operation_token)
		self.db_set("operation_type", "Deploy" if self.status == "Draft" else "Upgrade")
		self.db_set("operation_started_at", frappe.utils.now_datetime())
		self.db_set("helm_status_detail", "")
		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.helm_tasks.install_or_upgrade_release",
			release_name=self.name,
			operation_token=operation_token,
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
	def uninstall_release(self, force: bool = False, confirmation: str = "") -> None:
		"""Uninstall the Helm release via background task."""
		if isinstance(force, str):
			force = force.lower() in ("1", "true", "yes")

		if self.status not in ["Deployed", "Degraded", "Failed"]:
			frappe.throw(
				"Only deployed, degraded, or failed releases can be uninstalled."
			)

		blocking_sites = _get_uninstall_blocking_sites(self.name)
		if blocking_sites:
			if not force:
				frappe.throw(
					"Cannot uninstall this Helm release while Frappe Site rows still depend on it: "
					f"{_format_blocking_sites(blocking_sites)}. Drop or delete those sites first."
				)
			expected_confirmation = f"{_FORCE_UNINSTALL_PREFIX}{self.release_name}"
			if confirmation != expected_confirmation:
				frappe.throw(
					"Force uninstall requires typed confirmation "
					f"'{expected_confirmation}'."
				)

		operation_token = secrets.token_hex(16)
		self.db_set("operation_token", operation_token)
		self.db_set("operation_type", "Uninstall")
		self.db_set("operation_started_at", frappe.utils.now_datetime())
		self.db_set("helm_status_detail", "")
		self.db_set("status", "Uninstalling")

		frappe.enqueue(
			"kubeport.tasks.helm_tasks.uninstall_release",
			release_name=self.name,
			operation_token=operation_token,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			"Release uninstallation has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def rollback_release(self, revision: int) -> None:
		"""Roll back the release to a prior Helm revision via background task."""
		if self.status in ("In Progress", "Uninstalling"):
			frappe.throw("Another release operation is already in progress.")
		if self.status not in ("Deployed", "Degraded", "Failed"):
			frappe.throw("Rollback is only available for deployed, degraded, or failed releases.")

		try:
			revision = int(revision)
		except (TypeError, ValueError):
			frappe.throw("A numeric Helm revision is required.")
		if revision < 1:
			frappe.throw("Helm revision must be greater than zero.")

		operation_token = secrets.token_hex(16)
		self.db_set("operation_token", operation_token)
		self.db_set("operation_type", "Rollback")
		self.db_set("operation_started_at", frappe.utils.now_datetime())
		self.db_set("helm_status_detail", "")
		self.db_set("status", "In Progress")

		frappe.enqueue(
			"kubeport.tasks.helm_tasks.rollback_release",
			release_name=self.name,
			operation_token=operation_token,
			target_revision=revision,
			queue="long",
			enqueue_after_commit=True,
		)

		frappe.msgprint(
			f"Rollback of '{self.release_name}' to revision {revision} has been queued. "
			"Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def get_release_health(self) -> dict[str, object]:
		"""Return per-resource readiness for the form drilldown panel.

		Pure observed state — never persisted.  Called from the client form
		via ``frappe.xcall`` (per the AGENTS.md form-rendering rule), not
		``onload``, so a slow cluster cannot block document load.
		"""
		from kubeport.utils.release_health import walk

		try:
			return {
				"rows": [r.to_dict() for r in walk(self.name)],
				"error": "",
			}
		except Exception as e:
			frappe.logger("kubeport").warning(
				"Could not read Helm Release health for '%s': %s",
				self.name,
				e,
			)
			return {
				"rows": [],
				"error": str(e),
			}

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

	@frappe.whitelist()
	def get_release_history(self) -> list[dict[str, Any]]:
		"""Return live Helm revision history for this release."""
		from kubeport.utils import helm

		try:
			history = helm.history(
				release_name=self.release_name,
				namespace=self.namespace or "default",
				cluster_name=self.cluster,
			)
			return history if isinstance(history, list) else []
		except Exception as e:
			frappe.logger("kubeport").warning(
				"Could not read Helm Release history for '%s': %s",
				self.name,
				e,
			)
			return []

	def _resolve_chart_version(self) -> str:
		if self.chart_version:
			return str(self.chart_version)
		if not self.chart:
			return ""

		try:
			chart_doc = frappe.get_doc("Helm Chart", self.chart)
			return str(chart_doc.latest_version or "")
		except Exception:
			return ""


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


def calculate_release_spec_hash(
	chart: str | None,
	chart_version: str | None,
	namespace: str | None,
	release_name: str | None,
	values_yaml: str | None,
) -> str:
	"""Return a stable hash for the Helm desired-state fields Kubeport applies."""
	payload = {
		"chart": str(chart or "").strip(),
		"chart_version": str(chart_version or "").strip(),
		"namespace": str(namespace or "default").strip() or "default",
		"release_name": str(release_name or "").strip(),
		"values": _normalize_values_for_hash(values_yaml),
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_values_for_hash(values_yaml: str | None) -> Any:
	if not values_yaml:
		return {}

	parsed = yaml.safe_load(values_yaml)
	if parsed is None:
		return {}
	return parsed


def _get_uninstall_blocking_sites(release_docname: str) -> list[dict[str, str]]:
	try:
		sites = frappe.get_all(
			"Frappe Site",
			filters={"bench_release": release_docname},
			fields=["name", "site_name", "status", "operation_job_name"],
		)
	except Exception:
		# Frappe Site may not exist during partial installs/migrations; do not
		# block Helm cleanup in that bootstrap edge case.
		return []

	blocking_sites: list[dict[str, str]] = []
	for site in sites:
		status = str(_site_value(site, "status") or "")
		operation_job_name = str(_site_value(site, "operation_job_name") or "")
		if status in _BLOCKING_SITE_STATUSES or (status == "Failed" and operation_job_name):
			blocking_sites.append({
				"name": str(_site_value(site, "name") or ""),
				"site_name": str(_site_value(site, "site_name") or ""),
				"status": status,
			})

	return blocking_sites


def _site_value(site: Any, fieldname: str) -> Any:
	if isinstance(site, dict):
		return site.get(fieldname)
	return getattr(site, fieldname, None)


def _format_blocking_sites(sites: list[dict[str, str]]) -> str:
	return ", ".join(
		f"{site.get('site_name') or site.get('name')} ({site.get('status')})"
		for site in sites
	)


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
