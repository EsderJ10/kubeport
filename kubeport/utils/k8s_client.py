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

	# new_client_from_config_dict returns a scoped ApiClient without mutating
	# global state or writing any files to disk.
	api_client = config.new_client_from_config_dict(config_dict=kubeconfig_dict)

	# Development-only: skip TLS verification for local clusters (K3d, Kind, Minikube)
	# whose self-signed certificates don't include the Docker bridge IP in their SAN.
	if cluster_doc.skip_tls_verify:
		api_client.configuration.verify_ssl = False
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

	return api_client


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

	# Use the CA certificate if provided; otherwise skip TLS verification
	if cluster_doc.ca_certificate:
		import tempfile
		import os

		# The kubernetes client needs a file path for ssl_ca_cert, so we
		# write the PEM to a temporary file.  The file persists for the
		# lifetime of this ApiClient instance.
		ca_fd, ca_path = tempfile.mkstemp(suffix=".pem", prefix="kubeport_ca_")
		try:
			os.write(ca_fd, cluster_doc.ca_certificate.encode("utf-8"))
		finally:
			os.close(ca_fd)

		configuration.ssl_ca_cert = ca_path
	else:
		# No CA certificate — skip TLS verification (development only)
		configuration.verify_ssl = False
		urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

	return client.ApiClient(configuration=configuration)


def _client_from_incluster() -> client.ApiClient:
	"""Build a client using in-cluster service account credentials.

	This works when Kubeport is deployed as a pod inside a Kubernetes cluster.
	The service account token and CA certificate are automatically mounted
	by Kubernetes at well-known paths.
	"""
	config.load_incluster_config()
	return client.ApiClient()
