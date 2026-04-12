# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.tasks.reconciliation import reconcile_all_releases


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
