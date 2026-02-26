"""
backend/tests/test_odds_service_events.py

Purpose:
    Publisher tests for OddsService event emission.
"""

from __future__ import annotations

import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import odds_service as odds_module


class _FakeRepo:
    async def insert_events_idempotent(self, events):
        return {"inserted": len(events), "deduplicated": 0}

    async def get_latest_provider_market_values(self, *_args, **_kwargs):
        return [{"provider": "bet365", "values": {"1": 2.1, "X": 3.2, "2": 3.8}, "line": None}]

    async def get_stale_provider_names(self, *_args, **_kwargs):
        return []

    async def update_match_odds_meta(self, *_args, **_kwargs):
        return 1

    async def set_market_closing_once(self, *_args, **_kwargs):
        return True


@pytest.mark.asyncio
async def test_odds_service_publishes_odds_ingested(monkeypatch):
    published = []
    service = odds_module.OddsService(repository=_FakeRepo())
    match_oid = ObjectId()

    async def _resolve_match_id(_provider, _snap):
        return match_oid

    async def _recompute_market(_match_id, _market, reference_ts=None):
        return True, odds_module.utcnow(), odds_module.utcnow()

    monkeypatch.setattr(service, "_resolve_match_id", _resolve_match_id)
    monkeypatch.setattr(service, "_recompute_market", _recompute_market)
    monkeypatch.setattr(odds_module.settings, "EVENT_BUS_ENABLED", True, raising=False)
    monkeypatch.setattr(odds_module.event_bus, "publish", lambda event: published.append(event))

    result = await service.ingest_snapshot_batch(
        provider="football_data",
        snapshots=[
            {
                "match_id": str(match_oid),
                "sport_key": "soccer_epl",
                "snapshot_at": "2025-08-10T14:00:00Z",
                "odds": {"1": 2.1, "X": 3.2, "2": 3.8},
            }
        ],
        correlation_id="corr-odds-1",
    )

    assert result["inserted"] >= 1
    assert len(published) == 1
    assert published[0].event_type == "odds.ingested"
    assert published[0].provider == "football_data"
    assert published[0].correlation_id == "corr-odds-1"
    assert str(match_oid) in published[0].match_ids

