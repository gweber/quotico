"""
backend/tests/test_teams_router_contract.py

Purpose:
    Contract tests for public /api/teams endpoints to ensure team_id-first
    payloads used by frontend Team views.
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.routers import teams as teams_router
from app.services import team_service
from app.utils import utcnow


class _Cursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)
        self._limit = None
        self._reverse = False
        self._sort_key = None

    def sort(self, key: str, direction: int):
        self._sort_key = key
        self._reverse = int(direction) < 0
        return self

    def limit(self, value: int):
        self._limit = int(value)
        return self

    async def to_list(self, length: int | None = None):
        rows = list(self._docs)
        if self._sort_key is not None:
            rows = sorted(rows, key=lambda d: d.get(self._sort_key), reverse=self._reverse)
        if self._limit is not None:
            rows = rows[: self._limit]
        if length is not None:
            rows = rows[:length]
        return rows


class _TeamsCollection:
    def __init__(self, docs: list[dict]):
        self.docs = list(docs)

    def find(self, query: dict, projection: dict | None = None):
        rows = list(self.docs)
        ors = query.get("$or") or []
        if ors:
            filtered: list[dict] = []
            for row in rows:
                matched = False
                for clause in ors:
                    if "display_name" in clause:
                        pattern = clause["display_name"]["$regex"]
                        if re.search(pattern, str(row.get("display_name") or ""), flags=re.I):
                            matched = True
                    if "aliases.name" in clause:
                        pattern = clause["aliases.name"]["$regex"]
                        alias_names = [str(a.get("name") or "") for a in (row.get("aliases") or []) if isinstance(a, dict)]
                        if any(re.search(pattern, n, flags=re.I) for n in alias_names):
                            matched = True
                if matched:
                    filtered.append(dict(row))
            rows = filtered
        if projection:
            projected: list[dict] = []
            for row in rows:
                projected.append({k: v for k, v in row.items() if k in projection or k == "_id"})
            rows = projected
        return _Cursor(rows)

    async def find_one(self, query: dict):
        for row in self.docs:
            if row.get("_id") == query.get("_id"):
                return dict(row)
        return None


class _MatchesCollection:
    def __init__(self, docs: list[dict]):
        self.docs = list(docs)

    def find(self, query: dict, projection: dict | None = None):
        rows: list[dict] = []
        for row in self.docs:
            if row.get("status") != query.get("status") and query.get("status") != {"$in": ["scheduled", "live"]}:
                pass

            if isinstance(query.get("status"), dict):
                allowed = set(query["status"].get("$in", []))
                if row.get("status") not in allowed:
                    continue
            else:
                if row.get("status") != query.get("status"):
                    continue

            sport_filter = query.get("league_id")
            if isinstance(sport_filter, dict) and "$in" in sport_filter:
                if row.get("league_id") not in set(sport_filter["$in"]):
                    continue
            elif isinstance(sport_filter, str):
                if row.get("league_id") != sport_filter:
                    continue

            ors = query.get("$or") or []
            if ors:
                team_ids = {clause.get("home_team_id") for clause in ors if "home_team_id" in clause}
                team_ids |= {clause.get("away_team_id") for clause in ors if "away_team_id" in clause}
                if row.get("home_team_id") not in team_ids and row.get("away_team_id") not in team_ids:
                    continue

            rows.append(dict(row))

        if projection:
            projected: list[dict] = []
            for row in rows:
                projected.append({k: v for k, v in row.items() if k in projection or k == "_id" or k == "result"})
            rows = projected
        return _Cursor(rows)


@pytest.mark.asyncio
async def test_search_contract_returns_team_id(monkeypatch):
    team_id = ObjectId()
    fake_db = SimpleNamespace(
        teams=_TeamsCollection(
            [
                {
                    "_id": team_id,
                    "display_name": "Bayern München",
                    "league_id": 82,
                    "aliases": [{"name": "Bayern", "league_id": 82}],
                }
            ]
        ),
        matches=_MatchesCollection([]),
    )
    monkeypatch.setattr(team_service._db, "db", fake_db, raising=False)

    rows = await teams_router.search(q="Bayern", league_id=None, limit=10)
    assert len(rows) == 1
    assert rows[0]["team_id"] == str(team_id)
    assert "team_key" not in rows[0]


@pytest.mark.asyncio
async def test_team_profile_contract_returns_team_id(monkeypatch):
    team_id = ObjectId()
    now = utcnow()
    fake_db = SimpleNamespace(
        teams=_TeamsCollection(
            [
                {
                    "_id": team_id,
                    "display_name": "Bayern München",
                    "league_id": 82,
                    "needs_review": False,
                    "aliases": [{"name": "Bayern", "league_id": 82}],
                }
            ]
        ),
        matches=_MatchesCollection(
            [
                {
                    "_id": ObjectId(),
                    "status": "final",
                    "league_id": 82,
                    "season": 2025,
                    "home_team": "Bayern München",
                    "away_team": "Dortmund",
                    "home_team_id": team_id,
                    "away_team_id": ObjectId(),
                    "result": {"home_score": 2, "away_score": 1, "outcome": "1"},
                    "match_date": now,
                },
                {
                    "_id": ObjectId(),
                    "status": "scheduled",
                    "league_id": 82,
                    "home_team": "Bayern München",
                    "away_team": "Leipzig",
                    "home_team_id": team_id,
                    "away_team_id": ObjectId(),
                    "odds": {"h2h": {"1": 1.8, "X": 3.8, "2": 4.1}},
                    "match_date": now,
                },
            ]
        ),
    )
    monkeypatch.setattr(team_service._db, "db", fake_db, raising=False)
    monkeypatch.setattr(team_service, "league_ids_for", lambda league_id: [league_id] if league_id else [])

    profile = await teams_router.get_team(
        team_slug=team_service._slug("Bayern München"),
        league_id=82,
    )
    assert profile["team_id"] == str(team_id)
    assert "team_key" not in profile
    assert "82" in profile["league_ids"]
    assert profile["season_stats"]["season_label"] == "2025"
