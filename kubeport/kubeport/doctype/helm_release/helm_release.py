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

# StorageClasses whose provisioners only support ``ReadWriteOnce``.  Used
# to align injected ``persistence.worker.accessModes`` with what the
# discovered class can actually schedule — kept in sync with
# ``_validate_storage_access_modes``.
_RWO_ONLY_STORAGE_CLASSES = frozenset({"local-path"})


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
		site_image: DF.Link | None
		site_image_detail: DF.HTML | None
		status: DF.Literal["Draft", "In Progress", "Deployed", "Degraded", "Uninstalling", "Failed"]
		values: DF.Code | None
	# end: auto-generated types

	def validate(self) -> None:
		"""Validate YAML syntax in the values field."""
		site_image = getattr(self, "site_image", None)
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
					frappe.throw("Values must be a YAML mapping (key-value pairs), not a list or scalar.")
				_validate_storage_access_modes(parsed)
				_validate_site_image_value_conflicts(parsed, _get_site_image_for_release(site_image))
			except yaml.YAMLError as e:
				frappe.throw(f"Invalid YAML in values: {e}")
		elif site_image:
			_get_site_image_for_release(site_image)

		self.desired_spec_hash = calculate_release_spec_hash(
			chart=self.chart,
			chart_version=self._resolve_chart_version(),
			namespace=self.namespace,
			release_name=self.release_name,
			values_yaml=self.values,
			site_image=site_image,
			site_image_digest=_get_site_image_digest(site_image),
		)
		self.pending_changes = int(
			bool(self.last_applied_spec_hash and self.desired_spec_hash != self.last_applied_spec_hash)
		)

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
			f"Deployment of '{self.release_name}' has been queued. Status will update automatically.",
			alert=True,
			indicator="blue",
		)

	@frappe.whitelist()
	def uninstall_release(self, force: bool = False, confirmation: str = "") -> None:
		"""Uninstall the Helm release via background task."""
		if isinstance(force, str):
			force = force.lower() in ("1", "true", "yes")

		if self.status not in ["Deployed", "Degraded", "Failed"]:
			frappe.throw("Only deployed, degraded, or failed releases can be uninstalled.")

		blocking_sites = _get_uninstall_blocking_sites(self.name)
		if blocking_sites:
			if not force:
				frappe.throw(
					"Cannot uninstall this Helm release while Frappe Site rows still depend on it: "
					f"{_format_blocking_sites(blocking_sites)}. Drop or delete those sites first."
				)
			expected_confirmation = f"{_FORCE_UNINSTALL_PREFIX}{self.release_name}"
			if confirmation != expected_confirmation:
				frappe.throw(f"Force uninstall requires typed confirmation '{expected_confirmation}'.")

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
				"error": _format_observed_state_error(e),
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
		version = self.chart_version or chart_doc.latest_version
		if chart_doc.default_values and str(version or "") == str(chart_doc.latest_version or ""):
			values = chart_doc.default_values
		else:
			# Fall back to a live fetch
			from kubeport.utils.helm import show_values

			chart_ref = chart_doc.get_chart_reference()
			values = show_values(chart_ref, version=version)

		return (
			prepare_release_values(
				values,
				chart_doc,
				cluster_name=self.cluster,
				site_image=getattr(self, "site_image", None),
				allow_site_image_override=True,
			)
			or ""
		)

	@frappe.whitelist()
	def get_release_history(self) -> dict[str, Any]:
		"""Return live Helm revision history for this release."""
		from kubeport.utils import helm

		try:
			history = helm.history(
				release_name=self.release_name,
				namespace=self.namespace or "default",
				cluster_name=self.cluster,
			)
			return {
				"rows": history if isinstance(history, list) else [],
				"error": "",
			}
		except Exception as e:
			frappe.logger("kubeport").warning(
				"Could not read Helm Release history for '%s': %s",
				self.name,
				e,
			)
			return {
				"rows": [],
				"error": _format_observed_state_error(e),
			}

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
	return "/".join(
		[
			str(cluster or "").strip(),
			str(namespace or "default").strip() or "default",
			str(release_name or "").strip(),
		]
	)


