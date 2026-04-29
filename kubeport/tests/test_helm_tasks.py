# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.helm_tasks import (
	_group_chart_inventory,
	_sync_charts,
	install_or_upgrade_release,
	sync_repo_charts,
	sync_all_repos,
	uninstall_release,
)


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
	@patch("kubeport.tasks.helm_tasks._safe_walk", return_value=([], None))
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_reads_release_without_loading_document(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		_mock_safe_walk,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			# First call: token + status guard.
			{"operation_token": "tok-1", "status": "In Progress"},
			# Second call: release fields.
			{
				"release_name": "bench-a",
				"chart": "ERPNext",
				"chart_version": "8.0.41",
				"namespace": "tfg",
				"cluster": "cluster-a",
				"values": "jobs:\n  createSite:\n    enabled: true\n",
			},
			# Third call: token re-check before writeback.
			{"operation_token": "tok-1", "status": "In Progress"},
		]
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)
		mock_install_or_upgrade.return_value = {
			"version": 3,
			"info": {"status": "deployed"},
		}

		install_or_upgrade_release("bench-a", "tok-1")

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
			"helm_status_detail": "deployed (no workload resources)",
		})
		mock_publish_realtime.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks._safe_walk", return_value=([], None))
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_marks_non_deployed_runtime_state_as_failed(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		_mock_safe_walk,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			{"operation_token": "tok-1", "status": "In Progress"},
			{
				"release_name": "bench-a",
				"chart": "ERPNext",
				"chart_version": "8.0.41",
				"namespace": "tfg",
				"cluster": "cluster-a",
				"values": "",
			},
			{"operation_token": "tok-1", "status": "In Progress"},
		]
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)
		mock_install_or_upgrade.return_value = {
			"version": 3,
			"info": {"status": "failed"},
		}

		install_or_upgrade_release("bench-a", "tok-1")

		mock_set_helm_release_fields.assert_called_once_with("bench-a", {
			"status": "Failed",
			"helm_revision": 3,
			"helm_status_detail": "Helm reports: failed",
		})
		mock_publish_realtime.assert_called_once_with(
			"helm_release_status_update",
			{
				"release_docname": "bench-a",
				"release_name": "bench-a",
				"status": "Failed",
			},
			doctype="Helm Release",
			docname="bench-a",
		)

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks._safe_walk", return_value=([], None))
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_marks_pending_helm_state_as_failed_with_recovery_hint(
		self,
		mock_get_value,
		mock_get_doc,
		mock_install_or_upgrade,
		_mock_safe_walk,
		mock_set_helm_release_fields,
		_mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			{"operation_token": "tok-1", "status": "In Progress"},
			{
				"release_name": "bench-a",
				"chart": "ERPNext",
				"chart_version": "8.0.41",
				"namespace": "tfg",
				"cluster": "cluster-a",
				"values": "",
			},
			{"operation_token": "tok-1", "status": "In Progress"},
		]
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)
		mock_install_or_upgrade.return_value = {
			"version": 3,
			"info": {"status": "pending-upgrade"},
		}

		install_or_upgrade_release("bench-a", "tok-1")

		mock_set_helm_release_fields.assert_called_once()
		_, fields = mock_set_helm_release_fields.call_args.args
		self.assertEqual(fields["status"], "Failed")
		self.assertIn("stuck", fields["helm_status_detail"])
		self.assertIn("pending-upgrade", fields["helm_status_detail"])

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
		# Token mismatch: row was rotated by a newer operation before this
		# worker began executing.
		mock_get_value.return_value = {
			"operation_token": "tok-2",
			"status": "In Progress",
		}

		install_or_upgrade_release("bench-a", "tok-1")

		mock_get_doc.assert_not_called()
		mock_install_or_upgrade.assert_not_called()
		mock_set_helm_release_fields.assert_not_called()
		mock_publish_realtime.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks._safe_walk", return_value=([], None))
	@patch("kubeport.tasks.helm_tasks.helm.install_or_upgrade")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_install_or_upgrade_release_drops_writeback_when_token_rotates_mid_helm_call(
		self,
		mock_get_value,
		mock_get_doc,
		_mock_install_or_upgrade,
		_mock_safe_walk,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			# Initial guard passes.
			{"operation_token": "tok-1", "status": "In Progress"},
			# Release fields read.
			{
				"release_name": "bench-a",
				"chart": "ERPNext",
				"chart_version": "8.0.41",
				"namespace": "tfg",
				"cluster": "cluster-a",
				"values": "",
			},
			# Re-check after helm returns: a newer operation has taken over.
			{"operation_token": "tok-2", "status": "In Progress"},
		]
		mock_get_doc.return_value = SimpleNamespace(
			latest_version="8.0.41",
			get_chart_reference=lambda: "repo/erpnext",
		)

		install_or_upgrade_release("bench-a", "tok-1")

		mock_set_helm_release_fields.assert_not_called()
		mock_publish_realtime.assert_not_called()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.uninstall")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_uninstall_release_treats_helm_not_found_as_success(
		self,
		mock_get_value,
		mock_uninstall,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			{"operation_token": "tok-1", "status": "Uninstalling"},
			{
				"release_name": "bench-a",
				"namespace": "tfg",
				"cluster": "cluster-a",
			},
			{"operation_token": "tok-1", "status": "Uninstalling"},
			{"operation_token": "tok-1", "status": "Uninstalling"},
		]
		mock_uninstall.side_effect = RuntimeError("Helm command failed: release: not found")

		uninstall_release("cluster-a/tfg/bench-a", "tok-1")

		mock_set_helm_release_fields.assert_called_once_with("cluster-a/tfg/bench-a", {
			"status": "Draft",
			"helm_revision": 0,
			"helm_status_detail": "",
		})
		mock_publish_realtime.assert_called_once_with(
			"helm_release_status_update",
			{
				"release_docname": "cluster-a/tfg/bench-a",
				"release_name": "bench-a",
				"status": "Draft",
			},
			doctype="Helm Release",
			docname="cluster-a/tfg/bench-a",
		)

	@patch("kubeport.tasks.helm_tasks.frappe.logger")
	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.uninstall")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_uninstall_release_skips_stale_worker_execution(
		self,
		mock_get_value,
		mock_uninstall,
		mock_set_helm_release_fields,
		mock_publish_realtime,
		mock_logger,
	):
		mock_get_value.return_value = {
			"operation_token": "tok-2",
			"status": "Uninstalling",
		}

		uninstall_release("cluster-a/tfg/bench-a", "tok-1")

		mock_uninstall.assert_not_called()
		mock_set_helm_release_fields.assert_not_called()
		mock_publish_realtime.assert_not_called()
		mock_logger.return_value.info.assert_called_once()

	@patch("kubeport.tasks.helm_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.helm_tasks._set_helm_release_fields")
	@patch("kubeport.tasks.helm_tasks.helm.uninstall")
	@patch("kubeport.tasks.helm_tasks.frappe.db.get_value")
	def test_uninstall_release_drops_writeback_when_token_rotates_mid_helm_call(
		self,
		mock_get_value,
		mock_uninstall,
		mock_set_helm_release_fields,
		mock_publish_realtime,
	):
		mock_get_value.side_effect = [
			{"operation_token": "tok-1", "status": "Uninstalling"},
			{
				"release_name": "bench-a",
				"namespace": "tfg",
				"cluster": "cluster-a",
			},
			{"operation_token": "tok-2", "status": "Uninstalling"},
		]

		uninstall_release("cluster-a/tfg/bench-a", "tok-1")

		mock_uninstall.assert_called_once_with(
			release_name="bench-a",
			namespace="tfg",
			cluster_name="cluster-a",
		)
		mock_set_helm_release_fields.assert_not_called()
		mock_publish_realtime.assert_not_called()

	@patch("kubeport.tasks.helm_tasks.frappe.enqueue")
	@patch("kubeport.tasks.helm_tasks.frappe.db.set_value")
	@patch("kubeport.tasks.helm_tasks.frappe.get_all")
	@patch("kubeport.tasks.helm_tasks.secrets.token_hex", side_effect=["token-a", "token-b"])
	def test_sync_all_repos_enqueues_background_sync_jobs(
		self,
		_mock_token_hex,
		mock_get_all,
		mock_set_value,
		mock_enqueue,
	):
		mock_get_all.return_value = ["repo-a", "repo-b"]

		sync_all_repos()

		self.assertEqual(mock_set_value.call_count, 4)
		mock_enqueue.assert_any_call(
			"kubeport.tasks.helm_tasks.sync_repo_charts",
			repo_name="repo-a",
			sync_token="token-a",
			queue="long",
			enqueue_after_commit=True,
		)
		mock_enqueue.assert_any_call(
			"kubeport.tasks.helm_tasks.sync_repo_charts",
			repo_name="repo-b",
			sync_token="token-b",
			queue="long",
			enqueue_after_commit=True,
		)

	def test_group_chart_inventory_sorts_versions_and_filters_duplicates(self):
		grouped = _group_chart_inventory(
			[
				{
					"name": "bitnami/nginx",
					"version": "18.2.4",
					"app_version": "1.2.0",
					"description": "newest",
				},
				{
					"name": "bitnami/nginx",
					"version": "18.1.0",
					"app_version": "1.1.0",
					"description": "older",
				},
				{
					"name": "bitnami/nginx",
					"version": "18.2.4",
					"app_version": "1.2.0",
					"description": "duplicate",
				},
				{
					"name": "bitnami/redis",
					"version": "3.0.0",
					"app_version": "7.0.0",
					"description": "filtered",
				},
			],
			["nginx"],
		)

		self.assertEqual(list(grouped), ["nginx"])
		self.assertEqual(
			[row["version"] for row in grouped["nginx"]],
			["18.2.4", "18.1.0"],
		)

	@patch("kubeport.tasks.helm_tasks.frappe.delete_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.get_all")
	@patch("kubeport.tasks.helm_tasks.frappe.get_doc")
	@patch("kubeport.tasks.helm_tasks.frappe.db.exists")
	@patch("kubeport.tasks.helm_tasks.helm.search_repo_with_options")
	def test_sync_charts_rebuilds_versions_and_removes_stale_charts(
		self,
		mock_search_repo,
		mock_exists,
		mock_get_doc,
		mock_get_all,
		mock_delete_doc,
	):
		repo_doc = SimpleNamespace(
			name="bitnami",
			repo_name="bitnami",
			include_patterns="",
		)
		chart_doc = MagicMock()
		chart_doc.latest_version = "18.1.0"
		mock_search_repo.return_value = [
			{
				"name": "bitnami/nginx",
				"version": "18.2.4",
				"app_version": "1.2.0",
				"description": "newest",
			},
			{
				"name": "bitnami/nginx",
				"version": "18.1.0",
				"app_version": "1.1.0",
				"description": "older",
			},
		]
		mock_exists.side_effect = lambda doctype, name: name == "bitnami/nginx"
		mock_get_doc.return_value = chart_doc
		mock_get_all.return_value = ["bitnami/nginx", "bitnami/redis"]

		_sync_charts(repo_doc)

		mock_search_repo.assert_called_once_with(
			"bitnami",
			all_versions=True,
			timeout=600,
		)
		chart_doc.set.assert_called_once_with("versions", [
			{"version": "18.2.4", "app_version": "1.2.0", "description": "newest"},
			{"version": "18.1.0", "app_version": "1.1.0", "description": "older"},
		])
		self.assertEqual(chart_doc.latest_version, "18.2.4")
		self.assertEqual(chart_doc.latest_app_version, "1.2.0")
		self.assertEqual(chart_doc.description, "newest")
		self.assertEqual(chart_doc.default_values, "")
		chart_doc.save.assert_called_once_with(ignore_permissions=True)
		mock_delete_doc.assert_called_once_with(
			"Helm Chart",
			"bitnami/redis",
			ignore_permissions=True,
		)
