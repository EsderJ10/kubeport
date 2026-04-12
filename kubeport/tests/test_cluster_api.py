# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.api import get_cluster_namespaces


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
