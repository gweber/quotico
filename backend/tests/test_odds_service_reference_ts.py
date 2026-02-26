"""
backend/tests/test_odds_service_reference_ts.py

Purpose:
    Verifies historical ingest behavior for odds meta aggregation with and
    without explicit reference timestamps.

Dependencies:
    - pytest
    - app.services.odds_service
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

import app.services.odds_service as odds_service_module
from app.services.odds_service import OddsService


class _FakeMatchesCollection:
    def __init__(self, match_id: ObjectId):
        self.match_id = match_id
        self.doc = {
            "_id": match_id,
            "sport_key": "soccer_epl",
            "odds_meta": {"version": 0, "markets": {}},
        }

    async def find_one(self, query, _projection=None):
        if query.get("_id") != self.match_id:
            return None
        return dict(self.doc)


class _FakeRepository:
    def __init__(self, snapshot_at: datetime):
        self.snapshot_at = snapshot_at
        self.updated_calls = 0

    async def insert_events_idempotent(self, events):
        return {"inserted": len(events), "deduplicated": 0}

    async def get_latest_provider_market_values(self, _match_id, _market, since_ts):
        if since_ts > self.snapshot_at:
            return []
        return [
            {
                "provider": "bet365",
                "snapshot_at": self.snapshot_at,
                "line": None,
                "values": {"1": 2.1, "X": 3.4, "2": 3.8},
            }
        ]

    async def get_stale_provider_names(self, _match_id, _market, _since_ts):
        return []

    async def update_match_odds_meta(self, _match_id, _set_fields, expected_version=None):
        self.updated_calls += 1
        return None


@pytest.mark.asyncio
async def test_historical_ingest_requires_reference_ts_for_meta_update(monkeypatch, caplog):
    match_id = ObjectId()
    historical_ts = datetime(2024, 8, 10, 12, 0, tzinfo=timezone.utc)
    fake_repo = _FakeRepository(snapshot_at=historical_ts)
    service = OddsService(repository=fake_repo)

    fake_db = SimpleNamespace(matches=_FakeMatchesCollection(match_id))
    monkeypatch.setattr(odds_service_module._db, "db", fake_db, raising=False)

    snapshot = {
        "match_id": str(match_id),
        "league_id": ObjectId(),
        "sport_key": "soccer_epl",
        "snapshot_at": historical_ts,
        "odds": {"1": 2.1, "X": 3.4, "2": 3.8},
    }

    caplog.set_level("WARNING")
    no_ref = await service.ingest_snapshot_batch("bet365", [snapshot])
    assert no_ref["inserted"] == 3
    assert no_ref["markets_updated"] == 0
    assert "No provider data within staleness window" in caplog.text

    with_ref = await service.ingest_snapshot_batch("bet365", [snapshot], reference_ts=historical_ts)
    assert with_ref["inserted"] == 3
    assert with_ref["markets_updated"] == 1
    assert fake_repo.updated_calls >= 1


@pytest.mark.asyncio
async def test_live_snapshot_fallback_updates_only_within_staleness_window(monkeypatch):
    fixed_now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(odds_service_module, "utcnow", lambda: fixed_now)

    match_id = ObjectId()
    fresh_ts = datetime(2026, 2, 25, 11, 30, tzinfo=timezone.utc)
    stale_ts = datetime(2026, 2, 25, 8, 0, tzinfo=timezone.utc)

    # Fresh snapshot should materialize in live mode (no explicit reference_ts).
    fresh_repo = _FakeRepository(snapshot_at=fresh_ts)
    fresh_service = OddsService(repository=fresh_repo)
    fake_db = SimpleNamespace(matches=_FakeMatchesCollection(match_id))
    monkeypatch.setattr(odds_service_module._db, "db", fake_db, raising=False)
    fresh_result = await fresh_service.ingest_snapshot_batch(
        "bet365",
        [{
            "match_id": str(match_id),
            "league_id": ObjectId(),
            "sport_key": "soccer_epl",
            "snapshot_at": fresh_ts,
            "odds": {"1": 2.1, "X": 3.4, "2": 3.8},
        }],
    )
    assert fresh_result["markets_updated"] == 1

    # Stale snapshot should not materialize in live mode.
    stale_repo = _FakeRepository(snapshot_at=stale_ts)
    stale_service = OddsService(repository=stale_repo)
    stale_result = await stale_service.ingest_snapshot_batch(
        "bet365",
        [{
            "match_id": str(match_id),
            "league_id": ObjectId(),
            "sport_key": "soccer_epl",
            "snapshot_at": stale_ts,
            "odds": {"1": 2.1, "X": 3.4, "2": 3.8},
        }],
    )
    assert stale_result["markets_updated"] == 0


@pytest.mark.asyncio
async def test_reference_ts_edge_case_far_past_is_safe(monkeypatch):
    match_id = ObjectId()
    historical_ts = datetime(2024, 8, 10, 12, 0, tzinfo=timezone.utc)
    far_past_reference = datetime(2000, 1, 1, 0, 0, tzinfo=timezone.utc)
    fake_repo = _FakeRepository(snapshot_at=historical_ts)
    service = OddsService(repository=fake_repo)

    fake_db = SimpleNamespace(matches=_FakeMatchesCollection(match_id))
    monkeypatch.setattr(odds_service_module._db, "db", fake_db, raising=False)

    result = await service.ingest_snapshot_batch(
        "bet365",
        [{
            "match_id": str(match_id),
            "league_id": ObjectId(),
            "sport_key": "soccer_epl",
            "snapshot_at": historical_ts,
            "odds": {"1": 2.1, "X": 3.4, "2": 3.8},
        }],
        reference_ts=far_past_reference,
    )
    # No exception and deterministic behavior even with far past reference.
    # The window still includes the snapshot, so one market is updated.
    assert result["markets_updated"] == 1
