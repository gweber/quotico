"""
backend/tests/test_league_navigation_v3.py

Purpose:
    Verify public league navigation is sourced from league_registry_v3 only
    and respects greenfield visibility flags.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "backend")

from app.services import league_service


class _Cursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, sort_fields):
        for field, direction in reversed(sort_fields):
            reverse = int(direction) < 0
            self._docs.sort(key=lambda row: row.get(field), reverse=reverse)
        return self

    async def to_list(self, length=500):
        return self._docs[:length]


class _Collection:
    def __init__(self, docs: list[dict]):
        self.docs = list(docs)

    def find(self, query: dict, projection: dict | None = None):
        items = []
        for row in self.docs:
            if row.get("is_active") is not query.get("is_active"):
                continue
            features = row.get("features") or {}
            if features.get("tipping") is not True:
                continue
            if projection:
                items.append({k: row.get(k) for k in projection.keys()})
            else:
                items.append(dict(row))
        return _Cursor(items)


@pytest.mark.asyncio
async def test_navigation_reads_from_league_registry_v3(monkeypatch):
    league_service._nav_cache_data = None
    league_service._nav_cache_expires_at = None

    fake_db = SimpleNamespace(
        league_registry_v3=_Collection(
            [
                {
                    "_id": 1,
                    "league_id": 82,
                    "name": "Bundesliga",
                    "country": "Germany",
                    "country_code": "DE",
                    "ui_order": 2,
                    "is_active": True,
                    "features": {"tipping": True},
                },
                {
                    "_id": 2,
                    "league_id": 8,
                    "name": "Premier League",
                    "country": "England",
                    "country_code": "GB",
                    "ui_order": 1,
                    "is_active": True,
                    "features": {"tipping": False},
                },
                {
                    "_id": 3,
                    "league_id": 564,
                    "name": "La Liga",
                    "country": "Spain",
                    "country_code": "ES",
                    "ui_order": 3,
                    "is_active": False,
                    "features": {"tipping": True},
                },
            ]
        )
    )

    monkeypatch.setattr(league_service._db, "db", fake_db, raising=False)

    items = await league_service.get_active_navigation()

    assert len(items) == 1
    assert items[0]["league_id"] == "82"
    assert items[0]["name"] == "Bundesliga"
