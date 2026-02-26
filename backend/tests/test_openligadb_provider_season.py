"""
backend/tests/test_openligadb_provider_season.py

Purpose:
    Unit tests for OpenLigaDB season-fetch normalization.
"""

from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, "backend")

from app.providers.openligadb import OpenLigaDBProvider


@pytest.mark.asyncio
async def test_get_season_matches_normalizes_fields():
    provider = OpenLigaDBProvider()

    payload = [
        {
            "matchId": 123,
            "matchDateTimeUTC": "2024-08-23T18:30:00Z",
            "matchIsFinished": True,
            "group": {"groupOrderID": 1},
            "team1": {"teamId": 40, "teamName": "FC Bayern München"},
            "team2": {"teamId": 16, "teamName": "Werder Bremen"},
            "matchResults": [
                {"resultTypeID": 1, "pointsTeam1": 1, "pointsTeam2": 0},
                {"resultTypeID": 2, "pointsTeam1": 3, "pointsTeam2": 0},
            ],
        }
    ]

    async def _fake_get(_url):
        return SimpleNamespace(status_code=200, json=lambda: payload, raise_for_status=lambda: None)

    provider._client = SimpleNamespace(get=_fake_get)
    result = await provider.get_season_matches("bl1", 2024)
    assert len(result) == 1
    row = result[0]
    assert row["match_id"] == "123"
    assert row["matchday"] == 1
    assert row["home_team_id"] == "40"
    assert row["away_team_id"] == "16"
    assert row["home_score"] == 3
    assert row["away_score"] == 0
    assert row["half_time_home"] == 1
    assert row["half_time_away"] == 0
    assert row["season"] == 2024

