# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import inspect
from unittest.mock import MagicMock, call, patch

from frappe.tests import UnitTestCase

from kubeport.api import observability


def _release_doc(cluster: str = "server-cluster", namespace: str = "erp", release_name: str = "bench"):
	doc = MagicMock()
	doc.cluster = cluster
	doc.namespace = namespace
	doc.release_name = release_name
	return doc


class UnitTestObservabilityAPI(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.only_for_patcher = patch("kubeport.api.observability.frappe.only_for")
		self.mock_only_for = self.only_for_patcher.start()
		self.addCleanup(self.only_for_patcher.stop)

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

	@patch("kubeport.api.observability.frappe.get_doc")
	def test_endpoint_rejects_non_system_manager_before_loading_release(self, mock_get_doc):
		self.mock_only_for.side_effect = RuntimeError("No permission")

		with self.assertRaisesRegex(RuntimeError, "No permission"):
			observability.get_release_resource_events(
				release_docname="cluster-a/erp/bench",
				kind="Pod",
				name="bench-web-1",
			)

		mock_get_doc.assert_not_called()

	@patch("kubeport.api.observability.get_pod_logs", return_value="pod logs")
	@patch("kubeport.api.observability.list_pods_for_resource")
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_logs_endpoint_resolves_cluster_from_release_row(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_list_pods,
		mock_get_logs,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-web"}},
		]
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
		self.assertEqual(result["selected_pod"], "bench-web-1")
		self.mock_only_for.assert_called_with("System Manager")
		mock_get_doc.return_value.check_permission.assert_called_once_with("read")
		mock_get_manifest.assert_called_once_with(
			release_name="bench",
			namespace="erp",
			cluster_name="server-cluster",
		)
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
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_events_endpoint_does_not_accept_client_controlled_cluster(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_list_events,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Pod", "metadata": {"name": "bench-web-1"}},
		]

		result = observability.get_release_resource_events(
			release_docname="cluster-a/erp/bench",
			kind="Pod",
			name="bench-web-1",
			limit=5,
		)

		self.assertEqual(result, {"rows": [], "error": ""})
		mock_list_events.assert_called_once_with(
			cluster="server-cluster",
			namespace="erp",
			kind="Pod",
			name="bench-web-1",
			limit=5,
		)

	@patch("kubeport.api.observability.list_resource_events", return_value=[])
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_events_endpoint_uses_manifest_resource_namespace(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_list_events,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Pod", "metadata": {"name": "bench-web-1", "namespace": "jobs"}},
		]

		result = observability.get_release_resource_events(
			release_docname="cluster-a/erp/bench",
			kind="Pod",
			name="bench-web-1",
			namespace="jobs",
			limit=5,
		)

		self.assertEqual(result, {"rows": [], "error": ""})
		mock_list_events.assert_called_once_with(
			cluster="server-cluster",
			namespace="jobs",
			kind="Pod",
			name="bench-web-1",
			limit=5,
		)

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_events_endpoint_rejects_resource_not_in_release_manifest(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_throw,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Pod", "metadata": {"name": "bench-web-1"}},
		]
		mock_throw.side_effect = RuntimeError("not part")

		with self.assertRaisesRegex(RuntimeError, "not part"):
			observability.get_release_resource_events(
				release_docname="cluster-a/erp/bench",
				kind="Pod",
				name="other-pod",
			)

		mock_throw.assert_called_once()

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_events_endpoint_rejects_malformed_kind(
		self,
		mock_get_doc,
		mock_throw,
	):
		mock_get_doc.return_value = _release_doc()
		mock_throw.side_effect = RuntimeError("Unsupported resource kind")

		with self.assertRaisesRegex(RuntimeError, "Unsupported resource kind"):
			observability.get_release_resource_events(
				release_docname="cluster-a/erp/bench",
				kind="Secret",
				name="bad",
			)

		mock_throw.assert_called_once()

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_logs_endpoint_rejects_malformed_name(
		self,
		mock_get_doc,
		mock_throw,
	):
		mock_get_doc.return_value = _release_doc()
		mock_throw.side_effect = RuntimeError("Resource name is required")

		with self.assertRaisesRegex(RuntimeError, "Resource name is required"):
			observability.get_release_resource_logs(
				release_docname="cluster-a/erp/bench",
				kind="Pod",
				name="bad,name",
			)

		mock_throw.assert_called_once()

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.frappe.get_doc", return_value=None)
	def test_release_scope_requires_existing_release_row(self, _mock_get_doc, mock_throw):
		mock_throw.side_effect = RuntimeError("not found")

		with self.assertRaisesRegex(RuntimeError, "not found"):
			observability.get_release_resource_rollout(
				release_docname="missing",
				kind="Deployment",
				name="bench-web",
			)

	@patch("kubeport.api.observability.get_pod_logs")
	@patch("kubeport.api.observability.list_pods_for_resource")
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_logs_endpoint_reads_all_resolved_pods(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_list_pods,
		mock_get_logs,
	):
		def log_side_effect(**kwargs):
			if kwargs["pod"] == "bench-web-2":
				raise RuntimeError("pod failed")
			return f"{kwargs['pod']} logs"

		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-web"}},
		]
		mock_get_logs.side_effect = log_side_effect
		mock_list_pods.return_value = [
			{"name": "bench-web-1", "container_names": ["web"]},
			{"name": "bench-web-2", "container_names": ["web"]},
		]

		result = observability.get_release_resource_logs(
			release_docname="cluster-a/erp/bench",
			kind="Deployment",
			name="bench-web",
			pod_name="bench-web-2",
		)

		self.assertEqual(result["selected_pod"], "bench-web-2")
		self.assertEqual(result["logs_by_pod"]["bench-web-1"], "bench-web-1 logs")
		self.assertEqual(result["logs_by_pod"]["bench-web-2"], "")
		self.assertEqual(result["errors_by_pod"]["bench-web-2"], "pod failed")
		self.assertEqual(mock_get_logs.call_args_list, [
			call(
				cluster="server-cluster",
				namespace="erp",
				pod="bench-web-1",
				container=None,
				tail_lines=200,
				previous=False,
			),
			call(
				cluster="server-cluster",
				namespace="erp",
				pod="bench-web-2",
				container=None,
				tail_lines=200,
				previous=False,
			),
		])

	@patch("kubeport.api.observability.list_resource_events", side_effect=RuntimeError("events failed"))
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_events_endpoint_returns_explicit_error_payload(
		self,
		mock_get_doc,
		mock_get_manifest,
		_mock_list_events,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Pod", "metadata": {"name": "bench-web-1"}},
		]

		result = observability.get_release_resource_events(
			release_docname="cluster-a/erp/bench",
			kind="Pod",
			name="bench-web-1",
		)

		self.assertEqual(result, {"rows": [], "error": "events failed"})

	@patch("kubeport.api.observability.get_rollout_history", return_value=[{"revision": "1"}])
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_rollout_endpoint_returns_explicit_success_payload(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_get_rollout,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-web"}},
		]

		result = observability.get_release_resource_rollout(
			release_docname="cluster-a/erp/bench",
			kind="Deployment",
			name="bench-web",
			limit=5,
		)

		self.assertEqual(result, {"rows": [{"revision": "1"}], "error": ""})
		mock_get_rollout.assert_called_once_with(
			cluster="server-cluster",
			namespace="erp",
			kind="Deployment",
			name="bench-web",
			limit=5,
		)

	@patch("kubeport.api.observability.get_rollout_history", side_effect=RuntimeError("rollout failed"))
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_rollout_endpoint_returns_explicit_error_payload(
		self,
		mock_get_doc,
		mock_get_manifest,
		_mock_get_rollout,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-web"}},
		]

		result = observability.get_release_resource_rollout(
			release_docname="cluster-a/erp/bench",
			kind="Deployment",
			name="bench-web",
		)

		self.assertEqual(result, {"rows": [], "error": "rollout failed"})

	@patch("kubeport.api.observability.frappe.throw")
	@patch("kubeport.api.observability.list_pods_for_resource")
	@patch("kubeport.api.observability.helm.get_manifest")
	@patch("kubeport.api.observability.frappe.get_doc")
	def test_logs_endpoint_rejects_pod_outside_resource(
		self,
		mock_get_doc,
		mock_get_manifest,
		mock_list_pods,
		mock_throw,
	):
		mock_get_doc.return_value = _release_doc()
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-web"}},
		]
		mock_list_pods.return_value = [{"name": "bench-web-1", "container_names": ["web"]}]
		mock_throw.side_effect = RuntimeError("not part of this resource")

		with self.assertRaisesRegex(RuntimeError, "not part of this resource"):
			observability.get_release_resource_logs(
				release_docname="cluster-a/erp/bench",
				kind="Deployment",
				name="bench-web",
				pod_name="other-pod",
			)

		mock_throw.assert_called_once()
