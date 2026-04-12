# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.kubernetes_cluster.kubernetes_cluster import KubernetesCluster


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class UnitTestKubernetesCluster(UnitTestCase):
	@patch("kubeport.kubeport.doctype.kubernetes_cluster.kubernetes_cluster.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.kubernetes_cluster.kubernetes_cluster.client.CoreV1Api")
	@patch("kubeport.kubeport.doctype.kubernetes_cluster.kubernetes_cluster.get_k8s_api_client")
	def test_test_connection_uses_request_timeout(
		self,
		mock_get_k8s_api_client,
		mock_core_v1_api,
		_mock_msgprint,
	):
		doc = object.__new__(KubernetesCluster)
		doc.name = "cluster-a"
		doc.db_set = MagicMock()

		mock_get_k8s_api_client.return_value = object()
		mock_core_v1_api.return_value.list_node.return_value = SimpleNamespace(items=[object()])

		doc.test_connection()

		mock_core_v1_api.return_value.list_node.assert_called_once_with(
			_request_timeout=15.0,
		)
		doc.db_set.assert_called_once_with("status", "Connected")


class IntegrationTestKubernetesCluster(IntegrationTestCase):
	"""
	Integration tests for KubernetesCluster.
	Use this class for testing interactions between multiple components.
	"""

	pass
