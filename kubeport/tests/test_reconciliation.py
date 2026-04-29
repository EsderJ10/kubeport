# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.reconciliation import (
	_finalize_site_status,
	_job_belongs_to_site,
	_reconcile_frappe_sites,
	_reconcile_helm_releases,
	_reconcile_service_bundles,
	_sweep_orphan_site_jobs,
	reconcile_all_releases,
)
from kubeport.utils.release_health import ResourceHealth


class UnitTestReconciliation(UnitTestCase):
	@patch("kubeport.tasks.reconciliation._sweep_orphan_site_jobs")
	@patch("kubeport.tasks.reconciliation._reconcile_frappe_sites")
	@patch("kubeport.tasks.reconciliation._reconcile_service_bundles")
	@patch("kubeport.tasks.reconciliation._reconcile_helm_releases")
	def test_reconcile_all_releases_only_runs_active_sweeps(
		self,
		mock_reconcile_helm_releases,
		mock_reconcile_service_bundles,
		mock_reconcile_frappe_sites,
		mock_sweep_orphan_site_jobs,
	):
		reconcile_all_releases()

		mock_reconcile_helm_releases.assert_called_once_with()
		mock_reconcile_service_bundles.assert_called_once_with()
		mock_reconcile_frappe_sites.assert_called_once_with()
		mock_sweep_orphan_site_jobs.assert_called_once_with()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_heals_degraded_release(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		mock_get_value,
		mock_publish,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bench-a",
				cluster="cluster-a",
				namespace="default",
				release_name="bench-a",
				status="Degraded",
				operation_token="tok-1",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Degraded",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_called_once_with("Helm Release", "bench-a", {
			"status": "Deployed",
			"helm_status_detail": "deployed (no workload resources)",
		})
		mock_publish.assert_called_once()

	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_marks_unready_workloads_degraded(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		mock_get_value,
		mock_publish,
		mock_log_error,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bench-a",
				cluster="cluster-a",
				namespace="default",
				release_name="bench-a",
				status="Deployed",
				operation_token="tok-1",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = [
			ResourceHealth("Deployment", "bench-a-web", "default", False, "ImagePullBackOff", ""),
		]
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Deployed",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Degraded")
		self.assertIn("ImagePullBackOff", fields["helm_status_detail"])
		mock_publish.assert_called_once()
		mock_log_error.assert_called_once()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_skips_when_row_enters_worker_status_mid_check(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		mock_get_value,
		mock_publish,
	):
		for worker_status in ("In Progress", "Uninstalling"):
			with self.subTest(worker_status=worker_status):
				mock_get_all.return_value = [
					SimpleNamespace(
						name="bench-a",
						cluster="cluster-a",
						namespace="default",
						release_name="bench-a",
						status="Deployed",
						operation_token="tok-1",
					),
				]
				mock_helm_status.return_value = {"info": {"status": "deployed"}}
				mock_walk.return_value = []
				mock_get_value.return_value = {
					"operation_token": "tok-1",
					"status": worker_status,
				}

				_reconcile_helm_releases()

				mock_set_value.assert_not_called()
				mock_publish.assert_not_called()
				mock_set_value.reset_mock()
				mock_publish.reset_mock()

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_skips_when_operation_token_rotates_mid_check(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		mock_get_value,
		mock_publish,
		mock_logger,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="bench-a",
				cluster="cluster-a",
				namespace="default",
				release_name="bench-a",
				status="Degraded",
				operation_token="tok-old",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.return_value = {
			"operation_token": "tok-new",
			"status": "Degraded",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_not_called()
		mock_publish.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.check_resources_exist")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_service_bundles_heals_degraded_bundle(
		self,
		mock_get_all,
		mock_check_resources_exist,
		mock_set_value,
		_mock_get_api_client,
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

	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
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
		_mock_get_api_client,
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
			"status": "In Progress",
			"operation_job_name": "ks-demo-abcdef123456",
			"operation_job_token": "token-1",
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
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com",
			{"status": "Active", "status_detail": ""},
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
		mock_get_all.return_value = [self._site(operation_job_token="token-old")]
		# Current document now has a fresh operation_token (user re-triggered)
		mock_db_get_value.return_value = {
			"operation_token": "token-new",
			"status": "In Progress",
		}

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		# No status writes should happen when tokens no longer match
		mock_db_set_value.assert_not_called()
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
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
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com",
			{"status": "Active", "status_detail": ""},
		)
		mock_extract_detail.assert_not_called()
		mock_log_error.assert_not_called()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
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
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}
		mock_probe_state.return_value = "missing"
		mock_extract_detail.return_value = "Traceback: DB error"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site", "rel-a/demo.example.com",
			{"status": "Failed", "status_detail": "Traceback: DB error"},
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
			name="rel/s", operation_job_token="t", operation_job_name="j",
		)
		applied = _finalize_site_status(site, "In Progress", "Active", "")
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
			name="rel/s", operation_job_token="t", operation_job_name="j",
		)
		applied = _finalize_site_status(site, "In Progress", "Active", "")
		self.assertTrue(applied)
		mock_set_value.assert_called_once_with("Frappe Site", "rel/s", {
			"status": "Active",
			"status_detail": "",
		})
		mock_publish.assert_called_once()


