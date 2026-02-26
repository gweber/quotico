"""
backend/tests/test_provider_cache_invalidation.py

Purpose:
    Verify provider settings cache behavior and explicit invalidation.

Dependencies:
    - app.services.provider_settings_service
"""

from __future__ import annotations

from types import SimpleNamespace

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


class _FakeDb:
    def __init__(self):
        self.provider_settings = _Collection()
        self.provider_secrets = _Collection()
        self.leagues = _Collection()


@pytest.mark.asyncio
async def test_provider_settings_cache_invalidation(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(service_module._db, "db", fake_db, raising=False)
    monkeypatch.setattr(service_module.settings, "PROVIDER_SETTINGS_CACHE_TTL", 60, raising=False)

    service = ProviderSettingsService()
    await service.set_settings(
        "openligadb",
        {"base_url": "https://one.example", "enabled": True},
        scope="global",
        actor_id="admin-1",
    )

    first = await service.get_effective("openligadb", include_secret=False)
    assert first["effective_config"]["base_url"] == "https://one.example"

    # Simulate external DB mutation while cache is hot.
    fake_db.provider_settings.docs[0]["base_url"] = "https://two.example"
    cached = await service.get_effective("openligadb", include_secret=False)
    assert cached["effective_config"]["base_url"] == "https://one.example"

    await service.invalidate(provider="openligadb")
    refreshed = await service.get_effective("openligadb", include_secret=False)
    assert refreshed["effective_config"]["base_url"] == "https://two.example"

