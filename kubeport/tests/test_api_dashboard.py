# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from datetime import datetime, timezone
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.api.dashboard import count_stale_operations, stale_operations_card_value
from kubeport.utils.constants import STALE_OPERATION_THRESHOLD_MINUTES


class UnitTestApiDashboard(UnitTestCase):
	def test_count_stale_operations_returns_per_doctype_breakdown_and_total(self):
		count_returns = {
			"Helm Release": 2,
			"Frappe Site": 1,
			"Frappe Site Backup": 0,
		}

		def fake_count(doctype, filters=None):
			return count_returns[doctype]

		with patch("kubeport.api.dashboard.frappe.db.count", side_effect=fake_count):
			counts = count_stale_operations()

		self.assertEqual(counts["helm_release"], 2)
		self.assertEqual(counts["frappe_site"], 1)
		self.assertEqual(counts["frappe_site_backup"], 0)
		self.assertEqual(counts["total"], 3)

	def test_count_stale_operations_uses_correct_filters_per_doctype(self):
		"""Helm Release / Frappe Site Backup gate on operation_started_at;
		Frappe Site (which has no such field) gates on modified."""
		captured: list[tuple[str, dict]] = []

		def fake_count(doctype, filters=None):
			captured.append((doctype, dict(filters or {})))
			return 0

		fake_now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
		with (
			patch("kubeport.api.dashboard.frappe.db.count", side_effect=fake_count),
			patch("kubeport.api.dashboard.frappe.utils.now_datetime", return_value=fake_now),
		):
			count_stale_operations()

		filters_by_doctype = {dt: f for dt, f in captured}

		self.assertIn("operation_started_at", filters_by_doctype["Helm Release"])
		self.assertIn("operation_started_at", filters_by_doctype["Frappe Site Backup"])
		self.assertIn("modified", filters_by_doctype["Frappe Site"])
		self.assertNotIn("operation_started_at", filters_by_doctype["Frappe Site"])

		# Every query must require operation_token to be set.
		for doctype, filters in captured:
			self.assertEqual(filters["operation_token"], ["is", "set"])

	def test_count_stale_operations_threshold_matches_reconciliation_constant(self):
		"""The cutoff handed to frappe.db.count must equal now -
		STALE_OPERATION_THRESHOLD_MINUTES.  This is the contract that keeps
		the dashboard honest about what reconciliation considers stale."""
		captured_cutoffs: list = []

		def fake_count(doctype, filters=None):
			# The time field varies per DocType but the cutoff value is identical.
			for key, value in (filters or {}).items():
				if key in ("operation_started_at", "modified"):
					captured_cutoffs.append(value[1])
			return 0

		fake_now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
		with (
			patch("kubeport.api.dashboard.frappe.db.count", side_effect=fake_count),
			patch("kubeport.api.dashboard.frappe.utils.now_datetime", return_value=fake_now),
		):
			count_stale_operations()

		self.assertEqual(len(captured_cutoffs), 3)
		expected_delta_minutes = STALE_OPERATION_THRESHOLD_MINUTES
		for cutoff in captured_cutoffs:
			delta = fake_now - cutoff
			self.assertEqual(delta.total_seconds(), expected_delta_minutes * 60)

	def test_stale_operations_card_value_returns_total_only(self):
		"""Frappe's Custom Number Card reads {value: ...} from the method."""
		count_returns = {
			"Helm Release": 4,
			"Frappe Site": 2,
			"Frappe Site Backup": 1,
		}
		with patch(
			"kubeport.api.dashboard.frappe.db.count",
			side_effect=lambda dt, filters=None: count_returns[dt],
		):
			result = stale_operations_card_value()

		self.assertEqual(result, {"value": 7})
