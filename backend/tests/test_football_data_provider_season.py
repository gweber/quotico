"""
backend/tests/test_football_data_provider_season.py

Purpose:
    Unit tests for football-data.org season-fetch normalization.
"""

from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, "backend")

from app.providers.football_data import FootballDataProvider


@pytest.mark.asyncio
async def test_get_season_matches_normalizes_fields(monkeypatch):
    provider = FootballDataProvider()
    monkeypatch.setattr("app.providers.football_data.settings.FOOTBALL_DATA_ORG_API_KEY", "abc", raising=False)

    payload = {
        "matches": [
            {
                "id": 987,
                "utcDate": "2024-08-10T14:00:00Z",
                "status": "FINISHED",
                "matchday": 1,
                "stage": "REGULAR_SEASON",
                "group": None,
                "homeTeam": {"id": 57, "name": "Arsenal FC"},
                "awayTeam": {"id": 61, "name": "Chelsea FC"},
                "score": {
                    "fullTime": {"home": 2, "away": 1},
                    "halfTime": {"home": 1, "away": 1},
                    "extraTime": {"home": None, "away": None},
                    "penalties": {"home": None, "away": None},
                },
            }
        ]
    }

    async def _fake_get(_url, params=None, headers=None):
        return SimpleNamespace(status_code=200, json=lambda: payload, raise_for_status=lambda: None)

    provider._client = SimpleNamespace(get=_fake_get)
    result = await provider.get_season_matches("PL", 2024)
    assert len(result) == 1
    row = result[0]
    assert row["match_id"] == "987"
    assert row["status_raw"] == "FINISHED"
    assert row["matchday"] == 1
    assert row["home_team_id"] == "57"
    assert row["away_team_id"] == "61"
    assert row["score"]["full_time"]["home"] == 2
    assert row["score"]["full_time"]["away"] == 1

