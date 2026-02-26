"""
backend/tests/test_event_pipeline_integration.py

Purpose:
    Integration-style checks for event dispatch + subscriber execution.
"""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services.event_bus import InMemoryEventBus
from app.services.event_handlers import match_handlers
from app.services.event_models import MatchFinalizedEvent


@pytest.mark.asyncio
async def test_match_finalized_pipeline_and_failure_isolation(monkeypatch):
    calls = {"resolve": 0, "leaderboard": 0, "matchday": 0}

    class _FakeSlips:
        async def find_one(self, *_args, **_kwargs):
            return {"_id": ObjectId()}

    monkeypatch.setattr(match_handlers._db, "db", SimpleNamespace(betting_slips=_FakeSlips()), raising=False)

    async def _resolve(_match_id):
        calls["resolve"] += 1

    async def _leaderboard():
        calls["leaderboard"] += 1

    async def _matchday(**_kwargs):
        calls["matchday"] += 1

    async def _failing(_event):
        raise RuntimeError("failing-subscriber")

    monkeypatch.setattr(match_handlers, "resolve_single_match", _resolve)
    monkeypatch.setattr(match_handlers, "materialize_leaderboard", _leaderboard)
    monkeypatch.setattr(match_handlers, "materialize_matchday_leaderboard", _matchday)

    bus = InMemoryEventBus(ingress_maxsize=10, handler_maxsize=10, default_concurrency=1, error_buffer_size=10)
    bus.subscribe("match.finalized", match_handlers.handle_match_finalized, handler_name="match_finalized", concurrency=1)
    bus.subscribe("match.finalized", _failing, handler_name="failing", concurrency=1)
    await bus.start()

    bus.publish(
        MatchFinalizedEvent(
            source="test",
            correlation_id="corr-integration-1",
            match_id=str(ObjectId()),
            league_id=str(ObjectId()),
            sport_key="soccer_epl",
            season=2025,
            final_score={"home": 1, "away": 0},
        )
    )
    await asyncio.sleep(0.08)
    await bus.stop()

    stats = bus.stats()
    assert calls["resolve"] == 1
    assert calls["leaderboard"] == 1
    assert calls["matchday"] == 1
    assert stats["failed_total"] >= 1
    assert stats["handled_total"] >= 1
