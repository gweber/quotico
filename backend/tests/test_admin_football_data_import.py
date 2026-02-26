"""
backend/tests/test_admin_football_data_import.py

Purpose:
    Router-level tests for admin football-data import endpoint behavior, including
    rate limiting and response contract.

Dependencies:
    - pytest
    - app.routers.admin
"""

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, "backend")

from app.routers import admin as admin_router


class _FakeMetaCollection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query, projection=None):
        doc = self.docs.get(query.get("_id"))
        if not doc:
            return None
        if projection:
            return {k: v for k, v in doc.items() if k in projection or k == "_id"}
        return dict(doc)

    async def update_one(self, query, update):
        doc = self.docs.get(query.get("_id"))
        if not doc:
            return
        for key, value in update.get("$set", {}).items():
            doc[key] = value

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = dict(doc)


class _FakeLeaguesCollection:
    def __init__(self, league_doc):
        self.league_doc = dict(league_doc)

    async def find_one(self, query, projection=None):
        if query.get("_id") != self.league_doc["_id"]:
            return None
        if projection:
            return {k: v for k, v in self.league_doc.items() if k in projection or k == "_id"}
        return dict(self.league_doc)

    async def update_one(self, query, update):
        if query.get("_id") != self.league_doc["_id"]:
            return
        for key, value in update.get("$set", {}).items():
            self.league_doc[key] = value


class _FakeImportJobsCollection:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        oid = ObjectId()
        self.docs[oid] = dict(doc)
        self.docs[oid]["_id"] = oid
        return SimpleNamespace(inserted_id=oid)

    async def find_one(self, query):
        return self.docs.get(query.get("_id"))

    async def update_one(self, query, update):
        doc = self.docs.get(query.get("_id"))
        if not doc:
            return
        for key, value in update.get("$set", {}).items():
            doc[key] = value


@pytest.mark.asyncio
async def test_admin_import_endpoint_success_and_rate_limit(monkeypatch):
    league_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection({"_id": league_id}),
        meta=_FakeMetaCollection(),
        admin_import_jobs=_FakeImportJobsCollection(),
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    async def _fake_import(*_args, **kwargs):
        return {
            "processed": 1,
            "matched": 1,
            "updated": 1,
            "season": kwargs.get("season") or "2425",
            "division": "E0",
            "odds_snapshots_total": 3,
            "odds_providers_seen": 1,
            "odds_ingest_inserted": 3,
            "odds_ingest_deduplicated": 0,
            "odds_ingest_markets_updated": 3,
            "odds_ingest_errors": 0,
        }

    async def _noop_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_router, "import_football_data_stats", _fake_import)
    monkeypatch.setattr(admin_router, "log_audit", _noop_audit)

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}

    body = admin_router.FootballDataImportRequest(season="2425", dry_run=False)
    response = await admin_router.trigger_league_football_data_import_admin(
        league_id=str(league_id),
        body=body,
        request=request,
        admin=admin,
    )

    assert response["success"] is True
    assert response["league_id"] == str(league_id)
    assert response["dry_run"] is False
    assert response["results"]["updated"] == 1

    # Immediate second call should be rate-limited.
    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_league_football_data_import_admin(
            league_id=str(league_id),
            body=body,
            request=request,
            admin=admin,
        )
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_admin_import_endpoint_invalid_league_id(monkeypatch):
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection({"_id": ObjectId()}),
        meta=_FakeMetaCollection(),
        admin_import_jobs=_FakeImportJobsCollection(),
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    body = admin_router.FootballDataImportRequest(season="2425", dry_run=True)

    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_league_football_data_import_admin(
            league_id="invalid",
            body=body,
            request=request,
            admin=admin,
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_admin_async_import_start_and_status(monkeypatch):
    league_id = ObjectId()
    jobs = _FakeImportJobsCollection()
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection({"_id": league_id}),
        meta=_FakeMetaCollection(),
        admin_import_jobs=jobs,
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    async def _noop_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_router, "log_audit", _noop_audit)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    body = admin_router.FootballDataImportRequest(season="2526", dry_run=True)
    from fastapi import BackgroundTasks
    background = BackgroundTasks()

    start = await admin_router.trigger_league_football_data_import_async_admin(
        league_id=str(league_id),
        body=body,
        background_tasks=background,
        request=request,
        admin=admin,
    )
    assert start["accepted"] is True
    assert start["status"] == "queued"
    assert start["job_id"]

    status = await admin_router.get_league_import_job_status_admin(start["job_id"], admin=admin)
    assert status["status"] == "queued"
    assert status["phase"] == "queued"
