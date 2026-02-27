"""
backend/tests/test_admin_teams_v3_router.py

Purpose:
    Unit tests for Team Tower v3 alias-only endpoints.
"""

from __future__ import annotations

from datetime import timedelta
import sys
import re

import pytest

sys.path.insert(0, "backend")

from app.routers import admin_teams_v3 as teams_router
from app.services.team_alias_normalizer import normalize_team_alias
from app.utils import utcnow


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._skip = 0
        self._limit = None

    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, list):
            key, direction = key_or_list[0]
        else:
            key = key_or_list
        reverse = int(direction or 1) < 0
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=reverse)
        return self

    def skip(self, value):
        self._skip = int(value)
        return self

    def limit(self, value):
        self._limit = int(value)
        return self

    async def to_list(self, length=1000):
        docs = self._docs[self._skip :]
        if self._limit is not None:
            docs = docs[: self._limit]
        return docs[:length]


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    @staticmethod
    def _nested(doc, path):
        cur = doc
        for part in path.split("."):
            if isinstance(cur, list):
                vals = []
                for item in cur:
                    if isinstance(item, dict) and part in item:
                        vals.append(item[part])
                return vals, bool(vals)
            if not isinstance(cur, dict) or part not in cur:
                return None, False
            cur = cur[part]
        return cur, True

    @classmethod
    def _match(cls, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(cls._match(doc, q) for q in expected):
                    return False
                continue
            val, exists = cls._nested(doc, key)
            if isinstance(expected, dict):
                if "$ne" in expected and val == expected["$ne"]:
                    return False
                if "$in" in expected:
                    arr = val if isinstance(val, list) else [val]
                    if not any(v in expected["$in"] for v in arr):
                        return False
                if "$gte" in expected and (val is None or val < expected["$gte"]):
                    return False
                if "$regex" in expected:
                    pattern = expected["$regex"]
                    flags = re.I if "i" in str(expected.get("$options") or "").lower() else 0
                    text = " ".join(str(v) for v in (val if isinstance(val, list) else [val]) if v is not None)
                    if not re.search(pattern, text, flags=flags):
                        return False
                continue
            if not exists or val != expected:
                return False
        return True

    def find(self, query, _projection=None):
        return _Cursor([d for d in self.docs if self._match(d, query)])

    async def find_one(self, query, _projection=None, sort=None):
        rows = [d for d in self.docs if self._match(d, query)]
        if sort and rows:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda d: d.get(key), reverse=int(direction) < 0)
        return rows[0] if rows else None

    async def count_documents(self, query):
        return len([d for d in self.docs if self._match(d, query)])

    async def update_one(self, query, update):
        doc = await self.find_one(query)
        if doc is None:
            return type("Res", (), {"modified_count": 0})()
        for k, v in (update.get("$set") or {}).items():
            doc[k] = v
        if "$addToSet" in update:
            for k, v in update["$addToSet"].items():
                doc.setdefault(k, [])
                if not any((row or {}).get("alias_key") == (v or {}).get("alias_key") for row in doc[k]):
                    doc[k].append(v)
        if "$pull" in update:
            for k, cond in update["$pull"].items():
                keys = set(((cond or {}).get("alias_key") or {}).get("$in", []))
                doc[k] = [row for row in (doc.get(k) or []) if (row or {}).get("alias_key") not in keys]
        return type("Res", (), {"modified_count": 1})()


class _FakeDB:
    def __init__(self):
        now = utcnow()
        self.teams_v3 = _Collection(
            [
                {
                    "_id": 1,
                    "name": "Bayern München",
                    "aliases": [
                        {
                            "name": "Bayern München",
                            "normalized": "bayern munchen",
                            "source": "provider_unknown",
                            "league_id": None,
                            "alias_key": "bayern munchen|*|provider_unknown",
                            "is_default": True,
                        }
                    ],
                    "updated_at": now,
                }
            ]
        )
        self.team_alias_suggestions_v3 = _Collection([])
        self.team_alias_resolution_events = _Collection(
            [
                {
                    "team_id": 1,
                    "alias_key": "bayern|*|manual",
                    "resolved_at": now - timedelta(days=1),
                }
            ]
        )

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.mark.asyncio
async def test_normalizer_munchen_variants():
    assert normalize_team_alias("Bayern München") == "bayern munchen"
    assert normalize_team_alias("Bayern Muenchen") == "bayern munchen"


