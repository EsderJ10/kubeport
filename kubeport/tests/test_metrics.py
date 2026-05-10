# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import logging
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from kubeport.utils import metrics


class _FakeRedisCache:
	"""Minimal stand-in for ``frappe.cache()`` covering the operations
	``metrics`` calls: ``incrby``, ``get``, ``delete``, plus list-style
	``lpush`` / ``ltrim`` / ``lrange`` and ``delete_value`` for histograms.
	``make_key`` returns the key unchanged so tests can assert on namespaces.
	"""

	def __init__(self):
		self._counters: dict[str, int] = {}
		self._lists: dict[str, list[str]] = {}

	def make_key(self, key):
		return key

	def incrby(self, key, by):
		self._counters[key] = self._counters.get(key, 0) + by
		return self._counters[key]

	def get(self, key):
		v = self._counters.get(key)
		return None if v is None else str(v).encode("utf-8")

	def delete(self, key):
		self._counters.pop(key, None)

	def lpush(self, key, value):
		self._lists.setdefault(key, []).insert(0, value)

	def ltrim(self, key, start, stop):
		if key in self._lists:
			self._lists[key] = self._lists[key][start : stop + 1]

	def lrange(self, key, start, stop):
		return list(self._lists.get(key, [])[start : stop + 1])

	def delete_value(self, key):
		self._lists.pop(key, None)


