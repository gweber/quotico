"""
backend/tests/test_sportmonks_connector_upserts.py

Purpose:
    Verify Sportmonks v3 upsert semantics:
    - $set + $setOnInsert contract
    - created_at immutability on reingest
    - updated_at refresh on every upsert
"""

from __future__ import annotations

from datetime import datetime, timezone
import sys

import pytest

sys.path.insert(0, "backend")

from app.services import sportmonks_connector as connector_module


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[int, dict] = {}
        self.calls: list[dict] = []

    async def update_one(self, query, update, upsert=False):
        self.calls.append({"query": query, "update": update, "upsert": upsert})
        doc_id = int(query["_id"])
        existing = self.docs.get(doc_id)
        upserted_id = None
        if existing is None and upsert:
            existing = {"_id": doc_id}
            self.docs[doc_id] = existing
            upserted_id = doc_id
            for key, value in update.get("$setOnInsert", {}).items():
                existing[key] = value
        if existing is not None:
            for key, value in update.get("$set", {}).items():
                existing[key] = value
        return type("Result", (), {"upserted_id": upserted_id})()

    async def find_one(self, query, _projection=None):
        doc_id = int(query["_id"])
        existing = self.docs.get(doc_id)
        return dict(existing) if isinstance(existing, dict) else None


class _FakeBulkCollection(_FakeCollection):
    def __init__(self) -> None:
        super().__init__()
        self.bulk_calls: list[dict] = []

    async def bulk_write(self, operations, ordered=False):
        ops = list(operations or [])
        self.bulk_calls.append({"operations": ops, "ordered": ordered})
        for op in ops:
            await self.update_one(op._filter, op._doc, upsert=getattr(op, "_upsert", False))
        return type("BulkResult", (), {"bulk_api_result": {"nUpserted": len(ops)}})()


class _FakeDB:
    def __init__(self) -> None:
        self.matches_v3 = _FakeCollection()
        self.persons = _FakeCollection()
        self.league_registry_v3 = _FakeCollection()
        self.teams_v3 = _FakeBulkCollection()


class _LeagueRegistryCollection(_FakeCollection):
    async def find_one(self, query, _projection=None):
        return self.docs.get(int(query["_id"]))


@pytest.mark.asyncio
async def test_match_upsert_keeps_created_at_on_reingest(monkeypatch):
    fake_db = _FakeDB()
    connector = connector_module.SportmonksConnector(database=fake_db)
    timestamps = [
        datetime(2026, 2, 26, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 26, 12, 0, tzinfo=timezone.utc),
    ]

    def _fake_utcnow():
        return timestamps.pop(0)

    monkeypatch.setattr(connector_module, "utcnow", _fake_utcnow)

    await connector.upsert_match_v3(1001, {"league_id": 5, "season_id": 2025, "status": "SCHEDULED"})
    created_first = fake_db.matches_v3.docs[1001]["created_at"]
    updated_first = fake_db.matches_v3.docs[1001]["updated_at"]

    await connector.upsert_match_v3(1001, {"league_id": 5, "season_id": 2025, "status": "LIVE"})
    created_second = fake_db.matches_v3.docs[1001]["created_at"]
    updated_second = fake_db.matches_v3.docs[1001]["updated_at"]

    assert created_first == created_second
    assert updated_second > updated_first
    assert fake_db.matches_v3.docs[1001]["status"] == "LIVE"
    assert all(call["upsert"] is True for call in fake_db.matches_v3.calls)


