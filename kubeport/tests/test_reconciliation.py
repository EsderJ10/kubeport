# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase

from kubeport.kubeport.doctype.helm_release.helm_release import calculate_release_spec_hash
from kubeport.tasks.reconciliation import (
	SITE_PROBE_EXISTS,
	SITE_PROBE_MISSING,
	SITE_PROBE_UNKNOWN,
	_bucket_for_error,
	_finalize_site_status,
	_job_belongs_to_backup,
	_job_belongs_to_site,
	_prune_backup_retention,
	_reconcile_frappe_site_backups,
	_reconcile_frappe_sites,
	_reconcile_helm_releases,
	_reconcile_service_bundles,
	_reconcile_site_backup,
	_reconcile_stale_helm_operations,
	_run_scheduled_backups,
	_summarize_unrunnable_pod,
	_sweep_orphan_site_jobs,
	_TickErrorLog,
	reconcile_all_releases,
	reconcile_site_backups,
)
from kubeport.utils.release_health import ResourceHealth


class UnitTestReconciliation(UnitTestCase):
	@patch("kubeport.tasks.reconciliation._sweep_orphan_site_jobs")
	@patch("kubeport.tasks.reconciliation._reconcile_frappe_sites")
	@patch("kubeport.tasks.reconciliation._reconcile_service_bundles")
	@patch("kubeport.tasks.reconciliation._reconcile_stale_helm_operations")
	@patch("kubeport.tasks.reconciliation._reconcile_helm_releases")
	def test_reconcile_all_releases_only_runs_active_sweeps(
		self,
		mock_reconcile_helm_releases,
		mock_reconcile_stale_helm_operations,
		mock_reconcile_service_bundles,
		mock_reconcile_frappe_sites,
		mock_sweep_orphan_site_jobs,
	):
		reconcile_all_releases()

		mock_reconcile_helm_releases.assert_called_once_with()
		mock_reconcile_stale_helm_operations.assert_called_once_with()
		mock_reconcile_service_bundles.assert_called_once_with()
		mock_reconcile_frappe_sites.assert_called_once_with()
		mock_sweep_orphan_site_jobs.assert_called_once_with()

	@patch("kubeport.tasks.reconciliation._prune_backup_retention")
	@patch("kubeport.tasks.reconciliation._run_scheduled_backups")
	@patch("kubeport.tasks.reconciliation._reconcile_frappe_site_backups")
	def test_reconcile_site_backups_delegates_to_frappe_site_backups(
		self,
		mock_reconcile_frappe_site_backups,
		mock_run_scheduled_backups,
		mock_prune_backup_retention,
	):
		reconcile_site_backups()

		mock_reconcile_frappe_site_backups.assert_called_once_with()
		mock_run_scheduled_backups.assert_called_once_with()
		mock_prune_backup_retention.assert_called_once_with()

	def test_summarize_unrunnable_pod_returns_terminated_exit_code_with_reason(self):
		pod = SimpleNamespace(
			status=SimpleNamespace(
				container_statuses=[
					SimpleNamespace(
						state=SimpleNamespace(
							terminated=SimpleNamespace(exit_code=137, reason="OOMKilled", message="killed"),
							waiting=None,
						)
					)
				],
				init_container_statuses=None,
				conditions=None,
				phase="Failed",
				reason="",
				message="",
			)
		)

		detail = _summarize_unrunnable_pod(pod)

		self.assertEqual(detail, "Job pod exited with code 137 (OOMKilled): killed")

	def test_summarize_unrunnable_pod_surfaces_waiting_container_reason(self):
		pod = SimpleNamespace(
			status=SimpleNamespace(
				container_statuses=[
					SimpleNamespace(
						state=SimpleNamespace(
							terminated=None,
							waiting=SimpleNamespace(
								reason="ContainerCreating",
								message="unbound PersistentVolumeClaim 'kubeport-backups'",
							),
						)
					)
				],
				init_container_statuses=None,
				conditions=None,
				phase="Pending",
				reason="",
				message="",
			)
		)

		detail = _summarize_unrunnable_pod(pod)

		self.assertEqual(
			detail,
			"Job pod stuck in ContainerCreating: unbound PersistentVolumeClaim 'kubeport-backups'",
		)

	def test_summarize_unrunnable_pod_surfaces_unschedulable_condition(self):
		pod = SimpleNamespace(
			status=SimpleNamespace(
				container_statuses=None,
				init_container_statuses=None,
				conditions=[
					SimpleNamespace(
						status="False",
						reason="Unschedulable",
						message="0/1 nodes are available: 1 pod has unbound immediate PersistentVolumeClaims",
					)
				],
				phase="Pending",
				reason="",
				message="",
			)
		)

		detail = _summarize_unrunnable_pod(pod)

		self.assertEqual(
			detail,
			"Job pod could not run (Unschedulable): "
			"0/1 nodes are available: 1 pod has unbound immediate PersistentVolumeClaims",
		)

	def test_summarize_unrunnable_pod_falls_back_to_phase_when_no_signals(self):
		pod = SimpleNamespace(
			status=SimpleNamespace(
				container_statuses=None,
				init_container_statuses=None,
				conditions=None,
				phase="Failed",
				reason="DeadlineExceeded",
				message="Job was active longer than specified deadline",
			)
		)

		detail = _summarize_unrunnable_pod(pod)

		self.assertEqual(
			detail,
			"Job pod phase=Failed, reason=DeadlineExceeded: Job was active longer than specified deadline",
		)

	def test_summarize_unrunnable_pod_returns_none_when_status_missing(self):
		self.assertIsNone(_summarize_unrunnable_pod(SimpleNamespace(status=None)))

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

		mock_set_value.assert_called_once_with(
			"Helm Release",
			"bench-a",
			{
				"status": "Deployed",
				"helm_status_detail": "deployed (no workload resources)",
			},
		)
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
	def test_reconcile_helm_releases_skips_unchanged_status_detail(
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
				status="Deployed",
				operation_token="tok-1",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Deployed",
			"helm_status_detail": "deployed (no workload resources)",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_not_called()
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_refreshes_same_status_detail_without_modified(
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
				status="Deployed",
				operation_token="tok-1",
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Deployed",
			"helm_status_detail": "old detail",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_called_once_with(
			"Helm Release",
			"bench-a",
			"helm_status_detail",
			"deployed (no workload resources)",
			update_modified=False,
		)
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.helm.status", side_effect=RuntimeError("release: not found"))
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_surfaces_missing_live_release(
		self,
		mock_get_all,
		mock_set_value,
		_mock_helm_status,
		mock_get_value,
		_mock_publish,
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
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Deployed",
		}

		_reconcile_helm_releases()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Degraded")
		self.assertIn("missing from the cluster", fields["helm_status_detail"])
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

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.helm.status", side_effect=RuntimeError("release: not found"))
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_dedups_log_error_per_cluster_per_tick(
		self,
		mock_get_all,
		_mock_set_value,
		_mock_helm_status,
		mock_get_value,
		_mock_publish,
		mock_log_error,
		mock_logger,
	):
		"""Many releases failing on the same root cause must not multiply Error Log rows.

		Mirrors the scaling-benchmark seeded-rows scenario (all rows pointing at
		one unreachable cluster).  Each row still gets its own status writeback,
		but the Error Log rotation only takes one row per (cluster, error class)
		per tick — recurring rows demote to ``logger.warning``.
		"""
		mock_get_all.return_value = [
			SimpleNamespace(
				name=f"cluster-a/default/rel-{i:03d}",
				cluster="cluster-a",
				namespace="default",
				release_name=f"rel-{i:03d}",
				status="Deployed",
				operation_token=f"tok-{i:03d}",
			)
			for i in range(5)
		]

		def _matching_state(_doctype, docname, *_args, **_kwargs):
			# docname looks like "cluster-a/default/rel-003"; mirror the row's
			# operation_token so the token guard inside
			# _set_helm_reconciliation_state passes for every release.
			idx = docname.rsplit("-", 1)[-1]
			return {
				"operation_token": f"tok-{idx}",
				"status": "Deployed",
			}

		mock_get_value.side_effect = _matching_state

		_reconcile_helm_releases()

		# Exactly one Error Log row for the whole batch — not five.
		mock_log_error.assert_called_once()
		title = mock_log_error.call_args.kwargs.get("title", "")
		self.assertIn("Helm Reconciliation Error", title)

		# The remaining four rows demoted to logger.warning so the operator
		# still has visibility without polluting tabError Log.
		warning_calls = mock_logger.return_value.warning.call_args_list
		self.assertGreaterEqual(len(warning_calls), 4)

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.helm.status", side_effect=RuntimeError("release: not found"))
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_helm_releases_demotes_recurring_failure_to_warning(
		self,
		mock_get_all,
		_mock_set_value,
		_mock_helm_status,
		mock_get_value,
		_mock_publish,
		mock_log_error,
		mock_logger,
	):
		"""A row already Degraded with the same error must not log_error again.

		``_set_helm_reconciliation_state`` returns ``False`` when status equals
		next_status and the truncated detail matches; the dedup path then
		emits a warning instead of writing another Error Log row.
		"""
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
		# The row is already Degraded with an identical detail — no transition.
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Degraded",
			"helm_status_detail": ("Helm release is missing from the cluster: release: not found"),
		}

		_reconcile_helm_releases()

		mock_log_error.assert_not_called()
		mock_logger.return_value.warning.assert_called()

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_service_bundles_dedups_cluster_unreachable_log_error(
		self,
		mock_get_all,
		mock_get_client,
		_mock_set_value,
		mock_log_error,
		_mock_logger,
	):
		"""Many bundles on one unreachable cluster must produce one Error Log.

		Without dedup, a fleet of bundles bound to one broken cluster wrote
		one Error Log row per bundle per tick.  After dedup the cluster-build
		failure logs once per (cluster, error class) per tick; per-bundle
		status writebacks still happen so the UI surface is unchanged.
		"""
		mock_get_all.return_value = [
			SimpleNamespace(
				name=f"bundle-{i:03d}",
				cluster="cluster-a",
				namespace="default",
				content="{}",
				status="Deployed",
			)
			for i in range(5)
		]
		mock_get_client.side_effect = RuntimeError("kubeconfig invalid")

		_reconcile_service_bundles()

		mock_log_error.assert_called_once()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.tasks.reconciliation._helm_operation_is_stale", return_value=True)
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_stale_helm_operation_recovers_successful_deploy(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		_mock_is_stale,
		mock_get_value,
		mock_publish,
	):
		# Use frappe._dict (a dict subclass) — the same shape that
		# frappe.get_all returns in production. Plain SimpleNamespace
		# masks the ``release.values`` vs ``release.get("values")`` field
		# resolution path; see test_reconcile_stale_helm_operation_reads_values_via_get.
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "cluster-a/default/bench-a",
					"cluster": "cluster-a",
					"namespace": "default",
					"release_name": "bench-a",
					"chart": "repo/erpnext",
					"chart_version": "8.0.41",
					"values": "",
					"status": "In Progress",
					"operation_token": "tok-1",
					"operation_started_at": "2026-04-12 10:00:00",
					"modified": "2026-04-12 10:00:00",
				}
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "In Progress",
		}

		_reconcile_stale_helm_operations()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Deployed")
		self.assertEqual(fields["last_applied_chart_version"], "8.0.41")
		self.assertEqual(fields["pending_changes"], 0)
		self.assertEqual(fields["operation_type"], "")
		mock_publish.assert_called_once()

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.tasks.reconciliation._helm_operation_is_stale", return_value=True)
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_stale_helm_operation_hash_includes_site_image_digest(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		_mock_is_stale,
		mock_get_value,
		_mock_publish,
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "cluster-a/default/bench-a",
					"cluster": "cluster-a",
					"namespace": "default",
					"release_name": "bench-a",
					"chart": "repo/erpnext",
					"chart_version": "8.0.41",
					"values": "workers:\n  replicaCount: 2\n",
					"site_image": "ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
					"status": "In Progress",
					"operation_token": "tok-1",
					"operation_started_at": "2026-04-12 10:00:00",
					"modified": "2026-04-12 10:00:00",
				}
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		mock_get_value.side_effect = [
			"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			{
				"operation_token": "tok-1",
				"status": "In Progress",
			},
		]

		_reconcile_stale_helm_operations()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		expected_hash = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="default",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			site_image="ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			site_image_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		)
		self.assertEqual(fields["desired_spec_hash"], expected_hash)
		self.assertEqual(fields["last_applied_spec_hash"], expected_hash)

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.tasks.reconciliation._helm_operation_is_stale", return_value=True)
	@patch("kubeport.utils.release_health.walk")
	@patch("kubeport.utils.helm.status")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_stale_helm_operation_reads_values_via_get_not_attribute(
		self,
		mock_get_all,
		mock_set_value,
		mock_helm_status,
		mock_walk,
		_mock_is_stale,
		mock_get_value,
		_mock_publish,
	):
		"""Regression for the field/method name clash on ``frappe._dict``.

		``release.values`` resolves to the inherited ``dict.values`` method,
		not the YAML field — so the previous reconciler implementation
		threw ``AttributeError`` from ``yaml.safe_load`` and unconditionally
		marked stale rows ``Failed`` instead of recovering them. This test
		pins the spec hash to the *content* of the YAML, which only matches
		when the reconciler reads the field via ``release.get("values")``
		(or subscript). If the bug regresses, ``yaml.safe_load`` raises and
		the assertion below catches the wrong terminal state.
		"""
		values_yaml = "workers:\n  replicaCount: 2\nresources:\n  cpu: 500m\n"
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "cluster-a/default/bench-a",
					"cluster": "cluster-a",
					"namespace": "default",
					"release_name": "bench-a",
					"chart": "repo/erpnext",
					"chart_version": "8.0.41",
					"values": values_yaml,
					"site_image": "",
					"status": "In Progress",
					"operation_token": "tok-1",
					"operation_started_at": "2026-04-12 10:00:00",
					"modified": "2026-04-12 10:00:00",
				}
			),
		]
		mock_helm_status.return_value = {"info": {"status": "deployed"}}
		mock_walk.return_value = []
		# Empty site_image short-circuits _get_site_image_digest_for_hash
		# without hitting the DB, so the only get_value call is the
		# token/status guard inside _set_stale_helm_operation_state.
		mock_get_value.return_value = {"operation_token": "tok-1", "status": "In Progress"}

		_reconcile_stale_helm_operations()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Deployed")
		expected_hash = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="default",
			release_name="bench-a",
			values_yaml=values_yaml,
			site_image="",
			site_image_digest="",
		)
		self.assertEqual(fields["desired_spec_hash"], expected_hash)
		self.assertEqual(fields["last_applied_spec_hash"], expected_hash)

	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.tasks.reconciliation._helm_operation_is_stale", return_value=True)
	@patch("kubeport.utils.helm.status", side_effect=RuntimeError("release: not found"))
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_reconcile_stale_uninstall_returns_missing_release_to_draft(
		self,
		mock_get_all,
		mock_set_value,
		_mock_helm_status,
		_mock_is_stale,
		mock_get_value,
		mock_publish,
	):
		mock_get_all.return_value = [
			SimpleNamespace(
				name="cluster-a/default/bench-a",
				cluster="cluster-a",
				namespace="default",
				release_name="bench-a",
				chart="repo/erpnext",
				chart_version="8.0.41",
				values="",
				status="Uninstalling",
				operation_token="tok-1",
				operation_started_at="2026-04-12 10:00:00",
				modified="2026-04-12 10:00:00",
			),
		]
		mock_get_value.return_value = {
			"operation_token": "tok-1",
			"status": "Uninstalling",
		}

		_reconcile_stale_helm_operations()

		mock_set_value.assert_called_once()
		_, _, fields = mock_set_value.call_args.args
		self.assertEqual(fields["status"], "Draft")
		self.assertEqual(fields["last_applied_spec_hash"], "")
		self.assertEqual(fields["pending_changes"], 0)
		mock_publish.assert_called_once()

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

		self.assertEqual(
			mock_set_value.call_args_list,
			[
				call("Service Bundle", "bundle-a", "status", "Deployed"),
				call("Service Bundle", "bundle-a", "status_detail", ""),
			],
		)

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

		self.assertEqual(
			mock_set_value.call_args_list,
			[
				call("Service Bundle", "bundle-a", "status", "Degraded"),
				call(
					"Service Bundle",
					"bundle-a",
					"status_detail",
					"ConfigMap/demo not found in namespace default.",
				),
			],
		)
		mock_log_error.assert_called_once()


