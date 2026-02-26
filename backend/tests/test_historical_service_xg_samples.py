"""
backend/tests/test_historical_service_xg_samples.py

Purpose:
    Validate H2H xG sample semantics in historical service responses:
    - xg_samples_used / xg_samples_total are always present
    - avg_home_xg / avg_away_xg only exist when xG samples are available
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
import sys

from bson import ObjectId
import pytest

sys.path.insert(0, "backend")

from app.services import historical_service


class _Cursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, field: str, direction: int):
        reverse = direction == -1
        self._docs.sort(key=lambda d: d.get(field), reverse=reverse)
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _MatchesCollection:
    def __init__(self, docs: list[dict]):
        self.docs = list(docs)

    def find(self, query: dict, projection: dict | None = None):
        results = [row for row in self.docs if self._matches_query(row, query)]
        return _Cursor(results)

    @staticmethod
    def _matches_query(row: dict, query: dict) -> bool:
        sport_filter = query.get("sport_key", {}).get("$in")
        if sport_filter and row.get("sport_key") not in sport_filter:
            return False
        if query.get("status") and row.get("status") != query.get("status"):
            return False
        outcome_filter = query.get("result.outcome", {}).get("$ne")
        if outcome_filter is None and row.get("result", {}).get("outcome") is None:
            return False
        if "$or" in query:
            or_ok = False
            for clause in query["$or"]:
                clause_ok = True
                for key, expected in clause.items():
                    if row.get(key) != expected:
                        clause_ok = False
                        break
                if clause_ok:
                    or_ok = True
                    break
            if not or_ok:
                return False
        return True


def _make_match(
    home_id: ObjectId,
    away_id: ObjectId,
    *,
    home_score: int = 1,
    away_score: int = 0,
    home_xg: float | None = None,
    away_xg: float | None = None,
    days_ago: int = 0,
) -> dict:
    result: dict = {"home_score": home_score, "away_score": away_score, "outcome": "1" if home_score > away_score else "2"}
    if home_xg is not None:
        result["home_xg"] = home_xg
    if away_xg is not None:
        result["away_xg"] = away_xg
    return {
        "match_date": datetime(2026, 2, 1) - timedelta(days=days_ago),
        "home_team": "Home FC",
        "away_team": "Away FC",
        "home_team_id": home_id,
        "away_team_id": away_id,
        "sport_key": "soccer_germany_bundesliga",
        "status": "final",
        "result": result,
    }


@pytest.mark.asyncio
async def test_h2h_summary_includes_xg_samples_used_and_total(monkeypatch):
    home_id = ObjectId()
    away_id = ObjectId()
    docs = []
    for i in range(13):
        with_xg = i < 5
        docs.append(
            _make_match(
                home_id,
                away_id,
                home_score=2,
                away_score=1,
                home_xg=1.4 if with_xg else None,
                away_xg=0.9 if with_xg else None,
                days_ago=i,
            )
        )
    fake_db = SimpleNamespace(matches=_MatchesCollection(docs))
    monkeypatch.setattr(historical_service._db, "db", fake_db, raising=False)

    class _Registry:
        async def resolve(self, name: str, _sport_key: str):
            return home_id if "Home" in name else away_id

    monkeypatch.setattr(historical_service.TeamRegistry, "get", staticmethod(lambda: _Registry()))
    historical_service.clear_context_cache()

    result = await historical_service.build_match_context(
        home_team="Home FC",
        away_team="Away FC",
        sport_key="soccer_germany_bundesliga",
        h2h_limit=20,
        form_limit=5,
    )

    summary = result["h2h"]["summary"]
    assert summary["xg_samples_used"] == 5
    assert summary["xg_samples_total"] == 13
    assert summary["avg_home_xg"] == 1.4
    assert summary["avg_away_xg"] == 0.9


@pytest.mark.asyncio
async def test_h2h_summary_reports_zero_xg_samples_without_avg_fields(monkeypatch):
    home_id = ObjectId()
    away_id = ObjectId()
    docs = [
        _make_match(home_id, away_id, home_score=1, away_score=0, days_ago=i)
        for i in range(7)
    ]
    fake_db = SimpleNamespace(matches=_MatchesCollection(docs))
    monkeypatch.setattr(historical_service._db, "db", fake_db, raising=False)

    class _Registry:
        async def resolve(self, name: str, _sport_key: str):
            return home_id if "Home" in name else away_id

    monkeypatch.setattr(historical_service.TeamRegistry, "get", staticmethod(lambda: _Registry()))
    historical_service.clear_context_cache()

    result = await historical_service.build_match_context(
        home_team="Home FC",
        away_team="Away FC",
        sport_key="soccer_germany_bundesliga",
        h2h_limit=20,
        form_limit=5,
    )

    summary = result["h2h"]["summary"]
    assert summary["xg_samples_used"] == 0
    assert summary["xg_samples_total"] == 7
    assert "avg_home_xg" not in summary
    assert "avg_away_xg" not in summary
