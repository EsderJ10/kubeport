# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.tasks.helm_tasks import install_or_upgrade_release


class UnitTestHelmTasks(UnitTestCase):
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
		mock_get_value.return_value = SimpleNamespace(
			release_name="bench-a",
			chart="ERPNext",
			chart_version="8.0.41",
			namespace="tfg",
			cluster="cluster-a",
			values="jobs:\n  createSite:\n    enabled: true\n",
		)
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
			["release_name", "chart", "chart_version", "namespace", "cluster", "values"],
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
