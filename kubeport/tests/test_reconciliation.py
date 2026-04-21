# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import call, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.reconciliation import (
	_finalize_site_status,
	_reconcile_frappe_sites,
	_reconcile_helm_releases,
	_reconcile_service_bundles,
	reconcile_all_releases,
)


class UnitTestReconciliation(UnitTestCase):
	@patch("kubeport.tasks.reconciliation._reconcile_frappe_sites")
	@patch("kubeport.tasks.reconciliation._reconcile_service_bundles")
	@patch("kubeport.tasks.reconciliation._reconcile_helm_releases")
	def test_reconcile_all_releases_only_runs_active_sweeps(
		self,
		mock_reconcile_helm_releases,
		mock_reconcile_service_bundles,
		mock_reconcile_frappe_sites,
	):
		reconcile_all_releases()

		mock_reconcile_helm_releases.assert_called_once_with()
		mock_reconcile_service_bundles.assert_called_once_with()
		mock_reconcile_frappe_sites.assert_called_once_with()

	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_heals_degraded_release(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bench-a",
				cluster="cluster-a",
				namespace="default",
				release_name="bench-a",
				status="Degraded",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}

		_reconcile_helm_releases()

		self.assertEqual(mock_set_value.call_args_list, [
			call("Helm Release", "bench-a", "status", "Deployed"),
			call("Helm Release", "bench-a", "helm_status_detail", "deployed"),
		])

	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.check_resources_exist")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_service_bundles_heals_degraded_bundle(
		self,
		mock_get_all,
		mock_check_resources_exist,
		mock_set_value,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bundle-a",
				cluster="cluster-a",
				namespace="default",
				content="apiVersion: v1",
				status="Degraded",
			),
		]
		mock_check_resources_exist.return_value = (True, "All managed resources are present.")

		_reconcile_service_bundles()

		self.assertEqual(mock_set_value.call_args_list, [
			call("Service Bundle", "bundle-a", "status", "Deployed"),
			call("Service Bundle", "bundle-a", "status_detail", ""),
		])

	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.check_resources_exist")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_service_bundles_marks_drifted_bundle_degraded(
		self,
		mock_get_all,
		mock_check_resources_exist,
		mock_set_value,
		mock_log_error,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bundle-a",
				cluster="cluster-a",
				namespace="default",
				content="apiVersion: v1",
				status="Deployed",
			),
		]
		mock_check_resources_exist.return_value = (False, "ConfigMap/demo not found in namespace default.")

		_reconcile_service_bundles()

		self.assertEqual(mock_set_value.call_args_list, [
			call("Service Bundle", "bundle-a", "status", "Degraded"),
			call(
				"Service Bundle",
				"bundle-a",
				"status_detail",
				"ConfigMap/demo not found in namespace default.",
			),
		])
		mock_log_error.assert_called_once()


class UnitTestReconcileFrappeSites(UnitTestCase):
	def _site(self, **overrides) -> SimpleNamespace:
		defaults = {
			"name": "rel-a/demo.example.com",
			"cluster": "cluster-a",
			"namespace": "ns",
			"creation_job_name": "ks-demo-abcdef123456",
			"creation_job_token": "token-1",
			"bench_release": "rel-a",
			"site_name": "demo.example.com",
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _job(self, succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-abcdef123456"),
			status=SimpleNamespace(succeeded=succeeded, failed=failed),
		)

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_frappe_sites_transitions_to_active_on_success(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {
			"operation_token": "token-1",
			"status": "In Progress",
		}

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com", "status", "Active",
		)
		mock_publish.assert_called_once()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_frappe_sites_skips_when_operation_token_superseded(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
	):
		mock_get_all.return_value = [self._site(creation_job_token="token-old")]
		# Current document now has a fresh operation_token (user re-triggered)
		mock_db_get_value.return_value = {
			"operation_token": "token-new",
			"status": "In Progress",
		}

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		# No status writes should happen when tokens no longer match
		for call_args in mock_db_set_value.call_args_list:
			self.assertNotIn(call_args.args[2], ("status", "status_detail"))
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation._site_exists_in_bench")
	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_frappe_sites_recovers_false_negative_when_site_exists(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_log_error,
		mock_extract_detail,
		mock_site_exists,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}
		mock_site_exists.return_value = True

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com", "status", "Active",
		)
		mock_extract_detail.assert_not_called()
		mock_log_error.assert_not_called()

	@patch("kubeport.tasks.reconciliation._site_exists_in_bench")
	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_frappe_sites_marks_failed_with_pod_log_detail(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_log_error,
		mock_extract_detail,
		mock_site_exists,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}
		mock_site_exists.return_value = False
		mock_extract_detail.return_value = "Traceback: DB error"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com", "status", "Failed",
		)
		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
			"status_detail",
			"Traceback: DB error",
		)
		mock_log_error.assert_called_once()


class UnitTestFinalizeSiteStatus(UnitTestCase):
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	def test_finalize_skips_when_status_no_longer_in_progress(
		self,
		mock_get_value,
		mock_set_value,
		mock_publish,
	):
		mock_get_value.return_value = {"operation_token": "t", "status": "Failed"}
		site = SimpleNamespace(
			name="rel/s", creation_job_token="t", creation_job_name="j",
		)
		applied = _finalize_site_status(site, "Active", "")
		self.assertFalse(applied)
		mock_set_value.assert_not_called()
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	def test_finalize_applies_when_tokens_match(
		self,
		mock_get_value,
		mock_set_value,
		mock_publish,
	):
		mock_get_value.return_value = {"operation_token": "t", "status": "In Progress"}
		site = SimpleNamespace(
			name="rel/s", creation_job_token="t", creation_job_name="j",
		)
		applied = _finalize_site_status(site, "Active", "")
		self.assertTrue(applied)
		mock_set_value.assert_any_call("Frappe Site", "rel/s", "status", "Active")
		mock_publish.assert_called_once()
