"""
backend/tests/test_provider_secret_versioning.py

Purpose:
    Ensure provider secret key_version increases monotonically on rotations.

Dependencies:
    - app.services.provider_settings_service
"""

from __future__ import annotations

import pytest

from app.services import provider_settings_service as service_module
from app.services.provider_settings_service import ProviderSettingsService


def _match(doc: dict, query: dict) -> bool:
    for key, value in query.items():
        if doc.get(key) != value:
            return False
    return True


class _Collection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if _match(doc, query):
                if not projection:
                    return dict(doc)
                return {k: doc.get(k) for k in projection if k in doc}
        return None

    async def update_one(self, query, update, upsert=False):
        for idx, doc in enumerate(self.docs):
            if _match(doc, query):
                next_doc = dict(doc)
                next_doc.update(update.get("$set", {}))
                self.docs[idx] = next_doc
                return
        if upsert:
            payload = dict(query)
            payload.update(update.get("$setOnInsert", {}))
            payload.update(update.get("$set", {}))
            self.docs.append(payload)

    async def delete_one(self, query):
        self.docs = [doc for doc in self.docs if not _match(doc, query)]


class _FakeDb:
    def __init__(self):
        self.provider_settings = _Collection()
        self.provider_secrets = _Collection()
        self.leagues = _Collection()


@pytest.mark.asyncio
async def test_provider_secret_version_increments(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(service_module._db, "db", fake_db, raising=False)
    service = ProviderSettingsService()

    first = await service.set_secret(
        "football_data",
        api_key="key-one",
        scope="global",
        actor_id="admin-1",
    )
    second = await service.set_secret(
        "football_data",
        api_key="key-two",
        scope="global",
        actor_id="admin-1",
    )

    assert first["key_version"] == 1
    assert second["key_version"] == 2

