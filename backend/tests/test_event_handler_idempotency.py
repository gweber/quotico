"""
backend/tests/test_event_handler_idempotency.py

Purpose:
    Idempotency checks for event subscribers.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services.event_handlers import match_handlers, matchday_handlers
from app.services.event_models import MatchFinalizedEvent, MatchUpdatedEvent


@pytest.mark.asyncio
async def test_match_finalized_handler_idempotent(monkeypatch):
    calls = {"resolve": 0, "leaderboard": 0, "matchday": 0, "slip_checks": 0}

    class _FakeSlips:
        async def find_one(self, *_args, **_kwargs):
            calls["slip_checks"] += 1
            return {"_id": ObjectId()} if calls["slip_checks"] == 1 else None

    fake_db = SimpleNamespace(betting_slips=_FakeSlips())
    monkeypatch.setattr(match_handlers._db, "db", fake_db, raising=False)

    async def _resolve(_match_id):
        calls["resolve"] += 1

    async def _leaderboard():
        calls["leaderboard"] += 1

    async def _matchday(**_kwargs):
        calls["matchday"] += 1

    monkeypatch.setattr(match_handlers, "resolve_single_match", _resolve)
    monkeypatch.setattr(match_handlers, "materialize_leaderboard", _leaderboard)
    monkeypatch.setattr(match_handlers, "materialize_matchday_leaderboard", _matchday)

    event = MatchFinalizedEvent(
        source="test",
        correlation_id="corr-idempotent-1",
        match_id=str(ObjectId()),
        league_id=str(ObjectId()),
        sport_key="soccer_epl",
        season=2025,
        final_score={"home": 1, "away": 0},
    )
    await match_handlers.handle_match_finalized(event)
    await match_handlers.handle_match_finalized(event)

    assert calls["resolve"] == 1
    assert calls["leaderboard"] == 2
    assert calls["matchday"] == 2


@pytest.mark.asyncio
async def test_matchday_handler_idempotent_status_update(monkeypatch):
    match_id = str(ObjectId())
    matchday_id = ObjectId()
    state = {"status": "upcoming", "updates": 0}

    class _FakeMatchdays:
        def find(self, *_args, **_kwargs):
            return self

        async def to_list(self, length=100):
            return [{"_id": matchday_id, "match_ids": [match_id], "status": state["status"]}]

        async def update_one(self, _query, update):
            state["updates"] += 1
            state["status"] = update["$set"]["status"]

    class _FakeMatches:
        def find(self, *_args, **_kwargs):
            return self

        async def to_list(self, length=100):
            return [{"status": "final"}]

    fake_db = SimpleNamespace(matchdays=_FakeMatchdays(), matches=_FakeMatches())
    monkeypatch.setattr(matchday_handlers._db, "db", fake_db, raising=False)

    event = MatchUpdatedEvent(
        source="test",
        correlation_id="corr-idempotent-2",
        match_id=match_id,
        league_id=str(ObjectId()),
        sport_key="soccer_epl",
        season=2025,
        previous_status="live",
        new_status="final",
        ingest_source="football_data",
        external_id="ext-1",
        changed_fields=["status"],
    )
    await matchday_handlers.handle_match_updated(event)
    await matchday_handlers.handle_match_updated(event)

    assert state["updates"] == 1
    assert state["status"] == "completed"