class UnitTestReconcileFrappeSitesExtra(UnitTestCase):
	"""Additional guards added alongside the orphan-sweep / 3-state probe work."""

	def _site(self, **overrides) -> SimpleNamespace:
		defaults = {
			"name": "rel-a/demo.example.com",
			"cluster": "cluster-a",
			"namespace": "ns",
			"status": "In Progress",
			"operation_job_name": "ks-demo-abcdef123456",
			"operation_job_token": "token-1",
			"bench_release": "rel-a",
			"site_name": "demo.example.com",
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _job_with_labels(self, labels: dict, succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-abcdef123456", labels=labels),
			status=SimpleNamespace(succeeded=succeeded, failed=failed),
		)

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_frappe_sites_skips_job_with_wrong_site_label(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
	):
		"""A Job whose kubeport.io/frappe-site label does not match the doc must
		be ignored — prevents a (very unlikely) name collision from moving the
		wrong row's status."""
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}

		wrong_job = self._job_with_labels(
			labels={
				"app.kubernetes.io/managed-by": "kubeport",
				"kubeport.io/frappe-site": "some-other-site",
			},
			succeeded=1,
		)

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = wrong_job
			_reconcile_frappe_sites()

		# No status writes should occur — the Job did not belong to us.
		mock_db_set_value.assert_not_called()
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_skips_finalize_on_transient_probe_failure(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_extract_detail,
		mock_probe_state,
	):
		"""A rolling bench restart yields a SITE_PROBE_UNKNOWN; reconciliation
		must defer the decision to the next tick rather than marking Failed."""
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "In Progress"}
		mock_probe_state.return_value = "unknown"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(name="ks-demo-abcdef123456", labels={}),
				status=SimpleNamespace(succeeded=0, failed=1),
			)
			_reconcile_frappe_sites()

		# No terminal status writes; no failure detail extraction either.
		mock_db_set_value.assert_not_called()
		mock_publish.assert_not_called()
		mock_extract_detail.assert_not_called()


