# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase
from kubernetes.client.rest import ApiException

from kubeport.api.discovery import adopt_helm_release, get_cluster_discovery, get_ingress_suggestions
from kubeport.utils.discovery import (
	_normalize_release_row,
	_parse_site_names,
	build_nip_io_hostname,
	discover_cluster_issuers,
	discover_ingress_classes,
	discover_ingress_controller_addresses,
	discover_release_sites,
)


class UnitTestClusterDiscoveryUtils(UnitTestCase):
	def test_normalize_release_marks_official_frappe_chart(self):
		release = _normalize_release_row(
			{
				"name": "bench-prod",
				"namespace": "erp",
				"status": "deployed",
				"chart": "erpnext-8.1.0",
				"app_version": "16.10.1",
				"updated": "2026-04-10 10:00:00 +0000 UTC",
			}
		)

		self.assertEqual(release["release_name"], "bench-prod")
		self.assertEqual(release["chart_name"], "erpnext")
		self.assertEqual(release["chart_version"], "8.1.0")
		self.assertTrue(release["is_frappe_bench"])

	def test_parse_site_names_filters_assets_and_duplicates(self):
		sites = _parse_site_names(
			[
				"site1.local",
				"assets",
				"",
				"site2.local",
				"site1.local",
			]
		)

		self.assertEqual(sites, ["site1.local", "site2.local"])

	def test_discover_release_sites_skips_non_frappe_release(self):
		sites = discover_release_sites(
			object(),
			{
				"release_name": "redis",
				"namespace": "infra",
				"is_frappe_bench": False,
			},
		)

		self.assertEqual(sites, [])

	@patch("kubeport.utils.discovery.client.NetworkingV1Api")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client", return_value=object())
	def test_discover_ingress_classes_marks_default_class(
		self,
		_mock_get_client,
		mock_networking_v1_api,
	):
		networking_v1 = mock_networking_v1_api.return_value
		networking_v1.list_ingress_class.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="internal", annotations={}),
					spec=SimpleNamespace(controller="example.com/internal"),
				),
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="nginx",
						annotations={"ingressclass.kubernetes.io/is-default-class": "true"},
					),
					spec=SimpleNamespace(controller="k8s.io/ingress-nginx"),
				),
			]
		)

		rows = discover_ingress_classes("cluster-a")

		self.assertEqual(rows[0]["name"], "nginx")
		self.assertTrue(rows[0]["is_default"])

	@patch("kubeport.utils.discovery.client.CustomObjectsApi")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client", return_value=object())
	def test_discover_cluster_issuers_reports_missing_crd_as_unavailable(
		self,
		_mock_get_client,
		mock_custom_api,
	):
		exc = ApiException(status=404, reason="Not Found")
		mock_custom_api.return_value.list_cluster_custom_object.side_effect = exc

		result = discover_cluster_issuers("cluster-a")

		self.assertFalse(result["available"])
		self.assertEqual(result["issuers"], [])

	@patch("kubeport.utils.discovery.client.CoreV1Api")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client", return_value=object())
	def test_discover_ingress_controller_addresses_prefers_likely_ingress_services(
		self,
		_mock_get_client,
		mock_core_v1_api,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.list_service_for_all_namespaces.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="app-lb", namespace="default", labels={}),
					spec=SimpleNamespace(type="LoadBalancer", selector={}),
					status=SimpleNamespace(
						load_balancer=SimpleNamespace(ingress=[SimpleNamespace(ip="10.0.0.5")])
					),
				),
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="ingress-nginx-controller",
						namespace="ingress-nginx",
						labels={"app.kubernetes.io/name": "ingress-nginx"},
					),
					spec=SimpleNamespace(
						type="LoadBalancer",
						selector={"app.kubernetes.io/name": "ingress-nginx"},
					),
					status=SimpleNamespace(
						load_balancer=SimpleNamespace(ingress=[SimpleNamespace(ip="192.168.1.50")])
					),
				),
			]
		)

		rows = discover_ingress_controller_addresses("cluster-a")

		self.assertEqual(rows[0]["address"], "192.168.1.50")
		self.assertTrue(rows[0]["is_likely_ingress_controller"])
		self.assertEqual(build_nip_io_hostname("bench-a", rows[0]["address"]), "bench-a.192.168.1.50.nip.io")

	@patch("kubeport.utils.discovery.client.CoreV1Api")
	def test_discover_release_sites_ignores_running_infra_pods(
		self,
		mock_core_v1_api,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-erpnext-mariadb-sts-0", labels={}),
					status=SimpleNamespace(phase="Running", container_statuses=[]),
					spec=SimpleNamespace(containers=[SimpleNamespace(name="mariadb")]),
				),
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-valkey-cache-123", labels={}),
					status=SimpleNamespace(phase="Running", container_statuses=[]),
					spec=SimpleNamespace(containers=[SimpleNamespace(name="valkey")]),
				),
			]
		)

		with self.assertRaisesRegex(RuntimeError, "Only non-Frappe pods found"):
			discover_release_sites(
				object(),
				{
					"release_name": "bench-a",
					"namespace": "erp",
					"is_frappe_bench": True,
				},
			)

	@patch("kubeport.utils.discovery.stream")
	@patch("kubeport.utils.discovery.client.CoreV1Api")
	def test_discover_release_sites_falls_back_to_namespace_scan_when_selector_misses(
		self,
		mock_core_v1_api,
		mock_stream,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.connect_get_namespaced_pod_exec = object()
		core_v1.list_namespaced_pod.side_effect = [
			SimpleNamespace(items=[]),
			SimpleNamespace(
				items=[
					SimpleNamespace(
						metadata=SimpleNamespace(name="bench-a-erpnext-gunicorn-123", labels={}),
						status=SimpleNamespace(
							phase="Running",
							container_statuses=[SimpleNamespace(ready=True)],
						),
						spec=SimpleNamespace(containers=[SimpleNamespace(name="web")]),
					),
					SimpleNamespace(
						metadata=SimpleNamespace(name="other-release-nginx-123", labels={}),
						status=SimpleNamespace(phase="Running", container_statuses=[]),
						spec=SimpleNamespace(containers=[SimpleNamespace(name="nginx")]),
					),
				]
			),
		]
		mock_stream.return_value = "site-one.local"

		sites = discover_release_sites(
			object(),
			{
				"release_name": "bench-a",
				"namespace": "erp",
				"is_frappe_bench": True,
			},
		)

		self.assertEqual([site["site_name"] for site in sites], ["site-one.local"])
		self.assertEqual(core_v1.list_namespaced_pod.call_count, 2)
		self.assertEqual(
			core_v1.list_namespaced_pod.call_args_list[0].kwargs["label_selector"],
			"app.kubernetes.io/instance=bench-a",
		)
		self.assertNotIn("label_selector", core_v1.list_namespaced_pod.call_args_list[1].kwargs)

	@patch("kubeport.utils.discovery.client.CoreV1Api")
	def test_discover_release_sites_reports_pending_workload_pods(
		self,
		mock_core_v1_api,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-erpnext-gunicorn-123", labels={}),
					status=SimpleNamespace(phase="Pending", container_statuses=[]),
					spec=SimpleNamespace(containers=[SimpleNamespace(name="web")]),
				),
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-erpnext-nginx-123", labels={}),
					status=SimpleNamespace(phase="Pending", container_statuses=[]),
					spec=SimpleNamespace(containers=[SimpleNamespace(name="nginx")]),
				),
			]
		)

		with self.assertRaisesRegex(RuntimeError, "none are running"):
			discover_release_sites(
				object(),
				{
					"release_name": "bench-a",
					"namespace": "erp",
					"is_frappe_bench": True,
				},
			)

	@patch("kubeport.utils.discovery.stream")
	@patch("kubeport.utils.discovery.client.CoreV1Api")
	def test_discover_release_sites_uses_scalar_request_timeouts(
		self,
		mock_core_v1_api,
		mock_stream,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.connect_get_namespaced_pod_exec = object()
		pod = SimpleNamespace(
			metadata=SimpleNamespace(
				name="bench-a-gunicorn-123",
				labels={"app.kubernetes.io/component": "gunicorn"},
			),
			status=SimpleNamespace(
				phase="Running",
				container_statuses=[SimpleNamespace(ready=True)],
			),
			spec=SimpleNamespace(containers=[SimpleNamespace(name="web")]),
		)
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
		mock_stream.return_value = "\n".join(["site-one.local", "assets", "site-two.local"])

		sites = discover_release_sites(
			object(),
			{
				"release_name": "bench-a",
				"namespace": "erp",
				"is_frappe_bench": True,
			},
		)

		self.assertEqual(
			[site["site_name"] for site in sites],
			["site-one.local", "site-two.local"],
		)
		self.assertEqual(core_v1.list_namespaced_pod.call_args.kwargs["_request_timeout"], 15.0)
		self.assertEqual(mock_stream.call_args.kwargs["_request_timeout"], 20.0)


class UnitTestClusterDiscoveryAPI(UnitTestCase):
	def _empty_capabilities(self):
		return {
			"ingress": {"available": False, "ingress_classes": [], "controller_addresses": []},
			"cert_manager": {"available": False, "cluster_issuers": []},
		}

	@patch("kubeport.api.discovery._discover_cluster_capabilities")
	@patch("kubeport.api.discovery.frappe.log_error")
	@patch("kubeport.api.discovery.discover_release_sites")
	@patch("kubeport.api.discovery.get_k8s_api_client")
	@patch("kubeport.api.discovery.discover_cluster_releases")
	def test_get_cluster_discovery_returns_partial_release_errors(
		self,
		mock_discover_cluster_releases,
		mock_get_k8s_api_client,
		mock_discover_release_sites,
		_mock_log_error,
		mock_discover_capabilities,
	):
		mock_discover_capabilities.return_value = self._empty_capabilities()
		mock_discover_cluster_releases.return_value = [
			{
				"release_name": "bench-a",
				"namespace": "erp",
				"is_frappe_bench": True,
				"chart": "erpnext-8.1.0",
			},
			{
				"release_name": "bench-b",
				"namespace": "erp",
				"is_frappe_bench": True,
				"chart": "erpnext-8.1.0",
			},
		]
		mock_get_k8s_api_client.return_value = object()
		mock_discover_release_sites.side_effect = [
			[
				{
					"site_name": "site1.local",
					"bench_release": "bench-a",
					"namespace": "erp",
					"source": "pod_exec",
					"pod_name": "bench-a-gunicorn-123",
				}
			],
			RuntimeError("pods/exec forbidden"),
		]

		result = get_cluster_discovery("cluster-a")

		self.assertEqual(result["cluster"], "cluster-a")
		self.assertEqual(len(result["benches"]), 2)
		self.assertEqual(result["sites"][0]["site_name"], "site1.local")
		self.assertEqual(len(result["errors"]), 1)
		self.assertIn("bench-b", result["errors"][0]["message"])
		self.assertIn("capabilities", result)

	@patch("kubeport.api.discovery._discover_cluster_capabilities")
	@patch("kubeport.api.discovery.frappe.log_error")
	@patch("kubeport.api.discovery.discover_cluster_releases")
	def test_get_cluster_discovery_returns_cluster_error_on_helm_failure(
		self,
		mock_discover_cluster_releases,
		_mock_log_error,
		mock_discover_capabilities,
	):
		mock_discover_capabilities.return_value = self._empty_capabilities()
		mock_discover_cluster_releases.side_effect = RuntimeError("helm not available")

		result = get_cluster_discovery("cluster-a")

		self.assertEqual(result["benches"], [])
		self.assertEqual(result["sites"], [])
		self.assertEqual(len(result["errors"]), 1)
		self.assertIn("Failed to list Helm releases", result["errors"][0]["message"])

	@patch("kubeport.api.discovery._discover_cluster_capabilities")
	def test_get_ingress_suggestions_returns_defaults_and_nip_hostname(
		self,
		mock_discover_capabilities,
	):
		mock_discover_capabilities.return_value = {
			"ingress": {
				"available": True,
				"ingress_classes": [
					{"name": "nginx", "is_default": True, "controller": "k8s.io/ingress-nginx"}
				],
				"controller_addresses": [
					{
						"address": "192.168.1.50",
						"address_type": "ip",
						"service": "ingress-nginx-controller",
						"namespace": "ingress-nginx",
					}
				],
			},
			"cert_manager": {
				"available": True,
				"cluster_issuers": [{"name": "selfsigned", "ready": True}],
			},
		}

		result = get_ingress_suggestions("cluster-a", "bench-a")

		self.assertEqual(result["default_ingress_class"], "nginx")
		self.assertEqual(result["default_cluster_issuer"], "selfsigned")
		self.assertEqual(result["suggested_hostname"], "bench-a.192.168.1.50.nip.io")

	@patch("kubeport.api.discovery.frappe.db.set_value")
	@patch("kubeport.utils.release_health.walk", return_value=[])
	@patch("kubeport.api.discovery.frappe.get_doc")
	@patch("kubeport.api.discovery.frappe.get_all")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.utils.helm.get_values", return_value="workers:\n  replicaCount: 2\n")
	@patch("kubeport.api.discovery.discover_cluster_releases")
	@patch("kubeport.api.discovery.frappe.db.exists", return_value=False)
	def test_adopt_helm_release_creates_tracking_doc_from_live_release(
		self,
		_mock_exists,
		mock_discover_cluster_releases,
		_mock_get_values,
		mock_status,
		mock_get_all,
		mock_get_doc,
		_mock_walk,
		mock_set_value,
	):
		mock_discover_cluster_releases.return_value = [
			{
				"release_name": "bench-a",
				"namespace": "erp",
				"chart_name": "erpnext",
				"chart_version": "8.0.41",
				"status": "deployed",
			}
		]
		mock_get_all.return_value = [SimpleNamespace(name="repo/erpnext")]
		doc = SimpleNamespace(
			name="cluster-a/erp/bench-a",
			insert=lambda: None,
		)
		mock_get_doc.return_value = doc
		mock_status.return_value = {
			"version": 3,
			"info": {"status": "deployed"},
		}

		result = adopt_helm_release("cluster-a", "erp", "bench-a")

		self.assertTrue(result["created"])
		self.assertEqual(result["name"], "cluster-a/erp/bench-a")
		mock_get_doc.assert_called_once()
		doc_payload = mock_get_doc.call_args.args[0]
		self.assertEqual(doc_payload["doctype"], "Helm Release")
		self.assertEqual(doc_payload["chart"], "repo/erpnext")
		self.assertIn("replicaCount", doc_payload["values"])
		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Deployed")
		self.assertEqual(fields["last_applied_chart_version"], "8.0.41")
