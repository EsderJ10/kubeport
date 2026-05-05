# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from frappe.tests import UnitTestCase

from kubeport import hooks


class UnitTestHooks(UnitTestCase):
	def test_scheduler_events_use_direct_module_paths(self):
		self.assertEqual(
			hooks.scheduler_events["cron"]["*/5 * * * *"],
			[
				"kubeport.tasks.reconciliation.reconcile_all_releases",
				"kubeport.tasks.reconciliation.reconcile_site_backups",
			],
		)
		self.assertEqual(
			hooks.scheduler_events["daily"],
			["kubeport.tasks.helm_tasks.sync_all_repos"],
		)
