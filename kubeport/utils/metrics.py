"""
Internal Observability — counters, histograms, correlation IDs.

Frappe-native (no Prometheus / external metrics dependency).  Counter and
histogram state live in Redis under the ``kubeport_metrics`` namespace via
``frappe.cache()`` so that increments emitted in the RQ ``long`` worker are
visible to the web process that renders the operator dashboard.  TODO-14
reads "in-memory counters (process-local)"; the observability acceptance
criterion ("the operator workspace shows live counters that increment
under load") cannot be satisfied process-locally because the worker and
the web request live in different processes — Redis-backed counters via
``frappe.cache()`` are the smallest Frappe-native fit.

This module is metadata about Kubeport itself, never about the cluster, so
it does not violate the desired/observed state invariant in AGENTS.md.

Usage from a worker:

    from kubeport.utils import metrics

    def my_task(release_name: str, operation_token: str, correlation_id: str | None = None):
        with metrics.correlation_scope(correlation_id):
            log = metrics.logger("kubeport.helm")
            log.info("install_or_upgrade_release start name=%s", release_name)
            ...

Usage at an enqueue site:

    cid = metrics.new_correlation_id()
    metrics.logger("kubeport.helm").info("enqueue install for %s", self.name)
    frappe.enqueue(..., correlation_id=cid)
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import frappe

# ---------------------------------------------------------------------------
# Public name constants
# ---------------------------------------------------------------------------

COUNTER_RECONCILE_TICKS = "reconcile_ticks_total"
COUNTER_STALE_OPS_RECOVERED = "stale_ops_recovered_total"
COUNTER_ORPHAN_JOBS_SWEPT = "orphan_jobs_swept_total"

HISTOGRAM_HELM_LATENCY = "helm_subprocess_latency_seconds"

KNOWN_COUNTERS: tuple[str, ...] = (
	COUNTER_RECONCILE_TICKS,
	COUNTER_STALE_OPS_RECOVERED,
	COUNTER_ORPHAN_JOBS_SWEPT,
)
KNOWN_HISTOGRAMS: tuple[str, ...] = (HISTOGRAM_HELM_LATENCY,)

# Rolling window for histograms.  At one helm subprocess call per second this
# is ~17 minutes of recent activity — sufficient for p50/p95/p99 trend.
_HISTOGRAM_WINDOW = 1000

_NAMESPACE = "kubeport_metrics"


# ---------------------------------------------------------------------------
# Counter API
# ---------------------------------------------------------------------------


def _counter_key(name: str) -> bytes:
	"""Return the site-scoped Redis key for a counter."""
	return frappe.cache().make_key(f"{_NAMESPACE}:counter:{name}")


def _histogram_key(name: str) -> str:
	"""Return the unprefixed key passed to RedisWrapper list ops.

	RedisWrapper.lpush / ltrim / lrange apply ``make_key`` themselves, so the
	caller passes the unprefixed namespace key.
	"""
	return f"{_NAMESPACE}:histogram:{name}"


def increment_counter(name: str, by: int = 1) -> None:
	"""Increment a cross-process counter atomically.

	Cache failures are swallowed: observability must never fail a request.
	"""
	if name not in KNOWN_COUNTERS:
		raise ValueError(f"Unknown counter: {name}")
	if by <= 0:
		return
	try:
		frappe.cache().incrby(_counter_key(name), by)
	except Exception as exc:
		frappe.logger("kubeport.metrics").warning("metrics counter '%s' increment failed: %s", name, exc)


def get_counter(name: str) -> int:
	"""Return the current value of a counter (0 if never incremented)."""
	if name not in KNOWN_COUNTERS:
		raise ValueError(f"Unknown counter: {name}")
	try:
		raw = frappe.cache().get(_counter_key(name))
	except Exception:
		return 0
	if raw is None:
		return 0
	try:
		return int(raw)
	except (TypeError, ValueError):
		return 0


def get_all_counters() -> dict[str, int]:
	"""Return ``{counter_name: value}`` for every known counter."""
	return {name: get_counter(name) for name in KNOWN_COUNTERS}


def reset_all() -> None:
	"""Reset every known counter and histogram.  Test / admin use only."""
	cache = frappe.cache()
	for name in KNOWN_COUNTERS:
		try:
			cache.delete(_counter_key(name))
		except Exception:
			pass
	for name in KNOWN_HISTOGRAMS:
		try:
			cache.delete_value(_histogram_key(name))
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Histogram API
# ---------------------------------------------------------------------------


def record_histogram_sample(name: str, value: float) -> None:
	"""Append a sample to a rolling-window histogram."""
	if name not in KNOWN_HISTOGRAMS:
		raise ValueError(f"Unknown histogram: {name}")
	if value < 0:
		return
	try:
		cache = frappe.cache()
		key = _histogram_key(name)
		cache.lpush(key, repr(float(value)))
		cache.ltrim(key, 0, _HISTOGRAM_WINDOW - 1)
	except Exception as exc:
		frappe.logger("kubeport.metrics").warning("metrics histogram '%s' record failed: %s", name, exc)


def get_histogram_summary(name: str) -> dict[str, float | int]:
	"""Return ``{count, p50, p95, p99}`` over the rolling window.

	An empty histogram returns counts and percentiles of zero.
	"""
	if name not in KNOWN_HISTOGRAMS:
		raise ValueError(f"Unknown histogram: {name}")
	try:
		raw_samples = frappe.cache().lrange(_histogram_key(name), 0, _HISTOGRAM_WINDOW - 1)
	except Exception:
		raw_samples = []

	samples: list[float] = []
	for raw in raw_samples or ():
		if isinstance(raw, bytes):
			raw = raw.decode("utf-8", errors="ignore")
		try:
			samples.append(float(raw))
		except (TypeError, ValueError):
			continue

	if not samples:
		return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

	samples.sort()
	n = len(samples)

	def _q(q: float) -> float:
		idx = min(n - 1, max(0, int(q * (n - 1))))
		return samples[idx]

	return {
		"count": n,
		"p50": round(_q(0.50), 6),
		"p95": round(_q(0.95), 6),
		"p99": round(_q(0.99), 6),
	}


@contextmanager
def time_helm_subprocess() -> Iterator[None]:
	"""Record a helm subprocess wall-clock latency on exit."""
	start = time.monotonic()
	try:
		yield
	finally:
		record_histogram_sample(HISTOGRAM_HELM_LATENCY, time.monotonic() - start)


# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------


def new_correlation_id() -> str:
	"""Return a fresh UUID4 correlation identifier."""
	return str(uuid.uuid4())


@contextmanager
def correlation_scope(correlation_id: str | None) -> Iterator[None]:
	"""Bind ``correlation_id`` to ``frappe.local.correlation_id`` for the block.

	Restores any prior value on exit.  A falsy ``correlation_id`` is a no-op.
	"""
	if not correlation_id:
		yield
		return

	local = getattr(frappe, "local", None)
	if local is None:
		yield
		return

	prev = getattr(local, "correlation_id", None)
	local.correlation_id = correlation_id
	try:
		yield
	finally:
		if prev is None:
			try:
				del local.correlation_id
			except AttributeError:
				pass
		else:
			local.correlation_id = prev


def current_correlation_id() -> str | None:
	"""Return the correlation ID bound by the active scope, or ``None``."""
	local = getattr(frappe, "local", None)
	if local is None:
		return None
	return getattr(local, "correlation_id", None)


class _CorrelationLogger:
	"""Frappe logger proxy that prefixes every line with the active correlation ID.

	Stdlib ``logging`` does not have ``.bind`` (that is structlog).  Frappe's
	``frappe.logger()`` returns a stdlib ``logging.Logger``; this proxy reads
	``frappe.local.correlation_id`` (set by :func:`correlation_scope`) and
	prefixes ``[correlation_id=...]`` to every formatted message so a single
	operation is grep-able from web → enqueue → worker.
	"""

	def __init__(self, name: str) -> None:
		self._name = name

	def _emit(self, level: int, msg: str, *args: object, **kwargs: object) -> None:
		cid = current_correlation_id()
		fmt = f"[correlation_id={cid}] {msg}" if cid else msg
		frappe.logger(self._name).log(level, fmt, *args, **kwargs)

	def debug(self, msg: str, *args: object, **kwargs: object) -> None:
		self._emit(logging.DEBUG, msg, *args, **kwargs)

	def info(self, msg: str, *args: object, **kwargs: object) -> None:
		self._emit(logging.INFO, msg, *args, **kwargs)

	def warning(self, msg: str, *args: object, **kwargs: object) -> None:
		self._emit(logging.WARNING, msg, *args, **kwargs)

	def error(self, msg: str, *args: object, **kwargs: object) -> None:
		self._emit(logging.ERROR, msg, *args, **kwargs)

	def exception(self, msg: str, *args: object, **kwargs: object) -> None:
		# stdlib's exception() is sugar for error(exc_info=True).
		kwargs.setdefault("exc_info", True)
		self._emit(logging.ERROR, msg, *args, **kwargs)


def logger(name: str = "kubeport") -> _CorrelationLogger:
	"""Return a logger that auto-injects the active correlation ID."""
	return _CorrelationLogger(name)
