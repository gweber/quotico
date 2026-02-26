"""
backend/tests/test_event_bus_monitor_handlers_agg.py

Purpose:
    Tests for handler 1h rollup aggregation from persisted monitor snapshots.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, "backend")

from app.services import event_bus_monitor as monitor_module
from app.utils import utcnow


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=50000):
        return list(self.docs)


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *_args, **_kwargs):
        return _FakeCursor(self.docs)


@pytest.mark.asyncio
async def test_handler_rollups_from_snapshots(monkeypatch):
    now = utcnow()
    docs = [
        {
            "ts": now - timedelta(minutes=59),
            "per_handler": [
                {
                    "name": "match.finalized:handler",
                    "concurrency": 1,
                    "queue_depth": 1,
                    "queue_limit": 10,
                    "handled_total": 10,
                    "failed_total": 2,
                    "dropped_total": 0,
                }
            ],
        },
        {
            "ts": now - timedelta(minutes=1),
            "per_handler": [
                {
                    "name": "match.finalized:handler",
                    "concurrency": 1,
                    "queue_depth": 0,
                    "queue_limit": 10,
                    "handled_total": 25,
                    "failed_total": 5,
                    "dropped_total": 1,
                }
            ],
        },
    ]
    fake_db = SimpleNamespace(event_bus_stats=_FakeCollection(docs))
    monkeypatch.setattr(monitor_module._db, "db", fake_db, raising=False)

    monitor = monitor_module.EventBusMonitor()
    rows = await monitor.get_handler_rollups("1h")
    assert len(rows) == 1
    row = rows[0]
    assert row["handled_1h"] == 15
    assert row["failed_1h"] == 3
    assert row["dropped_1h"] == 1

