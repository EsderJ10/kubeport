# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.api.discovery import get_cluster_discovery
from kubeport.utils.discovery import (
	_parse_site_names,
	_normalize_release_row,
	discover_release_sites,
)


class UnitTestClusterDiscoveryUtils(UnitTestCase):
	def test_normalize_release_marks_official_frappe_chart(self):
		release = _normalize_release_row({
			"name": "bench-prod",
			"namespace": "erp",
			"status": "deployed",
			"chart": "erpnext-8.1.0",
			"app_version": "16.10.1",
			"updated": "2026-04-10 10:00:00 +0000 UTC",
		})

		self.assertEqual(release["release_name"], "bench-prod")
		self.assertEqual(release["chart_name"], "erpnext")
		self.assertEqual(release["chart_version"], "8.1.0")
		self.assertTrue(release["is_frappe_bench"])

	def test_parse_site_names_filters_assets_and_duplicates(self):
		sites = _parse_site_names([
			"site1.local",
			"assets",
			"",
			"site2.local",
			"site1.local",
		])

		self.assertEqual(sites, ["site1.local", "site2.local"])

	def test_discover_release_sites_skips_non_frappe_release(self):
		sites = discover_release_sites(object(), {
			"release_name": "redis",
			"namespace": "infra",
			"is_frappe_bench": False,
		})

		self.assertEqual(sites, [])

	@patch("kubeport.utils.discovery.client.CoreV1Api")
	def test_discover_release_sites_ignores_running_infra_pods(
		self,
		mock_core_v1_api,
	):
		core_v1 = mock_core_v1_api.return_value
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[
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
		])

		with self.assertRaisesRegex(RuntimeError, "Only non-Frappe pods found"):
			discover_release_sites(object(), {
				"release_name": "bench-a",
				"namespace": "erp",
				"is_frappe_bench": True,
			})

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
			SimpleNamespace(items=[
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
			]),
		]
		mock_stream.return_value = "site-one.local"

		sites = discover_release_sites(object(), {
			"release_name": "bench-a",
			"namespace": "erp",
			"is_frappe_bench": True,
		})

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
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[
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
		])

		with self.assertRaisesRegex(RuntimeError, "none are running"):
			discover_release_sites(object(), {
				"release_name": "bench-a",
				"namespace": "erp",
				"is_frappe_bench": True,
			})

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

		sites = discover_release_sites(object(), {
			"release_name": "bench-a",
			"namespace": "erp",
			"is_frappe_bench": True,
		})

		self.assertEqual(
			[site["site_name"] for site in sites],
			["site-one.local", "site-two.local"],
		)
		self.assertEqual(core_v1.list_namespaced_pod.call_args.kwargs["_request_timeout"], 15.0)
		self.assertEqual(mock_stream.call_args.kwargs["_request_timeout"], 20.0)


class UnitTestClusterDiscoveryAPI(UnitTestCase):
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
	):
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
			[{
				"site_name": "site1.local",
				"bench_release": "bench-a",
				"namespace": "erp",
				"source": "pod_exec",
				"pod_name": "bench-a-gunicorn-123",
			}],
			RuntimeError("pods/exec forbidden"),
		]

		result = get_cluster_discovery("cluster-a")

		self.assertEqual(result["cluster"], "cluster-a")
		self.assertEqual(len(result["benches"]), 2)
		self.assertEqual(result["sites"][0]["site_name"], "site1.local")
		self.assertEqual(len(result["errors"]), 1)
		self.assertIn("bench-b", result["errors"][0]["message"])

	@patch("kubeport.api.discovery.frappe.log_error")
	@patch("kubeport.api.discovery.discover_cluster_releases")
	def test_get_cluster_discovery_returns_cluster_error_on_helm_failure(
		self,
		mock_discover_cluster_releases,
		_mock_log_error,
	):
		mock_discover_cluster_releases.side_effect = RuntimeError("helm not available")

		result = get_cluster_discovery("cluster-a")

		self.assertEqual(result["benches"], [])
		self.assertEqual(result["sites"], [])
		self.assertEqual(len(result["errors"]), 1)
		self.assertIn("Failed to list Helm releases", result["errors"][0]["message"])
