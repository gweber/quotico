"""
backend/tests/test_match_ingest_service.py

Purpose:
    Unit tests for the unified MatchIngestService, focused on hook execution
    and provider metadata merge behavior during updates.
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
        existing = cur.get(key)
        if not isinstance(existing, dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


class _FakeMatches:
    def __init__(self, docs: list[dict]):
        self.docs = [dict(doc) for doc in docs]

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if "_id" in query and doc.get("_id") != query.get("_id"):
                continue
            ext_query = next((k for k in query if k.startswith("external_ids.")), None)
            if ext_query:
                _, source = ext_query.split(".", 1)
                if doc.get("external_ids", {}).get(source) != query[ext_query]:
                    continue
            if all(key in {"_id", ext_query} for key in query.keys()):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        existing = None
        for doc in self.docs:
            if query.get("_id") and doc.get("_id") == query.get("_id"):
                existing = doc
                break
            if (
                doc.get("league_id") == query.get("league_id")
                and doc.get("home_team_id") == query.get("home_team_id")
                and doc.get("away_team_id") == query.get("away_team_id")
                and doc.get("match_date_hour") == query.get("match_date_hour")
            ):
                existing = doc
                break

        inserted_id = None
        if existing is None and upsert:
            existing = {"_id": ObjectId()}
            inserted_id = existing["_id"]
            self.docs.append(existing)
            for dotted, value in update.get("$setOnInsert", {}).items():
                _set_nested(existing, dotted, value)

        if existing is not None:
            for dotted, value in update.get("$set", {}).items():
                _set_nested(existing, dotted, value)

        return SimpleNamespace(upserted_id=inserted_id)


class _FakeDB:
    def __init__(self, matches_docs: list[dict]):
        self.matches = _FakeMatches(matches_docs)
        self.leagues = SimpleNamespace(find_one=self._find_none)
        self.teams = SimpleNamespace(find_one=self._find_none)

    async def _find_none(self, *_args, **_kwargs):
        return None


class _FakeTeamRegistry:
    async def resolve_by_external_id_or_name(self, **kwargs):
        name = str(kwargs.get("name") or "").strip()
        return ObjectId() if name else None


class _HookCollector:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.batch = 0

    async def on_match_created(self, _match_doc, _context):
        self.created += 1

    async def on_match_updated(self, _match_doc, _context):
        self.updated += 1

    async def on_batch_completed(self, _result, _context):
        self.batch += 1


@pytest.mark.asyncio
async def test_match_ingest_updates_provider_metadata_and_calls_batch_hook(monkeypatch):
    league_id = ObjectId()
    home_id = ObjectId()
    away_id = ObjectId()
    existing_id = ObjectId()
    fake_db = _FakeDB(
        [
            {
                "_id": existing_id,
                "league_id": league_id,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "match_date_hour": None,
                "external_ids": {"football_data": "fd-100"},
                "metadata": {"providers": {"openligadb": {"raw": "keep"}}},
            }
        ]
    )

    monkeypatch.setattr(ingest_module._db, "db", fake_db, raising=False)
    monkeypatch.setattr(ingest_module.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))

    hooks = _HookCollector()
    ingest = ingest_module.MatchIngestService()
    result = await ingest.process_matches(
        [
            {
                "external_id": "fd-100",
                "source": "football_data",
                "league_external_id": "PL",
                "season": 2025,
                "sport_key": "soccer_epl",
                "match_date": "2025-08-10T14:00:00Z",
                "home_team": {"external_id": "57", "name": "Arsenal"},
                "away_team": {"external_id": "61", "name": "Chelsea"},
                "status": "SCHEDULED",
                "matchday": 1,
                "score": {"full_time": {"home": None, "away": None}},
                "metadata": {"raw": "fd"},
            }
        ],
        league_id=league_id,
        dry_run=False,
        hooks=hooks,
    )

    assert result["updated"] == 1
    assert hooks.updated == 1
    assert hooks.batch == 1
    assert hooks.created == 0

    updated = await fake_db.matches.find_one({"_id": existing_id})
    assert updated is not None
    assert updated.get("metadata", {}).get("providers", {}).get("openligadb", {}).get("raw") == "keep"
    assert updated.get("metadata", {}).get("providers", {}).get("football_data", {}).get("raw") == "fd"
