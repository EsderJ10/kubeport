"""
Cluster Discovery API

Whitelisted endpoints for live, read-only discovery of Helm releases and
Frappe sites in a Kubernetes cluster.
"""

from __future__ import annotations

from typing import Any

import frappe
import yaml

from kubeport.utils.discovery import (
	build_nip_io_hostname,
	discover_cluster_issuers,
	discover_cluster_releases,
	discover_ingress_classes,
	discover_ingress_controller_addresses,
	discover_release_sites,
)
from kubeport.utils.k8s_client import get_k8s_api_client


@frappe.whitelist()
def get_cluster_discovery(cluster_name: str) -> dict[str, Any]:
	"""Return live discovery data for a Kubernetes Cluster form view."""
	response: dict[str, Any] = {
		"cluster": cluster_name or "",
		"generated_at": frappe.utils.now(),
		"benches": [],
		"sites": [],
		"capabilities": _empty_cluster_capabilities(),
		"errors": [],
	}

	if not cluster_name:
		response["errors"].append(
			{
				"scope": "cluster",
				"message": "Cluster name is required.",
			}
		)
		return response

	response["capabilities"] = _discover_cluster_capabilities(cluster_name, response["errors"])

	try:
		releases = discover_cluster_releases(cluster_name)
		response["benches"] = _annotate_tracking_state(cluster_name, releases)
	except Exception as e:
		frappe.log_error(
			title=f"Cluster Discovery Failed: {cluster_name}",
			message=str(e),
		)
		response["errors"].append(
			{
				"scope": "cluster",
				"message": f"Failed to list Helm releases: {e}",
			}
		)
		return response

	frappe_releases = [release for release in releases if release.get("is_frappe_bench")]
	if not frappe_releases:
		return response

	try:
		api_client = get_k8s_api_client(cluster_name)
	except Exception as e:
		frappe.log_error(
			title=f"Site Discovery Auth Failed: {cluster_name}",
			message=str(e),
		)
		response["errors"].append(
			{
				"scope": "cluster",
				"message": f"Failed to initialize Kubernetes site discovery: {e}",
			}
		)
		return response

	for release in frappe_releases:
		try:
			response["sites"].extend(discover_release_sites(api_client, release))
		except Exception as e:
			frappe.log_error(
				title=f"Site Discovery Failed: {cluster_name}/{release.get('release_name', '')}",
				message=str(e),
			)
			response["errors"].append(
				{
					"scope": "release",
					"release_name": release.get("release_name", ""),
					"namespace": release.get("namespace", "default"),
					"message": f"Failed to discover sites for release '{release.get('release_name', '')}': {e}",
				}
			)

	return response


@frappe.whitelist()
def get_ingress_suggestions(cluster_name: str, release_name: str | None = None) -> dict[str, Any]:
	"""Return live ingress suggestions for the Helm Release form.

	This endpoint is read-only.  It never persists discovered cluster state.
	"""
	response: dict[str, Any] = {
		"cluster": cluster_name or "",
		"release_name": release_name or "",
		"ingress_classes": [],
		"default_ingress_class": "",
		"cluster_issuers": [],
		"default_cluster_issuer": "",
		"controller_addresses": [],
		"suggested_hostname": "",
		"capabilities": _empty_cluster_capabilities(),
		"errors": [],
	}

	if not cluster_name:
		response["errors"].append(
			{
				"scope": "cluster",
				"message": "Cluster name is required.",
			}
		)
		return response

	_capability_bits = _discover_cluster_capabilities(cluster_name, response["errors"])
	response["capabilities"] = _capability_bits
	response["ingress_classes"] = _capability_bits["ingress"]["ingress_classes"]
	response["cluster_issuers"] = _capability_bits["cert_manager"]["cluster_issuers"]
	response["controller_addresses"] = _capability_bits["ingress"]["controller_addresses"]
	response["default_ingress_class"] = _pick_default_ingress_class(response["ingress_classes"])
	response["default_cluster_issuer"] = _pick_default_cluster_issuer(response["cluster_issuers"])
	response["suggested_hostname"] = _suggest_hostname(
		release_name,
		response["controller_addresses"],
	)
	return response


