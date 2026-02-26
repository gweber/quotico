"""
backend/tests/test_football_data_uk_season_year_input.py

Purpose:
    Ensure year-style season input is translated to football-data.co.uk code
    during import execution.

Dependencies:
    - app.services.football_data_service
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bson import ObjectId

from app.services import football_data_service as fds


class _FakeLeaguesCollection:
    def __init__(self, league_doc: dict):
        self._league_doc = league_doc

    async def find_one(self, query):
        if query.get("_id") == self._league_doc.get("_id"):
            return dict(self._league_doc)
        return None


class _FakeMatchesCollection:
    async def find_one(self, *_args, **_kwargs):
        return None

    async def update_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_import_accepts_start_year_and_converts_to_code(monkeypatch):
    league_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2025,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=_FakeMatchesCollection(),
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    seen: dict[str, str] = {}

    class _FakeProvider:
        async def fetch_season_csv(self, season, division):
            seen["season"] = season
            seen["division"] = division
            return "Date,HomeTeam,AwayTeam\n"

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FakeProvider())

    result = await fds.import_football_data_stats(league_id, season="2025")

    assert seen["season"] == "2526"
    assert seen["division"] == "E0"
    assert result["season"] == "2526"

