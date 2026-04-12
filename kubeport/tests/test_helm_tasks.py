# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.helm_tasks import install_or_upgrade_release, sync_repo_charts


class UnitTestHelmTasks(UnitTestCase):
	@patch("kubeport.tasks.helm_tasks.frappe.logger")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_sync_repo_charts_skips_stale_repo_worker_execution(
		self,
		mock_get_value,
		mock_get_doc,
		mock_logger,
	):
		mock_get_value.return_value = "active-token"

		sync_repo_charts("repo-a", "stale-token")

		mock_get_doc.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.utils.now", return_value="2026-04-12 10:00:00")
	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._sync_charts")
	@patch("kubeport.tasks.helm_tasks.helm.repo_add")
	@patch("kubeport.tasks.helm_tasks.helm.repo_update")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_sync_repo_charts_updates_repo_only_when_worker_token_is_current(
		self,
		mock_get_value,
		mock_get_doc,
		mock_repo_update,
		mock_repo_add,
		mock_sync_charts,
		mock_publish_realtime,
		_mock_now,
	):
		mock_get_value.side_effect = ["sync-token", "sync-token"]
		doc = MagicMock()
		doc.repo_name = "bitnami"
		doc.repo_url = "https://charts.bitnami.com/bitnami"
		doc.repo_username = "user-a"
		doc.repo_password = "secret"
		doc.get_password.return_value = "secret-value"
		mock_get_doc.return_value = doc

		sync_repo_charts("repo-a", "sync-token")

		mock_get_doc.assert_called_once_with("Helm Repository", "repo-a")
		mock_repo_add.assert_called_once_with(
			"bitnami",
			"https://charts.bitnami.com/bitnami",
			username="user-a",
			password="secret-value",
		)
		mock_repo_update.assert_called_once_with("bitnami")
		mock_sync_charts.assert_called_once_with(doc)
		doc.db_set.assert_any_call("status", "Synced")
		doc.db_set.assert_any_call("last_synced", "2026-04-12 10:00:00")
		mock_publish_realtime.assert_called_once_with(
			"helm_repo_sync_update",
			{"repo_name": "repo-a", "status": "Synced"},
			doctype="Helm Repository",
			docname="repo-a",
		)

	@patch("kubeport.tasks.helm_tasks.frappe.logger")
	@patch("kubeport.tasks.helm_tasks.frappe.log_error")
	@patch("kubeport.tasks.helm_tasks.frappe.db.rollback")
	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks.helm.repo_add")
	@patch("kubeport.tasks.helm_tasks.helm.repo_update")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_sync_repo_charts_does_not_mark_error_when_worker_becomes_stale_after_failure(
		self,
		mock_get_value,
		mock_get_doc,
		mock_repo_update,
		_mock_repo_add,
		mock_publish_realtime,
		mock_rollback,
		mock_log_error,
		mock_logger,
	):
		mock_get_value.side_effect = ["sync-token", "newer-token"]
		doc = MagicMock()
		doc.repo_name = "bitnami"
		doc.repo_url = "https://charts.bitnami.com/bitnami"
		doc.repo_username = None
		doc.repo_password = None
		mock_get_doc.return_value = doc
		mock_repo_update.side_effect = RuntimeError("helm repo update failed")

		sync_repo_charts("repo-a", "sync-token")

		mock_rollback.assert_called_once_with()
		doc.db_set.assert_not_called()
		mock_publish_realtime.assert_not_called()
		mock_log_error.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.utils.now", return_value="2026-04-12 10:00:00")
	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._sync_charts")
	@patch("kubeport.tasks.helm_tasks.helm.repo_add")
	@patch("kubeport.tasks.helm_tasks.helm.repo_update")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_sync_repo_charts_re_registers_repo_before_update(
		self,
		mock_get_value,
		mock_get_doc,
		mock_repo_update,
		mock_repo_add,
		mock_sync_charts,
		mock_publish_realtime,
		_mock_now,
	):
		mock_get_value.side_effect = ["sync-token", "sync-token"]
		call_order = []
		doc = MagicMock()
		doc.repo_name = "bitnami"
		doc.repo_url = "https://charts.bitnami.com/bitnami"
		doc.repo_username = None
		doc.repo_password = None
		mock_get_doc.return_value = doc
		mock_repo_add.side_effect = lambda *args, **kwargs: call_order.append("repo_add")
		mock_repo_update.side_effect = lambda *args, **kwargs: call_order.append("repo_update")

		sync_repo_charts("repo-a", "sync-token")

		mock_repo_add.assert_called_once_with(
			"bitnami",
			"https://charts.bitnami.com/bitnami",
			username=None,
			password=None,
		)
		mock_repo_update.assert_called_once_with("bitnami")
		self.assertEqual(call_order, ["repo_add", "repo_update"])
		mock_sync_charts.assert_called_once_with(doc)
		mock_publish_realtime.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_reads_release_without_loading_document(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.return_value = {
			"status": "In Progress",
			"release_name": "bench-a",
			"chart": "ERPNext",
			"chart_version": "8.0.41",
			"namespace": "tfg",
			"cluster": "cluster-a",
			"values": "jobs:\n  createSite:\n    enabled: true\n",
		}
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)
		mock_install_or_upgrade.return_value = {
			"version": 3,
			"info": {"status": "deployed"},
		}

		install_or_upgrade_release("bench-a")

		mock_get_value.assert_called_once_with(
			"Helm Release",
			"bench-a",
			["status", "release_name", "chart", "chart_version", "namespace", "cluster", "values"],
			as_dict=True,
		)
		mock_get_doc.assert_called_once_with("Helm Chart", "ERPNext")
		mock_install_or_upgrade.assert_called_once_with(
			release_name="bench-a",
			chart_ref="repo/erpnext",
			namespace="tfg",
			cluster_name="cluster-a",
			values_yaml="jobs:\n  createSite:\n    enabled: true\n",
			chart_version="8.0.41",
		)
		mock_set_helm_release_fields.assert_called_once_with("bench-a", {
			"status": "Deployed",
			"helm_revision": 3,
			"helm_status_detail": "deployed",
		})
		mock_publish_realtime.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_marks_non_deployed_runtime_state_as_degraded(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.return_value = {
			"status": "In Progress",
			"release_name": "bench-a",
			"chart": "ERPNext",
			"chart_version": "8.0.41",
			"namespace": "tfg",
			"cluster": "cluster-a",
			"values": "",
		}
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)
		mock_install_or_upgrade.return_value = {
			"version": 3,
			"info": {"status": "failed"},
		}

		install_or_upgrade_release("bench-a")

		mock_set_helm_release_fields.assert_called_once_with("bench-a", {
			"status": "Degraded",
			"helm_revision": 3,
			"helm_status_detail": "failed",
		})
		mock_publish_realtime.assert_called_once_with(
			"helm_release_status_update",
			{"release_name": "bench-a", "status": "Degraded"},
			doctype="Helm Release",
			docname="bench-a",
		)

	@patch("kubeport.tasks.helm_tasks.frappe.logger")
	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_skips_stale_worker_execution(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		mock_set_helm_release_fields,
		mock_publish_realtime,
		mock_logger,
	):
		mock_get_value.return_value = {
			"status": "Draft",
			"release_name": "bench-a",
			"chart": "ERPNext",
			"chart_version": "8.0.41",
			"namespace": "tfg",
			"cluster": "cluster-a",
			"values": "",
		}

		install_or_upgrade_release("bench-a")

		mock_get_doc.assert_not_called()
		mock_install_or_upgrade.assert_not_called()
		mock_set_helm_release_fields.assert_not_called()
		mock_publish_realtime.assert_not_called()
		mock_logger.return_value.info.assert_called_once()
