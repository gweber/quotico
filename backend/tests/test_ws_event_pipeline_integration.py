"""
backend/tests/test_ws_event_pipeline_integration.py

Purpose:
    Integration-style test for qbus -> websocket handler -> managed connection
    delivery with filtering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services.event_bus import InMemoryEventBus
from app.services.event_handlers import websocket_handlers
from app.services.event_models import OddsIngestedEvent
from app.services.websocket_manager import WebSocketManager


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=100):
        return list(self.docs)


class _FakeMatches:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        return _FakeCursor([dict(doc) for doc in self.docs if doc.get("_id") in ids])


class _FakeLeagues:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        return _FakeCursor([dict(doc) for doc in self.docs if doc.get("_id") in ids])


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.messages.append(payload)


# FIXME: ODDS_V3_BREAK — tests OddsIngestedEvent which is no longer published by connector
@pytest.mark.asyncio
async def test_ws_pipeline_receives_filtered_odds_event(monkeypatch):
    league_id = ObjectId()
    m1 = ObjectId()
    m2 = ObjectId()
    fake_db = SimpleNamespace(
        matches=_FakeMatches(
            [
                {"_id": m1, "league_id": league_id, "league_id": 8, "odds_meta": {}},
                {"_id": m2, "league_id": league_id, "league_id": 8, "odds_meta": {}},
            ]
        ),
        leagues=_FakeLeagues([{"_id": league_id, "external_ids": {"openligadb": "bl1"}}]),
    )
    monkeypatch.setattr(websocket_handlers._db, "db", fake_db, raising=False)
    monkeypatch.setattr(websocket_handlers.settings, "WS_EVENTS_ENABLED", True, raising=False)

    manager = WebSocketManager(max_connections=10, heartbeat_seconds=60)
    monkeypatch.setattr(websocket_handlers, "websocket_manager", manager)

    ws_relevant = _FakeWS()
    ws_other = _FakeWS()
    cid1 = await manager.connect(ws_relevant, user_id="u1")
    cid2 = await manager.connect(ws_other, user_id="u2")
    await manager.update_filters(cid1, "subscribe", {"match_ids": [str(m1)]})
    await manager.update_filters(cid2, "subscribe", {"match_ids": [str(ObjectId())]})

    bus = InMemoryEventBus(ingress_maxsize=10, handler_maxsize=10, default_concurrency=1, error_buffer_size=10)
    bus.subscribe("odds.ingested", websocket_handlers.handle_odds_ingested_ws, handler_name="ws_odds", concurrency=1)
    await bus.start()
    bus.publish(
        OddsIngestedEvent(
            source="football_data",
            correlation_id="corr-pipe-1",
            provider="theoddsapi",
            match_ids=[str(m1), str(m2)],
            inserted=2,
            deduplicated=0,
            markets_updated=2,
        )
    )
    await asyncio.sleep(0.08)
    await bus.stop()
    await manager.disconnect(cid1)
    await manager.disconnect(cid2)

    assert any(msg.get("type") == "odds.ingested" for msg in ws_relevant.messages)
    assert not any(msg.get("type") == "odds.ingested" for msg in ws_other.messages)
