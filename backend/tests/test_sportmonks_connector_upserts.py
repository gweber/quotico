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

    @staticmethod
    def _set_nested(doc: dict, dotted_key: str, value) -> None:
        parts = dotted_key.split(".")
        target = doc
        for part in parts[:-1]:
            node = target.get(part)
            if not isinstance(node, dict):
                node = {}
                target[part] = node
            target = node
        target[parts[-1]] = value

    @staticmethod
    def _get_nested(doc: dict, dotted_key: str):
        target = doc
        for part in dotted_key.split("."):
            if not isinstance(target, dict):
                return None
            target = target.get(part)
        return target

    def _matches(self, doc: dict, query: dict | None) -> bool:
        if not isinstance(query, dict):
            return True
        for key, expected in query.items():
            actual = self._get_nested(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected:
                    if actual not in expected["$in"]:
                        return False
                    continue
                if "$ne" in expected:
                    if actual == expected["$ne"]:
                        return False
                    continue
                if "$gt" in expected:
                    if actual is None or actual <= expected["$gt"]:
                        return False
                    continue
                if "$exists" in expected:
                    if bool(expected["$exists"]) != bool(actual is not None):
                        return False
                    continue
            if actual != expected:
                return False
        return True

    async def update_one(self, query, update, upsert=False):
        self.calls.append({"query": query, "update": update, "upsert": upsert})
        doc_id = int(query.get("_id"))
        existing = self.docs.get(doc_id)
        upserted_id = None
        if isinstance(update, list):
            raise TypeError("pipeline updates are not supported by _FakeCollection")
        if existing is None and upsert:
            existing = {"_id": doc_id}
            self.docs[doc_id] = existing
            upserted_id = doc_id
            for key, value in update.get("$setOnInsert", {}).items():
                self._set_nested(existing, key, value)
        if existing is not None:
            for key, value in update.get("$set", {}).items():
                self._set_nested(existing, key, value)
            for key, value in update.get("$pull", {}).items():
                current = self._get_nested(existing, key)
                if isinstance(current, list):
                    self._set_nested(existing, key, [item for item in current if item != value])
            for key, value in (update.get("$push") or {}).items():
                current = self._get_nested(existing, key)
                if not isinstance(current, list):
                    current = []
                if isinstance(value, dict) and isinstance(value.get("$each"), list):
                    current.extend(value["$each"])
                    if "$slice" in value and isinstance(value["$slice"], int):
                        limit = int(value["$slice"])
                        current = current[limit:] if limit < 0 else current[:limit]
                else:
                    current.append(value)
                self._set_nested(existing, key, current)
        return type("Result", (), {"upserted_id": upserted_id})()

    async def find_one(self, query=None, _projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda doc: self._get_nested(doc, key) or 0, reverse=int(direction) < 0)
        if not rows:
            return None
        return rows[0]

    def find(self, query=None, _projection=None):
        rows = [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]

        class _Cursor:
            def __init__(self, items):
                self._items = list(items)
                self._limit = None

            def limit(self, value: int):
                self._limit = int(value)
                return self

            async def to_list(self, length=None):
                items = self._items
                if self._limit is not None:
                    items = items[: self._limit]
                if length is None:
                    return list(items)
                return list(items)[: int(length)]

            def __aiter__(self):
                items = self._items
                if self._limit is not None:
                    items = items[: self._limit]
                self._iter_items = items
                self._idx = 0
                return self

            async def __anext__(self):
                if self._idx >= len(self._iter_items):
                    raise StopAsyncIteration
                item = self._iter_items[self._idx]
                self._idx += 1
                return item

        return _Cursor(rows)

    async def insert_many(self, docs, ordered=False):
        _ = ordered
        inserted_ids = []
        for row in docs:
            row_id = int(row["_id"])
            self.docs[row_id] = dict(row)
            inserted_ids.append(row_id)
        return type("InsertManyResult", (), {"inserted_ids": inserted_ids})()


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
        self.xg_raw = _FakeCollection()
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
    fake_db.league_registry_v3.docs[301] = {
        "_id": 301,
        "sport_key": "soccer_germany_bundesliga",
        "is_active": True,
        "features": {"tipping": True, "match_load": True, "xg_sync": True, "odds_sync": True},
        "ui_order": 1,
        "available_seasons": [{"id": 25535, "name": "2024/2025"}],
        "created_at": datetime(2026, 2, 26, 13, 0, tzinfo=timezone.utc),
    }
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

    assert result == {"inserted": 0, "updated": 1, "rejected": 0}
    stored = fake_db.league_registry_v3.docs[301]
    assert stored["name"] == "Bundesliga"
    assert stored["created_at"] == datetime(2026, 2, 26, 13, 0, tzinfo=timezone.utc)
    assert stored["updated_at"] == fixed_now


@pytest.mark.asyncio
async def test_sync_leagues_rejects_non_provisioned_entries(monkeypatch):
    fake_db = _FakeDB()
    fake_db.league_registry_v3 = _LeagueRegistryCollection()
    connector = connector_module.SportmonksConnector(database=None)
    monkeypatch.setattr(connector_module._db, "db", fake_db, raising=False)

    result = await connector.sync_leagues_to_registry(
        [
            {
                "_id": 999,
                "name": "Unknown League",
                "country": "Nowhere",
                "is_cup": False,
                "available_seasons": [{"id": 1, "name": "2025"}],
            }
        ]
    )

    assert result == {"inserted": 0, "updated": 0, "rejected": 1}
    assert 999 not in fake_db.league_registry_v3.docs


@pytest.mark.asyncio
async def test_sync_season_xg_sets_advanced_flag(monkeypatch):
    fake_db = _FakeDB()
    fake_db.matches_v3.docs[19433656] = {
        "_id": 19433656,
        "season_id": 25536,
        "status": "FINISHED",
        "teams": {"home": {"sm_id": 3320}, "away": {"sm_id": 277}},
    }
    connector = connector_module.SportmonksConnector(database=fake_db)

    pages = [
        {
            "payload": {
                    "data": [
                        {"id": 900001, "fixture_id": 19433656, "type_id": 5304, "participant_id": 3320, "location": "home", "data": {"value": 1.35}},
                        {"id": 900002, "fixture_id": 19433656, "type_id": 5304, "participant_id": 277, "location": "away", "data": {"value": 2.79}},
                    ],
                "pagination": {"has_more": False, "next_page": None},
            },
            "remaining": 99,
            "reset_at": 123456,
        }
    ]

    async def _expected_page(*, season_id: int | None = None, next_page_url=None, page=None):
        _ = season_id, next_page_url, page
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

    async def _get_round_fixtures(_round_id: int, *, include_odds: bool = True):
        _ = include_odds
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

    odds_calls = {"count": 0}

    async def _sync_odds(_fixture_id):
        odds_calls["count"] += 1
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
    assert result["odds_synced"] == 0
    assert odds_calls["count"] == 0


# ---------------------------------------------------------------------------
# _map_events tests
# ---------------------------------------------------------------------------

def _make_connector():
    return connector_module.SportmonksConnector(database=_FakeDB())


class TestMapEvents:
    def test_extracts_goals_and_cards(self):
        c = _make_connector()
        fixture = {
            "events": [
                {"type_id": 14, "minute": 23, "player_name": "Mbappe", "sub_type": "", "team_id": 1, "sort_order": 1},
                {"type_id": 15, "minute": 45, "extra_minute": 2, "player_name": "Kimmich", "team_id": 2, "sort_order": 2},
                {"type_id": 16, "minute": 55, "player_name": "Kane", "team_id": 1, "sort_order": 3},
                {"type_id": 19, "minute": 30, "player_name": "Muller", "team_id": 2, "sort_order": 4},
                {"type_id": 20, "minute": 78, "player_name": "Vini", "team_id": 1, "sort_order": 5},
                {"type_id": 21, "minute": 89, "player_name": "Gavi", "team_id": 2, "sort_order": 6},
            ]
        }
        events = c._map_events(fixture)
        assert len(events) == 6
        assert events[0] == {"type": "goal", "minute": 23, "extra_minute": None, "player_name": "Mbappe", "player_id": None, "team_id": 1, "detail": "regular", "sort_order": 1}
        assert events[1]["detail"] == "own_goal"
        assert events[1]["extra_minute"] == 2
        assert events[2]["detail"] == "penalty"
        assert events[3] == {"type": "card", "minute": 30, "extra_minute": None, "player_name": "Muller", "player_id": None, "team_id": 2, "detail": "yellow", "sort_order": 4}
        assert events[4]["detail"] == "red"
        assert events[5]["detail"] == "yellow_red"

    def test_penalty_goal_via_sub_type(self):
        c = _make_connector()
        fixture = {"events": [{"type_id": 14, "minute": 60, "player_name": "X", "sub_type": "penalty", "sort_order": 1}]}
        events = c._map_events(fixture)
        assert events[0]["detail"] == "penalty"

    def test_var_and_missed_penalty(self):
        c = _make_connector()
        fixture = {
            "events": [
                {"type_id": 10, "minute": 50, "player_name": "", "info": "Goal Disallowed", "sort_order": 1},
                {"type_id": 17, "minute": 65, "player_name": "Saka", "info": "Saved", "sort_order": 2},
            ]
        }
        events = c._map_events(fixture)
        assert len(events) == 2
        assert events[0]["type"] == "var"
        assert events[0]["detail"] == "Goal Disallowed"
        assert events[1]["type"] == "missed_penalty"
        assert events[1]["detail"] == "Saved"

    def test_skips_unknown_type_ids(self):
        c = _make_connector()
        fixture = {"events": [{"type_id": 18, "minute": 70, "player_name": "Sub"}]}
        events = c._map_events(fixture)
        assert events == []

    def test_sorts_by_sort_order(self):
        c = _make_connector()
        fixture = {
            "events": [
                {"type_id": 14, "minute": 90, "player_name": "B", "sort_order": 5},
                {"type_id": 19, "minute": 10, "player_name": "A", "sort_order": 1},
            ]
        }
        events = c._map_events(fixture)
        assert events[0]["player_name"] == "A"
        assert events[1]["player_name"] == "B"

    def test_nullable_minute(self):
        c = _make_connector()
        fixture = {"events": [{"type_id": 14, "minute": None, "player_name": "X", "sort_order": 1}]}
        events = c._map_events(fixture)
        assert events[0]["minute"] is None

    def test_empty_events(self):
        c = _make_connector()
        assert c._map_events({}) == []
        assert c._map_events({"events": []}) == []
        assert c._map_events({"events": None}) == []


# ---------------------------------------------------------------------------
# _extract_period_scores tests
# ---------------------------------------------------------------------------

class TestExtractPeriodScores:
    def test_extracts_half_and_full_time(self):
        c = _make_connector()
        fixture = {
            "scores": [
                {"description": "1ST_HALF", "score": {"participant": "home", "goals": 1}},
                {"description": "1ST_HALF", "score": {"participant": "away", "goals": 0}},
                {"description": "CURRENT", "score": {"participant": "home", "goals": 2}},
                {"description": "CURRENT", "score": {"participant": "away", "goals": 1}},
            ]
        }
        result = c._extract_period_scores(fixture)
        assert result["half_time"] == {"home": 1, "away": 0}
        assert result["full_time"] == {"home": 2, "away": 1}

    def test_returns_nulls_for_missing_scores(self):
        c = _make_connector()
        result = c._extract_period_scores({})
        assert result["half_time"] == {"home": None, "away": None}
        assert result["full_time"] == {"home": None, "away": None}

    def test_partial_scores(self):
        c = _make_connector()
        fixture = {
            "scores": [
                {"description": "CURRENT", "score": {"participant": "home", "goals": 3}},
            ]
        }
        result = c._extract_period_scores(fixture)
        assert result["full_time"] == {"home": 3, "away": None}
        assert result["half_time"] == {"home": None, "away": None}

    def test_ignores_other_descriptions(self):
        c = _make_connector()
        fixture = {
            "scores": [
                {"description": "2ND_HALF", "score": {"participant": "home", "goals": 1}},
                {"description": "EXTRA_TIME", "score": {"participant": "away", "goals": 2}},
            ]
        }
        result = c._extract_period_scores(fixture)
        assert result["half_time"] == {"home": None, "away": None}
        assert result["full_time"] == {"home": None, "away": None}


# ---------------------------------------------------------------------------
# _map_fixture_to_match: team denormalization
# ---------------------------------------------------------------------------

class TestTeamDenormalization:
    def test_includes_short_code_and_image_path(self):
        c = _make_connector()
        fixture = {
            "id": 5001,
            "league_id": 8,
            "round_id": 100,
            "starting_at": "2026-02-22T15:00:00Z",
            "state": {"short_name": "NS"},
            "participants": [
                {"id": 11, "name": "Bayern", "short_code": "FCB", "image_path": "https://cdn.sportmonks.com/teams/11.png"},
                {"id": 22, "name": "Dortmund", "short_code": "BVB", "image_path": "https://cdn.sportmonks.com/teams/22.png"},
            ],
            "scores": [],
            "statistics": [],
            "lineups": [],
            "events": [],
            "referees": [],
        }
        result = c._map_fixture_to_match(fixture, 25536)
        assert result["teams"]["home"]["short_code"] == "FCB"
        assert result["teams"]["home"]["image_path"] == "https://cdn.sportmonks.com/teams/11.png"
        assert result["teams"]["away"]["short_code"] == "BVB"
        assert result["teams"]["away"]["image_path"] == "https://cdn.sportmonks.com/teams/22.png"


# ---------------------------------------------------------------------------
# Data guard tests
# ---------------------------------------------------------------------------

class TestDataGuard:
    def _finished_fixture(self, *, scores=None, statistics=None, lineups=None, events=None):
        return {
            "id": 9001,
            "league_id": 8,
            "round_id": 100,
            "starting_at": "2026-02-22T15:00:00Z",
            "state": {"short_name": "FT"},
            "participants": [
                {"id": 11, "name": "A FC"},
                {"id": 22, "name": "B FC"},
            ],
            "scores": scores or [],
            "statistics": statistics or [],
            "lineups": lineups or [],
            "events": events or [],
            "referees": [],
        }

    def test_finished_without_scores_flags_critical(self):
        c = _make_connector()
        fixture = self._finished_fixture()
        result = c._map_fixture_to_match(fixture, 25536)
        assert result["manual_check_required"] is True
        assert "finished_without_scores" in result["manual_check_reasons"]

    def test_finished_with_scores_no_xg_is_not_critical(self):
        c = _make_connector()
        fixture = self._finished_fixture(scores=[
            {"description": "CURRENT", "score": {"participant": "home", "goals": 2}},
            {"description": "CURRENT", "score": {"participant": "away", "goals": 1}},
        ])
        result = c._map_fixture_to_match(fixture, 25536)
        # xG missing is soft (post-sync), so not flagged here
        assert result["manual_check_required"] is False

    def test_finished_without_lineups_is_soft(self):
        c = _make_connector()
        fixture = self._finished_fixture(scores=[
            {"description": "CURRENT", "score": {"participant": "home", "goals": 0}},
            {"description": "CURRENT", "score": {"participant": "away", "goals": 0}},
        ])
        result = c._map_fixture_to_match(fixture, 25536)
        assert "finished_without_lineups" in result["manual_check_reasons"]
        assert result["manual_check_required"] is False

    def test_walkover_with_scores_flags(self):
        c = _make_connector()
        fixture = self._finished_fixture(scores=[
            {"description": "CURRENT", "score": {"participant": "home", "goals": 3}},
            {"description": "CURRENT", "score": {"participant": "away", "goals": 0}},
        ])
        fixture["state"] = {"short_name": "WO"}
        result = c._map_fixture_to_match(fixture, 25536)
        assert result["manual_check_required"] is True
        assert "walkover_with_scores" in result["manual_check_reasons"]

    def test_finish_type_extracted(self):
        c = _make_connector()
        for raw, expected in [("FT", "FT"), ("AET", "AET"), ("PEN", "PEN"), ("NS", None), ("HT", None)]:
            fixture = self._finished_fixture()
            fixture["state"] = {"short_name": raw}
            result = c._map_fixture_to_match(fixture, 25536)
            assert result["finish_type"] == expected, f"Expected {expected} for {raw}"

    def test_fulltime_fallback_from_current_scores(self):
        c = _make_connector()
        # Scores only as CURRENT, no 1ST_HALF — full_time should be filled via fallback
        fixture = self._finished_fixture(scores=[
            {"description": "CURRENT", "score": {"participant": "home", "goals": 1}},
            {"description": "CURRENT", "score": {"participant": "away", "goals": 1}},
        ])
        result = c._map_fixture_to_match(fixture, 25536)
        assert result["scores"]["full_time"] == {"home": 1, "away": 1}
        assert "finished_without_scores" not in result["manual_check_reasons"]


class TestManualCheckAutoHeal:
    def test_recompute_defaults_to_false_for_empty_list(self):
        c = _make_connector()
        result = c._recompute_manual_check_fields([])
        assert result["manual_check_reasons"] == []
        assert result["manual_check_required"] is False

    def test_recompute_uses_critical_reasons_only(self):
        c = _make_connector()
        result = c._recompute_manual_check_fields(
            ["finished_without_lineups", "finished_without_scores", "finished_without_lineups"]
        )
        assert result["manual_check_reasons"] == ["finished_without_lineups", "finished_without_scores"]
        assert result["manual_check_required"] is True

    @pytest.mark.asyncio
    async def test_write_fixture_xg_clears_finished_without_xg_reason(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.matches_v3.docs[19433556] = {
            "_id": 19433556,
            "manual_check_reasons": ["finished_without_xg"],
            "manual_check_required": False,
        }
        c = connector_module.SportmonksConnector(database=fake_db)
        fixed_now = datetime(2026, 2, 26, 15, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(connector_module, "utcnow", lambda: fixed_now)

        synced, partial = await c._write_fixture_xg(
            fixture_id=19433556,
            xg_rows=[
                {"location": "home", "value": 1.2},
                {"location": "away", "value": 0.8},
            ],
            team_lookup={"home_sm_id": 11, "away_sm_id": 22},
            job_id=None,
            season_id=25536,
        )

        assert synced == 1
        assert partial == 0
        update_set = fake_db.matches_v3.calls[-1]["update"]["$set"]
        assert update_set["manual_check_reasons"] == []
        assert update_set["manual_check_required"] is False
        assert update_set["has_advanced_stats"] is True

    @pytest.mark.asyncio
    async def test_sync_fixture_odds_summary_clears_reason_only_for_valid_odds(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.matches_v3.docs[19433556] = {
            "_id": 19433556,
            "manual_check_reasons": ["finished_without_odds"],
            "manual_check_required": False,
            "odds_timeline": [],
        }
        c = connector_module.SportmonksConnector(database=fake_db)

        async def _valid_odds(_fixture_id: int):
            return {
                "payload": {
                    "data": [
                        {"market_id": 1, "label": "Home", "value": "2.10", "probability": "47.6%"},
                        {"market_id": 1, "label": "Draw", "value": "3.40", "probability": "29.4%"},
                        {"market_id": 1, "label": "Away", "value": "3.10", "probability": "32.2%"},
                    ]
                },
                "remaining": 88,
                "reset_at": 123456,
            }

        monkeypatch.setattr(connector_module.sportmonks_provider, "get_prematch_odds_by_fixture", _valid_odds)
        ok = await c.sync_fixture_odds_summary(19433556)
        assert ok is True
        update_set = fake_db.matches_v3.calls[-1]["update"]["$set"]
        assert update_set["manual_check_reasons"] == []
        assert update_set["manual_check_required"] is False

        fake_db.matches_v3.docs[19433556]["manual_check_reasons"] = ["finished_without_odds"]
        fake_db.matches_v3.docs[19433556]["manual_check_required"] = False

        async def _invalid_odds(_fixture_id: int):
            return {
                "payload": {
                    "data": [
                        {"market_id": 1, "label": "Home", "value": "2.10", "probability": "47.6%"},
                        {"market_id": 1, "label": "Draw", "value": "1.00", "probability": "100%"},
                        {"market_id": 1, "label": "Away", "value": "3.10", "probability": "32.2%"},
                    ]
                },
                "remaining": 77,
                "reset_at": 123999,
            }

        monkeypatch.setattr(connector_module.sportmonks_provider, "get_prematch_odds_by_fixture", _invalid_odds)
        ok_invalid = await c.sync_fixture_odds_summary(19433556)
        assert ok_invalid is True
        update_set_invalid = fake_db.matches_v3.calls[-1]["update"]["$set"]
        assert update_set_invalid["manual_check_reasons"] == ["finished_without_odds"]
        assert update_set_invalid["manual_check_required"] is False
