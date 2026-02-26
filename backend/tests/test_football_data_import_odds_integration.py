"""
backend/tests/test_football_data_import_odds_integration.py

Purpose:
    Integration-style tests for football-data import odds wiring into the
    greenfield odds ingestion service.

Dependencies:
    - pytest
    - app.services.football_data_service
"""

from types import SimpleNamespace
import sys

import httpx
import pytest
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, "backend")

from app.services import football_data_service as fds


class _FakeUpdateResult:
    def __init__(self, modified_count: int):
        self.modified_count = modified_count


class _FakeLeaguesCollection:
    def __init__(self, league_doc: dict):
        self.league_doc = league_doc

    async def find_one(self, query):
        if query.get("_id") == self.league_doc["_id"]:
            return dict(self.league_doc)
        return None


class _FakeMatchesCollection:
    def __init__(self, match_id: ObjectId):
        self.match_id = match_id
        self.updated = 0

    async def find_one(self, _query, _projection=None):
        return {"_id": self.match_id}

    async def update_one(self, _query, _update):
        self.updated += 1
        return _FakeUpdateResult(modified_count=1)


class _FakeMatchesCollectionMissing:
    def __init__(self):
        self.docs = {}
        self.updated = 0
        self.inserted = 0

    async def find_one(self, _query, _projection=None):
        return None

    async def insert_one(self, doc):
        oid = ObjectId()
        self.docs[str(oid)] = dict(doc)
        self.inserted += 1
        return SimpleNamespace(inserted_id=oid)

    async def update_one(self, _query, _update):
        self.updated += 1
        return _FakeUpdateResult(modified_count=1)


@pytest.mark.asyncio
async def test_import_stats_ingests_odds_grouped_by_provider(monkeypatch):
    league_id = ObjectId()
    match_id = ObjectId()
    home_id = ObjectId()
    away_id = ObjectId()

    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2024,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=_FakeMatchesCollection(match_id),
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    class _FakeTeamRegistry:
        async def resolve(self, name, _sport_key, create_if_missing=True):
            assert create_if_missing is True
            return home_id if name == "Arsenal" else away_id

    class _FakeProvider:
        async def fetch_season_csv(self, _season, _division):
            return (
                "Date,HomeTeam,AwayTeam,HS,AS,B365H,B365D,B365A,B365>2.5,B365<2.5,B365AHH,B365AHA,AHh\n"
                "10/08/2024,Arsenal,Chelsea,12,8,2.10,3.40,3.80,1.85,1.95,1.90,1.98,-0.5\n"
            )

    ingest_calls: list[tuple[str, list[dict]]] = []

    class _FakeOddsService:
        async def ingest_snapshot_batch(self, provider: str, snapshots: list[dict], reference_ts=None):
            ingest_calls.append((provider, snapshots))
            return {"inserted": 3, "deduplicated": 0, "markets_updated": 3}

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FakeProvider())
    monkeypatch.setattr(fds, "odds_service", _FakeOddsService())

    result = await fds.import_football_data_stats(league_id, season="2425")

    assert result["processed"] == 1
    assert result["matched"] == 1
    assert result["existing_matches"] == 1
    assert result["new_matches"] == 0
    assert result["updated"] == 1
    assert result["odds_snapshots_total"] == 3
    assert result["odds_providers_seen"] == 1
    assert result["odds_ingest_errors"] == 0
    assert ingest_calls
    provider, snapshots = ingest_calls[0]
    assert provider == "bet365"
    assert any("odds" in snap for snap in snapshots)
    assert any("totals" in snap for snap in snapshots)
    assert any("spreads" in snap for snap in snapshots)


@pytest.mark.asyncio
async def test_import_stats_keeps_running_when_odds_ingest_fails(monkeypatch):
    league_id = ObjectId()
    match_id = ObjectId()

    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2024,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=_FakeMatchesCollection(match_id),
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    class _FakeTeamRegistry:
        async def resolve(self, _name, _sport_key, create_if_missing=True):
            assert create_if_missing is True
            return ObjectId()

    class _FakeProvider:
        async def fetch_season_csv(self, _season, _division):
            return (
                "Date,HomeTeam,AwayTeam,HS,B365H,B365D,B365A\n"
                "10/08/2024,Arsenal,Chelsea,12,2.10,3.40,3.80\n"
            )

    class _FailingOddsService:
        async def ingest_snapshot_batch(self, _provider, _snapshots, reference_ts=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FakeProvider())
    monkeypatch.setattr(fds, "odds_service", _FailingOddsService())

    result = await fds.import_football_data_stats(league_id, season="2425")

    assert result["processed"] == 1
    assert result["matched"] == 1
    assert result["existing_matches"] == 1
    assert result["new_matches"] == 0
    assert result["updated"] == 1
    assert result["odds_snapshots_total"] == 1
    assert result["odds_ingest_errors"] == 1