def calculate_release_spec_hash(
	chart: str | None,
	chart_version: str | None,
	namespace: str | None,
	release_name: str | None,
	values_yaml: str | None,
	site_image: str | None = None,
	site_image_digest: str | None = None,
) -> str:
	"""Return a stable hash for the Helm desired-state fields Kubeport applies."""
	payload = {
		"chart": str(chart or "").strip(),
		"chart_version": str(chart_version or "").strip(),
		"namespace": str(namespace or "default").strip() or "default",
		"release_name": str(release_name or "").strip(),
		"site_image": str(site_image or "").strip(),
		"site_image_digest": str(site_image_digest or "").strip(),
		"values": _normalize_values_for_hash(values_yaml),
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_site_image_values(
	values_yaml: str | None,
	site_image: str | None,
	default_storage_class: str | None = None,
	allow_image_override: bool = False,
) -> str | None:
	"""Return Helm values YAML with the selected catalog image applied.

	When ``default_storage_class`` is provided and the user's values do
	not already specify ``persistence.worker.storageClass``, that key is
	filled in.  This unblocks the ERPNext chart's required-field check
	without overriding any user-supplied storage class.
	"""
	site_image_doc = _get_site_image_for_release(site_image)
	if not site_image_doc:
		return values_yaml

	values = _normalize_values_for_hash(values_yaml)
	if not isinstance(values, dict):
		frappe.throw("Values must be a YAML mapping (key-value pairs), not a list or scalar.")

	if not allow_image_override:
		_validate_site_image_value_conflicts(values, site_image_doc)
	rendered = dict(values)
	image_values = dict(rendered.get("image") or {})
	image_values.update(
		{
			"repository": site_image_doc["image_repository"],
			"tag": _image_tag_with_digest(site_image_doc["image_tag"], site_image_doc.get("image_digest")),
			"pullPolicy": "IfNotPresent",
		}
	)
	rendered["image"] = image_values

	if default_storage_class:
		persistence = dict(rendered.get("persistence") or {})
		worker = dict(persistence.get("worker") or {})
		if not worker.get("storageClass"):
			worker["storageClass"] = default_storage_class
			# The ERPNext chart defaults ``accessModes: [ReadWriteMany]`` for
			# the worker PVC.  On local-path-style provisioners that mode
			# can't schedule, so when we inject a known-RWO-only class we
			# also downgrade accessModes — but only when the user hasn't
			# specified them, to preserve operator intent on real RWX
			# storage backends.
			if default_storage_class in _RWO_ONLY_STORAGE_CLASSES and "accessModes" not in worker:
				worker["accessModes"] = ["ReadWriteOnce"]
			persistence["worker"] = worker
			rendered["persistence"] = persistence

	return yaml.safe_dump(rendered, default_flow_style=False, sort_keys=False)


def prepare_release_values(
	values_yaml: str | None,
	chart_doc: Any,
	cluster_name: str | None,
	site_image: str | None = None,
	allow_site_image_override: bool = False,
) -> str | None:
	"""Return values YAML ready for Helm for a Kubeport Helm Release.

	Raw chart defaults are not always deployable.  The ERPNext/Frappe chart
	requires a worker StorageClass, so Kubeport derives that value from the
	target cluster before Helm template rendering.  Site-image rendering remains
	optional desired state on top of those chart-specific starter values.
	"""
	values_yaml = render_chart_starter_values(values_yaml, chart_doc, cluster_name)
	return render_site_image_values(
		values_yaml,
		site_image,
		allow_image_override=allow_site_image_override,
	)


def render_chart_starter_values(
	values_yaml: str | None,
	chart_doc: Any,
	cluster_name: str | None,
) -> str | None:
	if not is_frappe_site_chart(chart_doc):
		return values_yaml

	values = _normalize_values_for_hash(values_yaml)
	if not isinstance(values, dict):
		frappe.throw("Values must be a YAML mapping (key-value pairs), not a list or scalar.")

	rendered = dict(values)
	persistence = dict(rendered.get("persistence") or {})
	worker = dict(persistence.get("worker") or {})
	if not _worker_storage_class_is_required(worker) or worker.get("storageClass"):
		return values_yaml

	if not cluster_name:
		frappe.throw("Select a target cluster before loading deployable values for this chart.")

	from kubeport.utils.discovery import discover_default_storage_class

	default_storage_class = discover_default_storage_class(str(cluster_name))
	if not default_storage_class:
		frappe.throw(
			f"Cluster '{cluster_name}' has no default StorageClass annotated. "
			"Either annotate one with "
			"'storageclass.kubernetes.io/is-default-class: \"true\"', or set "
			"'persistence.worker.storageClass' in the Helm Release values."
		)

	worker["storageClass"] = default_storage_class
	if default_storage_class in _RWO_ONLY_STORAGE_CLASSES and _should_force_rwo_access_modes(
		worker.get("accessModes")
	):
		worker["accessModes"] = ["ReadWriteOnce"]

	persistence["worker"] = worker
	rendered["persistence"] = persistence
	return yaml.safe_dump(rendered, default_flow_style=False, sort_keys=False)


def is_frappe_site_chart(chart_doc: Any) -> bool:
	chart_name = _chart_value(chart_doc, "chart_name") or _chart_value(chart_doc, "name")
	return "erpnext" in chart_name.lower() or "frappe" in chart_name.lower()


def _normalize_values_for_hash(values_yaml: str | None) -> Any:
	if not values_yaml:
		return {}

	parsed = yaml.safe_load(values_yaml)
	if parsed is None:
		return {}
	return parsed


def _get_site_image_digest(site_image: str | None) -> str:
	site_image_doc = _get_site_image_for_release(site_image)
	if not site_image_doc:
		return ""
	return str(site_image_doc.get("image_digest") or "")


def _get_site_image_digest_for_hash(site_image: str | None) -> str:
	if not site_image:
		return ""
	return str(frappe.db.get_value("Kubeport Site Image", site_image, "image_digest") or "")


def _image_tag_with_digest(image_tag: str | None, image_digest: str | None) -> str:
	tag = str(image_tag or "").strip()
	digest = str(image_digest or "").strip().lower()
	if not digest:
		return tag
	if "@" in tag:
		return tag
	return f"{tag}@{digest}"


def _chart_value(chart_doc: Any, fieldname: str) -> str:
	if isinstance(chart_doc, dict):
		return str(chart_doc.get(fieldname) or "")
	return str(getattr(chart_doc, fieldname, "") or "")


def _worker_storage_class_is_required(worker: dict) -> bool:
	if worker.get("existingClaim"):
		return False
	return worker.get("enabled") is not False


def _should_force_rwo_access_modes(access_modes: Any) -> bool:
	if access_modes in (None, "", []):
		return True
	if isinstance(access_modes, list):
		return [mode for mode in access_modes if isinstance(mode, str)] == ["ReadWriteMany"]
	return False


def _get_site_image_for_release(site_image: str | None) -> dict[str, str] | None:
	if not site_image:
		return None

	row = frappe.db.get_value(
		"Kubeport Site Image",
		site_image,
		["image_repository", "image_tag", "image_digest", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"Kubeport Site Image '{site_image}' was not found.")

	row = dict(row)
	if row.get("status") == "Deprecated":
		frappe.throw(f"Kubeport Site Image '{site_image}' is deprecated and cannot be deployed.")
	return row


def _validate_site_image_value_conflicts(
	values: dict | None,
	site_image_doc: dict[str, str] | None,
) -> None:
	if not values or not site_image_doc:
		return

	manual_image = values.get("image")
	if manual_image is None:
		return
	if not isinstance(manual_image, dict):
		frappe.throw("Invalid Helm values: image must be a mapping when a Kubeport Site Image is selected.")

	expected = {
		"repository": site_image_doc.get("image_repository") or "",
		"pullPolicy": "IfNotPresent",
	}
	for fieldname, selected_value in expected.items():
		manual_value = manual_image.get(fieldname)
		if manual_value in (None, "") or str(manual_value) == str(selected_value):
			continue
		frappe.throw(
			"Invalid Helm values: selected Kubeport Site Image controls "
			f"image.{fieldname}; remove the manual value '{manual_value}' or clear Site Image."
		)

	base_tag = str(site_image_doc.get("image_tag") or "")
	pinned_tag = _image_tag_with_digest(base_tag, site_image_doc.get("image_digest"))
	manual_tag = manual_image.get("tag")
	if manual_tag not in (None, "") and str(manual_tag) not in (base_tag, pinned_tag):
		frappe.throw(
			"Invalid Helm values: selected Kubeport Site Image controls "
			f"image.tag; remove the manual value '{manual_tag}' or clear Site Image."
		)


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
			blocking_sites.append(
				{
					"name": str(_site_value(site, "name") or ""),
					"site_name": str(_site_value(site, "site_name") or ""),
					"status": status,
				}
			)

	return blocking_sites


def _site_value(site: Any, fieldname: str) -> Any:
	if isinstance(site, dict):
		return site.get(fieldname)
	return getattr(site, fieldname, None)


def _format_blocking_sites(sites: list[dict[str, str]]) -> str:
	return ", ".join(f"{site.get('site_name') or site.get('name')} ({site.get('status')})" for site in sites)


def _format_observed_state_error(error: Exception) -> str:
	from kubeport.utils import helm

	if helm.is_release_not_found_error(error):
		return "No Helm release exists yet for this row. Deploy the release successfully before reading live state."
	return str(error)


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
