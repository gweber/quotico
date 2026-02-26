"""
backend/tests/test_ws_event_handlers.py

Purpose:
    Tests for websocket qbus handlers, including single-batch odds broadcast.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services.event_handlers import websocket_handlers
from app.services.event_models import MatchFinalizedEvent, MatchUpdatedEvent, OddsIngestedEvent


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=100):
        return list(self.docs)


class _FakeMatches:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query, projection=None):
        oid = query.get("_id")
        for doc in self.docs:
            if doc.get("_id") == oid:
                return dict(doc)
        return None

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        return _FakeCursor([dict(doc) for doc in self.docs if doc.get("_id") in ids])


class _FakeLeagues:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query, projection=None):
        oid = query.get("_id")
        for doc in self.docs:
            if doc.get("_id") == oid:
                return dict(doc)
        return None

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        return _FakeCursor([dict(doc) for doc in self.docs if doc.get("_id") in ids])


@pytest.mark.asyncio
async def test_odds_ingested_ws_handler_single_batch_broadcast(monkeypatch):
    league_id = ObjectId()
    m1 = ObjectId()
    m2 = ObjectId()
    fake_db = SimpleNamespace(
        matches=_FakeMatches(
            [
                {"_id": m1, "league_id": league_id, "sport_key": "soccer_epl", "odds_meta": {"updated_at": None}},
                {"_id": m2, "league_id": league_id, "sport_key": "soccer_epl", "odds_meta": {"updated_at": None}},
            ]
        ),
        leagues=_FakeLeagues([{"_id": league_id, "external_ids": {"openligadb": "bl1"}}]),
    )
    monkeypatch.setattr(websocket_handlers._db, "db", fake_db, raising=False)
    monkeypatch.setattr(websocket_handlers.settings, "WS_EVENTS_ENABLED", True, raising=False)

    calls = []

    async def _broadcast(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(websocket_handlers.websocket_manager, "broadcast", _broadcast)

    event = OddsIngestedEvent(
        source="football_data",
        correlation_id="corr-odds-1",
        provider="theoddsapi",
        match_ids=[str(m1), str(m1), str(m2)],
        inserted=2,
        deduplicated=1,
        markets_updated=3,
    )
    await websocket_handlers.handle_odds_ingested_ws(event)
    assert len(calls) == 1
    selectors = calls[0]["selectors"]
    assert sorted(selectors["match_ids"]) == sorted([str(m1), str(m2)])


@pytest.mark.asyncio
async def test_match_event_ws_handlers_broadcast(monkeypatch):
    league_id = ObjectId()
    match_id = ObjectId()
    fake_db = SimpleNamespace(
        matches=_FakeMatches(
            [
                {
                    "_id": match_id,
                    "league_id": league_id,
                    "sport_key": "soccer_epl",
                    "status": "final",
                    "score": {"full_time": {"home": 2, "away": 0}},
                    "result": {"outcome": "1"},
                }
            ]
        ),
        leagues=_FakeLeagues([{"_id": league_id, "external_ids": {"openligadb": "bl1"}}]),
    )
    monkeypatch.setattr(websocket_handlers._db, "db", fake_db, raising=False)
    monkeypatch.setattr(websocket_handlers.settings, "WS_EVENTS_ENABLED", True, raising=False)
    calls = []

    async def _broadcast(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(websocket_handlers.websocket_manager, "broadcast", _broadcast)

    updated = MatchUpdatedEvent(
        source="openligadb",
        correlation_id="corr-u",
        match_id=str(match_id),
        league_id=str(league_id),
        sport_key="soccer_epl",
        season=2025,
        previous_status="live",
        new_status="final",
        ingest_source="openligadb",
        external_id="ext-1",
        changed_fields=["status"],
    )
    finalized = MatchFinalizedEvent(
        source="openligadb",
        correlation_id="corr-f",
        match_id=str(match_id),
        league_id=str(league_id),
        sport_key="soccer_epl",
        season=2025,
        final_score={"home": 2, "away": 0},
    )
    await websocket_handlers.handle_match_updated_ws(updated)
    await websocket_handlers.handle_match_finalized_ws(finalized)
    assert len(calls) == 2
    assert calls[0]["event_type"] == "match.updated"
    assert calls[1]["event_type"] == "match.finalized"
