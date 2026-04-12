# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.utils.k8s_client import _client_from_bearer_token


class UnitTestK8sClient(UnitTestCase):
	@patch("kubeport.utils.k8s_client.urllib3.disable_warnings")
	@patch("kubeport.utils.k8s_client.client.ApiClient")
	@patch("kubeport.utils.k8s_client._write_ca_tempfile")
	def test_client_from_bearer_token_uses_ca_certificate_when_tls_verification_enabled(
		self,
		mock_write_ca_tempfile,
		mock_api_client,
		mock_disable_warnings,
	):
		cluster_doc = SimpleNamespace(
			name="cluster-a",
			api_server_url="https://cluster.example",
			ca_certificate="-----BEGIN CERTIFICATE-----\nPEM\n-----END CERTIFICATE-----",
			skip_tls_verify=0,
			get_password=lambda fieldname: "token-123",
		)
		mock_write_ca_tempfile.return_value = "/tmp/kubeport_ca_cluster_a.pem"

		_client_from_bearer_token(cluster_doc)

		configuration = mock_api_client.call_args.args[0]
		self.assertEqual(configuration.host, "https://cluster.example")
		self.assertEqual(configuration.api_key["authorization"], "Bearer token-123")
		self.assertEqual(configuration.ssl_ca_cert, "/tmp/kubeport_ca_cluster_a.pem")
		mock_write_ca_tempfile.assert_called_once_with(cluster_doc.ca_certificate)
		mock_disable_warnings.assert_not_called()

	@patch("kubeport.utils.k8s_client.urllib3.disable_warnings")
	@patch("kubeport.utils.k8s_client.client.ApiClient")
	@patch("kubeport.utils.k8s_client._write_ca_tempfile")
	def test_client_from_bearer_token_skips_tls_verification_when_requested(
		self,
		mock_write_ca_tempfile,
		mock_api_client,
		mock_disable_warnings,
	):
		cluster_doc = SimpleNamespace(
			name="cluster-a",
			api_server_url="https://172.22.0.1:34439",
			ca_certificate="-----BEGIN CERTIFICATE-----\nPEM\n-----END CERTIFICATE-----",
			skip_tls_verify=1,
			get_password=lambda fieldname: "token-123",
		)

		_client_from_bearer_token(cluster_doc)

		configuration = mock_api_client.call_args.args[0]
		self.assertFalse(configuration.verify_ssl)
		self.assertIsNone(configuration.ssl_ca_cert)
		mock_write_ca_tempfile.assert_not_called()
		mock_disable_warnings.assert_called_once()
