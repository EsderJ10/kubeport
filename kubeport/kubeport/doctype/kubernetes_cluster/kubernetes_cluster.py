import frappe
from frappe.model.document import Document
from kubernetes import client

from kubeport.utils.k8s_client import get_k8s_api_client

_K8S_API_REQUEST_TIMEOUT_SECONDS = 15.0


class KubernetesCluster(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_server_url: DF.Data | None
		auth_method: DF.Literal["Kubeconfig", "Bearer Token", "In-Cluster"]
		bearer_token: DF.Password | None
		ca_certificate: DF.Code | None
		cluster_name: DF.Data
		kubeconfig: DF.Code | None
		kubeconfig_context: DF.Data | None
		skip_tls_verify: DF.Check
		status: DF.Literal["Pending", "Connected", "Error"]
	# end: auto-generated types

	def validate(self):
		"""Validate that required fields for the selected auth method are present."""
		if self.auth_method == "Kubeconfig":
			if not self.kubeconfig:
				frappe.throw("A kubeconfig is required when using the Kubeconfig auth method.")
			self._validate_kubeconfig_syntax()

		elif self.auth_method == "Bearer Token":
			if not self.api_server_url:
				frappe.throw("An API Server URL is required when using Bearer Token auth.")
			if not self.bearer_token:
				frappe.throw("A Bearer Token is required when using Bearer Token auth.")
			if not self.api_server_url.startswith("https://"):
				frappe.throw("API Server URL must start with https://")
			if not self.skip_tls_verify and not self.ca_certificate:
				frappe.throw(
					"Provide a CA certificate for Bearer Token auth, or explicitly enable "
					"'Skip TLS Verification (Development Only)' for local development."
				)

		# In-Cluster requires no user-provided fields — the service account
		# is auto-detected from the pod environment at connection time.

	def _validate_kubeconfig_syntax(self):
		"""Check that the kubeconfig field contains valid YAML."""
		import yaml

		try:
			parsed = yaml.safe_load(self.kubeconfig)
			if not isinstance(parsed, dict):
				frappe.throw("Kubeconfig must be a YAML mapping, not a scalar or list.")
		except yaml.YAMLError as e:
			frappe.throw(f"Invalid kubeconfig YAML: {e}")

	@frappe.whitelist()
	def test_connection(self):
		"""Test connectivity to the cluster using the configured auth method."""
		try:
			api_client = get_k8s_api_client(self.name)
			v1 = client.CoreV1Api(api_client=api_client)
			nodes = v1.list_node(_request_timeout=_K8S_API_REQUEST_TIMEOUT_SECONDS)

			self.db_set("status", "Connected")
			frappe.msgprint(
				f"Successfully connected. The cluster responded and has {len(nodes.items)} node(s).",
				alert=True,
				indicator="green",
			)

		except Exception as e:
			self.db_set("status", "Error")
			message = str(e)
			if (
				not self.skip_tls_verify
				and self.auth_method in {"Kubeconfig", "Bearer Token"}
				and "CERTIFICATE_VERIFY_FAILED" in message
			):
				message = (
					f"{message} "
					"Enable 'Skip TLS Verification (Development Only)' only for local or "
					"non-production clusters whose API certificate does not match the "
					"configured server address."
				)
			frappe.throw(f"Failed to connect: {message}")
