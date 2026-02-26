"""
backend/tests/test_ws_router_auth.py

Purpose:
    Authentication helper tests for the WebSocket event endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId
from jwt.exceptions import InvalidTokenError

sys.path.insert(0, "backend")

from app.routers import ws as ws_router


class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    async def find_one(self, query, projection=None):
        if "jti" in query:
            for doc in self.docs:
                if doc.get("jti") == query.get("jti"):
                    return dict(doc)
            return None
        for doc in self.docs:
            if doc.get("_id") == query.get("_id") and doc.get("is_deleted") == query.get("is_deleted"):
                return dict(doc)
        return None


@pytest.mark.asyncio
async def test_resolve_ws_user_with_valid_token(monkeypatch):
    user_id = ObjectId()
    fake_db = SimpleNamespace(
        access_blocklist=_FakeCollection([]),
        users=_FakeCollection([{"_id": user_id, "is_deleted": False, "is_banned": False}]),
    )
    monkeypatch.setattr(ws_router._db, "db", fake_db, raising=False)
    monkeypatch.setattr(
        ws_router,
        "decode_jwt",
        lambda token: {"sub": str(user_id), "type": "access", "jti": "ok"},
    )
    user = await ws_router._resolve_ws_user("valid")
    assert user is not None
    assert str(user["_id"]) == str(user_id)


@pytest.mark.asyncio
async def test_resolve_ws_user_invalid_token(monkeypatch):
    monkeypatch.setattr(ws_router, "decode_jwt", lambda token: (_ for _ in ()).throw(InvalidTokenError("bad")))
    user = await ws_router._resolve_ws_user("invalid")
    assert user is None


def test_token_from_ws_prefers_cookie():
    class _WS:
        cookies = {"access_token": "cookie-token"}
        query_params = {"token": "query-token"}

    assert ws_router._token_from_ws(_WS()) == "cookie-token"
