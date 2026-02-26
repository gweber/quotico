"""
backend/tests/test_event_bus_monitor.py

Purpose:
    Unit tests for qbus monitor snapshot recording and threshold checks.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, "backend")

from app.services import event_bus_monitor as monitor_module


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=len(self.docs))


@pytest.mark.asyncio
async def test_record_stats_snapshot_excludes_recent_errors(monkeypatch):
    fake_coll = _FakeCollection()
    fake_db = SimpleNamespace(event_bus_stats=fake_coll)
    monkeypatch.setattr(monitor_module._db, "db", fake_db, raising=False)

    sample_stats = {
        "ingress_queue_depth": 1,
        "ingress_queue_limit": 10,
        "ingress_queue_usage_pct": 10.0,
        "max_ingress_queue_depth_seen": 2,
        "published_rate_1m": 0.2,
        "handled_rate_1m": 0.2,
        "failed_rate_1m": 0.0,
        "dropped_rate_1m": 0.0,
        "latency_ms": {"avg": 5.0, "p50": 5.0, "p95": 7.0},
        "published_total": 10,
        "handled_total": 9,
        "failed_total": 1,
        "dropped_total": 0,
        "per_handler": {
            "match.created:h1": {
                "name": "match.created:h1",
                "concurrency": 1,
                "queue_depth": 0,
                "queue_limit": 20,
                "handled_total": 9,
                "failed_total": 1,
                "dropped_total": 0,
            }
        },
        "per_event_type": {
            "match.created": {
                "published_total": 10,
                "handled_total": 9,
                "failed_total": 1,
                "dropped_total": 0,
            }
        },
        "per_source": {
            "football_data": {
                "published_total": 10,
                "handled_total": 9,
                "failed_total": 1,
                "dropped_total": 0,
            }
        },
        "recent_errors": [{"event_id": "e1"}],
    }
    monkeypatch.setattr(monitor_module.event_bus, "stats", lambda: sample_stats)
    monitor = monitor_module.EventBusMonitor()
    snapshot = await monitor.record_stats()

    assert snapshot["recent_errors_count"] == 1
    assert "recent_errors" not in snapshot
    assert snapshot["per_event_type"][0]["name"] == "match.created"
    assert snapshot["per_source"][0]["name"] == "football_data"
    assert len(fake_coll.docs) == 1


def test_thresholds_trigger_red(monkeypatch):
    monitor = monitor_module.EventBusMonitor()
    stats = {
        "ingress_queue_usage_pct": 98.0,
        "handler_queue_limit": 10,
        "handler_queue_depth": {"h1": 10},
        "handled_rate_1m": 0.1,
        "failed_rate_1m": 0.1,
        "dropped_rate_1m": 1.0,
        "latency_ms": {"p95": 2000},
    }
    result = monitor.check_thresholds(stats)
    assert result["status_level"] == "red"
    assert len(result["alerts"]) > 0
