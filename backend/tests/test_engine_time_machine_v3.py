"""
backend/tests/test_engine_time_machine_v3.py

Purpose:
    Validate Engine Time Machine v3 helper logic:
    - strict temporal safety query
    - greenfield odds_meta earliest-match lookup
    - market/xG analytics robustness
    - xP table team_id-only behavior
    - idempotent justice export upsert contract
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.engine_time_machine import (
    _compute_market_performance,
    _compute_statistical_integrity,
    _export_justice_table_to_mongo,
    _find_earliest_match,
    _snapshot_match_query,
)


def test_snapshot_match_query_is_strictly_temporal_safe() -> None:
    step = datetime(2026, 2, 1, tzinfo=timezone.utc)
    q = _snapshot_match_query(82, step, 365)
    assert q["start_at"]["$lt"] == step
    assert q["start_at"]["$gte"] == step - timedelta(days=365)
    assert "odds_meta.markets.h2h.current.1" in q


@pytest.mark.asyncio
async def test_find_earliest_match_uses_odds_meta_paths() -> None:
    captured = {}

    class _Matches:
        async def find_one(self, query, *_args, **_kwargs):
            captured.update(query)
            return {"start_at": datetime(2024, 1, 1, tzinfo=timezone.utc)}

    class _DB:
        matches_v3 = _Matches()

    dt = await _find_earliest_match(_DB(), 8)
    assert dt is not None
    assert "odds_meta.markets.h2h.current.1" in captured
    assert "odds.h2h.1" not in captured


def test_market_performance_has_pick_splits() -> None:
    m = {
        "_id": "m1",
        "odds_meta": {
            "markets": {
                "h2h": {
                    "opening": {"1": 2.40},
                    "current": {"1": 2.20, "X": 3.20, "2": 3.60},
                    "max": {"1": 2.50, "X": 3.30, "2": 3.80},
                    "min": {"1": 2.10, "X": 3.00, "2": 3.40},
                }
            }
        },
    }
    predictions = {
        "m1": {
            "pick": "1",
            "true_probability": 0.5,
            "probs": {"1": 0.5, "X": 0.25, "2": 0.25},
            "edge_pct": 12.0,
        }
    }
    perf = _compute_market_performance([m], predictions)
    assert perf["sample_size"] == 1
    assert perf["avg_clv"] is not None
    assert perf["beat_closing_rate_by_pick"]["1"] is not None


def test_statistical_integrity_handles_missing_xg() -> None:
    matches = [
        {
            "_id": "m1",
            "result": {"home_score": 2, "away_score": 1, "home_xg": 1.5, "away_xg": 0.8},
        },
        {
            "_id": "m2",
            "result": {"home_score": 1, "away_score": 1, "home_xg": 1.2},
        },
    ]
    predictions = {
        "m1": {"probs": {"1": 0.6, "X": 0.22, "2": 0.18}},
        "m2": {"probs": {"1": 0.4, "X": 0.32, "2": 0.28}},
    }
    s = _compute_statistical_integrity(matches, predictions, pure_brier=0.21)
    assert s["used_xg_matches"] == 1
    assert s["skipped_missing_xg"] == 1
    assert s["xg_brier_score"] is not None


@pytest.mark.asyncio
async def test_export_justice_uses_upsert() -> None:
    calls = []

    class _Justice:
        async def update_one(self, query, update, upsert=False):
            calls.append({"query": query, "update": update, "upsert": upsert})

    class _DB:
        engine_time_machine_justice = _Justice()

    matches = [
        {
            "teams": {
                "home": {"sm_id": 11, "name": "Home", "score": 1, "xg": 1.1},
                "away": {"sm_id": 22, "name": "Away", "score": 1, "xg": 1.0},
            },
        }
    ]
    exported = await _export_justice_table_to_mongo(
        _DB(),
        league_id=8,
        step_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        window_days=365,
        matches=matches,
        dry_run=False,
    )
    assert exported is True
    assert len(calls) == 1
    assert calls[0]["upsert"] is True
