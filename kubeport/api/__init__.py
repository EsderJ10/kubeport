"""
Kubeport Cluster API

Whitelisted endpoints for querying live Kubernetes cluster data and
parsing kubeconfig files uploaded from the browser.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import frappe
import yaml
from kubernetes import client

from kubeport.utils.k8s_client import get_k8s_api_client

_K8S_API_REQUEST_TIMEOUT_SECONDS = 15.0
_LOCAL_KUBECONFIG_HOSTS = {"localhost"}


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
		namespaces = v1.list_namespace(
			_request_timeout=_K8S_API_REQUEST_TIMEOUT_SECONDS,
		)
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
def parse_kubeconfig_contexts(kubeconfig_content: str) -> list[dict[str, Any]]:
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

	result: list[dict[str, Any]] = []
	for ctx in contexts:
		ctx_name = ctx.get("name", "")
		ctx_data = ctx.get("context", {})
		cluster_ref = ctx_data.get("cluster", "")
		user_ref = ctx_data.get("user", "")
		server_info = _normalize_kubeconfig_server(cluster_servers.get(cluster_ref, ""))

		result.append(
			{
				"context_name": ctx_name,
				"cluster_name": cluster_ref,
				"server": server_info["server"],
				"original_server": server_info["original_server"],
				"server_was_normalized": server_info["server_was_normalized"],
				"normalization_reason": server_info["normalization_reason"],
				"user": user_ref,
				"is_current": ctx_name == current_context,
			}
		)

	return result


@frappe.whitelist()
def extract_kubeconfig_context(kubeconfig_content: str, context_name: str) -> dict[str, Any]:
	"""Extract a minimal, self-contained kubeconfig for a single context.

	Given the full kubeconfig YAML and a context name, this returns a new
	kubeconfig containing only the selected context and its referenced
	cluster and user entries.  Equivalent to
	``kubectl config view --minify --flatten --context=<name>``.

	Args:
		kubeconfig_content: Raw YAML string from the uploaded kubeconfig file.
		context_name: The name of the context to extract.

	Returns:
		A dict containing the minimal kubeconfig YAML plus normalized server metadata.
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

	selected_cluster_entry = deepcopy(cluster_entry)
	server_info = _normalize_kubeconfig_server(selected_cluster_entry.get("cluster", {}).get("server", ""))
	selected_cluster_entry.setdefault("cluster", {})["server"] = server_info["server"]

	# Build the minimal kubeconfig
	minimal_config = {
		"apiVersion": "v1",
		"kind": "Config",
		"current-context": context_name,
		"clusters": [selected_cluster_entry],
		"contexts": [deepcopy(context_entry)],
		"users": [deepcopy(user_entry)],
		"preferences": {},
	}

	return {
		"kubeconfig": yaml.dump(minimal_config, default_flow_style=False, sort_keys=False),
		"server": server_info["server"],
		"original_server": server_info["original_server"],
		"server_was_normalized": server_info["server_was_normalized"],
		"normalization_reason": server_info["normalization_reason"],
	}


def _normalize_kubeconfig_server(server: str) -> dict[str, Any]:
	"""Resolve clearly local-only kubeconfig endpoints for containerized dev use."""
	server_info = {
		"server": server,
		"original_server": server,
		"server_was_normalized": False,
		"normalization_reason": "",
	}
	if not server:
		return server_info

	try:
		parsed = urlsplit(server)
	except ValueError:
		return server_info

	if not parsed.scheme or not parsed.netloc or not parsed.hostname:
		return server_info

	reason = _classify_local_kubeconfig_host(parsed.hostname)
	if not reason:
		return server_info

	replacement_host = _get_preferred_gateway_ip()
	if not replacement_host:
		server_info["normalization_reason"] = reason
		return server_info

	server_info["server"] = _replace_server_host(server, replacement_host)
	server_info["server_was_normalized"] = server_info["server"] != server
	server_info["normalization_reason"] = reason
	return server_info


def _classify_local_kubeconfig_host(hostname: str) -> str | None:
	normalized_host = hostname.strip().lower()
	if normalized_host in _LOCAL_KUBECONFIG_HOSTS:
		return "localhost"

	try:
		address = ipaddress.ip_address(normalized_host)
	except ValueError:
		return None

	if address.is_unspecified:
		return "wildcard"
	if address.is_loopback:
		return "loopback"

	return None


def _replace_server_host(server: str, replacement_host: str) -> str:
	parsed = urlsplit(server)
	if not parsed.scheme or not parsed.netloc:
		return server

	netloc = replacement_host
	if parsed.port is not None:
		netloc = f"{replacement_host}:{parsed.port}"

	return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _get_preferred_gateway_ip() -> str | None:
	return _get_default_gateway_ip() or _guess_gateway_ip_from_container_ip()


def _get_default_gateway_ip() -> str | None:
	try:
		with open("/proc/net/route", encoding="utf-8") as route_file:
			next(route_file, None)
			for line in route_file:
				fields = line.strip().split()
				if len(fields) < 4:
					continue

				destination = fields[1]
				gateway = fields[2]
				flags = int(fields[3], 16)

				if destination != "00000000" or not (flags & 0x2):
					continue

				return socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
	except (OSError, ValueError, struct.error):
		return None

	return None


def _guess_gateway_ip_from_container_ip() -> str | None:
	try:
		container_ip = ipaddress.ip_address(socket.gethostbyname(socket.gethostname()))
	except (socket.gaierror, ValueError):
		return None

	if container_ip.version != 4 or container_ip.is_loopback or container_ip.is_unspecified:
		return None

	network = ipaddress.ip_network(f"{container_ip}/24", strict=False)
	gateway_ip = network.network_address + 1
	if gateway_ip == container_ip:
		return None

	return str(gateway_ip)
