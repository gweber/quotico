"""
backend/tests/test_admin_matches_api.py

Purpose:
    Router-level tests for admin matches list/detail behavior, including cache
    semantics, search guards, and audit payload integrity for overrides.

Dependencies:
    - pytest
    - app.routers.admin
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId
from fastapi import HTTPException, Response

sys.path.insert(0, "backend")

from app.routers import admin as admin_router


class _FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs
        self._skip = 0
        self._limit = len(docs)

    def sort(self, key: str, direction: int):
        reverse = direction < 0
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=reverse)
        return self

    def skip(self, value: int):
        self._skip = value
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    async def to_list(self, length: int):
        end = self._skip + min(self._limit, length)
        return [dict(d) for d in self._docs[self._skip:end]]


class _FakeMatchesCollection:
    def __init__(self, docs: list[dict]):
        self.docs = [dict(d) for d in docs]
        self.find_calls = 0
        self.count_calls = 0

    async def count_documents(self, _query):
        self.count_calls += 1
        return len(self.docs)

    def find(self, _query):
        self.find_calls += 1
        return _FakeCursor(self.docs)

    async def find_one(self, query):
        for doc in self.docs:
            if doc["_id"] == query.get("_id"):
                return dict(doc)
        return None

    async def update_one(self, query, update):
        for idx, doc in enumerate(self.docs):
            if doc["_id"] != query.get("_id"):
                continue
            for key, value in (update.get("$set") or {}).items():
                if "." in key:
                    parts = key.split(".")
                    node = doc
                    for p in parts[:-1]:
                        node = node.setdefault(p, {})
                    node[parts[-1]] = value
                else:
                    doc[key] = value
            self.docs[idx] = doc
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


class _FakeLeaguesCollection:
    def __init__(self, league_id: ObjectId):
        self.league_id = league_id

    def find(self, _query, _projection):
        return _FakeCursor([{"_id": self.league_id, "display_name": "Bundesliga"}])

    async def find_one(self, query, _projection=None):
        if query.get("_id") == self.league_id:
            return {"_id": self.league_id, "display_name": "Bundesliga"}
        return None


class _FakeBettingSlipsCollection:
    def __init__(self):
        self.aggregate_calls = 0

    def aggregate(self, _pipeline):
        self.aggregate_calls += 1
        return _FakeCursor([])


class _FakeTeamsCollection:
    async def distinct(self, _field, _query):
        return []


@pytest.mark.asyncio
async def test_admin_matches_list_cache_hit_and_miss(monkeypatch):
    admin_router._ADMIN_MATCHES_CACHE.clear()
    league_id = ObjectId()
    match_id = ObjectId()
    fake_db = SimpleNamespace(
        matches=_FakeMatchesCollection(
            [
                {
                    "_id": match_id,
                    "league_id": league_id,
                    "league_id": 82,
                    "home_team": "Bayern Munich",
                    "away_team": "RB Leipzig",
                    "match_date": datetime(2025, 8, 22, tzinfo=timezone.utc),
                    "status": "final",
                    "result": {"home_score": 2, "away_score": 1, "outcome": "1"},
                    "odds_meta": {"updated_at": datetime(2025, 8, 21, tzinfo=timezone.utc)},  # FIXME: ODDS_V3_BREAK — test uses odds_meta no longer produced by connector
                }
            ]
        ),
        leagues=_FakeLeaguesCollection(league_id),
        betting_slips=_FakeBettingSlipsCollection(),
        teams=_FakeTeamsCollection(),
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    response_1 = Response()
    first = await admin_router.list_all_matches(
        response=response_1,
        league_id=None,
        status_filter=None,
        date_from=None,
        date_to=None,
        needs_review=None,
        odds_available=None,
        search=None,
        page=1,
        page_size=50,
        admin={"_id": ObjectId()},
    )
    assert response_1.headers.get("X-Admin-Cache") == "MISS"
    assert first["total"] == 1
    assert first["items"][0]["has_odds"] is True

    response_2 = Response()
    second = await admin_router.list_all_matches(
        response=response_2,
        league_id=None,
        status_filter=None,
        date_from=None,
        date_to=None,
        needs_review=None,
        odds_available=None,
        search=None,
        page=1,
        page_size=50,
        admin={"_id": ObjectId()},
    )
    assert response_2.headers.get("X-Admin-Cache") == "HIT"
    assert second["total"] == 1


@pytest.mark.asyncio
async def test_admin_matches_search_guard(monkeypatch):
    admin_router._ADMIN_MATCHES_CACHE.clear()
    fake_db = SimpleNamespace(
        matches=_FakeMatchesCollection([]),
        leagues=_FakeLeaguesCollection(ObjectId()),
        betting_slips=_FakeBettingSlipsCollection(),
        teams=_FakeTeamsCollection(),
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    with pytest.raises(HTTPException) as exc:
        await admin_router.list_all_matches(
            response=Response(),
            league_id=None,
            status_filter=None,
            date_from=None,
            date_to=None,
            needs_review=None,
            odds_available=None,
            search="x" * 65,
            page=1,
            page_size=50,
            admin={"_id": ObjectId()},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_override_logs_before_after(monkeypatch):
    admin_router._ADMIN_MATCHES_CACHE.clear()
    match_id = ObjectId()
    league_id = ObjectId()
    match_doc = {
        "_id": match_id,
        "league_id": league_id,
        "league_id": 82,
        "home_team": "Bayern Munich",
        "away_team": "RB Leipzig",
        "match_date": datetime(2025, 8, 22, tzinfo=timezone.utc),
        "status": "final",
        "result": {"outcome": "1", "home_score": 2, "away_score": 1},
    }
    fake_matches = _FakeMatchesCollection([match_doc])
    fake_db = SimpleNamespace(matches=fake_matches)
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    captured = {}

    async def _fake_log_audit(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(admin_router, "log_audit", _fake_log_audit)
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    body = admin_router.ResultOverride(result="1", home_score=2, away_score=1)

    result = await admin_router.override_result(
        match_id=str(match_id),
        body=body,
        request=req,
        admin={"_id": ObjectId()},
    )
    assert "Result overridden" in result["message"]
    assert captured["action"] == "MATCH_OVERRIDE"
    assert "before" in captured["metadata"]
    assert "after" in captured["metadata"]
