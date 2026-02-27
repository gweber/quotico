"""
backend/tests/test_justice_service.py

Purpose:
    Validate deterministic Monte Carlo xP behavior and unjust-table aggregation
    for the v3.1 JusticeService.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.justice_service import JusticeService
from app.services import justice_service


class _Cursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, field: str, direction: int):
        reverse = direction == -1
        self._docs.sort(key=lambda doc: doc.get(field), reverse=reverse)
        return self

    async def to_list(self, length: int | None = None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _MatchesCollection:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def find(self, query: dict, projection: dict | None = None):
        out: list[dict] = []
        for doc in self._docs:
            if doc.get("league_id") != query.get("league_id"):
                continue
            if doc.get("season_id") != query.get("season_id"):
                continue
            if doc.get("status") != query.get("status"):
                continue
            if bool(doc.get("has_advanced_stats")) is not bool(query.get("has_advanced_stats")):
                continue
            if projection:
                subset = {k: doc.get(k) for k in projection.keys()}
                out.append(subset)
            else:
                out.append(dict(doc))
        return _Cursor(out)


class _LeagueRegistryCollection:
    def __init__(self, docs: dict[int, dict]):
        self._docs = docs

    async def find_one(self, query: dict, projection: dict | None = None):
        row = self._docs.get(int(query.get("_id", -1)))
        if row is None:
            return None
        if projection:
            return {k: row.get(k) for k in projection.keys()}
        return dict(row)


def _match(match_id: int, *, league_id: int = 8, season_id: int = 99) -> dict:
    return {
        "_id": match_id,
        "league_id": league_id,
        "season_id": season_id,
        "status": "FINISHED",
        "has_advanced_stats": True,
        "start_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "teams": {
            "home": {
                "sm_id": 1,
                "name": "Home FC",
                "xg": 1.8,
                "score": 2,
                "short_code": "HOM",
                "image_path": "home.png",
            },
            "away": {
                "sm_id": 2,
                "name": "Away FC",
                "xg": 0.6,
                "score": 1,
                "short_code": "AWY",
                "image_path": "away.png",
            },
        },
    }


def test_calculate_match_xp_is_seed_stable():
    service = JusticeService()
    match = _match(123)

    first = service.calculate_match_xp(match)
    second = service.calculate_match_xp(match)

    assert first == second
    assert round(first["expected_points_home"] + first["expected_points_away"], 6) == round(3.0 - first["draw_prob"], 6)
    assert 0.0 <= first["draw_prob"] <= 1.0


def test_calculate_match_xp_rejects_invalid_xg():
    service = JusticeService()
    match = _match(124)
    match["teams"]["home"]["xg"] = "bad"

    with pytest.raises(ValueError):
        service.calculate_match_xp(match)


@pytest.mark.asyncio
async def test_get_unjust_table_aggregates_and_marks_cache(monkeypatch):
    JusticeService._cache.clear()
    docs = [_match(2001), _match(2002)]
    fake_db = SimpleNamespace(
        matches_v3=_MatchesCollection(docs),
        league_registry_v3=_LeagueRegistryCollection({8: {"_id": 8, "name": "Bundesliga", "available_seasons": [{"id": 99}]}}),
    )
    monkeypatch.setattr(justice_service._db, "db", fake_db, raising=False)

    service = JusticeService()
    fresh = await service.get_unjust_table(league_id=8, season_id=99)
    cached = await service.get_unjust_table(league_id=8, season_id=99)

    assert fresh["calculation_meta"]["cached"] is False
    assert cached["calculation_meta"]["cached"] is True
    assert fresh["excluded_matches_count"] == 0
    assert fresh["match_count"] == 2
    assert len(fresh["table"]) == 2

    home_row = next(row for row in fresh["table"] if row["team_sm_id"] == 1)
    assert home_row["real_pts"] == 6
    assert isinstance(home_row["expected_pts"], float)
    assert round(home_row["luck_factor"], 2) == round(home_row["real_pts"] - home_row["expected_pts"], 2)


@pytest.mark.asyncio
async def test_get_unjust_table_counts_excluded_matches(monkeypatch):
    JusticeService._cache.clear()
    valid = _match(3001)
    invalid = _match(3002)
    invalid["teams"]["home"]["xg"] = None

    fake_db = SimpleNamespace(
        matches_v3=_MatchesCollection([valid, invalid]),
        league_registry_v3=_LeagueRegistryCollection({8: {"_id": 8, "name": "Bundesliga", "available_seasons": [{"id": 99}]}}),
    )
    monkeypatch.setattr(justice_service._db, "db", fake_db, raising=False)

    service = JusticeService()
    result = await service.get_unjust_table(league_id=8, season_id=99)

    assert result["match_count"] == 1
    assert result["excluded_matches_count"] == 1
