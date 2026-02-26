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
async def test_admin_import_endpoint_is_disabled(monkeypatch):
    _ = monkeypatch
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    body = admin_router.FootballDataImportRequest(season="2425", dry_run=False)

    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_league_football_data_import_admin(
            league_id=str(ObjectId()),
            body=body,
            request=request,
            admin=admin,
        )
    assert exc.value.status_code == 410
    assert "Legacy endpoint disabled in v3.1" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_admin_import_endpoint_invalid_league_id(monkeypatch):
    _ = monkeypatch
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

    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_admin_async_import_start_and_status(monkeypatch):
    _ = monkeypatch
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    body = admin_router.FootballDataImportRequest(season="2526", dry_run=True)
    from fastapi import BackgroundTasks
    background = BackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        await admin_router.trigger_league_football_data_import_async_admin(
            league_id=str(ObjectId()),
            body=body,
            background_tasks=background,
            request=request,
            admin=admin,
        )
    assert exc.value.status_code == 410

    with pytest.raises(HTTPException) as status_exc:
        await admin_router.get_league_import_job_status_admin(str(ObjectId()), admin=admin)
    assert status_exc.value.status_code == 410
