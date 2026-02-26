"""
backend/tests/test_matchday_v3_hardcut.py

Purpose:
    Validate key v3.1 hard-cut semantics for matchday routing/service logic:
    - strict v3 matchday ID format behavior
    - decimal-safe favorite strategy on close odds
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "backend")

from app.routers.matchday import _parse_v3_matchday_id
from app.services.matchday_service import _favorite_prediction


def test_parse_v3_matchday_id_rejects_incomplete_format():
    assert _parse_v3_matchday_id("v3:soccer:123") is None
    assert _parse_v3_matchday_id("v3:soccer:x:y") is None
    assert _parse_v3_matchday_id("legacy-id") is None


def test_favorite_prediction_uses_decimal_precision_for_close_odds():
    match = {
        "odds_meta": {
            "summary_1x2": {
                "home": {"avg": 1.85},
                "away": {"avg": 1.86},
            }
        }
    }
    # Home has lower odds -> higher implied probability -> favorite.
    assert _favorite_prediction(match) == (2, 1)

