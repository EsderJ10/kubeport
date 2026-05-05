# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import inspect
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.api import observability


class UnitTestObservabilityAPI(UnitTestCase):
	def test_whitelisted_methods_are_type_annotated(self):
		for method_name in (
			"get_release_resource_logs",
			"get_release_resource_events",
			"get_release_resource_rollout",
		):
			method = getattr(observability, method_name)
			signature = inspect.signature(method)
			for parameter in signature.parameters.values():
				self.assertIsNot(parameter.annotation, inspect.Signature.empty)
			self.assertIsNot(signature.return_annotation, inspect.Signature.empty)

	@patch("kubeport.api.observability.get_pod_logs", return_value="pod logs")
	@patch("kubeport.api.observability.list_pods_for_resource")
	@patch("kubeport.api.observability.frappe.db.get_value")
	def test_logs_endpoint_resolves_cluster_from_release_row(
		self,
		mock_get_value,
		mock_list_pods,
		mock_get_logs,
	):
		mock_get_value.return_value = {
			"cluster": "server-cluster",
			"namespace": "erp",
		}
		mock_list_pods.return_value = [{"name": "bench-web-1", "container_names": ["web"]}]

		result = observability.get_release_resource_logs(
			release_docname="cluster-a/erp/bench",
			kind="Deployment",
			name="bench-web",
			container="web",
			tail_lines=500,
			previous=True,
		)

		self.assertEqual(result["logs_by_pod"]["bench-web-1"], "pod logs")
		mock_list_pods.assert_called_once_with("server-cluster", "erp", "Deployment", "bench-web")
		mock_get_logs.assert_called_once_with(
			cluster="server-cluster",
			namespace="erp",
			pod="bench-web-1",
			container="web",
			tail_lines=500,
			previous=True,
		)

	@patch("kubeport.api.observability.list_resource_events", return_value=[])
	@patch("kubeport.api.observability.frappe.db.get_value")
	def test_events_endpoint_does_not_accept_client_controlled_cluster(
		self,
		mock_get_value,
		mock_list_events,
	):
		mock_get_value.return_value = {
			"cluster": "server-cluster",
			"namespace": "erp",
		}

		result = observability.get_release_resource_events(
			release_docname="cluster-a/erp/bench",
			kind="Pod",
			name="bench-web-1",
			limit=5,
		)

		self.assertEqual(result, [])
		mock_list_events.assert_called_once_with(
			cluster="server-cluster",
			namespace="erp",
			kind="Pod",
			name="bench-web-1",
			limit=5,
		)

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.list_resource_events")
	@patch("kubeport.api.observability.frappe.db.get_value")
	def test_events_endpoint_rejects_malformed_kind(
		self,
		mock_get_value,
		mock_list_events,
		mock_throw,
	):
		mock_get_value.return_value = {
			"cluster": "server-cluster",
			"namespace": "erp",
		}
		mock_list_events.side_effect = ValueError("Unsupported resource kind")
		mock_throw.side_effect = RuntimeError("Unsupported resource kind")

		with self.assertRaisesRegex(RuntimeError, "Unsupported resource kind"):
			observability.get_release_resource_events(
				release_docname="cluster-a/erp/bench",
				kind="Secret",
				name="bad",
			)

		mock_throw.assert_called_once()

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.list_pods_for_resource")
	@patch("kubeport.api.observability.frappe.db.get_value")
	def test_logs_endpoint_rejects_malformed_name(
		self,
		mock_get_value,
		mock_list_pods,
		mock_throw,
	):
		mock_get_value.return_value = {
			"cluster": "server-cluster",
			"namespace": "erp",
		}
		mock_list_pods.side_effect = ValueError("Resource name is required")
		mock_throw.side_effect = RuntimeError("Resource name is required")

		with self.assertRaisesRegex(RuntimeError, "Resource name is required"):
			observability.get_release_resource_logs(
				release_docname="cluster-a/erp/bench",
				kind="Pod",
				name="bad,name",
			)

		mock_throw.assert_called_once()

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.frappe.db.get_value", return_value=None)
	def test_release_scope_requires_existing_release_row(self, _mock_get_value, mock_throw):
		mock_throw.side_effect = RuntimeError("not found")

		with self.assertRaisesRegex(RuntimeError, "not found"):
			observability.get_release_resource_rollout(
				release_docname="missing",
				kind="Deployment",
				name="bench-web",
			)