class UnitTestJobBelongsToSite(UnitTestCase):
	def test_returns_true_when_labels_match(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(
			metadata=SimpleNamespace(labels={
				"app.kubernetes.io/managed-by": "kubeport",
				"kubeport.io/frappe-site": "rel-a-demo.example.com",
			}),
		)
		self.assertTrue(_job_belongs_to_site(job, site))

	def test_returns_false_when_managed_by_wrong(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(
			metadata=SimpleNamespace(labels={
				"app.kubernetes.io/managed-by": "someone-else",
				"kubeport.io/frappe-site": "rel-a-demo.example.com",
			}),
		)
		self.assertFalse(_job_belongs_to_site(job, site))

	def test_returns_false_when_site_label_missing(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(
			metadata=SimpleNamespace(labels={
				"app.kubernetes.io/managed-by": "kubeport",
			}),
		)
		self.assertFalse(_job_belongs_to_site(job, site))

	def test_returns_false_when_metadata_missing(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(metadata=None)
		self.assertFalse(_job_belongs_to_site(job, site))


class UnitTestSweepOrphanSiteJobs(UnitTestCase):
	def _job(self, name: str) -> SimpleNamespace:
		return SimpleNamespace(metadata=SimpleNamespace(name=name))

	def _job_with_age(self, name: str, age_seconds: int) -> SimpleNamespace:
		from datetime import datetime, timedelta, timezone

		created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
		return SimpleNamespace(
			metadata=SimpleNamespace(name=name, creation_timestamp=created),
		)

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_sweeps_labeled_orphan_jobs(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		"""A labeled Job whose name appears in no Frappe Site row is deleted."""
		mock_get_all.return_value = [
			SimpleNamespace(
				name="rel-a/demo",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name="ks-demo-known123456ab",
			),
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(items=[
				self._job("ks-demo-known123456ab"),  # tracked — keep
				self._job("ks-demo-orphanabcd1234"),  # not tracked — delete
			])
			_sweep_orphan_site_jobs()

		# Exactly one delete — the orphan.
		mock_delete_job.assert_called_once()
		_api, name, namespace = mock_delete_job.call_args.args
		self.assertEqual(name, "ks-demo-orphanabcd1234")
		self.assertEqual(namespace, "ns")

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_does_not_sweep_recently_created_orphan_within_grace(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="rel-a/demo",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name=None,
			),
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(items=[
				self._job_with_age("ks-demo-fresh1234abcd", age_seconds=30),
			])
			_sweep_orphan_site_jobs()

		mock_delete_job.assert_not_called()

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_sweeps_orphan_older_than_grace(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="rel-a/demo",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name=None,
			),
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(items=[
				self._job_with_age("ks-demo-orphanold123", age_seconds=600),
			])
			_sweep_orphan_site_jobs()

		mock_delete_job.assert_called_once()

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_does_not_touch_jobs_referenced_by_a_doc(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		"""Every labeled Job is referenced by some site row — nothing is deleted."""
		mock_get_all.return_value = [
			SimpleNamespace(
				name="rel-a/demo",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name="ks-demo-abc123abc123",
			),
			SimpleNamespace(
				name="rel-a/other",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name="ks-other-xyz456xyz4",
			),
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(items=[
				self._job("ks-demo-abc123abc123"),
				self._job("ks-other-xyz456xyz4"),
			])
			_sweep_orphan_site_jobs()

		mock_delete_job.assert_not_called()

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_sweeps_namespace_even_when_all_rows_have_empty_operation_job_name(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		"""The worker-crash scenario: every row has empty operation_job_name but
		a labeled Job still exists in the cluster.  The sweep must still run."""
		mock_get_all.return_value = [
			SimpleNamespace(
				name="rel-a/demo",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name=None,
			),
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(items=[
				self._job("ks-demo-orphanabcd1234"),
			])
			_sweep_orphan_site_jobs()

		mock_delete_job.assert_called_once()


class UnitTestReconcileSiteDelete(UnitTestCase):
	"""H4: per-branch coverage for _reconcile_site_delete."""

	def _site(self, **overrides) -> SimpleNamespace:
		defaults = {
			"name": "rel-a/demo",
			"cluster": "cluster-a",
			"namespace": "ns",
			"status": "Deleting",
			"operation_job_name": "ks-demo-abcdef123456",
			"operation_job_token": "token-1",
			"bench_release": "rel-a",
			"site_name": "demo",
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _job(self, succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-abcdef123456"),
			status=SimpleNamespace(succeeded=succeeded, failed=failed),
		)

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.delete_doc")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_succeeded_and_missing_deletes_row(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_publish,
		mock_delete_doc,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "missing"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_delete_doc.assert_called_once_with(
			"Frappe Site",
			"rel-a/demo",
			ignore_permissions=True,
			force=True,
			delete_permanently=True,
		)

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_succeeded_but_still_exists_marks_failed_with_detail(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_log_error,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{
				"status": "Failed",
				"status_detail": "Drop-site Job ran but the site is still present on the bench.",
			},
		)
		mock_log_error.assert_called_once()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.delete_doc")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_succeeded_and_unknown_defers_no_state_write(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_delete_doc,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "unknown"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_delete_doc.assert_not_called()
		mock_db_set_value.assert_not_called()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.delete_doc")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_job_failed_but_site_missing_still_deletes_row(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_publish,
		mock_delete_doc,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "missing"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True), \
			patch("kubeport.tasks.reconciliation._extract_job_failure_detail", return_value="ignored"):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_delete_doc.assert_called_once()

	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_job_failed_and_site_exists_marks_failed_with_log_detail(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_log_error,
		mock_probe_state,
		mock_extract_detail,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "exists"
		mock_extract_detail.return_value = "drop failed: permission denied"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{"status": "Failed", "status_detail": "drop failed: permission denied"},
		)
		mock_log_error.assert_called_once()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.delete_doc")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_job_404_falls_back_to_probe_and_deletes_on_missing(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_publish,
		mock_delete_doc,
		mock_probe_state,
	):
		from kubernetes.client.rest import ApiException

		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Deleting"}
		mock_probe_state.return_value = "missing"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.side_effect = ApiException(status=404)
			_reconcile_frappe_sites()

		mock_delete_doc.assert_called_once()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.delete_doc")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_token_mismatch_skips_even_on_missing_probe(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_delete_doc,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site(operation_job_token="token-old")]
		mock_db_get_value.return_value = {"operation_token": "token-new", "status": "Deleting"}
		mock_probe_state.return_value = "missing"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_delete_doc.assert_not_called()
		mock_db_set_value.assert_not_called()


class UnitTestReconcileSiteMigrate(UnitTestCase):
	"""H4: per-branch coverage for _reconcile_site_migrate."""

	def _site(self, **overrides) -> SimpleNamespace:
		defaults = {
			"name": "rel-a/demo",
			"cluster": "cluster-a",
			"namespace": "ns",
			"status": "Migrating",
			"operation_job_name": "ks-demo-abcdef123456",
			"operation_job_token": "token-1",
			"bench_release": "rel-a",
			"site_name": "demo",
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _job(self, succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-abcdef123456"),
			status=SimpleNamespace(succeeded=succeeded, failed=failed),
		)

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_succeeded_and_exists_transitions_to_active(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{"status": "Active", "status_detail": ""},
		)

	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_succeeded_but_site_missing_marks_failed(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_log_error,
		mock_probe_state,
		mock_extract_detail,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "missing"
		mock_extract_detail.return_value = "Migration log: bad column"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{"status": "Failed", "status_detail": "Migration log: bad column"},
		)
		mock_log_error.assert_called_once()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_failed_but_site_functional_recovers_to_active(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{"status": "Active", "status_detail": ""},
		)

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_unknown_probe_defers_no_write(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "unknown"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_not_called()

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_job_404_with_exists_recovers_to_active(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_probe_state,
	):
		from kubernetes.client.rest import ApiException

		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.side_effect = ApiException(status=404)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo",
			{"status": "Active", "status_detail": ""},
		)

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_job_404_with_missing_marks_failed(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_probe_state,
	):
		from kubernetes.client.rest import ApiException

		mock_get_all.return_value = [self._site()]
		mock_db_get_value.return_value = {"operation_token": "token-1", "status": "Migrating"}
		mock_probe_state.return_value = "missing"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.side_effect = ApiException(status=404)
			_reconcile_frappe_sites()

		args = mock_db_set_value.call_args.args
		self.assertEqual(args[0:2], ("Frappe Site", "rel-a/demo"))
		self.assertEqual(args[2]["status"], "Failed")
		self.assertIn("Migrate Job disappeared", args[2]["status_detail"])

	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_token_mismatch_skips_state_write(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_probe_state,
	):
		mock_get_all.return_value = [self._site(operation_job_token="token-old")]
		mock_db_get_value.return_value = {"operation_token": "token-new", "status": "Migrating"}
		mock_probe_state.return_value = "exists"

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, \
			patch("kubernetes.client.CoreV1Api"), \
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_not_called()