class UnitTestMetricsCounters(UnitTestCase):
	def test_increment_and_get_counter_round_trips(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			metrics.increment_counter(metrics.COUNTER_RECONCILE_TICKS)
			metrics.increment_counter(metrics.COUNTER_RECONCILE_TICKS, by=4)

			self.assertEqual(metrics.get_counter(metrics.COUNTER_RECONCILE_TICKS), 5)

	def test_get_counter_returns_zero_when_unset(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			self.assertEqual(metrics.get_counter(metrics.COUNTER_ORPHAN_JOBS_SWEPT), 0)

	def test_unknown_counter_name_raises(self):
		with self.assertRaises(ValueError):
			metrics.increment_counter("not_a_real_counter")
		with self.assertRaises(ValueError):
			metrics.get_counter("not_a_real_counter")

	def test_increment_swallows_cache_errors(self):
		broken = MagicMock()
		broken.make_key.return_value = b"k"
		broken.incrby.side_effect = RuntimeError("redis down")
		with patch("kubeport.utils.metrics.frappe.cache", return_value=broken):
			# Must not raise — observability never breaks request handling.
			metrics.increment_counter(metrics.COUNTER_RECONCILE_TICKS)

	def test_get_all_counters_returns_every_known_counter(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			metrics.increment_counter(metrics.COUNTER_RECONCILE_TICKS, by=2)
			metrics.increment_counter(metrics.COUNTER_STALE_OPS_RECOVERED, by=3)

			values = metrics.get_all_counters()

			self.assertEqual(values[metrics.COUNTER_RECONCILE_TICKS], 2)
			self.assertEqual(values[metrics.COUNTER_STALE_OPS_RECOVERED], 3)
			self.assertEqual(values[metrics.COUNTER_ORPHAN_JOBS_SWEPT], 0)
			self.assertEqual(set(values), set(metrics.KNOWN_COUNTERS))

	def test_reset_all_clears_counters_and_histograms(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			metrics.increment_counter(metrics.COUNTER_RECONCILE_TICKS, by=7)
			metrics.record_histogram_sample(metrics.HISTOGRAM_HELM_LATENCY, 1.0)

			metrics.reset_all()

			self.assertEqual(metrics.get_counter(metrics.COUNTER_RECONCILE_TICKS), 0)
			self.assertEqual(metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)["count"], 0)


class UnitTestMetricsHistogram(UnitTestCase):
	def test_empty_histogram_returns_zero_summary(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			summary = metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)
		self.assertEqual(summary, {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0})

	def test_histogram_percentiles_match_sorted_samples(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			# 100 samples, evenly distributed 0.01..1.00 — p50 ≈ 0.50, p95 ≈ 0.95.
			for i in range(1, 101):
				metrics.record_histogram_sample(metrics.HISTOGRAM_HELM_LATENCY, i / 100.0)

			summary = metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)

		self.assertEqual(summary["count"], 100)
		# Tolerant comparison: percentile is a sample, not interpolated.
		self.assertGreaterEqual(summary["p50"], 0.45)
		self.assertLessEqual(summary["p50"], 0.55)
		self.assertGreaterEqual(summary["p95"], 0.90)
		self.assertLessEqual(summary["p95"], 1.0)
		self.assertGreaterEqual(summary["p99"], 0.95)
		self.assertLessEqual(summary["p99"], 1.0)

	def test_negative_samples_are_dropped(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			metrics.record_histogram_sample(metrics.HISTOGRAM_HELM_LATENCY, -0.5)
			summary = metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)
		self.assertEqual(summary["count"], 0)

	def test_unknown_histogram_name_raises(self):
		with self.assertRaises(ValueError):
			metrics.record_histogram_sample("not_a_histogram", 1.0)
		with self.assertRaises(ValueError):
			metrics.get_histogram_summary("not_a_histogram")

	def test_time_helm_subprocess_records_a_sample(self):
		fake = _FakeRedisCache()
		with patch("kubeport.utils.metrics.frappe.cache", return_value=fake):
			with metrics.time_helm_subprocess():
				pass
			summary = metrics.get_histogram_summary(metrics.HISTOGRAM_HELM_LATENCY)
		self.assertEqual(summary["count"], 1)
		# Trivial body — wall-clock fits in a few milliseconds, certainly < 5 s.
		self.assertLess(summary["p50"], 5.0)


class UnitTestCorrelationScope(UnitTestCase):
	def setUp(self):
		# Clean slate on each test — frappe.local is process-global.
		try:
			del frappe.local.correlation_id
		except AttributeError:
			pass

	def tearDown(self):
		try:
			del frappe.local.correlation_id
		except AttributeError:
			pass

	def test_new_correlation_id_is_unique_uuid_string(self):
		a = metrics.new_correlation_id()
		b = metrics.new_correlation_id()
		self.assertNotEqual(a, b)
		self.assertEqual(len(a), 36)
		self.assertIn("-", a)

	def test_scope_sets_and_clears_local_correlation_id(self):
		self.assertIsNone(metrics.current_correlation_id())
		with metrics.correlation_scope("abc-123"):
			self.assertEqual(metrics.current_correlation_id(), "abc-123")
		self.assertIsNone(metrics.current_correlation_id())

	def test_scope_restores_previous_value(self):
		with metrics.correlation_scope("outer"):
			self.assertEqual(metrics.current_correlation_id(), "outer")
			with metrics.correlation_scope("inner"):
				self.assertEqual(metrics.current_correlation_id(), "inner")
			self.assertEqual(metrics.current_correlation_id(), "outer")

	def test_scope_with_falsy_id_is_a_no_op(self):
		with metrics.correlation_scope(None):
			self.assertIsNone(metrics.current_correlation_id())
		with metrics.correlation_scope(""):
			self.assertIsNone(metrics.current_correlation_id())

	def test_logger_emits_correlation_prefix_under_scope(self):
		"""The wrapper must read frappe.local.correlation_id at log time and
		prefix every record so a single operation is grep-able from web →
		enqueue → worker."""
		recorded: list[str] = []

		class _Captor:
			def log(self, level, msg, *args, **kwargs):
				try:
					rendered = msg % args if args else msg
				except Exception:
					rendered = msg
				recorded.append(rendered)

		with patch("kubeport.utils.metrics.frappe.logger", return_value=_Captor()):
			with metrics.correlation_scope("op-42"):
				metrics.logger("kubeport.helm").info("install %s", "release-x")

		self.assertEqual(len(recorded), 1)
		self.assertIn("[correlation_id=op-42]", recorded[0])
		self.assertIn("install release-x", recorded[0])

	def test_logger_omits_prefix_when_no_scope_bound(self):
		recorded: list[str] = []

		class _Captor:
			def log(self, level, msg, *args, **kwargs):
				recorded.append(msg)

		with patch("kubeport.utils.metrics.frappe.logger", return_value=_Captor()):
			metrics.logger("kubeport.helm").info("hello")

		self.assertEqual(recorded, ["hello"])

	def test_logger_uses_correct_logging_levels(self):
		captured: list[int] = []

		class _Captor:
			def log(self, level, msg, *args, **kwargs):
				captured.append(level)

		with patch("kubeport.utils.metrics.frappe.logger", return_value=_Captor()):
			log = metrics.logger("kubeport.helm")
			log.debug("d")
			log.info("i")
			log.warning("w")
			log.error("e")

		self.assertEqual(
			captured,
			[logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR],
		)
