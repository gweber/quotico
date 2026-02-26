"""
backend/tests/test_match_ingest_events.py

Purpose:
    Publisher tests for MatchIngestService event emission.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import match_ingest_service as ingest_module


def _set_nested(target: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = target
    for key in parts[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


class _FakeMatches:
    def __init__(self, docs: list[dict]):
        self.docs = [dict(doc) for doc in docs]

    async def find_one(self, query, projection=None):
        ext_key = next((k for k in query.keys() if k.startswith("external_ids.")), None)
        for doc in self.docs:
            if query.get("_id") and doc.get("_id") != query.get("_id"):
                continue
            if ext_key:
                _, src = ext_key.split(".", 1)
                if doc.get("external_ids", {}).get(src) != query.get(ext_key):
                    continue
            if not ext_key and query.get("league_id") and doc.get("league_id") != query.get("league_id"):
                continue
            if projection:
                return {k: v for k, v in doc.items() if k in projection or k == "_id"}
            return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        existing = await self.find_one(query)
        inserted_id = None
        if existing is None and upsert:
            existing = {"_id": ObjectId()}
            inserted_id = existing["_id"]
            self.docs.append(existing)
            for key, value in update.get("$setOnInsert", {}).items():
                _set_nested(existing, key, value)
        if existing is not None:
            for key, value in update.get("$set", {}).items():
                _set_nested(existing, key, value)
        return SimpleNamespace(upserted_id=inserted_id)


class _FakeDB:
    def __init__(self, matches_docs: list[dict]):
        self.matches = _FakeMatches(matches_docs)
        self.leagues = SimpleNamespace(find_one=self._none)
        self.teams = SimpleNamespace(find_one=self._none)

    async def _none(self, *_args, **_kwargs):
        return None


class _FakeTeamRegistry:
    async def resolve_by_external_id_or_name(self, **_kwargs):
        return ObjectId()


@pytest.mark.asyncio
async def test_match_ingest_publishes_create_and_finalized(monkeypatch):
    published = []
    fake_db = _FakeDB([])
    league_id = ObjectId()

    monkeypatch.setattr(ingest_module._db, "db", fake_db, raising=False)
    monkeypatch.setattr(ingest_module.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(ingest_module.settings, "EVENT_BUS_ENABLED", True, raising=False)
    monkeypatch.setattr(ingest_module.event_bus, "publish", lambda event: published.append(event))

    service = ingest_module.MatchIngestService()
    result = await service.process_matches(
        [
            {
                "external_id": "ext-create-1",
                "source": "football_data",
                "league_external_id": "PL",
                "season": 2025,
                "sport_key": "soccer_epl",
                "match_date": "2025-08-10T14:00:00Z",
                "home_team": {"external_id": "57", "name": "Arsenal"},
                "away_team": {"external_id": "61", "name": "Chelsea"},
                "status": "FINISHED",
                "matchday": 1,
                "score": {"full_time": {"home": 2, "away": 1}},
                "metadata": {"raw": "payload"},
                "correlation_id": "corr-create-1",
            }
        ],
        league_id=league_id,
        dry_run=False,
    )
    assert result["created"] == 1
    assert len(published) == 2
    assert published[0].event_type == "match.created"
    assert published[1].event_type == "match.finalized"
    assert published[0].correlation_id == "corr-create-1"


@pytest.mark.asyncio
async def test_match_ingest_publishes_update_and_finalized(monkeypatch):
    published = []
    league_id = ObjectId()
    existing_id = ObjectId()
    fake_db = _FakeDB(
        [
            {
                "_id": existing_id,
                "league_id": league_id,
                "status": "scheduled",
                "external_ids": {"football_data": "ext-update-1"},
            }
        ]
    )

    monkeypatch.setattr(ingest_module._db, "db", fake_db, raising=False)
    monkeypatch.setattr(ingest_module.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(ingest_module.settings, "EVENT_BUS_ENABLED", True, raising=False)
    monkeypatch.setattr(ingest_module.event_bus, "publish", lambda event: published.append(event))

    service = ingest_module.MatchIngestService()
    result = await service.process_matches(
        [
            {
                "external_id": "ext-update-1",
                "source": "football_data",
                "league_external_id": "PL",
                "season": 2025,
                "sport_key": "soccer_epl",
                "match_date": "2025-08-10T14:00:00Z",
                "home_team": {"external_id": "57", "name": "Arsenal"},
                "away_team": {"external_id": "61", "name": "Chelsea"},
                "status": "FINISHED",
                "matchday": 1,
                "score": {"full_time": {"home": 1, "away": 0}},
                "metadata": {"raw": "payload"},
                "correlation_id": "corr-update-1",
            }
        ],
        league_id=league_id,
        dry_run=False,
    )
    assert result["updated"] == 1
    assert len(published) == 2
    assert published[0].event_type == "match.updated"
    assert published[1].event_type == "match.finalized"
    assert published[0].correlation_id == "corr-update-1"


@pytest.mark.asyncio
async def test_match_ingest_noop_update_does_not_publish(monkeypatch):
    published = []
    league_id = ObjectId()
    existing_id = ObjectId()
    fake_db = _FakeDB(
        [
            {
                "_id": existing_id,
                "league_id": league_id,
                "status": "final",
                "score": {"full_time": {"home": 1, "away": 0}},
                "matchday": 1,
                "match_date": ingest_module.parse_utc("2025-08-10T14:00:00Z"),
                "external_ids": {"football_data": "ext-noop-1"},
            }
        ]
    )

    monkeypatch.setattr(ingest_module._db, "db", fake_db, raising=False)
    monkeypatch.setattr(ingest_module.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(ingest_module.settings, "EVENT_BUS_ENABLED", True, raising=False)
    monkeypatch.setattr(ingest_module.event_bus, "publish", lambda event: published.append(event))

    service = ingest_module.MatchIngestService()
    result = await service.process_matches(
        [
            {
                "external_id": "ext-noop-1",
                "source": "football_data",
                "league_external_id": "PL",
                "season": 2025,
                "sport_key": "soccer_epl",
                "match_date": "2025-08-10T14:00:00Z",
                "home_team": {"external_id": "57", "name": "Arsenal"},
                "away_team": {"external_id": "61", "name": "Chelsea"},
                "status": "FINISHED",
                "matchday": 1,
                "score": {"full_time": {"home": 1, "away": 0}},
                "metadata": {"raw": "payload"},
                "correlation_id": "corr-noop-1",
            }
        ],
        league_id=league_id,
        dry_run=False,
    )
    assert result["updated"] == 1
    assert published == []