class UnitTestTickErrorLog(UnitTestCase):
	"""Focused unit tests for the per-tick log_error dedup helper."""

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	def test_first_emit_writes_log_error_subsequent_demote_to_warning(
		self,
		mock_log_error,
		mock_logger,
	):
		log = _TickErrorLog()

		first = log.emit("bucket-a", title="t", message="m")
		second = log.emit("bucket-a", title="t", message="m", warn_msg="recurring")
		third = log.emit("bucket-a", title="t", message="m")

		self.assertTrue(first)
		self.assertFalse(second)
		self.assertFalse(third)
		mock_log_error.assert_called_once_with(title="t", message="m")
		warn_calls = mock_logger.return_value.warning.call_args_list
		self.assertEqual(len(warn_calls), 2)
		# The override warn_msg flows through.
		self.assertEqual(warn_calls[0].args[1], "recurring")

	@patch("kubeport.tasks.reconciliation.frappe.logger")
	@patch("kubeport.tasks.reconciliation.frappe.log_error")
	def test_distinct_buckets_each_get_one_log_error(
		self,
		mock_log_error,
		_mock_logger,
	):
		log = _TickErrorLog()

		log.emit("bucket-a", title="t-a", message="m-a")
		log.emit("bucket-b", title="t-b", message="m-b")

		self.assertEqual(mock_log_error.call_count, 2)

	def test_bucket_for_error_collapses_helm_not_found(self):
		# Both spellings of the helm "release: not found" message map to the
		# same dedup bucket so the variants do not multiply Error Log rows.
		bucket_a = _bucket_for_error(RuntimeError("Error: release: not found"))
		bucket_b = _bucket_for_error(RuntimeError("HELM RELEASE NOT FOUND"))
		self.assertEqual(bucket_a, bucket_b)
		self.assertEqual(bucket_a, "release-not-found")

	def test_bucket_for_error_uses_class_name_for_other_errors(self):
		self.assertEqual(_bucket_for_error(ValueError("x")), "ValueError")
		self.assertEqual(_bucket_for_error(KeyError("x")), "KeyError")


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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
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
			name="rel/s",
			operation_job_token="t",
			operation_job_name="j",
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
			name="rel/s",
			operation_job_token="t",
			operation_job_name="j",
		)
		applied = _finalize_site_status(site, "In Progress", "Active", "")
		self.assertTrue(applied)
		mock_set_value.assert_called_once_with(
			"Frappe Site",
			"rel/s",
			{
				"status": "Active",
				"status_detail": "",
			},
		)
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

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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
			metadata=SimpleNamespace(
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site": "rel-a-demo.example.com",
				}
			),
		)
		self.assertTrue(_job_belongs_to_site(job, site))

	def test_returns_false_when_managed_by_wrong(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(
			metadata=SimpleNamespace(
				labels={
					"app.kubernetes.io/managed-by": "someone-else",
					"kubeport.io/frappe-site": "rel-a-demo.example.com",
				}
			),
		)
		self.assertFalse(_job_belongs_to_site(job, site))

	def test_returns_false_when_site_label_missing(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(
			metadata=SimpleNamespace(
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
				}
			),
		)
		self.assertFalse(_job_belongs_to_site(job, site))

	def test_returns_false_when_metadata_missing(self):
		site = SimpleNamespace(name="rel-a/demo.example.com")
		job = SimpleNamespace(metadata=None)
		self.assertFalse(_job_belongs_to_site(job, site))


class UnitTestJobBelongsToBackup(UnitTestCase):
	def test_returns_true_when_backup_labels_match(self):
		backup = SimpleNamespace(name="demo.example.com::demo-20260430120000")
		job = SimpleNamespace(
			metadata=SimpleNamespace(
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site-backup": "demo.example.com-demo-20260430120000",
				}
			),
		)
		self.assertTrue(_job_belongs_to_backup(job, backup))

	def test_returns_false_when_backup_label_missing(self):
		backup = SimpleNamespace(name="demo.example.com::demo-20260430120000")
		job = SimpleNamespace(
			metadata=SimpleNamespace(
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site": "rel-a-demo.example.com",
				}
			),
		)
		self.assertFalse(_job_belongs_to_backup(job, backup))


