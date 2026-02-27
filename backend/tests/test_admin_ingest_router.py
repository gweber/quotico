"""
backend/tests/test_admin_ingest_router.py

Purpose:
    Unit tests for admin ingest router cache behavior, season job lock, and
    stale/can_retry status fields.
"""

from __future__ import annotations

from datetime import timedelta
import sys

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks, HTTPException

sys.path.insert(0, "backend")

from app.routers import admin_ingest as ingest_router
from app.utils import utcnow


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=1000):
        return self._docs[:length]


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, _query, _projection=None):
        return _Cursor([doc for doc in self.docs if self._matches(doc, _query)])

    @staticmethod
    def _get_nested(doc, path: str):
        cur = doc
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None, False
            cur = cur[part]
        return cur, True

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            value, exists = _Collection._get_nested(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$exists" in expected and bool(expected["$exists"]) != bool(exists):
                    return False
                if "$ne" in expected and value == expected["$ne"]:
                    return False
                if "$gte" in expected:
                    if value is None or value < expected["$gte"]:
                        return False
                continue
            if not exists or value != expected:
                return False
        return True

    async def find_one(self, query, _projection=None, sort=None):
        matches = [doc for doc in self.docs if self._matches(doc, query)]
        if sort and matches:
            key, direction = sort[0]
            reverse = int(direction) < 0
            matches = sorted(matches, key=lambda d: self._get_nested(d, key)[0], reverse=reverse)
        for doc in matches:
            return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        doc = None
        for row in self.docs:
            if self._matches(row, query):
                doc = row
                break
        if doc is None and upsert:
            doc = {"_id": query.get("_id")}
            self.docs.append(doc)
        if doc is not None:
            for key, value in (update.get("$set") or {}).items():
                doc[key] = value

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return type("InsertResult", (), {"inserted_id": doc.get("_id", ObjectId())})()

    async def count_documents(self, query, limit=0):
        hits = [doc for doc in self.docs if self._matches(doc, query)]
        if limit and len(hits) > limit:
            return limit
        return len(hits)


class _FakeDB:
    def __init__(self, *, leagues=None, meta=None, jobs=None, matches_v3=None):
        self.league_registry_v3 = _Collection(leagues)
        self.meta = _Collection(meta)
        self.admin_import_jobs = _Collection(jobs)
        self.matches_v3 = _Collection(matches_v3)
        self.users = _Collection([])
        self.betting_slips = _Collection([])


@pytest.mark.asyncio
async def test_discovery_uses_cache_when_not_stale(monkeypatch):
    now = utcnow()
    fake_db = _FakeDB(
        leagues=[
            {
                "_id": 8,
                "sport_key": "soccer_germany_bundesliga",
                "name": "Bundesliga",
                "country": "Germany",
                "is_cup": False,
                "is_active": True,
                "needs_review": False,
                "ui_order": 1,
                "features": {"tipping": True, "match_load": True, "xg_sync": True, "odds_sync": True},
                "available_seasons": [{"id": 1, "name": "2024/2025"}],
                "last_synced_at": now,
            }
        ]
    )
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)

    async def _should_not_call():
        raise AssertionError("External discovery must not run on valid cache")

    monkeypatch.setattr(ingest_router.sportmonks_connector, "get_available_leagues", _should_not_call)
    result = await ingest_router.discover_leagues(force=False, admin={"_id": ObjectId()})
    assert result["source"] == "cache"
    assert len(result["items"]) == 1
    assert result["items"][0]["is_active"] is True
    assert result["items"][0]["features"]["tipping"] is True


@pytest.mark.asyncio
async def test_patch_discovery_league_updates_runtime_flags(monkeypatch):
    now = utcnow()
    fake_db = _FakeDB(
        leagues=[
            {
                "_id": 8,
                "sport_key": "soccer_germany_bundesliga",
                "name": "Bundesliga",
                "country": "Germany",
                "is_cup": False,
                "is_active": False,
                "needs_review": False,
                "ui_order": 10,
                "features": {"tipping": False, "match_load": True, "xg_sync": False, "odds_sync": False},
                "available_seasons": [{"id": 1, "name": "2024/2025"}],
                "last_synced_at": now,
            }
        ]
    )
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)
    invalidated = {"hit": False}

    async def _invalidate():
        invalidated["hit"] = True

    monkeypatch.setattr(ingest_router, "invalidate_navigation_cache", _invalidate)
    body = ingest_router.IngestLeaguePatchBody(
        is_active=True,
        features=ingest_router.IngestLeagueFeaturesPatchBody(tipping=True),
    )
    result = await ingest_router.patch_discovery_league(
        league_id=8,
        body=body,
        admin={"_id": ObjectId()},
    )
    assert result["item"]["is_active"] is True
    assert result["item"]["features"]["tipping"] is True
    assert invalidated["hit"] is True


