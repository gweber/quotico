"""
backend/tests/test_admin_alias_suggestions.py

Purpose:
    Router-level tests for Team Tower alias suggestion listing, applying, and rejecting
    via the centralized team_alias_suggestions collection.
"""

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.routers import admin as admin_router


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, size):
        self._docs = self._docs[:size]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _FakeSuggestionsCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query=None, projection=None):
        query = query or {}
        out = []
        for doc in self.docs:
            ok = True
            for key, expected in query.items():
                if key == "$or":
                    continue
                if doc.get(key) != expected:
                    ok = False
                    break
            if ok:
                out.append(dict(doc))
        return _Cursor(out)

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        for idx, doc in enumerate(self.docs):
            if doc.get("_id") == query.get("_id"):
                set_part = update.get("$set", {})
                merged = dict(doc)
                merged.update(set_part)
                self.docs[idx] = merged
                return SimpleNamespace(upserted_id=None)
        if upsert:
            new_doc = {"_id": ObjectId(), **query}
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
            return SimpleNamespace(upserted_id=new_doc["_id"])
        return SimpleNamespace(upserted_id=None)

    async def delete_one(self, query):
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if doc.get("_id") != query.get("_id")]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def delete_many(self, query):
        before = len(self.docs)
        status = query.get("status")
        normalized_name = query.get("normalized_name")
        allowed_sport_keys = [item.get("sport_key") for item in (query.get("$or") or []) if isinstance(item, dict)]

        def _matches(doc):
            if status is not None and doc.get("status") != status:
                return False
            if normalized_name is not None and doc.get("normalized_name") != normalized_name:
                return False
            if allowed_sport_keys:
                return doc.get("sport_key") in allowed_sport_keys
            return True

        self.docs = [doc for doc in self.docs if not _matches(doc)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


class _FakeTeamsCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        selected = [d for d in self.docs if d.get("_id") in ids]
        if projection:
            projected = []
            for row in selected:
                projected.append({k: v for k, v in row.items() if k in projection or k == "_id"})
            return _Cursor(projected)
        return _Cursor(selected)

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None


class _FakeLeaguesCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query, projection=None):
        ids = set(query.get("_id", {}).get("$in", []))
        selected = [d for d in self.docs if d.get("_id") in ids]
        if projection:
            projected = []
            for row in selected:
                projected.append({k: v for k, v in row.items() if k in projection or k == "_id"})
            return _Cursor(projected)
        return _Cursor(selected)


@pytest.mark.asyncio
async def test_list_alias_suggestions(monkeypatch):
    league_id = ObjectId()
    team_id = ObjectId()
    suggestion_id = ObjectId()
    fake_db = SimpleNamespace(
        team_alias_suggestions=_FakeSuggestionsCollection(
            [
                {
                    "_id": suggestion_id,
                    "status": "pending",
                    "source": "openligadb",
                    "sport_key": "soccer_germany_bundesliga",
                    "league_id": league_id,
                    "raw_team_name": "Bayern München",
                    "normalized_name": "bayern munchen",
                    "reason": "name_mismatch",
                    "seen_count": 2,
                    "first_seen_at": admin_router.utcnow(),
                    "last_seen_at": admin_router.utcnow(),
                    "suggested_team_id": team_id,
                    "suggested_team_name": "Bayern Munich",
                    "sample_refs": [],
                }
            ]
        ),
        teams=_FakeTeamsCollection(
            [
                {
                    "_id": team_id,
                    "display_name": "Bayern Munich",
                    "aliases": [],
                }
            ]
        ),
        leagues=_FakeLeaguesCollection(
            [
                {"_id": league_id, "name": "Bundesliga", "sport_key": "soccer_germany_bundesliga"},
            ]
        ),
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)
    admin = {"_id": ObjectId()}
    result = await admin_router.list_alias_suggestions_admin(limit=50, admin=admin)
    assert result["total"] == 1
    assert result["items"][0]["raw_team_name"] == "Bayern München"
    assert result["items"][0]["league_name"] == "Bundesliga"


@pytest.mark.asyncio
async def test_apply_alias_suggestion(monkeypatch):
    suggestion_id = ObjectId()
    team_id = ObjectId()
    fake_db = SimpleNamespace(
        team_alias_suggestions=_FakeSuggestionsCollection(
            [
                {
                    "_id": suggestion_id,
                    "status": "pending",
                    "sport_key": "soccer_germany_bundesliga",
                    "raw_team_name": "Bayern München",
                    "suggested_team_id": team_id,
                }
            ]
        )
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    class _FakeRegistry:
        async def add_alias(self, *_args, **_kwargs):
            return True

        async def initialize(self):
            return None

    monkeypatch.setattr(admin_router.TeamRegistry, "get", staticmethod(lambda: _FakeRegistry()))

    async def _noop_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_router, "log_audit", _noop_audit)

    body = admin_router.ApplyAliasSuggestionsBody(
        items=[admin_router.AliasSuggestionApplyInput(id=str(suggestion_id))]
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    result = await admin_router.apply_alias_suggestions_admin(body=body, request=request, admin=admin)
    assert result["applied"] == 1
    updated = await fake_db.team_alias_suggestions.find_one({"_id": suggestion_id})
    assert updated is None


@pytest.mark.asyncio
async def test_reject_alias_suggestion(monkeypatch):
    suggestion_id = ObjectId()
    fake_db = SimpleNamespace(
        team_alias_suggestions=_FakeSuggestionsCollection(
            [
                {
                    "_id": suggestion_id,
                    "status": "pending",
                    "raw_team_name": "Unknown FC",
                }
            ]
        )
    )
    monkeypatch.setattr(admin_router._db, "db", fake_db, raising=False)

    async def _noop_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_router, "log_audit", _noop_audit)

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    admin = {"_id": ObjectId()}
    result = await admin_router.reject_alias_suggestion_admin(
        suggestion_id=str(suggestion_id),
        body=admin_router.RejectAliasSuggestionBody(reason="not relevant"),
        request=request,
        admin=admin,
    )
    assert result["ok"] is True
    updated = await fake_db.team_alias_suggestions.find_one({"_id": suggestion_id})
    assert updated["status"] == "rejected"