def _empty_cluster_capabilities() -> dict[str, Any]:
	return {
		"ingress": {
			"available": False,
			"ingress_classes": [],
			"controller_addresses": [],
		},
		"cert_manager": {
			"available": False,
			"cluster_issuers": [],
		},
	}


def _discover_cluster_capabilities(cluster_name: str, errors: list[dict[str, str]]) -> dict[str, Any]:
	capabilities = _empty_cluster_capabilities()

	try:
		ingress_classes = discover_ingress_classes(cluster_name)
		capabilities["ingress"]["ingress_classes"] = ingress_classes
	except Exception as e:
		frappe.log_error(title=f"IngressClass Discovery Failed: {cluster_name}", message=str(e))
		errors.append(
			{
				"scope": "ingress_classes",
				"message": f"Failed to list IngressClasses: {e}",
			}
		)

	try:
		controller_addresses = discover_ingress_controller_addresses(cluster_name)
		capabilities["ingress"]["controller_addresses"] = controller_addresses
	except Exception as e:
		frappe.log_error(title=f"Ingress Controller Discovery Failed: {cluster_name}", message=str(e))
		errors.append(
			{
				"scope": "ingress_controller",
				"message": f"Failed to inspect ingress controller Services: {e}",
			}
		)

	ingress_classes = capabilities["ingress"]["ingress_classes"]
	controller_addresses = capabilities["ingress"]["controller_addresses"]
	capabilities["ingress"]["available"] = bool(ingress_classes or controller_addresses)

	try:
		issuer_state = discover_cluster_issuers(cluster_name)
		capabilities["cert_manager"]["available"] = bool(issuer_state.get("available"))
		capabilities["cert_manager"]["cluster_issuers"] = issuer_state.get("issuers", [])
	except Exception as e:
		frappe.log_error(title=f"ClusterIssuer Discovery Failed: {cluster_name}", message=str(e))
		errors.append(
			{
				"scope": "cluster_issuers",
				"message": f"Failed to list cert-manager ClusterIssuers: {e}",
			}
		)

	return capabilities


def _pick_default_ingress_class(rows: list[dict[str, Any]]) -> str:
	default_row = next((row for row in rows if row.get("is_default")), None)
	if default_row:
		return str(default_row.get("name") or "")
	if len(rows) == 1:
		return str(rows[0].get("name") or "")
	return ""


def _pick_default_cluster_issuer(rows: list[dict[str, Any]]) -> str:
	ready_row = next((row for row in rows if row.get("ready")), None)
	if ready_row:
		return str(ready_row.get("name") or "")
	if rows:
		return str(rows[0].get("name") or "")
	return ""


def _suggest_hostname(release_name: str | None, controller_addresses: list[dict[str, Any]]) -> str:
	for row in controller_addresses:
		if row.get("address_type") != "ip":
			continue
		hostname = build_nip_io_hostname(release_name, str(row.get("address") or ""))
		if hostname:
			return hostname
	return ""


