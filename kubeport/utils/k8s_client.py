import frappe
import urllib3
import yaml
from kubernetes import client, config


def get_k8s_api_client(cluster_name: str) -> client.ApiClient:
	"""Create a Kubernetes API client from a Kubernetes Cluster DocType record.

	Loads the kubeconfig entirely in-memory — no temporary files are written to disk.
	The returned ApiClient can be used to instantiate any K8s API group
	(CoreV1Api, AppsV1Api, etc.).

	If the cluster has ``skip_tls_verify`` enabled (development-only), the client
	is configured to bypass TLS certificate verification. This should never be
	used in production environments.

	Args:
		cluster_name: The name (primary key) of the Kubernetes Cluster DocType.

	Returns:
		A configured kubernetes.client.ApiClient instance.

	Raises:
		frappe.ValidationError: If the cluster record or kubeconfig is missing.
	"""
	cluster_doc = frappe.get_doc("Kubernetes Cluster", cluster_name)

	if not cluster_doc.kubeconfig:
		frappe.throw(f"Kubernetes Cluster '{cluster_name}' has no kubeconfig configured.")

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
