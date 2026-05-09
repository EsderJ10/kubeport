"""
Kubernetes API Client Factory

Creates a configured ``kubernetes.client.ApiClient`` instance from a
Kubernetes Cluster DocType record.  Supports multiple authentication methods:

- **Kubeconfig** — loads a user-provided kubeconfig YAML in-memory.
- **Bearer Token** — connects to a remote API server using a service-account
  token and optional CA certificate.
- **In-Cluster** — auto-detects credentials from the pod environment when
  Kubeport is deployed inside Kubernetes.
"""

import atexit
import os
import tempfile

import frappe
import urllib3
import yaml
from kubernetes import client, config


def get_k8s_api_client(cluster_name: str) -> client.ApiClient:
	"""Create a Kubernetes API client from a Kubernetes Cluster DocType record.

	The auth method is determined by the ``auth_method`` field on the cluster
	document.  Each method produces a fully configured ApiClient that can be
	used to instantiate any K8s API group (CoreV1Api, AppsV1Api, etc.).

	Args:
		cluster_name: The name (primary key) of the Kubernetes Cluster DocType.

	Returns:
		A configured kubernetes.client.ApiClient instance.

	Raises:
		frappe.ValidationError: If the cluster record is missing required fields.
	"""
	cluster_doc = frappe.get_doc("Kubernetes Cluster", cluster_name)

	auth_method = cluster_doc.auth_method or "Kubeconfig"

	if auth_method == "Kubeconfig":
		return _client_from_kubeconfig(cluster_doc)
	elif auth_method == "Bearer Token":
		return _client_from_bearer_token(cluster_doc)
	elif auth_method == "In-Cluster":
		return _client_from_incluster()
	else:
		frappe.throw(f"Unknown auth method: {auth_method}")


def _client_from_kubeconfig(cluster_doc) -> client.ApiClient:
	"""Build a client from a kubeconfig YAML string (existing behavior)."""
	if not cluster_doc.kubeconfig:
		frappe.throw(f"Kubernetes Cluster '{cluster_doc.name}' has no kubeconfig configured.")

	kubeconfig_dict = yaml.safe_load(cluster_doc.kubeconfig)

	# The Python client snapshots TLS settings when the ApiClient is built, so
	# development-only TLS bypass must be applied to the kubeconfig payload
	# before constructing the client.
	if cluster_doc.skip_tls_verify:
		for cluster in kubeconfig_dict.get("clusters", []):
			cluster_config = cluster.get("cluster")
			if not isinstance(cluster_config, dict):
				continue
			cluster_config["insecure-skip-tls-verify"] = True
			cluster_config.pop("certificate-authority-data", None)
			cluster_config.pop("certificate-authority", None)
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

	# new_client_from_config_dict returns a scoped ApiClient without mutating
	# global state or writing any files to disk.
	return config.new_client_from_config_dict(config_dict=kubeconfig_dict)


def _client_from_bearer_token(cluster_doc) -> client.ApiClient:
	"""Build a client from an API server URL and service-account token.

	The bearer token is stored as a Frappe Password field, so it's encrypted
	at rest in the database rather than stored as plain text.
	"""
	if not cluster_doc.api_server_url:
		frappe.throw(f"Kubernetes Cluster '{cluster_doc.name}' has no API Server URL configured.")

	token = cluster_doc.get_password("bearer_token")
	if not token:
		frappe.throw(f"Kubernetes Cluster '{cluster_doc.name}' has no bearer token configured.")

	configuration = client.Configuration()
	configuration.host = cluster_doc.api_server_url
	configuration.api_key = {"authorization": f"Bearer {token}"}

	# Development-only: some local clusters expose the API through an IP whose
	# certificate SAN covers a DNS name instead. Allow explicitly bypassing
	# verification for those setups, even when a CA bundle is present.
	if cluster_doc.skip_tls_verify:
		configuration.verify_ssl = False
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
	elif cluster_doc.ca_certificate:
		ca_path = _write_ca_tempfile(cluster_doc.ca_certificate)
		configuration.ssl_ca_cert = ca_path
	else:
		frappe.throw(
			"Bearer Token authentication requires a CA certificate unless "
			"'Skip TLS Verification (Development Only)' is enabled."
		)

	return client.ApiClient(configuration)


# Well-known paths for in-cluster service account credentials.
_INCLUSTER_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_INCLUSTER_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_INCLUSTER_NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _client_from_incluster() -> client.ApiClient:
	"""Build a client using in-cluster service account credentials.

	This works when Kubeport is deployed as a pod inside a Kubernetes cluster.
	The service account token and CA certificate are automatically mounted
	by Kubernetes at well-known paths.

	Unlike ``config.load_incluster_config()``, this reads the credentials
	manually and builds a scoped ``Configuration`` instance, so we never
	mutate the global ``kubernetes.client.configuration``.  This is critical
	for multi-user safety — other threads or background jobs that connect
	to *different* clusters must not be affected.
	"""
	if not os.path.isfile(_INCLUSTER_TOKEN_PATH):
		frappe.throw(
			"In-Cluster auth failed: service account token not found at "
			f"{_INCLUSTER_TOKEN_PATH}. Is Kubeport running inside a Kubernetes pod?"
		)

	with open(_INCLUSTER_TOKEN_PATH) as f:
		token = f.read().strip()

	configuration = client.Configuration()
	configuration.host = "https://kubernetes.default.svc"
	configuration.api_key = {"authorization": f"Bearer {token}"}

	if os.path.isfile(_INCLUSTER_CA_PATH):
		configuration.ssl_ca_cert = _INCLUSTER_CA_PATH
	else:
		configuration.verify_ssl = False
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

	return client.ApiClient(configuration=configuration)


# ---------------------------------------------------------------------------
# Private Utilities
# ---------------------------------------------------------------------------


def _write_ca_tempfile(ca_pem: str) -> str:
	"""Write a PEM string to a temp file and register cleanup on exit.

	The kubernetes client requires a *file path* for ``ssl_ca_cert``, so we
	write the CA certificate to a temporary file.  An ``atexit`` handler
	ensures the file is removed when the worker process terminates, avoiding
	a slow leak of temp files in long-running Frappe workers.

	Returns:
		The absolute path to the temporary PEM file.
	"""
	ca_fd, ca_path = tempfile.mkstemp(suffix=".pem", prefix="kubeport_ca_")
	try:
		os.write(ca_fd, ca_pem.encode("utf-8"))
	finally:
		os.close(ca_fd)

	# Schedule cleanup so temp files don't accumulate across requests
	atexit.register(lambda p=ca_path: os.unlink(p) if os.path.exists(p) else None)

	return ca_path