@pytest.mark.asyncio
async def test_person_and_league_upserts_use_set_on_insert(monkeypatch):
    fake_db = _FakeDB()
    connector = connector_module.SportmonksConnector(database=fake_db)
    fixed_now = datetime(2026, 2, 26, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(connector_module, "utcnow", lambda: fixed_now)

    await connector.upsert_person(2001, {"type": "player", "name": "Max"})
    await connector.upsert_league_registry_v3(3001, {"name": "Bundesliga", "is_cup": False})

    person_call = fake_db.persons.calls[0]
    league_call = fake_db.league_registry_v3.calls[0]

    assert "$set" in person_call["update"] and "$setOnInsert" in person_call["update"]
    assert "$set" in league_call["update"] and "$setOnInsert" in league_call["update"]
    assert person_call["update"]["$setOnInsert"]["created_at"] == fixed_now
    assert league_call["update"]["$setOnInsert"]["created_at"] == fixed_now
    assert person_call["upsert"] is True
    assert league_call["upsert"] is True


@pytest.mark.asyncio
async def test_sync_leagues_uses_lazy_global_db_resolution(monkeypatch):
    fake_db = _FakeDB()
    fake_db.league_registry_v3 = _LeagueRegistryCollection()
    connector = connector_module.SportmonksConnector(database=None)
    fixed_now = datetime(2026, 2, 26, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(connector_module, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(connector_module._db, "db", fake_db, raising=False)

    result = await connector.sync_leagues_to_registry(
        [
            {
                "_id": 301,
                "name": "Bundesliga",
                "country": "Germany",
                "is_cup": False,
                "available_seasons": [{"id": 25536, "name": "2025/2026"}],
            }
        ]
    )

    assert result == {"inserted": 1, "updated": 0}
    stored = fake_db.league_registry_v3.docs[301]
    assert stored["name"] == "Bundesliga"
    assert stored["created_at"] == fixed_now
    assert stored["updated_at"] == fixed_now


@pytest.mark.asyncio
async def test_sync_season_xg_sets_advanced_flag(monkeypatch):
    fake_db = _FakeDB()
    fake_db.matches_v3.docs[19433656] = {
        "_id": 19433656,
        "teams": {"home": {"sm_id": 3320}, "away": {"sm_id": 277}},
    }
    connector = connector_module.SportmonksConnector(database=fake_db)

    pages = [
        {
            "payload": {
                "data": [
                    {"fixture_id": 19433656, "type_id": 5304, "participant_id": 3320, "location": "home", "data": {"value": 1.35}},
                    {"fixture_id": 19433656, "type_id": 5304, "participant_id": 277, "location": "away", "data": {"value": 2.79}},
                ],
                "pagination": {"has_more": False, "next_page": None},
            },
            "remaining": 99,
            "reset_at": 123456,
        }
    ]

    async def _expected_page(*, season_id: int, next_page_url=None):
        _ = season_id, next_page_url
        return pages.pop(0)

    monkeypatch.setattr(connector_module.sportmonks_provider, "get_expected_fixtures_page", _expected_page)
    result = await connector.sync_season_xg(25536)

    assert result["matches_synced"] == 1
    update = fake_db.matches_v3.calls[-1]["update"]["$set"]
    assert update["teams.home.xg"] == 1.35
    assert update["teams.away.xg"] == 2.79
    assert update["has_advanced_stats"] is True


@pytest.mark.asyncio
async def test_sync_fixture_odds_summary_compacts_market_one(monkeypatch):
    fake_db = _FakeDB()
    fake_db.matches_v3.docs[19433656] = {"_id": 19433656}
    connector = connector_module.SportmonksConnector(database=fake_db)

    async def _fixture_odds(_fixture_id: int):
        return {
            "payload": {
                "data": [
                    {"market_id": 1, "label": "Home", "value": "3.40", "probability": "29.41%"},
                    {"market_id": 1, "label": "Home", "value": "3.10", "probability": "32.26%"},
                    {"market_id": 1, "label": "Draw", "value": "3.90", "probability": "25.64%"},
                    {"market_id": 1, "label": "Away", "value": "1.98", "probability": "50.38%"},
                    {"market_id": 52, "label": "Away", "value": "1.50", "probability": "66.67%"},
                ]
            },
            "remaining": 88,
            "reset_at": 123456,
        }

    monkeypatch.setattr(connector_module.sportmonks_provider, "get_prematch_odds_by_fixture", _fixture_odds)
    ok = await connector.sync_fixture_odds_summary(19433656)

    assert ok is True
    summary = fake_db.matches_v3.calls[-1]["update"]["$set"]["odds_meta.summary_1x2"]
    assert summary["home"]["min"] == 3.1
    assert summary["home"]["max"] == 3.4
    assert summary["draw"]["count"] == 1
    assert "away" in summary


@pytest.mark.asyncio
async def test_ingest_season_bulk_writes_teams_v3_once_per_round_with_dedupe(monkeypatch):
    fake_db = _FakeDB()
    connector = connector_module.SportmonksConnector(database=fake_db)

    async def _get_season_rounds(_season_id: int):
        return {
            "payload": {"data": [{"id": 501, "name": "Round 1"}]},
            "remaining": 250,
            "reset_at": 123456,
        }

    async def _get_round_fixtures(_round_id: int):
        return {
            "payload": {
                "data": [
                    {
                        "id": 1001,
                        "league_id": 8,
                        "round_id": 501,
                        "participants": [
                            {"id": 11, "name": "A FC", "short_code": "AFC", "image_path": "/a.png"},
                            {"id": 22, "name": "B FC", "short_code": "BFC", "image_path": "/b.png"},
                        ],
                        "statistics": [],
                        "lineups": [],
                        "events": [],
                    },
                    {
                        "id": 1002,
                        "league_id": 8,
                        "round_id": 501,
                        "participants": [
                            {"id": 11, "name": "A FC", "short_code": "AFC", "image_path": "/a.png"},
                            {"id": 33, "name": "C FC", "short_code": "CFC", "image_path": "/c.png"},
                        ],
                        "statistics": [],
                        "lineups": [],
                        "events": [],
                    },
                ]
            },
            "remaining": 249,
            "reset_at": 123456,
        }

    async def _sync_people(_fixture):
        return 0

    async def _sync_odds(_fixture_id):
        return True

    monkeypatch.setattr(connector_module.sportmonks_provider, "get_season_rounds", _get_season_rounds)
    monkeypatch.setattr(connector_module.sportmonks_provider, "get_round_fixtures", _get_round_fixtures)
    monkeypatch.setattr(connector, "_sync_people_from_fixture", _sync_people)
    monkeypatch.setattr(connector, "sync_fixture_odds_summary", _sync_odds)

    result = await connector.ingest_season(25536)

    assert result["teams_upserted"] == 3
    assert len(fake_db.teams_v3.bulk_calls) == 1
    bulk_call = fake_db.teams_v3.bulk_calls[0]
    assert bulk_call["ordered"] is False
    assert len(bulk_call["operations"]) == 3
    assert sorted(fake_db.teams_v3.docs.keys()) == [11, 22, 33]
