"""
backend/tests/test_admin_provider_status_heartbeat.py

Purpose:
    Router-level tests for provider status heartbeat payload and manual
    heartbeat odds tick endpoint.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import admin as admin_router
from app.utils import utcnow


@pytest.mark.asyncio
async def test_provider_status_exposes_heartbeat_and_no_legacy_odds_worker(monkeypatch):
    class _Scheduler:
        running = False

        def get_jobs(self):
            return []

    class _MetaColl:
        async def find_one(self, *_args, **_kwargs):
            return {
                "last_tick_at": utcnow() - timedelta(minutes=5),
                "rounds_synced": 3,
                "fixtures_synced": 12,
                "matches_in_window": 44,
                "tier_breakdown": {"IMMINENT": 2, "CLOSING": 1},
            }

    async def _usage():
        return {"requests_used": 10, "requests_remaining": 90}

    async def _worker_state(_worker_id: str):
        return None

    import app.main as main_module

    monkeypatch.setattr(main_module, "scheduler", _Scheduler())
    monkeypatch.setattr(main_module, "automation_enabled", lambda: False)
    monkeypatch.setattr(main_module, "automated_job_count", lambda: 0)
    monkeypatch.setattr(admin_router.odds_provider, "load_usage", _usage)
    monkeypatch.setattr(admin_router, "get_worker_state", _worker_state)
    monkeypatch.setattr(
        admin_router,
        "_db",
        SimpleNamespace(db=SimpleNamespace(meta=_MetaColl())),
    )

    result = await admin_router.provider_status(admin={"_id": "admin"})
    worker_ids = {row["id"] for row in result["workers"]}

    assert all(worker_id != "odds" + "_poller" for worker_id in worker_ids)
    assert result["heartbeat"]["rounds_synced"] == 3
    assert result["heartbeat"]["fixtures_synced"] == 12
    assert result["heartbeat"]["matches_in_window"] == 44


@pytest.mark.asyncio
async def test_trigger_heartbeat_odds_tick_success(monkeypatch):
    async def _run_tick_now(*, triggered_by: str):
        now = utcnow()
        return {
            "started_at": now,
            "finished_at": now,
            "duration_ms": 11,
            "triggered_by": triggered_by,
            "tick": {
                "status": "ok",
                "rounds_synced": 2,
                "fixtures_synced": 8,
            },
        }

    async def _log_audit(**_kwargs):
        return None

    import app.services.metrics_heartbeat as heartbeat_module

    monkeypatch.setattr(heartbeat_module.metrics_heartbeat, "run_odds_tick_now", _run_tick_now)
    monkeypatch.setattr(admin_router, "log_audit", _log_audit)

    response = await admin_router.trigger_heartbeat_odds_tick(
        body=admin_router.HeartbeatOddsTickRequest(reason="manual_provider_overview"),
        request=object(),
        admin={"_id": "admin"},
    )

    assert response["ok"] is True
    assert response["duration_ms"] == 11
    assert response["result"]["status"] == "ok"


@pytest.mark.asyncio
async def test_trigger_heartbeat_odds_tick_conflict(monkeypatch):
    import app.services.metrics_heartbeat as heartbeat_module

    async def _raise_conflict(*, triggered_by: str):
        _ = triggered_by
        raise heartbeat_module.OddsTickAlreadyRunningError("running")

    monkeypatch.setattr(heartbeat_module.metrics_heartbeat, "run_odds_tick_now", _raise_conflict)

    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_heartbeat_odds_tick(
            body=admin_router.HeartbeatOddsTickRequest(reason="manual_provider_overview"),
            request=object(),
            admin={"_id": "admin"},
        )

    assert exc.value.status_code == 409