class UnitTestReconcileFrappeSiteBackups(UnitTestCase):
	def _backup(self, **overrides) -> SimpleNamespace:
		defaults = {
			"name": "demo.example.com::demo-20260430120000",
			"frappe_site": "rel-a/demo.example.com",
			"cluster": "cluster-a",
			"namespace": "ns",
			"status": "In Progress",
			"site_name": "demo.example.com",
			"source_bench_release": "rel-a",
			"operation_job_name": "ks-demo-abcdef123456",
			"operation_job_token": "token-1",
			"operation_token": "token-1",
			"storage_path": "/mnt/kubeport-backups/cluster-a/ns/demo/demo.tar.gz",
			"size_bytes": 0,
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _job(self, succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-abcdef123456", labels={}),
			status=SimpleNamespace(succeeded=succeeded, failed=failed),
		)

	@patch("kubeport.tasks.reconciliation.frappe.utils.now_datetime")
	@patch("kubeport.tasks.reconciliation._extract_backup_success_metadata")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_backup_success_marks_backup_available_and_parent_active(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_metadata,
		mock_now,
	):
		mock_get_all.return_value = [self._backup()]
		mock_metadata.return_value = {"size_bytes": 42, "bench_archive_name": "db.sql.gz"}
		mock_db_get_value.side_effect = [
			{"operation_token": "token-1", "status": "In Progress"},
			{"operation_token": "token-1", "status": "In Progress"},
		]

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_backup", return_value=True),
			patch(
				"kubeport.tasks.reconciliation._probe_backup_archive_on_pvc",
				return_value=(SITE_PROBE_EXISTS, 42),
			),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_site_backups()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
			{"status": "Active", "status_detail": ""},
		)
		backup_write = mock_db_set_value.call_args_list[0].args
		self.assertEqual(backup_write[0:2], ("Frappe Site Backup", "demo.example.com::demo-20260430120000"))
		self.assertEqual(backup_write[2]["status"], "Available")
		self.assertEqual(backup_write[2]["size_bytes"], 42)
		mock_publish.assert_called()

	@patch("kubeport.tasks.reconciliation.frappe.utils.now_datetime")
	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_restore_false_negative_uses_functional_probe(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_probe_state,
		mock_now,
	):
		mock_get_all.return_value = [self._backup(status="Restoring")]
		mock_db_get_value.side_effect = [
			{"operation_token": "token-1", "status": "Restoring"},
			{"operation_token": "token-1", "status": "Migrating"},
		]
		mock_probe_state.return_value = "exists"

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_backup", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_site_backups()

		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
			{"status": "Active", "status_detail": ""},
		)

	@patch("kubeport.tasks.reconciliation.frappe.utils.now_datetime")
	@patch("kubeport.tasks.reconciliation._extract_job_failure_detail")
	@patch("kubeport.tasks.reconciliation._probe_site_state")
	@patch("kubeport.tasks.reconciliation.frappe.publish_realtime")
	@patch("kubeport.tasks.reconciliation.frappe.db.set_value")
	@patch("kubeport.tasks.reconciliation.frappe.db.get_value")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_restore_missing_probe_marks_site_failed_and_backup_available(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_db_get_value,
		mock_db_set_value,
		mock_publish,
		mock_probe_state,
		mock_failure_detail,
		mock_now,
	):
		detail = "bench restore failed: database import failed"
		mock_get_all.return_value = [self._backup(status="Restoring")]
		mock_db_get_value.side_effect = [
			{"operation_token": "token-1", "status": "Restoring"},
			{"operation_token": "token-1", "status": "Migrating"},
		]
		mock_probe_state.return_value = SITE_PROBE_MISSING
		mock_failure_detail.return_value = detail

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_backup", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(failed=1)
			_reconcile_frappe_site_backups()

		backup_write = next(
			call_args.args
			for call_args in mock_db_set_value.call_args_list
			if call_args.args[0:2] == ("Frappe Site Backup", "demo.example.com::demo-20260430120000")
		)
		backup_fields = backup_write[2]
		self.assertEqual(backup_fields["status"], "Available")
		self.assertEqual(backup_fields["status_detail"], detail)
		self.assertIn("completed_at", backup_fields)
		self.assertEqual(backup_fields["operation_job_name"], "")
		self.assertEqual(backup_fields["operation_job_token"], "")
		mock_db_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
			{"status": "Failed", "status_detail": detail},
		)
		mock_publish.assert_any_call(
			"frappe_site_backup_status_update",
			{
				"site_docname": "rel-a/demo.example.com",
				"backup_docname": "demo.example.com::demo-20260430120000",
				"status": "Available",
			},
			doctype="Frappe Site Backup",
			docname="demo.example.com::demo-20260430120000",
		)


