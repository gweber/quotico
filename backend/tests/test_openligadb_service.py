"""
backend/tests/test_openligadb_service.py

Purpose:
    Service tests for OpenLigaDB season import dry-run alias suggestions.
"""

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import openligadb_service as service


class _FakeLeagues:
    def __init__(self, league):
        self.league = league

    async def find_one(self, query):
        if query.get("_id") == self.league["_id"]:
            return dict(self.league)
        return None


class _FakeTeams:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if query.get("external_ids.openligadb") and doc.get("external_ids", {}).get("openligadb") == query.get("external_ids.openligadb"):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None


class _FakeMatches:
    async def find_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_openligadb_dry_run_generates_alias_suggestions(monkeypatch):
    league_id = ObjectId()
    team_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeagues(
            {
                "_id": league_id,
                "sport_key": "soccer_germany_bundesliga",
                "external_ids": {"openligadb": "bl1"},
                "is_active": True,
            }
        ),
        teams=_FakeTeams(
            [
                {
                    "_id": team_id,
                    "display_name": "Bayern Munich",
                    "sport_key": "soccer_germany_bundesliga",
                    "external_ids": {"openligadb": "40"},
                }
            ]
        ),
        matches=_FakeMatches(),
    )
    monkeypatch.setattr(service._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return {"is_active": True}

    class _FakeTeamRegistry:
        async def resolve_by_external_id_or_name(self, **kwargs):
            if kwargs.get("external_id") == "40":
                return None
            return ObjectId()

    class _FakeProvider:
        async def get_season_matches(self, *_args, **_kwargs):
            return [
                {
                    "match_id": "100",
                    "utc_date": "2025-08-10T14:00:00Z",
                    "home_team_id": "40",
                    "home_team_name": "Bayern München",
                    "away_team_id": "11",
                    "away_team_name": "Augsburg",
                    "matchday": 1,
                    "is_finished": False,
                    "season": 2025,
                }
            ]

    monkeypatch.setattr(service.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(service.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(service, "openligadb_provider", _FakeProvider())
    result = await service.import_season(league_id, 2025, dry_run=True)
    assert result["skipped_conflicts"] == 1
    assert len(result.get("alias_suggestions", [])) == 1
    assert result["alias_suggestions"][0]["provider"] == "openligadb"

