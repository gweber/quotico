"""
backend/tests/test_admin_season_import_jobs.py

Purpose:
    Router-level tests for new async season import endpoints.
"""

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks
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


class _FakeImportJobsCollection:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        oid = ObjectId()
        stored = dict(doc)
        stored["_id"] = oid
        self.docs[oid] = stored
        return SimpleNamespace(inserted_id=oid)


@pytest.mark.asyncio
async def test_start_unified_match_ingest_job(monkeypatch):
    _ = monkeypatch
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}

    body = admin_router.UnifiedMatchIngestRequest(source="football_data", season=2025, dry_run=True)
    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_unified_match_ingest_async_admin(
            league_id=str(ObjectId()),
            body=body,
            background_tasks=BackgroundTasks(),
            request=request,
            admin=admin,
        )
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_unified_match_ingest_rejects_theoddsapi(monkeypatch):
    _ = monkeypatch
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    body = admin_router.UnifiedMatchIngestRequest(source="theoddsapi", season=2025, dry_run=True)

    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_unified_match_ingest_async_admin(
            league_id=str(ObjectId()),
            body=body,
            background_tasks=BackgroundTasks(),
            request=request,
            admin=admin,
        )
    assert exc.value.status_code == 410
