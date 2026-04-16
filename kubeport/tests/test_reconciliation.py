# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import call, patch

from frappe.tests import UnitTestCase

from kubeport.tasks.reconciliation import (
	_reconcile_helm_releases,
	_reconcile_service_bundles,
	reconcile_all_releases,
)


class UnitTestReconciliation(UnitTestCase):
	@patch("kubeport.tasks.reconciliation._reconcile_service_bundles")
	@patch("kubeport.tasks.reconciliation._reconcile_helm_releases")
	def test_reconcile_all_releases_only_runs_active_sweeps(
		self,
		mock_reconcile_helm_releases,
		mock_reconcile_service_bundles,
	):
		reconcile_all_releases()

		mock_reconcile_helm_releases.assert_called_once_with()
		mock_reconcile_service_bundles.assert_called_once_with()

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