@pytest.mark.asyncio
async def test_season_job_lock_returns_conflict(monkeypatch):
    active_job = {"_id": ObjectId(), "type": "sportmonks_deep_ingest", "season_id": 2025, "status": "running"}
    fake_db = _FakeDB(jobs=[active_job])
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)
    with pytest.raises(HTTPException) as exc:
        await ingest_router.start_season_ingest(
            season_id=2025,
            background_tasks=BackgroundTasks(),
            admin={"_id": ObjectId()},
        )
    assert exc.value.status_code == 409
    assert str(active_job["_id"]) == exc.value.detail["active_job_id"]


@pytest.mark.asyncio
async def test_job_status_marks_running_job_as_stale(monkeypatch):
    job_id = ObjectId()
    old = utcnow() - timedelta(minutes=10)
    fake_db = _FakeDB(
        jobs=[
            {
                "_id": job_id,
                "type": "sportmonks_deep_ingest",
                "status": "running",
                "phase": "ingesting_round",
                "season_id": 2025,
                "updated_at": old,
                "created_at": old,
                "progress": {"processed": 1, "total": 10, "percent": 10.0},
                "error_log": [],
            }
        ]
    )
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)
    result = await ingest_router.get_ingest_job(job_id=str(job_id), admin={"_id": ObjectId()})
    assert result["is_stale"] is True
    assert result["can_retry"] is True


@pytest.mark.asyncio
async def test_metrics_sync_requires_deep_ingest_precondition(monkeypatch):
    fake_db = _FakeDB(matches_v3=[])
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)
    with pytest.raises(HTTPException) as exc:
        await ingest_router.start_metrics_sync(
            season_id=25536,
            background_tasks=BackgroundTasks(),
            admin={"_id": ObjectId()},
        )
    assert exc.value.status_code == 428


@pytest.mark.asyncio
async def test_metrics_health_counts_coverage(monkeypatch):
    fake_db = _FakeDB(
        matches_v3=[
            {
                "_id": 1,
                "season_id": 25536,
                "has_advanced_stats": True,
                "odds_meta": {"summary_1x2": {"home": {"avg": 2.0}, "draw": {"avg": 3.2}, "away": {"avg": 3.4}}},
            },
            {
                "_id": 2,
                "season_id": 25536,
                "has_advanced_stats": False,
            },
        ]
    )
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)
    result = await ingest_router.get_metrics_health(25536, admin={"_id": ObjectId()})
    assert result["total_matches"] == 2
    assert result["xg_covered_matches"] == 1
    assert result["xg_coverage_percent"] == 50.0


@pytest.mark.asyncio
async def test_overview_stats_returns_v3_shape(monkeypatch):
    now = utcnow()
    fake_db = _FakeDB(
        meta=[{"_id": "sportmonks_rate_limit_state", "remaining": 77, "reset_at": now}],
        jobs=[
            {"_id": ObjectId(), "status": "queued"},
            {"_id": ObjectId(), "status": "running"},
            {"_id": ObjectId(), "status": "failed"},
        ],
        matches_v3=[
            {
                "_id": 1,
                "status": "FINISHED",
                "updated_at": now,
                "has_advanced_stats": True,
                "odds_meta": {"summary_1x2": {"home": {"avg": 2.1}, "draw": {"avg": 3.4}, "away": {"avg": 3.6}}},
            },
            {"_id": 2, "status": "LIVE", "updated_at": now},
            {"_id": 3, "status": "POSTPONED", "updated_at": now},
            {"_id": 4, "status": "CANCELED", "updated_at": now},
            {"_id": 5, "status": "WALKOVER", "updated_at": now},
            {"_id": 6, "status": "SCHEDULED", "updated_at": now},
        ],
    )
    fake_db.users = _Collection([{"_id": 1}, {"_id": 2, "is_banned": True}])
    fake_db.betting_slips = _Collection([{"_id": 1, "created_at": now}, {"_id": 2, "created_at": now}])
    monkeypatch.setattr(ingest_router._db, "db", fake_db, raising=False)

    result = await ingest_router.get_admin_overview_stats(admin={"_id": ObjectId()})
    assert result.matches_v3.total == 6
    assert result.matches_v3.finished == 1
    assert result.matches_v3.live == 1
    assert result.matches_v3.postponed == 1
    assert result.matches_v3.canceled == 1
    assert result.matches_v3.walkover == 1
    assert result.matches_v3.with_xg == 1
    assert result.matches_v3.with_odds == 1
    assert result.sportmonks_api.remaining == 77