@pytest.mark.asyncio
async def test_import_stats_dry_run_has_no_writes_and_returns_preview(monkeypatch):
    league_id = ObjectId()
    match_id = ObjectId()

    fake_matches = _FakeMatchesCollection(match_id)
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2024,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=fake_matches,
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    class _DryRunTeamRegistry:
        async def resolve(self, _name, _sport_key, create_if_missing=True):
            assert create_if_missing is False
            return ObjectId()

    class _FakeProvider:
        async def fetch_season_csv(self, _season, _division):
            return (
                "Date,HomeTeam,AwayTeam,HS,B365H,B365D,B365A\n"
                "10/08/2024,Arsenal,Chelsea,12,2.10,3.40,3.80\n"
            )

    class _OddsShouldNotBeCalled:
        async def ingest_snapshot_batch(self, _provider, _snapshots, reference_ts=None):
            raise AssertionError("ingest_snapshot_batch must not be called in dry_run")

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds.TeamRegistry, "get", staticmethod(lambda: _DryRunTeamRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FakeProvider())
    monkeypatch.setattr(fds, "odds_service", _OddsShouldNotBeCalled())

    result = await fds.import_football_data_stats(league_id, season="2425", dry_run=True)

    assert fake_matches.updated == 0
    assert result["processed"] == 1
    assert result["matched"] == 1
    assert result["existing_matches"] == 1
    assert result["new_matches"] == 0
    assert result["updated"] == 0
    assert result["odds_ingest_inserted"] == 0
    assert result["odds_ingest_markets_updated"] == 0
    assert "dry_run_preview" in result
    assert result["dry_run_preview"]["matches_found"] == 1
    assert result["dry_run_preview"]["existing_matches"] == 1
    assert result["dry_run_preview"]["new_matches"] == 0
    assert result["dry_run_preview"]["odds_snapshots_by_provider"]["bet365"] == 1
    assert result["dry_run_preview"]["would_update_stats"] == 1
    assert result["dry_run_preview"]["would_ingest_snapshots"] == 1


@pytest.mark.asyncio
async def test_import_stats_csv_error_contains_upstream_status(monkeypatch):
    league_id = ObjectId()
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2024,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=_FakeMatchesCollection(ObjectId()),
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    class _FakeTeamRegistry:
        async def resolve(self, _name, _sport_key, create_if_missing=True):
            return ObjectId()

    class _FailingProvider:
        async def fetch_season_csv(self, _season, _division):
            req = httpx.Request("GET", "https://www.football-data.co.uk/mmz4281/2425/E0.csv")
            resp = httpx.Response(status_code=404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FailingProvider())

    with pytest.raises(HTTPException) as exc:
        await fds.import_football_data_stats(league_id, season="2425")

    assert exc.value.status_code == 400
    assert "upstream_status=404" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_import_stats_creates_new_match_when_missing(monkeypatch):
    league_id = ObjectId()
    fake_matches = _FakeMatchesCollectionMissing()
    fake_db = SimpleNamespace(
        leagues=_FakeLeaguesCollection(
            {
                "_id": league_id,
                "sport_key": "soccer_epl",
                "current_season": 2025,
                "external_ids": {"football_data_uk": "E0"},
            }
        ),
        matches=fake_matches,
    )
    monkeypatch.setattr(fds._db, "db", fake_db, raising=False)

    class _FakeLeagueRegistry:
        async def ensure_for_import(self, *_args, **_kwargs):
            return None

    class _FakeTeamRegistry:
        async def resolve(self, _name, _sport_key, create_if_missing=True):
            assert create_if_missing is True
            return ObjectId()

    class _FakeProvider:
        async def fetch_season_csv(self, _season, _division):
            return (
                "Date,HomeTeam,AwayTeam,HS,AS,B365H,B365D,B365A\n"
                "10/08/2026,Arsenal,Chelsea,12,8,2.10,3.40,3.80\n"
            )

    ingest_calls = []

    class _FakeOddsService:
        async def ingest_snapshot_batch(self, provider: str, snapshots: list[dict], reference_ts=None):
            ingest_calls.append((provider, snapshots))
            return {"inserted": len(snapshots), "deduplicated": 0, "markets_updated": len(snapshots)}

    monkeypatch.setattr(fds.LeagueRegistry, "get", staticmethod(lambda: _FakeLeagueRegistry()))
    monkeypatch.setattr(fds.TeamRegistry, "get", staticmethod(lambda: _FakeTeamRegistry()))
    monkeypatch.setattr(fds, "football_data_uk_provider", _FakeProvider())
    monkeypatch.setattr(fds, "odds_service", _FakeOddsService())

    result = await fds.import_football_data_stats(league_id, season="2627")

    assert result["matched"] == 1
    assert result["existing_matches"] == 0
    assert result["new_matches"] == 1
    assert fake_matches.inserted == 1
    assert fake_matches.updated == 1
    assert ingest_calls
    created_doc = next(iter(fake_matches.docs.values()))
    assert created_doc["status"] == "scheduled"
    assert created_doc["season"] == 2026
