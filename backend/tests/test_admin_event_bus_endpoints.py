"""
backend/tests/test_admin_event_bus_endpoints.py

Purpose:
    Router-level tests for admin qbus monitor endpoints.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "backend")

from app.routers import admin as admin_router


@pytest.mark.asyncio
async def test_event_bus_status_endpoint(monkeypatch):
    class _Job:
        id = "match_resolver"
        next_run_time = None

    class _Scheduler:
        def get_jobs(self):
            return [_Job()]

    async def _health():
        return {
            "status_level": "green",
            "alerts": [],
            "stats": {"running": True},
            "recent_errors": [],
            "per_source_1h": [{"name": "football_data", "published_1h": 1, "handled_1h": 1, "failed_1h": 0, "dropped_1h": 0}],
            "per_event_type_1h": [{"name": "match.updated", "published_1h": 1, "handled_1h": 1, "failed_1h": 0, "dropped_1h": 0}],
        }

    monkeypatch.setattr(admin_router.event_bus_monitor, "get_current_health", _health)
    import app.main as main_module
    monkeypatch.setattr(main_module, "automation_enabled", lambda: True)
    monkeypatch.setattr(main_module, "scheduler", _Scheduler())
    result = await admin_router.event_bus_status(admin={"_id": "a"})
    assert result["status_level"] == "green"
    assert "fallback_polling" in result
    assert result["per_source_1h"][0]["name"] == "football_data"


@pytest.mark.asyncio
async def test_event_bus_history_endpoint(monkeypatch):
    async def _history(window="24h", bucket_seconds=10):
        return {"window": window, "bucket_seconds": bucket_seconds, "series": []}

    monkeypatch.setattr(admin_router.event_bus_monitor, "get_recent_stats", _history)
    result = await admin_router.event_bus_history(window="6h", bucket_seconds=30, admin={"_id": "a"})
    assert result["window"] == "6h"
    assert result["bucket_seconds"] == 30


@pytest.mark.asyncio
async def test_event_bus_handlers_endpoint(monkeypatch):
    async def _rollups(window="1h"):
        return [{"name": "h1", "handled_1h": 10, "failed_1h": 0, "dropped_1h": 0}]

    monkeypatch.setattr(admin_router.event_bus_monitor, "get_handler_rollups", _rollups)
    monkeypatch.setattr(admin_router.event_bus, "stats", lambda: {"recent_errors": []})
    result = await admin_router.event_bus_handlers(window="1h", admin={"_id": "a"})
    assert result["window"] == "1h"
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "h1"