class UnitTestSweepOrphanSiteJobs(UnitTestCase):
	def _job(self, name: str) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(
				name=name,
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site": "rel-a-demo",
				},
			)
		)

	def _job_with_age(self, name: str, age_seconds: int) -> SimpleNamespace:
		from datetime import datetime, timedelta

		created = datetime.now(UTC) - timedelta(seconds=age_seconds)
		return SimpleNamespace(
			metadata=SimpleNamespace(
				name=name,
				creation_timestamp=created,
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site": "rel-a-demo",
				},
			),
		)

	def _backup_job(self, name: str) -> SimpleNamespace:
		return SimpleNamespace(
			metadata=SimpleNamespace(
				name=name,
				labels={
					"app.kubernetes.io/managed-by": "kubeport",
					"kubeport.io/frappe-site-backup": "demo.example.com-demo-20260430120000",
				},
			)
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
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._job("ks-demo-known123456ab"),  # tracked — keep
					self._job("ks-demo-orphanabcd1234"),  # not tracked — delete
				]
			)
			_sweep_orphan_site_jobs()

		# Exactly one delete — the orphan.
		mock_delete_job.assert_called_once()
		_api, name, namespace = mock_delete_job.call_args.args
		self.assertEqual(name, "ks-demo-orphanabcd1234")
		self.assertEqual(namespace, "ns")

	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.utils.k8s_client.get_k8s_api_client")
	@patch("kubeport.tasks.reconciliation.frappe.get_all")
	def test_sweeps_backup_labeled_orphan_jobs(
		self,
		mock_get_all,
		mock_get_api_client,
		mock_delete_job,
	):
		mock_get_all.side_effect = [
			[],
			[
				SimpleNamespace(
					name="demo.example.com::demo-20260430120000",
					cluster="cluster-a",
					namespace="ns",
					operation_job_name="ks-backup-known1234",
				),
			],
		]
		mock_get_api_client.return_value = MagicMock()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api:
			batch = mock_batch_api.return_value
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._backup_job("ks-backup-known1234"),
					self._backup_job("ks-backup-orphan1234"),
				]
			)
			_sweep_orphan_site_jobs()

		mock_delete_job.assert_called_once()
		_api, name, namespace = mock_delete_job.call_args.args
		self.assertEqual(name, "ks-backup-orphan1234")
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
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._job_with_age("ks-demo-fresh1234abcd", age_seconds=30),
				]
			)
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
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._job_with_age("ks-demo-orphanold123", age_seconds=600),
				]
			)
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
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._job("ks-demo-abc123abc123"),
					self._job("ks-other-xyz456xyz4"),
				]
			)
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
			batch.list_namespaced_job.return_value = SimpleNamespace(
				items=[
					self._job("ks-demo-orphanabcd1234"),
				]
			)
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
			patch("kubeport.tasks.reconciliation._extract_job_failure_detail", return_value="ignored"),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
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

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
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

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
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

		with (
			patch("kubernetes.client.BatchV1Api") as mock_batch_api,
			patch("kubernetes.client.CoreV1Api"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			mock_batch_api.return_value.read_namespaced_job.return_value = self._job(succeeded=1)
			_reconcile_frappe_sites()

		mock_db_set_value.assert_not_called()


class UnitTestReconcileSiteBackupProbe(UnitTestCase):
	"""Backup ground-truth via the PVC probe.  Covers the gone-Job recovery
	path (where the Job aged out before reconciliation read its status) and
	the post-success verification path (where we re-check the archive after
	the Job's stdout claimed it wrote one)."""

	def _backup(self, **overrides):
		defaults = {
			"name": "demo::demo-20260504",
			"frappe_site": "rel-a/demo",
			"cluster": "cluster-a",
			"namespace": "ns",
			"site_name": "demo",
			"source_bench_release": "rel-a",
			"status": "In Progress",
			"operation_job_name": "ks-demo-deadbeef",
			"operation_job_token": "token-1",
			"operation_token": "token-1",
			"storage_path": "/mnt/kubeport-backups/cluster-a/ns/demo/demo-20260504.tar.gz",
			"size_bytes": 0,
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	def _gone_job(self):
		from kubernetes.client.rest import ApiException

		batch = MagicMock()
		batch.read_namespaced_job.side_effect = ApiException(status=404)
		return batch

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	def test_gone_job_with_archive_present_marks_available(
		self,
		mock_probe,
		mock_finalize,
	):
		"""Job aged out before we read it, but the probe finds the archive
		on the PVC — backup recovers to Available with the probed size."""
		mock_probe.return_value = (SITE_PROBE_EXISTS, 4096)

		_reconcile_site_backup(
			self._backup(),
			self._gone_job(),
			MagicMock(),
			MagicMock(),
		)

		mock_finalize.assert_called_once()
		args = mock_finalize.call_args
		self.assertEqual(args.args[1:4], ("In Progress", "Available", ""))
		self.assertEqual(args.kwargs.get("size_bytes"), 4096)

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	def test_gone_job_with_archive_missing_marks_failed(
		self,
		mock_probe,
		mock_finalize,
	):
		"""Job aged out and the probe confirms no archive on the PVC —
		backup must be Failed, not silently lost as the previous size_bytes
		heuristic did."""
		mock_probe.return_value = (SITE_PROBE_MISSING, None)

		_reconcile_site_backup(
			self._backup(),
			self._gone_job(),
			MagicMock(),
			MagicMock(),
		)

		mock_finalize.assert_called_once()
		args = mock_finalize.call_args
		self.assertEqual(args.args[1:3], ("In Progress", "Failed"))
		self.assertIn("not present on the PVC", args.args[3])

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	def test_gone_job_with_unknown_probe_defers(
		self,
		mock_probe,
		mock_finalize,
	):
		"""Probe failure (transient cluster issue, image pull stall) must
		defer the transition to the next reconcile tick — never finalize
		on UNKNOWN."""
		mock_probe.return_value = (SITE_PROBE_UNKNOWN, None)

		_reconcile_site_backup(
			self._backup(),
			self._gone_job(),
			MagicMock(),
			MagicMock(),
		)

		mock_finalize.assert_not_called()

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	@patch("kubeport.tasks.reconciliation._extract_backup_success_metadata")
	@patch("kubeport.tasks.reconciliation._job_belongs_to_backup", return_value=True)
	def test_succeeded_job_with_missing_archive_marks_failed(
		self,
		_mock_belongs,
		mock_extract_meta,
		mock_probe,
		mock_finalize,
	):
		"""Job exited zero and stdout reported a size, but the probe finds
		no archive on the PVC (narrow window: tar exited 0 but the inode
		was lost between exit and our read).  Better Failed than a
		dangling Available row that points at nothing."""
		mock_extract_meta.return_value = {
			"size_bytes": 4096,
			"bench_archive_name": "demo-2026-05-04.tar",
		}
		mock_probe.return_value = (SITE_PROBE_MISSING, None)

		batch = MagicMock()
		batch.read_namespaced_job.return_value = SimpleNamespace(
			metadata=SimpleNamespace(name="ks-demo-deadbeef"),
			status=SimpleNamespace(succeeded=1, failed=0),
		)

		_reconcile_site_backup(
			self._backup(),
			batch,
			MagicMock(),
			MagicMock(),
		)

		mock_finalize.assert_called_once()
		args = mock_finalize.call_args
		self.assertEqual(args.args[1:3], ("In Progress", "Failed"))
		self.assertIn("not on the PVC", args.args[3])

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	def test_gone_job_defers_when_probe_budget_exhausted(
		self,
		mock_probe,
		mock_finalize,
	):
		"""Per-tick budget cap: the synchronous probe is rate-limited so a
		fleet of gone-Job rows can't blow the 300 s RQ timeout.  When the
		budget is exhausted, the row stays In Progress and the next tick
		tries again — the probe must NOT be called and the row must NOT be
		finalized."""
		_reconcile_site_backup(
			self._backup(),
			self._gone_job(),
			MagicMock(),
			MagicMock(),
			{"remaining": 0},
		)

		mock_probe.assert_not_called()
		mock_finalize.assert_not_called()

	@patch("kubeport.tasks.reconciliation._finalize_backup_status")
	@patch("kubeport.tasks.reconciliation._probe_backup_archive_on_pvc")
	def test_succeeded_job_with_exhausted_budget_finalizes_on_log_metadata(
		self,
		mock_probe,
		mock_finalize,
	):
		"""When the probe budget is exhausted, a succeeded Job's stdout
		metadata is trusted (size_bytes > 0).  The alternative — defer —
		would risk the Job aging out before the next tick can verify."""
		# Patch the metadata extraction so the success path runs.
		with (
			patch("kubeport.tasks.reconciliation._extract_backup_success_metadata") as mock_meta,
			patch("kubeport.tasks.reconciliation._job_belongs_to_backup", return_value=True),
		):
			mock_meta.return_value = {"size_bytes": 4096, "bench_archive_name": "demo.tar"}
			batch = MagicMock()
			batch.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(name="ks-demo-deadbeef"),
				status=SimpleNamespace(succeeded=1, failed=0),
			)
			_reconcile_site_backup(
				self._backup(),
				batch,
				MagicMock(),
				MagicMock(),
				{"remaining": 0},
			)

		mock_probe.assert_not_called()
		mock_finalize.assert_called_once()
		args = mock_finalize.call_args
		self.assertEqual(args.args[1:4], ("In Progress", "Available", ""))
		self.assertEqual(args.kwargs.get("size_bytes"), 4096)


class UnitTestRunScheduledBackups(UnitTestCase):
	"""Cron-driven enqueue path for the scheduled-backup tick.

	Mocks Frappe ORM helpers so the unit test runs hermetically and does not
	require a live test site.  Validates: due-vs-not-due decision, the
	last_run advance happens before the enqueue, the in-flight guard skips
	without rolling back the marker, and the manual-vs-scheduler race is
	covered by the post-marker re-check.
	"""

	def _site_row(self, **overrides):
		now = datetime(2026, 5, 10, 12, 0, 0)
		defaults = {
			"name": "rel-a/demo",
			"backup_schedule": "0 2 * * *",
			"backup_schedule_last_run": now - timedelta(days=2),
			"creation": now - timedelta(days=10),
		}
		defaults.update(overrides)
		return SimpleNamespace(**defaults)

	@patch("frappe.utils.now_datetime")
	@patch("frappe.get_doc")
	@patch("frappe.db.set_value")
	@patch("frappe.db.get_value")
	@patch("frappe.get_all")
	def test_due_schedule_advances_marker_then_enqueues(
		self,
		mock_get_all,
		mock_db_get_value,
		mock_db_set_value,
		mock_get_doc,
		mock_now,
	):
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		row = self._site_row()
		mock_get_all.return_value = [row]
		mock_db_get_value.return_value = {
			"status": "Active",
			"backup_schedule_last_run": row.backup_schedule_last_run,
		}
		site_doc = MagicMock()
		site_doc.status = "Active"
		site_doc._has_in_flight_backup.return_value = False
		mock_get_doc.return_value = site_doc

		_run_scheduled_backups()

		mock_db_set_value.assert_called_once_with("Frappe Site", row.name, "backup_schedule_last_run", now)
		site_doc._enqueue_backup.assert_called_once_with(triggered_by="Administrator")

	@patch("frappe.utils.now_datetime")
	@patch("frappe.db.set_value")
	@patch("frappe.get_all")
	def test_not_due_skips_without_touching_marker(
		self,
		mock_get_all,
		mock_db_set_value,
		mock_now,
	):
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		# last_run was at 02:00 today — next slot is tomorrow 02:00 — not due.
		row = self._site_row(backup_schedule_last_run=datetime(2026, 5, 10, 2, 0, 0))
		mock_get_all.return_value = [row]

		_run_scheduled_backups()

		mock_db_set_value.assert_not_called()

	@patch("frappe.utils.now_datetime")
	@patch("frappe.get_doc")
	@patch("frappe.db.set_value")
	@patch("frappe.db.get_value")
	@patch("frappe.get_all")
	def test_in_flight_backup_skips_without_enqueue_but_advances_marker(
		self,
		mock_get_all,
		mock_db_get_value,
		mock_db_set_value,
		mock_get_doc,
		mock_now,
	):
		"""If the operator clicked Backup Now between the get_all and the
		re-check, the scheduler advances the marker (so the same slot does
		not double-fire next tick) but does not enqueue a duplicate."""
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		row = self._site_row()
		mock_get_all.return_value = [row]
		mock_db_get_value.return_value = {
			"status": "Active",
			"backup_schedule_last_run": row.backup_schedule_last_run,
		}
		site_doc = MagicMock()
		site_doc.status = "Active"
		site_doc._has_in_flight_backup.return_value = True
		mock_get_doc.return_value = site_doc

		_run_scheduled_backups()

		mock_db_set_value.assert_called_once()
		site_doc._enqueue_backup.assert_not_called()

	@patch("frappe.utils.now_datetime")
	@patch("frappe.db.set_value")
	@patch("frappe.db.get_value")
	@patch("frappe.get_all")
	def test_concurrent_marker_advance_skips_second_tick(
		self,
		mock_get_all,
		mock_db_get_value,
		mock_db_set_value,
		mock_now,
	):
		"""If another tick has already advanced ``backup_schedule_last_run``
		past the row's read value, the second tick must not re-fire the same
		slot."""
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		row = self._site_row()
		mock_get_all.return_value = [row]
		# Re-check sees the marker has already moved forward.
		mock_db_get_value.return_value = {
			"status": "Active",
			"backup_schedule_last_run": now - timedelta(minutes=2),
		}

		_run_scheduled_backups()

		mock_db_set_value.assert_not_called()

	@patch("frappe.utils.now_datetime")
	@patch("frappe.db.set_value")
	@patch("frappe.db.get_value")
	@patch("frappe.get_all")
	def test_status_no_longer_active_skips(
		self,
		mock_get_all,
		mock_db_get_value,
		mock_db_set_value,
		mock_now,
	):
		"""Status moved to In Progress / Failed / Deleting between read and
		re-check — do not enqueue."""
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		mock_get_all.return_value = [self._site_row()]
		mock_db_get_value.return_value = {
			"status": "In Progress",
			"backup_schedule_last_run": None,
		}

		_run_scheduled_backups()

		mock_db_set_value.assert_not_called()

	@patch("frappe.get_all")
	def test_no_scheduled_sites_returns_quickly(self, mock_get_all):
		mock_get_all.return_value = []

		_run_scheduled_backups()

	@patch("frappe.utils.now_datetime")
	@patch("frappe.db.set_value")
	@patch("frappe.db.get_value")
	@patch("frappe.get_all")
	def test_long_downtime_triggers_one_catchup_not_a_flood(
		self,
		mock_get_all,
		mock_db_get_value,
		mock_db_set_value,
		mock_now,
	):
		"""After a 30-day outage with a daily cron, one tick must enqueue
		exactly one backup, not 30."""
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		# last_run 30 days ago.
		row = self._site_row(backup_schedule_last_run=now - timedelta(days=30))
		mock_get_all.return_value = [row]
		mock_db_get_value.return_value = {
			"status": "Active",
			"backup_schedule_last_run": row.backup_schedule_last_run,
		}
		site_doc = MagicMock()
		site_doc.status = "Active"
		site_doc._has_in_flight_backup.return_value = False
		with patch("frappe.get_doc", return_value=site_doc):
			_run_scheduled_backups()

		# Exactly one marker advance, exactly one enqueue.
		self.assertEqual(mock_db_set_value.call_count, 1)
		self.assertEqual(site_doc._enqueue_backup.call_count, 1)


class UnitTestPruneBackupRetention(UnitTestCase):
	"""Retention pruning: count cap and age cap on Available rows only."""

	@patch("frappe.delete_doc")
	@patch("frappe.utils.now_datetime")
	@patch("frappe.get_all")
	def test_count_cap_trashes_oldest_beyond_n(
		self,
		mock_get_all,
		mock_now,
		mock_delete_doc,
	):
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		# First call returns the configured sites, subsequent calls return
		# the per-site Available rows (newest first).
		mock_get_all.side_effect = [
			[
				SimpleNamespace(
					name="rel-a/demo",
					backup_retention_count=2,
					backup_retention_days=0,
				),
			],
			[
				SimpleNamespace(name="b1", creation=now - timedelta(days=1)),
				SimpleNamespace(name="b2", creation=now - timedelta(days=2)),
				SimpleNamespace(name="b3", creation=now - timedelta(days=3)),
				SimpleNamespace(name="b4", creation=now - timedelta(days=4)),
			],
		]

		_prune_backup_retention()

		# Newest two retained, the older two trashed.
		self.assertEqual(mock_delete_doc.call_count, 2)
		trashed = {c.args[1] for c in mock_delete_doc.call_args_list}
		self.assertEqual(trashed, {"b3", "b4"})

	@patch("frappe.delete_doc")
	@patch("frappe.utils.now_datetime")
	@patch("frappe.get_all")
	def test_age_cap_trashes_anything_older_than_cutoff(
		self,
		mock_get_all,
		mock_now,
		mock_delete_doc,
	):
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		mock_get_all.side_effect = [
			[
				SimpleNamespace(
					name="rel-a/demo",
					backup_retention_count=0,
					backup_retention_days=7,
				),
			],
			[
				SimpleNamespace(name="b1", creation=now - timedelta(days=1)),
				SimpleNamespace(name="b2", creation=now - timedelta(days=8)),
				SimpleNamespace(name="b3", creation=now - timedelta(days=30)),
			],
		]

		_prune_backup_retention()

		trashed = {c.args[1] for c in mock_delete_doc.call_args_list}
		self.assertEqual(trashed, {"b2", "b3"})

	@patch("frappe.delete_doc")
	@patch("frappe.utils.now_datetime")
	@patch("frappe.get_all")
	def test_count_and_age_combine_via_union(
		self,
		mock_get_all,
		mock_now,
		mock_delete_doc,
	):
		"""When both caps are set, a row that violates either is trashed."""
		now = datetime(2026, 5, 10, 12, 0, 0)
		mock_now.return_value = now
		mock_get_all.side_effect = [
			[
				SimpleNamespace(
					name="rel-a/demo",
					backup_retention_count=3,
					backup_retention_days=7,
				),
			],
			[
				SimpleNamespace(name="b1", creation=now - timedelta(days=1)),
				SimpleNamespace(name="b2", creation=now - timedelta(days=2)),
				SimpleNamespace(name="b3", creation=now - timedelta(days=10)),  # over age cap
				SimpleNamespace(name="b4", creation=now - timedelta(days=15)),  # over count and age
			],
		]

		_prune_backup_retention()

		trashed = {c.args[1] for c in mock_delete_doc.call_args_list}
		self.assertEqual(trashed, {"b3", "b4"})

	@patch("frappe.delete_doc")
	@patch("frappe.get_all")
	def test_no_configured_sites_does_nothing(self, mock_get_all, mock_delete_doc):
		mock_get_all.return_value = []

		_prune_backup_retention()

		mock_delete_doc.assert_not_called()
