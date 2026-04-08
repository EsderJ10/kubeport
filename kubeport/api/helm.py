"""
Helm API Endpoints

Whitelisted endpoints for Helm chart discovery and release introspection.
These are called from the client-side forms to provide search, values
preview, and live status checking.
"""

import frappe

from kubeport.utils import helm


@frappe.whitelist()
def search_charts(repo_name: str, keyword: str = "") -> list[dict]:
	"""Search for charts in a Helm repository.

	Used by the Helm Release form to browse available charts before
	selecting one.

	Args:
		repo_name: The name of the Helm Repository document.
		keyword: Optional search keyword to filter results.

	Returns:
		A list of chart dicts with keys: name, version, app_version, description.
	"""
	if not repo_name:
		return []

	try:
		return helm.search_repo(repo_name, keyword=keyword)
	except Exception as e:
		frappe.log_error(
			title=f"Helm Chart Search Failed: {repo_name}",
			message=str(e),
		)
		return []


@frappe.whitelist()
def get_chart_default_values(chart_name: str) -> str:
	"""Get the default values.yaml for a Helm Chart.

	First checks the cached values on the Helm Chart document.
	Falls back to a live ``helm show values`` call.

	Args:
		chart_name: The name (primary key) of the Helm Chart document.

	Returns:
		The default values as a YAML string.
	"""
	if not chart_name:
		return ""

	chart_doc = frappe.get_doc("Helm Chart", chart_name)

	if chart_doc.default_values:
		return chart_doc.default_values

	# Live fetch
	try:
		chart_ref = chart_doc.get_chart_reference()
		values = helm.show_values(chart_ref, version=chart_doc.latest_version)
		# Cache it for next time
		chart_doc.db_set("default_values", values)
		return values
	except Exception as e:
		frappe.log_error(
			title=f"Helm Show Values Failed: {chart_name}",
			message=str(e),
		)
		return ""


@frappe.whitelist()
def get_helm_release_status(release_name: str) -> dict:
	"""Get live status of a Helm release from the cluster.

	Calls ``helm status`` and returns the parsed JSON output.

	Args:
		release_name: The name (primary key) of the Helm Release document.

	Returns:
		A dict with the Helm status information.
	"""
	if not release_name:
		return {}

	doc = frappe.get_doc("Helm Release", release_name)

	try:
		return helm.status(
			release_name=doc.release_name,
			namespace=doc.namespace or "default",
			cluster_name=doc.cluster,
		)
	except Exception as e:
		frappe.log_error(
			title=f"Helm Status Check Failed: {release_name}",
			message=str(e),
		)
		return {"error": str(e)}
