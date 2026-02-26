"""
backend/tests/test_football_data_org_service.py

Purpose:
    Service tests for football-data.org season import dry-run alias suggestions.
"""

from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import football_data_org_service as service


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
            if query.get("external_ids.football_data") and doc.get("external_ids", {}).get("football_data") == query.get("external_ids.football_data"):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None


class _FakeMatches:
    async def find_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_football_data_dry_run_generates_alias_suggestions(monkeypatch):
    league_id = ObjectId()
    team_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeagues(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "external_ids": {"football_data": "PL"},
                "is_active": True,
            }
        ),
        teams=_FakeTeams(
            [
                {
                    "_id": team_id,
                    "display_name": "Arsenal",
                    "sport_key": "soccer_epl",
                    "external_ids": {"football_data": "57"},
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
            if kwargs.get("external_id") == "57":
                return None
            return ObjectId()

    class _FakeProvider:
        async def get_season_matches(self, *_args, **_kwargs):
            return [
                {
                    "match_id": "200",
                    "utc_date": "2025-08-10T14:00:00Z",
                    "status_raw": "SCHEDULED",
                    "matchday": 1,
                    "season": 2025,
                    "stage": "REGULAR_SEASON",
                    "group": None,
                    "home_team_id": "57",
                    "home_team_name": "Arsenal FC",
                    "away_team_id": "61",
                    "away_team_name": "Chelsea FC",
                    "score": {"full_time": {"home": None, "away": None}},
                }
            ]

    monkeypatch.setattr(service.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(service.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(service, "football_data_provider", _FakeProvider())
    result = await service.import_season(league_id, 2025, dry_run=True)
    assert result["skipped_conflicts"] == 1
    assert len(result.get("alias_suggestions", [])) == 1
    assert result["alias_suggestions"][0]["provider"] == "football_data"