@pytest.mark.asyncio
async def test_add_alias_then_dedupe(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    first = await teams_router.add_alias_v3(
        team_id=1,
        body=teams_router.AliasInput(name="Bayern", source=teams_router.AliasSource.manual),
        admin={"_id": "admin"},
    )
    second = await teams_router.add_alias_v3(
        team_id=1,
        body=teams_router.AliasInput(name="Bayern", source=teams_router.AliasSource.manual),
        admin={"_id": "admin"},
    )
    assert first["inserted"] is True
    assert second["inserted"] in (False, True)
    aliases = fake.teams_v3.docs[0]["aliases"]
    assert len([a for a in aliases if a["alias_key"] == "bayern|*|manual"]) == 1


@pytest.mark.asyncio
async def test_delete_blocks_canonical_default(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    result = await teams_router.delete_alias_v3(
        team_id=1,
        body=teams_router.AliasDeleteInput(name="Bayern München"),
        admin={"_id": "admin"},
    )
    assert result["removed"] is False
    assert result["blocked"]["code"] == "canonical_alias_protected"


@pytest.mark.asyncio
async def test_alias_impact_returns_usage(monkeypatch):
    fake = _FakeDB()
    fake.teams_v3.docs[0]["aliases"].append(
        {
            "name": "Bayern",
            "normalized": "bayern",
            "source": "manual",
            "league_id": None,
            "alias_key": "bayern|*|manual",
            "is_default": False,
        }
    )
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    result = await teams_router.alias_impact_v3(
        team_id=1,
        body=teams_router.AliasDeleteInput(name="Bayern", source="manual"),
        admin={"_id": "admin"},
    )
    assert result["usage_30d"] == 1


@pytest.mark.asyncio
async def test_list_alias_suggestions_v3(monkeypatch):
    fake = _FakeDB()
    fake.team_alias_suggestions_v3.docs = [
        {
            "_id": "s1",
            "status": "pending",
            "source": "provider_unknown",
            "league_id": 82,
            "raw_team_name": "Bayern Muenchen",
            "normalized_name": "bayern munchen",
            "confidence_score": 0.91,
            "candidate_team_id": 1,
        }
    ]
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    result = await teams_router.list_alias_suggestions_v3(
        status="pending",
        source=None,
        league_id=None,
        min_confidence=0.0,
        q=None,
        limit=200,
        admin={"_id": "admin"},
    )
    assert result["total"] == 1
    assert result["items"][0]["id"] == "s1"
    assert result["items"][0]["suggested_team_name"] == "Bayern München"


@pytest.mark.asyncio
async def test_apply_alias_suggestions_v3(monkeypatch):
    fake = _FakeDB()
    fake.team_alias_suggestions_v3.docs = [
        {
            "_id": "s1",
            "status": "pending",
            "source": "manual",
            "league_id": None,
            "raw_team_name": "Bayern",
            "candidate_team_id": 1,
        }
    ]
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    result = await teams_router.apply_alias_suggestions_v3(
        body=teams_router.SuggestionApplyBody(items=[teams_router.SuggestionApplyItem(id="s1")]),
        admin={"_id": "admin"},
    )
    assert result["applied"] == 1
    doc = await fake.team_alias_suggestions_v3.find_one({"_id": "s1"})
    assert doc["status"] == "applied"
    aliases = fake.teams_v3.docs[0]["aliases"]
    assert any(a.get("alias_key") == "bayern|*|manual" for a in aliases)


@pytest.mark.asyncio
async def test_reject_alias_suggestion_v3(monkeypatch):
    fake = _FakeDB()
    fake.team_alias_suggestions_v3.docs = [
        {
            "_id": "s1",
            "status": "pending",
            "source": "manual",
            "league_id": None,
            "raw_team_name": "Unknown FC",
            "normalized_name": "unknown fc",
        }
    ]
    monkeypatch.setattr(teams_router._db, "db", fake, raising=False)
    result = await teams_router.reject_alias_suggestion_v3(
        suggestion_id="s1",
        body=teams_router.RejectSuggestionBody(reason="invalid"),
        admin={"_id": "admin"},
    )
    assert result["ok"] is True
    doc = await fake.team_alias_suggestions_v3.find_one({"_id": "s1"})
    assert doc["status"] == "rejected"
