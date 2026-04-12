# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase
from kubernetes.client.rest import ApiException

from kubeport.utils.k8s_resources import check_resources_exist, load_managed_manifest_objects


class UnitTestK8sResources(UnitTestCase):
	@patch("kubeport.utils.k8s_resources.frappe.throw")
	def test_load_managed_manifest_objects_rejects_empty_manifest(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Manifest content must contain at least one Kubernetes object.")

		with self.assertRaisesRegex(RuntimeError, "at least one Kubernetes object"):
			load_managed_manifest_objects("[]")

		mock_throw.assert_called()

	@patch("kubeport.utils.k8s_resources.frappe.throw")
	def test_load_managed_manifest_objects_rejects_missing_required_fields(self, mock_throw):
		mock_throw.side_effect = RuntimeError("missing required field")

		with self.assertRaisesRegex(RuntimeError, "missing required field"):
			load_managed_manifest_objects('[{"kind": "ConfigMap", "metadata": {"name": "demo"}}]')

		mock_throw.assert_called()

	@patch("kubeport.utils.k8s_resources.frappe.throw")
	def test_load_managed_manifest_objects_rejects_unsupported_kind(self, mock_throw):
		mock_throw.side_effect = RuntimeError("unsupported resource kind")

		with self.assertRaisesRegex(RuntimeError, "unsupported resource kind"):
			load_managed_manifest_objects(
				'[{"apiVersion": "example.com/v1", "kind": "CustomThing", "metadata": {"name": "demo"}}]'
			)

		mock_throw.assert_called()

	@patch("kubeport.utils.k8s_resources.read_resource")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	def test_check_resources_exist_returns_false_for_missing_resource(
		self,
		mock_get_k8s_api_client,
		mock_read_resource,
	):
		mock_get_k8s_api_client.return_value = object()
		mock_read_resource.side_effect = ApiException(status=404, reason="Not Found")

		result = check_resources_exist(
			cluster_name="cluster-a",
			manifest_json='[{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "demo"}}]',
			namespace="default",
			doctype="Service Bundle",
			docname="bundle-a",
		)

		self.assertEqual(
			result,
			(False, "ConfigMap/demo not found in namespace default."),
		)
