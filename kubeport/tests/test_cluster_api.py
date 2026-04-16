# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import yaml
from frappe.tests import UnitTestCase

from kubeport.api import (
	extract_kubeconfig_context,
	get_cluster_namespaces,
	parse_kubeconfig_contexts,
)


_LOCAL_ONLY_KUBECONFIG = """
apiVersion: v1
kind: Config
clusters:
  - name: local-cluster
    cluster:
      server: https://0.0.0.0:36443
contexts:
  - name: dev
    context:
      cluster: local-cluster
      user: local-user
current-context: dev
users:
  - name: local-user
    user:
      token: token-123
"""

_ROUTABLE_KUBECONFIG = """
apiVersion: v1
kind: Config
clusters:
  - name: prod-cluster
    cluster:
      server: https://k3d-frappe-cluster-serverlb:6443
contexts:
  - name: prod
    context:
      cluster: prod-cluster
      user: prod-user
current-context: prod
users:
  - name: prod-user
    user:
      token: token-123
"""


class UnitTestClusterAPI(UnitTestCase):
	@patch("kubeport.api.client.CoreV1Api")
	@patch("kubeport.api.get_k8s_api_client")
	def test_get_cluster_namespaces_uses_request_timeout(
		self,
		mock_get_k8s_api_client,
		mock_core_v1_api,
	):
		mock_get_k8s_api_client.return_value = object()
		mock_core_v1_api.return_value.list_namespace.return_value = SimpleNamespace(items=[
			SimpleNamespace(metadata=SimpleNamespace(name="zeta")),
			SimpleNamespace(metadata=SimpleNamespace(name="alpha")),
		])

		result = get_cluster_namespaces("cluster-a")

		self.assertEqual(result, ["alpha", "zeta"])
		mock_core_v1_api.return_value.list_namespace.assert_called_once_with(
			_request_timeout=15.0,
		)

	@patch("kubeport.api._get_preferred_gateway_ip", return_value="172.22.0.1")
	def test_parse_kubeconfig_contexts_rewrites_local_only_server_hosts(
		self,
		_mock_get_preferred_gateway_ip,
	):
		contexts = parse_kubeconfig_contexts(_LOCAL_ONLY_KUBECONFIG)

		self.assertEqual(len(contexts), 1)
		self.assertEqual(contexts[0]["original_server"], "https://0.0.0.0:36443")
		self.assertEqual(contexts[0]["server"], "https://172.22.0.1:36443")
		self.assertTrue(contexts[0]["server_was_normalized"])
		self.assertEqual(contexts[0]["normalization_reason"], "wildcard")

	@patch("kubeport.api._get_preferred_gateway_ip", return_value="172.22.0.1")
	def test_extract_kubeconfig_context_stores_rewritten_server_in_minified_payload(
		self,
		_mock_get_preferred_gateway_ip,
	):
		payload = extract_kubeconfig_context(_LOCAL_ONLY_KUBECONFIG, "dev")
		kubeconfig = yaml.safe_load(payload["kubeconfig"])

		self.assertEqual(payload["original_server"], "https://0.0.0.0:36443")
		self.assertEqual(payload["server"], "https://172.22.0.1:36443")
		self.assertTrue(payload["server_was_normalized"])
		self.assertEqual(
			kubeconfig["clusters"][0]["cluster"]["server"],
			"https://172.22.0.1:36443",
		)

	@patch("kubeport.api._get_preferred_gateway_ip", return_value="172.22.0.1")
	def test_extract_kubeconfig_context_preserves_routable_server_hosts(
		self,
		_mock_get_preferred_gateway_ip,
	):
		payload = extract_kubeconfig_context(_ROUTABLE_KUBECONFIG, "prod")
		kubeconfig = yaml.safe_load(payload["kubeconfig"])

		self.assertEqual(payload["original_server"], "https://k3d-frappe-cluster-serverlb:6443")
		self.assertEqual(payload["server"], "https://k3d-frappe-cluster-serverlb:6443")
		self.assertFalse(payload["server_was_normalized"])
		self.assertEqual(
			kubeconfig["clusters"][0]["cluster"]["server"],
			"https://k3d-frappe-cluster-serverlb:6443",
		)
