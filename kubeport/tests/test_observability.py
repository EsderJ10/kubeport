# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase
from kubernetes.client.rest import ApiException

from kubeport.utils.observability import (
	get_pod_logs,
	get_rollout_history,
	list_pods_for_resource,
	list_resource_events,
)


def _owner(kind: str, name: str):
	return SimpleNamespace(kind=kind, name=name)


def _metadata(name: str, owner_references=None, annotations=None, creation_timestamp=None):
	return SimpleNamespace(
		name=name,
		owner_references=owner_references or [],
		annotations=annotations or {},
		creation_timestamp=creation_timestamp,
	)


def _selector(labels: dict[str, str]):
	return SimpleNamespace(match_labels=labels, match_expressions=[])


def _pod(name: str, owner_references=None):
	return SimpleNamespace(
		metadata=_metadata(name, owner_references=owner_references),
		status=SimpleNamespace(phase="Running", container_statuses=[], init_container_statuses=[]),
		spec=SimpleNamespace(containers=[SimpleNamespace(name="web")]),
	)


class UnitTestObservability(UnitTestCase):
	@patch("kubeport.utils.observability.client.AppsV1Api")
	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_pods_for_deployment_resolves_replica_set_owned_pods(
		self,
		_mock_api_client,
		mock_core_api,
		mock_apps_api,
	):
		apps_v1 = mock_apps_api.return_value
		core_v1 = mock_core_api.return_value
		apps_v1.read_namespaced_deployment.return_value = SimpleNamespace(
			spec=SimpleNamespace(selector=_selector({"app": "bench"})),
		)
		apps_v1.list_namespaced_replica_set.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=_metadata("bench-web-abc", owner_references=[_owner("Deployment", "bench-web")])
				),
				SimpleNamespace(
					metadata=_metadata("other-abc", owner_references=[_owner("Deployment", "other")])
				),
			]
		)
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(
			items=[
				_pod("bench-web-abc-1", owner_references=[_owner("ReplicaSet", "bench-web-abc")]),
				_pod("other-abc-1", owner_references=[_owner("ReplicaSet", "other-abc")]),
			]
		)

		pods = list_pods_for_resource("cluster-a", "erp", "Deployment", "bench-web")

		self.assertEqual([pod["name"] for pod in pods], ["bench-web-abc-1"])
		self.assertEqual(core_v1.list_namespaced_pod.call_args.kwargs["label_selector"], "app=bench")

	@patch("kubeport.utils.observability.client.AppsV1Api")
	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_pods_for_deployment_does_not_fallback_to_selector_only_pods(
		self,
		_mock_api_client,
		mock_core_api,
		mock_apps_api,
	):
		apps_v1 = mock_apps_api.return_value
		core_v1 = mock_core_api.return_value
		apps_v1.read_namespaced_deployment.return_value = SimpleNamespace(
			spec=SimpleNamespace(selector=_selector({"app": "bench"})),
		)
		apps_v1.list_namespaced_replica_set.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=_metadata("other-abc", owner_references=[_owner("Deployment", "other")])
				),
			]
		)
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(
			items=[
				_pod("selector-match-1", owner_references=[_owner("ReplicaSet", "other-abc")]),
				_pod("selector-match-2"),
			]
		)

		pods = list_pods_for_resource("cluster-a", "erp", "Deployment", "bench-web")

		self.assertEqual(pods, [])

	@patch("kubeport.utils.observability.client.AppsV1Api")
	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_pods_for_controller_does_not_fallback_to_selector_only_pods(
		self,
		_mock_api_client,
		mock_core_api,
		mock_apps_api,
	):
		apps_v1 = mock_apps_api.return_value
		core_v1 = mock_core_api.return_value
		apps_v1.read_namespaced_stateful_set.return_value = SimpleNamespace(
			spec=SimpleNamespace(selector=_selector({"app": "bench"})),
		)
		core_v1.list_namespaced_pod.return_value = SimpleNamespace(
			items=[
				_pod("selector-match-1", owner_references=[_owner("StatefulSet", "other")]),
				_pod("selector-match-2"),
			]
		)

		pods = list_pods_for_resource("cluster-a", "erp", "StatefulSet", "bench-worker")

		self.assertEqual(pods, [])

	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_pods_for_missing_standalone_pod_returns_empty(self, _mock_api_client, mock_core_api):
		mock_core_api.return_value.read_namespaced_pod.side_effect = ApiException(
			status=404, reason="Not Found"
		)

		pods = list_pods_for_resource("cluster-a", "erp", "Pod", "gone")

		self.assertEqual(pods, [])

	@patch("kubeport.utils.observability.LOG_TEXT_LIMIT_BYTES", 12)
	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_get_pod_logs_caps_tail_lines_and_response_text(self, _mock_api_client, mock_core_api):
		core_v1 = mock_core_api.return_value
		core_v1.read_namespaced_pod_log.return_value = "0123456789abcdefghijklmnopqrstuvwxyz"

		logs = get_pod_logs("cluster-a", "erp", "bench-web-1", tail_lines=9999)

		self.assertIn("[truncated to 12 bytes]", logs)
		self.assertEqual(core_v1.read_namespaced_pod_log.call_args.kwargs["tail_lines"], 2000)

	def test_get_pod_logs_coerces_previous_string_false(self):
		with (
			patch("kubeport.utils.observability.get_k8s_api_client", return_value=object()),
			patch("kubeport.utils.observability.client.CoreV1Api") as mock_core_api,
		):
			core_v1 = mock_core_api.return_value
			core_v1.read_namespaced_pod_log.return_value = "logs"

			get_pod_logs("cluster-a", "erp", "bench-web-1", previous="0")

		self.assertFalse(core_v1.read_namespaced_pod_log.call_args.kwargs["previous"])

	@patch("kubeport.utils.observability.get_k8s_api_client", side_effect=RuntimeError("auth failed"))
	def test_get_pod_logs_wraps_client_initialization_failure(self, _mock_api_client):
		with self.assertRaisesRegex(RuntimeError, "Could not initialize Kubernetes client"):
			get_pod_logs("cluster-a", "erp", "bench-web-1")

	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_get_pod_logs_raises_runtime_error_on_cluster_api_failure(self, _mock_api_client, mock_core_api):
		mock_core_api.return_value.read_namespaced_pod_log.side_effect = ApiException(
			status=500,
			reason="Internal Server Error",
		)

		with self.assertRaisesRegex(RuntimeError, "500"):
			get_pod_logs("cluster-a", "erp", "bench-web-1")

	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_get_pod_logs_surfaces_bad_request_errors(self, _mock_api_client, mock_core_api):
		mock_core_api.return_value.read_namespaced_pod_log.side_effect = ApiException(
			status=400,
			reason="Bad Request",
		)

		with self.assertRaisesRegex(RuntimeError, "400 Bad Request"):
			get_pod_logs("cluster-a", "erp", "bench-web-1", previous=True)

	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.client.EventsV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_resource_events_returns_empty_when_apis_have_no_events(
		self,
		_mock_api_client,
		mock_events_api,
		mock_core_api,
	):
		mock_events_api.return_value.list_namespaced_event.return_value = SimpleNamespace(items=[])
		mock_core_api.return_value.list_namespaced_event.return_value = SimpleNamespace(items=[])

		events = list_resource_events("cluster-a", "erp", "Deployment", "bench-web")

		self.assertEqual(events, [])

	@patch("kubeport.utils.observability.client.CoreV1Api")
	@patch("kubeport.utils.observability.client.EventsV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_list_resource_events_caps_limit(
		self,
		_mock_api_client,
		mock_events_api,
		_mock_core_api,
	):
		mock_events_api.return_value.list_namespaced_event.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					type="Warning",
					reason=f"Reason-{i}",
					note="message",
					deprecated_count=1,
					deprecated_first_timestamp=f"2026-05-05T00:{i:02d}:00Z",
					event_time=f"2026-05-05T00:{i:02d}:00Z",
					deprecated_last_timestamp=None,
					reporting_controller="scheduler",
				)
				for i in range(60)
			]
		)

		events = list_resource_events("cluster-a", "erp", "Deployment", "bench-web", limit=999)

		self.assertEqual(len(events), 50)
		self.assertEqual(events[0]["reason"], "Reason-59")

	@patch("kubeport.utils.observability.client.AppsV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_get_rollout_history_caps_rows(self, _mock_api_client, mock_apps_api):
		apps_v1 = mock_apps_api.return_value
		apps_v1.read_namespaced_deployment.return_value = SimpleNamespace(
			metadata=_metadata("bench-web", annotations={"deployment.kubernetes.io/revision": "30"}),
			spec=SimpleNamespace(selector=_selector({"app": "bench"})),
		)
		apps_v1.list_namespaced_replica_set.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=_metadata(
						f"bench-web-{i}",
						owner_references=[_owner("Deployment", "bench-web")],
						annotations={"deployment.kubernetes.io/revision": str(i)},
						creation_timestamp=f"2026-05-05T00:{i:02d}:00Z",
					),
					spec=SimpleNamespace(
						template=SimpleNamespace(
							spec=SimpleNamespace(containers=[SimpleNamespace(image=f"image:{i}")])
						)
					),
				)
				for i in range(30)
			]
		)

		rows = get_rollout_history("cluster-a", "erp", "Deployment", "bench-web", limit=99)

		self.assertEqual(len(rows), 20)
		self.assertEqual(rows[0]["revision"], "29")

	@patch("kubeport.utils.observability.client.AppsV1Api")
	@patch("kubeport.utils.observability.get_k8s_api_client", return_value=object())
	def test_controller_revision_rows_distinguish_current_and_update(self, _mock_api_client, mock_apps_api):
		apps_v1 = mock_apps_api.return_value
		apps_v1.read_namespaced_stateful_set.return_value = SimpleNamespace(
			spec=SimpleNamespace(selector=_selector({"app": "bench"})),
			status=SimpleNamespace(current_revision="bench-worker-1", update_revision="bench-worker-2"),
		)
		apps_v1.list_namespaced_controller_revision.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=_metadata(
						"bench-worker-1", owner_references=[_owner("StatefulSet", "bench-worker")]
					),
					revision=1,
				),
				SimpleNamespace(
					metadata=_metadata(
						"bench-worker-2", owner_references=[_owner("StatefulSet", "bench-worker")]
					),
					revision=2,
				),
			]
		)

		rows = get_rollout_history("cluster-a", "erp", "StatefulSet", "bench-worker")

		by_name = {row["name"]: row for row in rows}
		self.assertTrue(by_name["bench-worker-1"]["current"])
		self.assertFalse(by_name["bench-worker-1"]["update"])
		self.assertFalse(by_name["bench-worker-2"]["current"])
		self.assertTrue(by_name["bench-worker-2"]["update"])
