"""
backend/app/monitoring/odds_metrics.py

Purpose:
    Prometheus metrics for odds ingest and match-meta aggregation pipelines.
    Uses no-op fallbacks when prometheus_client is unavailable.

Dependencies:
    - prometheus_client (optional)
"""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter

try:
    from prometheus_client import Counter, Gauge, Histogram
except Exception:  # pragma: no cover - optional dependency
    Counter = Gauge = Histogram = None


class _NoOpMetric:
    def labels(self, **_kwargs):
        return self

    def inc(self, *_args, **_kwargs):
        return None

    def set(self, *_args, **_kwargs):
        return None

    def observe(self, *_args, **_kwargs):
        return None


METRIC_EVENTS_TOTAL = (
    Counter(
        "odds_ingest_events_total",
        "Odds events processed by ingest.",
        ["provider", "market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_EVENTS_DEDUP = (
    Counter(
        "odds_ingest_events_deduplicated_total",
        "Odds events skipped due to idempotent deduplication.",
        ["provider", "market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_META_UPDATES = (
    Counter(
        "odds_meta_updates_total",
        "Successful odds_meta updates per market.",
        ["market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_META_CAS_CONFLICTS = (
    Counter(
        "odds_meta_cas_conflicts_total",
        "CAS conflicts while updating odds_meta.",
        ["market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_STALE_EXCLUDED = (
    Counter(
        "odds_provider_stale_excluded_total",
        "Provider values excluded due to staleness window.",
        ["provider", "market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_LINE_DROPPED = (
    Counter(
        "odds_provider_line_dropped_total",
        "Provider values excluded because line is not modal.",
        ["provider", "market"],
    )
    if Counter
    else _NoOpMetric()
)
METRIC_PROVIDER_COUNT = (
    Gauge(
        "odds_market_provider_count",
        "Providers contributing to current odds per market.",
        ["sport_key", "market"],
    )
    if Gauge
    else _NoOpMetric()
)
METRIC_PROVIDER_LAST_SEEN = (
    Gauge(
        "odds_provider_last_seen_seconds",
        "Epoch timestamp of provider last seen update.",
        ["provider", "sport_key"],
    )
    if Gauge
    else _NoOpMetric()
)
METRIC_INGEST_LATENCY = (
    Histogram(
        "odds_ingest_batch_latency_seconds",
        "Latency of ingest_snapshot_batch.",
    )
    if Histogram
    else _NoOpMetric()
)
METRIC_PROCESSING_LAG = (
    Histogram(
        "odds_snapshot_processing_lag_seconds",
        "Lag from snapshot_at to ingest processing time.",
    )
    if Histogram
    else _NoOpMetric()
)
METRIC_META_UPDATE_LATENCY = (
    Histogram(
        "odds_meta_update_latency_seconds",
        "Latency for one match/market meta update.",
        ["market"],
    )
    if Histogram
    else _NoOpMetric()
)


@contextmanager
def observe_latency(metric):
    start = perf_counter()
    try:
        yield
    finally:
        metric.observe(perf_counter() - start)
