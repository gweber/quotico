"""
backend/tests/test_football_data_org_service.py

Purpose:
    Service tests for football-data.org season import result mapping via the
    unified match ingest pipeline.
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


@pytest.mark.asyncio
async def test_football_data_dry_run_maps_ingest_counters(monkeypatch):
    league_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeagues(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "external_ids": {"football_data": "PL"},
                "is_active": True,
            }
        ),
    )
    monkeypatch.setattr(service._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return {"is_active": True}

    monkeypatch.setattr(service.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    async def _build_matches(**_kwargs):
        return ([{"external_id": "fd_org:200"}], 0)
    monkeypatch.setattr(service, "build_football_data_org_matches", _build_matches)
    async def _process_matches(_transformed, league_id=None, dry_run=False):
        _ = league_id, dry_run
        return {
            "processed": 1,
            "created": 0,
            "updated": 0,
            "conflicts": 1,
            "unresolved_team": 1,
            "team_name_conflict": 0,
            "conflicts_preview": [{"reason": "team_name_conflict"}],
            "items_preview": [{"external_id": "fd_org:200"}],
            "matched_by_external_id": 0,
            "matched_by_identity_window": 0,
            "other_conflicts": 1,
        }
    monkeypatch.setattr(service.match_ingest_service, "process_matches", _process_matches)

    result = await service.import_season(league_id, 2025, dry_run=True)
    assert result["processed"] == 1
    assert result["matched"] == 0
    assert result["skipped_conflicts"] == 1
    assert result["unresolved_teams"] == 1
    assert result["dry_run_preview"]["matches_found"] == 1
    assert result["dry_run_preview"]["would_create"] == 0