@frappe.whitelist()
def adopt_helm_release(cluster_name: str, namespace: str, release_name: str) -> dict[str, Any]:
	"""Create a Helm Release desired-state row for an existing live release.

	This is an explicit user action from discovery.  Discovery itself remains
	read-only and never persists live cluster inventory.
	"""
	from kubeport.kubeport.doctype.helm_release.helm_release import (
		build_release_docname,
		calculate_release_spec_hash,
	)
	from kubeport.utils import helm
	from kubeport.utils.release_health import classify_release_from_cluster

	cluster_name = str(cluster_name or "").strip()
	namespace = str(namespace or "default").strip() or "default"
	release_name = str(release_name or "").strip()
	if not cluster_name or not release_name:
		frappe.throw("Cluster and release name are required.")

	docname = build_release_docname(cluster_name, namespace, release_name)
	if frappe.db.exists("Helm Release", docname):
		return {
			"name": docname,
			"created": False,
		}

	live_release = _find_discovered_release(cluster_name, namespace, release_name)
	chart_name = str(live_release.get("chart_name") or "")
	chart_version = str(live_release.get("chart_version") or "")
	chart_docname = _resolve_chart_docname(chart_name, chart_version)
	values_yaml = _normalize_values_yaml(
		helm.get_values(
			release_name=release_name,
			namespace=namespace,
			cluster_name=cluster_name,
		)
	)

	doc = frappe.get_doc(
		{
			"doctype": "Helm Release",
			"release_name": release_name,
			"cluster": cluster_name,
			"namespace": namespace,
			"chart": chart_docname,
			"chart_version": chart_version,
			"values": values_yaml,
			"status": "Draft",
		}
	)
	doc.insert()

	helm_result = helm.status(
		release_name=release_name,
		namespace=namespace,
		cluster_name=cluster_name,
	)
	runtime_status = ""
	revision = 0
	if isinstance(helm_result, dict):
		revision = helm_result.get("version", 0)
		info = helm_result.get("info", {})
		if isinstance(info, dict):
			runtime_status = str(info.get("status") or "")

	status, detail = classify_release_from_cluster(
		release_docname=doc.name,
		runtime_status=runtime_status or str(live_release.get("status") or ""),
	)
	spec_hash = calculate_release_spec_hash(
		chart=chart_docname,
		chart_version=chart_version,
		namespace=namespace,
		release_name=release_name,
		values_yaml=values_yaml,
	)
	frappe.db.set_value(
		"Helm Release",
		doc.name,
		{
			"status": status,
			"helm_revision": revision,
			"helm_status_detail": detail,
			"desired_spec_hash": spec_hash,
			"last_applied_spec_hash": spec_hash,
			"last_applied_chart_version": chart_version,
			"pending_changes": 0,
		},
	)

	return {
		"name": doc.name,
		"created": True,
		"status": status,
	}


def _annotate_tracking_state(
	cluster_name: str,
	releases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	from kubeport.kubeport.doctype.helm_release.helm_release import build_release_docname

	annotated: list[dict[str, Any]] = []
	for release in releases:
		namespace = str(release.get("namespace") or "default")
		release_name = str(release.get("release_name") or "")
		docname = build_release_docname(cluster_name, namespace, release_name)
		row = dict(release)
		row["helm_release_docname"] = docname
		try:
			row["is_tracked"] = bool(frappe.db.exists("Helm Release", docname))
		except Exception:
			row["is_tracked"] = False
		annotated.append(row)
	return annotated


def _find_discovered_release(
	cluster_name: str,
	namespace: str,
	release_name: str,
) -> dict[str, Any]:
	for release in discover_cluster_releases(cluster_name):
		if (
			str(release.get("namespace") or "default") == namespace
			and str(release.get("release_name") or "") == release_name
		):
			return release
	frappe.throw(f"Helm release '{release_name}' was not found in namespace '{namespace}'.")


def _resolve_chart_docname(chart_name: str, chart_version: str) -> str:
	if not chart_name:
		frappe.throw("The live Helm release did not report a chart name.")

	charts = frappe.get_all(
		"Helm Chart",
		filters={"chart_name": chart_name},
		fields=["name"],
	)
	if not charts:
		frappe.throw(f"No synced Helm Chart matches '{chart_name}'. Sync the chart repository first.")
	if len(charts) == 1:
		return charts[0].name

	chart_names = [chart.name for chart in charts]
	if chart_version:
		matching_versions = frappe.get_all(
			"Helm Chart Version",
			filters={
				"parent": ["in", chart_names],
				"version": chart_version,
			},
			pluck="parent",
		)
		if len(matching_versions) == 1:
			return matching_versions[0]

	frappe.throw(
		f"Multiple synced Helm Charts match '{chart_name}'. "
		"Create the Helm Release manually and choose the repository."
	)


def _normalize_values_yaml(values_yaml: str | None) -> str:
	if not values_yaml:
		return ""
	try:
		parsed = yaml.safe_load(values_yaml)
	except yaml.YAMLError:
		return values_yaml
	if parsed in (None, {}):
		return ""
	if not isinstance(parsed, dict):
		return values_yaml
	return yaml.safe_dump(parsed, default_flow_style=False, sort_keys=True)
