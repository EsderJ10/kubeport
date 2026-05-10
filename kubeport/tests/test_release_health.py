# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase
from kubernetes.client.rest import ApiException

from kubeport.utils.release_health import (
	ResourceHealth,
	_attach_warning_events,
	_check_daemon_set,
	_check_deployment,
	_check_ingress,
	_check_job,
	_check_pod,
	_check_pvc,
	_check_service,
	_check_stateful_set,
	classify_release_state,
	summarize,
	walk,
)


def _condition(cond_type: str, status: str, reason: str = "", message: str = ""):
	return SimpleNamespace(type=cond_type, status=status, reason=reason, message=message)


class UnitTestReleaseHealth(UnitTestCase):
	# -----------------------------------------------------------------------
	# Deployment
	# -----------------------------------------------------------------------

	def test_check_deployment_ready_when_all_replicas_available(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe-prod"),
			spec=SimpleNamespace(replicas=2),
			status=SimpleNamespace(
				available_replicas=2,
				updated_replicas=2,
				conditions=[_condition("Available", "True")],
			),
		)
		health = _check_deployment(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.kind, "Deployment")
		self.assertEqual(health.reason, "")
		self.assertEqual(health.pod_count, 2)

	def test_check_deployment_unready_during_rollout_surfaces_replica_count(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe-prod"),
			spec=SimpleNamespace(replicas=2),
			status=SimpleNamespace(
				available_replicas=1,
				updated_replicas=2,
				conditions=[],
			),
		)
		health = _check_deployment(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertIn("1/2", health.reason)
		self.assertEqual(health.pod_count, 1)

	def test_check_deployment_uses_available_condition_when_unavailable(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe-prod"),
			spec=SimpleNamespace(replicas=1),
			status=SimpleNamespace(
				available_replicas=0,
				updated_replicas=0,
				conditions=[
					_condition(
						"Available",
						"False",
						reason="MinimumReplicasUnavailable",
						message="Deployment does not have minimum availability.",
					),
				],
			),
		)
		health = _check_deployment(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "MinimumReplicasUnavailable")

	def test_check_deployment_treats_zero_replicas_as_ready(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="scaled-down"),
			spec=SimpleNamespace(replicas=0),
			status=SimpleNamespace(available_replicas=0, updated_replicas=0, conditions=[]),
		)
		health = _check_deployment(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.pod_count, 0)

	# -----------------------------------------------------------------------
	# StatefulSet
	# -----------------------------------------------------------------------

	def test_check_stateful_set_ready_when_replicas_match_and_revision_settled(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="mariadb"),
			spec=SimpleNamespace(replicas=1),
			status=SimpleNamespace(
				ready_replicas=1,
				current_revision="rev-1",
				update_revision="rev-1",
			),
		)
		health = _check_stateful_set(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.pod_count, 1)

	def test_check_stateful_set_flags_partition_revision_mismatch(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="mariadb"),
			spec=SimpleNamespace(replicas=1),
			status=SimpleNamespace(
				ready_replicas=1,
				current_revision="rev-1",
				update_revision="rev-2",
			),
		)
		health = _check_stateful_set(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "rollout in progress")

	# -----------------------------------------------------------------------
	# DaemonSet
	# -----------------------------------------------------------------------

	def test_check_daemon_set_flags_misscheduled_pods(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="node-exporter"),
			status=SimpleNamespace(
				desired_number_scheduled=3,
				number_ready=3,
				number_misscheduled=1,
			),
		)
		health = _check_daemon_set(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "misscheduled")

	def test_check_daemon_set_ready_when_all_nodes_ready(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="node-exporter"),
			status=SimpleNamespace(
				desired_number_scheduled=3,
				number_ready=3,
				number_misscheduled=0,
			),
		)
		health = _check_daemon_set(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.pod_count, 3)

	# -----------------------------------------------------------------------
	# Pod
	# -----------------------------------------------------------------------

	def test_check_pod_ready_when_running_and_ready_condition_true(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="redis-0"),
			status=SimpleNamespace(
				phase="Running",
				conditions=[_condition("Ready", "True")],
				container_statuses=[],
				init_container_statuses=[],
			),
		)
		health = _check_pod(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.pod_count, 1)

	def test_check_pod_surfaces_image_pull_back_off_reason(self):
		container_status = SimpleNamespace(
			state=SimpleNamespace(
				waiting=SimpleNamespace(
					reason="ImagePullBackOff",
					message="Back-off pulling image",
				),
				terminated=None,
			),
		)
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="redis-0"),
			status=SimpleNamespace(
				phase="Pending",
				conditions=[],
				container_statuses=[container_status],
				init_container_statuses=[],
			),
		)
		health = _check_pod(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "ImagePullBackOff")
		self.assertIn("Back-off pulling image", health.message)

	def test_check_pod_treats_succeeded_phase_as_ready(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="install-job-pod"),
			status=SimpleNamespace(
				phase="Succeeded",
				conditions=[],
				container_statuses=[],
				init_container_statuses=[],
			),
		)
		health = _check_pod(obj, "tfg")
		self.assertTrue(health.ready)

	# -----------------------------------------------------------------------
	# Job
	# -----------------------------------------------------------------------

	def test_check_job_ready_when_complete_condition_true(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="install-app"),
			status=SimpleNamespace(
				conditions=[_condition("Complete", "True")],
				active=0,
				succeeded=1,
				failed=0,
			),
		)
		health = _check_job(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.to_dict()["pod_count"], 0)

	def test_check_job_failed_surfaces_failure_reason(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="install-app"),
			status=SimpleNamespace(
				conditions=[
					_condition("Failed", "True", reason="BackoffLimitExceeded", message="too many retries")
				],
				active=0,
				succeeded=0,
				failed=4,
			),
		)
		health = _check_job(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "BackoffLimitExceeded")

	def test_check_job_in_progress_marks_unready_with_progress_summary(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="install-app"),
			status=SimpleNamespace(
				conditions=[],
				active=1,
				succeeded=0,
				failed=0,
			),
		)
		health = _check_job(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "in progress")

	# -----------------------------------------------------------------------
	# PVC / Service / Ingress
	# -----------------------------------------------------------------------

	def test_check_pvc_ready_when_bound(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="sites"),
			status=SimpleNamespace(phase="Bound", conditions=[]),
		)
		health = _check_pvc(obj, "tfg")
		self.assertTrue(health.ready)

	def test_check_pvc_pending_is_unready(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="sites"),
			status=SimpleNamespace(phase="Pending", conditions=[]),
		)
		health = _check_pvc(obj, "tfg")
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "Pending")

	def test_check_service_requires_ready_endpoints_for_selector_service(self):
		core_v1 = SimpleNamespace(
			read_namespaced_endpoints=lambda name, namespace: SimpleNamespace(subsets=[]),
		)
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe"),
			spec=SimpleNamespace(type="ClusterIP", selector={"app": "frappe"}),
			status=SimpleNamespace(load_balancer=None),
		)
		health = _check_service(obj, "tfg", core_v1)
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "no ready endpoints")

	def test_check_service_load_balancer_requires_ingress(self):
		core_v1 = SimpleNamespace(
			read_namespaced_endpoints=lambda name, namespace: SimpleNamespace(
				subsets=[SimpleNamespace(addresses=[SimpleNamespace(ip="10.0.0.10")])]
			),
		)
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe"),
			spec=SimpleNamespace(type="LoadBalancer", selector={"app": "frappe"}),
			status=SimpleNamespace(load_balancer=SimpleNamespace(ingress=[])),
		)
		health = _check_service(obj, "tfg", core_v1)
		self.assertFalse(health.ready)
		self.assertEqual(health.reason, "load balancer pending")

	def test_check_ingress_ready_when_load_balancer_has_address(self):
		obj = SimpleNamespace(
			metadata=SimpleNamespace(name="frappe"),
			spec=SimpleNamespace(rules=[SimpleNamespace(host="erp.example.com")]),
			status=SimpleNamespace(
				load_balancer=SimpleNamespace(ingress=[SimpleNamespace(hostname="frappe.example.com")])
			),
		)
		health = _check_ingress(obj, "tfg")
		self.assertTrue(health.ready)
		self.assertEqual(health.addresses, ["frappe.example.com"])
		self.assertEqual(health.hosts, ["erp.example.com"])

	def test_attach_warning_events_appends_recent_warning_summary(self):
		core_v1 = SimpleNamespace(
			list_namespaced_event=lambda **kwargs: SimpleNamespace(
				items=[
					SimpleNamespace(
						type="Warning",
						reason="FailedScheduling",
						message="0/1 nodes available",
						last_timestamp="2026-04-12T10:00:00Z",
					),
				]
			),
		)
		health = ResourceHealth("Pod", "worker-1", "tfg", False, "Pending", "phase=Pending")

		with_events = _attach_warning_events(health, core_v1)

		self.assertIn("Events:", with_events.message)
		self.assertIn("FailedScheduling", with_events.message)

	# -----------------------------------------------------------------------
	# summarize
	# -----------------------------------------------------------------------

	def test_summarize_returns_friendly_message_when_no_resources(self):
		all_ready, summary = summarize([])
		self.assertTrue(all_ready)
		self.assertIn("no workload resources", summary)

	def test_summarize_reports_count_when_all_ready(self):
		results = [
			ResourceHealth("Deployment", "frappe-prod", "tfg", True, "", ""),
			ResourceHealth("StatefulSet", "mariadb", "tfg", True, "", ""),
		]
		all_ready, summary = summarize(results)
		self.assertTrue(all_ready)
		self.assertEqual(summary, "deployed | 2/2 ready")

	def test_summarize_includes_first_failing_resource_line(self):
		results = [
			ResourceHealth("Deployment", "frappe-prod", "tfg", True, "", "1/1 available"),
			ResourceHealth("Pod", "redis-0", "tfg", False, "ImagePullBackOff", "Back-off"),
			ResourceHealth("Job", "install-app", "tfg", False, "BackoffLimitExceeded", ""),
		]
		all_ready, summary = summarize(results)
		self.assertFalse(all_ready)
		self.assertIn("1/3 ready", summary)
		self.assertIn("Pod/redis-0", summary)
		self.assertIn("ImagePullBackOff", summary)

	# -----------------------------------------------------------------------
	# classify_release_state
	# -----------------------------------------------------------------------

	def test_classify_release_state_marks_deployed_when_helm_and_walker_agree(self):
		results = [
			ResourceHealth("Deployment", "frappe-prod", "tfg", True, "", "1/1 available"),
			ResourceHealth("StatefulSet", "frappe-prod-mariadb", "tfg", True, "", "1/1 ready"),
		]
		status, detail = classify_release_state("deployed", results)
		self.assertEqual(status, "Deployed")
		self.assertEqual(detail, "deployed | 2/2 ready")

	def test_classify_release_state_marks_degraded_when_workload_is_unready(self):
		results = [
			ResourceHealth("Deployment", "frappe-prod", "tfg", False, "ImagePullBackOff", ""),
			ResourceHealth("StatefulSet", "frappe-prod-mariadb", "tfg", True, "", "1/1 ready"),
		]
		status, detail = classify_release_state("deployed", results)
		self.assertEqual(status, "Degraded")
		self.assertIn("1/2 ready", detail)
		self.assertIn("ImagePullBackOff", detail)

	def test_classify_release_state_keeps_degraded_when_walker_errored(self):
		status, detail = classify_release_state(
			"deployed",
			walker_results=None,
			walker_error="kubernetes API timeout",
		)
		self.assertEqual(status, "Degraded")
		self.assertIn("readiness probe failed", detail)
		self.assertIn("kubernetes API timeout", detail)

	def test_classify_release_state_marks_pending_helm_states_as_failed(self):
		for runtime in ("pending-install", "pending-upgrade", "pending-rollback", "uninstalling"):
			with self.subTest(runtime=runtime):
				status, detail = classify_release_state(runtime, walker_results=[])
				self.assertEqual(status, "Failed")
				self.assertIn("stuck", detail)
				self.assertIn(runtime, detail)

	def test_classify_release_state_marks_unknown_helm_state_as_failed(self):
		status, detail = classify_release_state("failed", walker_results=[])
		self.assertEqual(status, "Failed")
		self.assertIn("failed", detail)

	# -----------------------------------------------------------------------
	# walk
	# -----------------------------------------------------------------------

	@patch("kubeport.utils.release_health.client.BatchV1Api")
	@patch("kubeport.utils.release_health.client.CoreV1Api")
	@patch("kubeport.utils.release_health.client.AppsV1Api")
	@patch("kubeport.utils.release_health.get_k8s_api_client")
	@patch("kubeport.utils.release_health.helm.get_manifest")
	@patch("kubeport.utils.release_health.frappe.db.get_value")
	def test_walk_reports_ready_unready_and_missing_resources(
		self,
		mock_get_value,
		mock_get_manifest,
		_mock_get_api_client,
		mock_apps_api,
		mock_core_api,
		_mock_batch_api,
	):
		mock_get_value.return_value = {
			"release_name": "bench-a",
			"namespace": "tfg",
			"cluster": "cluster-a",
		}
		mock_get_manifest.return_value = [
			{"kind": "Deployment", "metadata": {"name": "bench-a-web"}},
			{"kind": "Pod", "metadata": {"name": "bench-a-worker"}},
			{"kind": "Service", "metadata": {"name": "bench-a"}},
		]
		mock_apps_api.return_value.read_namespaced_deployment.return_value = SimpleNamespace(
			metadata=SimpleNamespace(name="bench-a-web"),
			spec=SimpleNamespace(replicas=1),
			status=SimpleNamespace(
				available_replicas=1,
				updated_replicas=1,
				conditions=[],
			),
		)
		mock_core_api.return_value.read_namespaced_pod.side_effect = ApiException(
			status=404, reason="Not Found"
		)

		results = walk("cluster-a/tfg/bench-a")

		self.assertEqual(len(results), 3)
		self.assertTrue(results[0].ready)
		self.assertFalse(results[1].ready)
		self.assertEqual(results[1].reason, "missing")
		self.assertFalse(results[2].ready)
		self.assertEqual(results[2].kind, "Service")

	@patch("kubeport.utils.release_health.client.BatchV1Api")
	@patch("kubeport.utils.release_health.client.CoreV1Api")
	@patch("kubeport.utils.release_health.client.AppsV1Api")
	@patch("kubeport.utils.release_health.get_k8s_api_client")
	@patch("kubeport.utils.release_health.helm.get_manifest")
	@patch("kubeport.utils.release_health.frappe.db.get_value")
	def test_walk_turns_resource_api_errors_into_unready_rows(
		self,
		mock_get_value,
		mock_get_manifest,
		_mock_get_api_client,
		_mock_apps_api,
		mock_core_api,
		_mock_batch_api,
	):
		mock_get_value.return_value = {
			"release_name": "bench-a",
			"namespace": "tfg",
			"cluster": "cluster-a",
		}
		mock_get_manifest.return_value = [
			{"kind": "Pod", "metadata": {"name": "bench-a-worker"}},
		]
		mock_core_api.return_value.read_namespaced_pod.side_effect = ApiException(
			status=500,
			reason="Internal Server Error",
		)

		results = walk("cluster-a/tfg/bench-a")

		self.assertEqual(len(results), 1)
		self.assertFalse(results[0].ready)
		self.assertEqual(results[0].reason, "api-error-500")
