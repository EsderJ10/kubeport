"""
Kubeport Cluster API

Whitelisted endpoints for querying live Kubernetes cluster data and
parsing kubeconfig files uploaded from the browser.
"""

import frappe
import yaml
from kubernetes import client

from kubeport.utils.k8s_client import get_k8s_api_client


@frappe.whitelist()
def get_cluster_namespaces(cluster_name: str) -> list[str]:
	"""Return the list of namespace names from a live Kubernetes cluster.

	Called by client-side scripts to populate the namespace field with
	real options fetched from the selected cluster.

	Args:
		cluster_name: The name (primary key) of the Kubernetes Cluster DocType.

	Returns:
		A sorted list of namespace name strings.
	"""
	if not cluster_name:
		return []

	try:
		api_client = get_k8s_api_client(cluster_name)
		v1 = client.CoreV1Api(api_client=api_client)
		namespaces = v1.list_namespace()
		return sorted(ns.metadata.name for ns in namespaces.items)
	except Exception as e:
		frappe.log_error(
			title=f"Namespace Discovery Failed: {cluster_name}",
			message=str(e),
		)
		# Return empty list rather than crashing the form — the user can
		# still type a namespace manually.
		return []


# ---------------------------------------------------------------------------
# Kubeconfig Upload & Context Import
# ---------------------------------------------------------------------------

@frappe.whitelist()
def parse_kubeconfig_contexts(kubeconfig_content: str) -> list[dict]:
	"""Parse a kubeconfig YAML string and return metadata for each context.

	The kubeconfig content is uploaded from the user's browser via the
	FileReader API — it is never read from the server filesystem.

	Args:
		kubeconfig_content: Raw YAML string from the uploaded kubeconfig file.

	Returns:
		A list of context dicts, each containing:
		- context_name: str
		- cluster_name: str
		- server: str
		- user: str
		- is_current: bool
	"""
	if not kubeconfig_content:
		frappe.throw("No kubeconfig content provided.")

	try:
		config = yaml.safe_load(kubeconfig_content)
	except yaml.YAMLError as e:
		frappe.throw(f"Invalid YAML: {e}")

	if not isinstance(config, dict):
		frappe.throw("Kubeconfig must be a YAML mapping.")

	contexts = config.get("contexts", [])
	clusters = config.get("clusters", [])
	current_context = config.get("current-context", "")

	if not contexts:
		frappe.throw("No contexts found in the kubeconfig file.")

	# Build a lookup for cluster → server URL
	cluster_servers: dict[str, str] = {}
	for cluster_entry in clusters:
		name = cluster_entry.get("name", "")
		server = cluster_entry.get("cluster", {}).get("server", "")
		cluster_servers[name] = server

	result = []
	for ctx in contexts:
		ctx_name = ctx.get("name", "")
		ctx_data = ctx.get("context", {})
		cluster_ref = ctx_data.get("cluster", "")
		user_ref = ctx_data.get("user", "")

		result.append({
			"context_name": ctx_name,
			"cluster_name": cluster_ref,
			"server": cluster_servers.get(cluster_ref, ""),
			"user": user_ref,
			"is_current": ctx_name == current_context,
		})

	return result


@frappe.whitelist()
def extract_kubeconfig_context(kubeconfig_content: str, context_name: str) -> str:
	"""Extract a minimal, self-contained kubeconfig for a single context.

	Given the full kubeconfig YAML and a context name, this returns a new
	kubeconfig containing only the selected context and its referenced
	cluster and user entries.  Equivalent to
	``kubectl config view --minify --flatten --context=<name>``.

	Args:
		kubeconfig_content: Raw YAML string from the uploaded kubeconfig file.
		context_name: The name of the context to extract.

	Returns:
		A minimal kubeconfig as a YAML string.
	"""
	if not kubeconfig_content or not context_name:
		frappe.throw("Both kubeconfig content and context name are required.")

	try:
		config = yaml.safe_load(kubeconfig_content)
	except yaml.YAMLError as e:
		frappe.throw(f"Invalid YAML: {e}")

	# Find the selected context
	context_entry = None
	for ctx in config.get("contexts", []):
		if ctx.get("name") == context_name:
			context_entry = ctx
			break

	if not context_entry:
		frappe.throw(f"Context '{context_name}' not found in kubeconfig.")

	ctx_data = context_entry.get("context", {})
	cluster_ref = ctx_data.get("cluster", "")
	user_ref = ctx_data.get("user", "")

	# Find the referenced cluster entry
	cluster_entry = None
	for cl in config.get("clusters", []):
		if cl.get("name") == cluster_ref:
			cluster_entry = cl
			break

	if not cluster_entry:
		frappe.throw(f"Cluster '{cluster_ref}' referenced by context '{context_name}' not found.")

	# Find the referenced user entry
	user_entry = None
	for usr in config.get("users", []):
		if usr.get("name") == user_ref:
			user_entry = usr
			break

	if not user_entry:
		frappe.throw(f"User '{user_ref}' referenced by context '{context_name}' not found.")

	# Build the minimal kubeconfig
	minimal_config = {
		"apiVersion": "v1",
		"kind": "Config",
		"current-context": context_name,
		"clusters": [cluster_entry],
		"contexts": [context_entry],
		"users": [user_entry],
		"preferences": {},
	}

	return yaml.dump(minimal_config, default_flow_style=False, sort_keys=False)
