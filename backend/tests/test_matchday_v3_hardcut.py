"""
backend/tests/test_matchday_v3_hardcut.py

Purpose:
    Validate key v3.1 hard-cut semantics for matchday routing/service logic:
    - strict v3 matchday ID format behavior
    - decimal-safe favorite strategy on close odds
"""

from __future__ import annotations

from datetime import timedelta
import sys

import pytest

sys.path.insert(0, "backend")

from app.models.matchday import MatchdayResponse
from app.routers import matchday as matchday_router
from app.routers.matchday import _parse_v3_matchday_id
from app.services.matchday_service import _favorite_prediction
from app.utils import utcnow


def test_parse_v3_matchday_id_rejects_incomplete_format():
    assert _parse_v3_matchday_id("v3:soccer:123") is None
    assert _parse_v3_matchday_id("v3:soccer:x:y") is None
    assert _parse_v3_matchday_id("legacy-id") is None


def test_favorite_prediction_uses_decimal_precision_for_close_odds():
    # FIXME: ODDS_V3_BREAK — test fixture uses odds_meta.summary_1x2 no longer produced by connector
    match = {
        "odds_meta": {
            "summary_1x2": {
                "home": {"avg": 1.85},
                "away": {"avg": 1.86},
            }
        }
    }
    # Home has lower odds -> higher implied probability -> favorite.
    assert _favorite_prediction(match) == (2, 1)


@pytest.mark.asyncio
async def test_matchday_detail_uses_season_display_number_not_round_id(monkeypatch):
    start = utcnow() + timedelta(days=1)

    class _Cursor:
        def __init__(self, docs):
            self._docs = docs

        def sort(self, *_args, **_kwargs):
            return self

        async def to_list(self, length=64):
            return self._docs[:length]

    class _Matches:
        def find(self, *_args, **_kwargs):
            return _Cursor([
                {
                    "_id": 19433556,
                    "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
                    "start_at": start,
                    "status": "SCHEDULED",
                }
            ])

    async def _fake_league_ids(_league_id: str):
        return [8]

    async def _fake_list_matchdays(*, league_id: str, season: int | None):
        _ = league_id, season
        return [
            MatchdayResponse(
                id="v3:82:2025:393442",
                league_id=82,
                season=2025,
                matchday_number=24,
                label="Matchday 24",
                match_count=9,
                first_kickoff=start,
                last_kickoff=start,
                status="upcoming",
                all_resolved=False,
            )
        ]

    monkeypatch.setattr(matchday_router, "_resolve_v3_league_ids_for_sport", _fake_league_ids)
    monkeypatch.setattr(matchday_router, "_list_v3_matchdays_for_sport", _fake_list_matchdays)
    monkeypatch.setattr(matchday_router._db, "db", type("DB", (), {"matches_v3": _Matches()})(), raising=False)

    result = await matchday_router._get_v3_matchday_detail(
        "v3:82:2025:393442",
        lock_mins=15,
    )
    assert result is not None
    assert result["matchday"].matchday_number == 24
