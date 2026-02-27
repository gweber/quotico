"""
backend/tests/test_qbot_2_4.py

Purpose:
    Validate Qbot Intelligence 2.4 xG-first post-match reasoning:
    - xG extraction from match.result
    - clinical efficiency trigger
    - xG betrayal trigger
    - decision trace contains xG fields
"""

from app.services.qbot_intelligence_service import (
    _build_decision_trace,
    _compute_post_match_reasoning,
    _extract_market_context,
)


def test_extract_market_context_reads_xg_from_result() -> None:
    match = {
        "odds_meta": {"markets": {"h2h": {"provider_count": 1, "current": {"1": "2.10"}}}},
        "result": {"home_xg": 1.74, "away_xg": 0.92},
    }
    market_ctx = _extract_market_context(match, "1")

    assert market_ctx["xg_home"] == 1.74
    assert market_ctx["xg_away"] == 0.92
    assert market_ctx["stats"]["xg_home"] == 1.74
    assert market_ctx["stats"]["xg_away"] == 0.92


def test_post_match_reasoning_prefers_clinical_efficiency() -> None:
    tip = {"recommended_selection": "1", "was_correct": True, "actual_result": "3:1"}
    stats = {
        "cards_red_home": 0,
        "cards_red_away": 0,
        "xg_home": 1.9,
        "xg_away": 1.1,
        "shots_home": 14,
        "shots_away": 11,
    }

    reasoning = _compute_post_match_reasoning(tip, stats)

    assert reasoning is not None
    assert reasoning["type"] == "clinical_efficiency"
    assert reasoning["efficient_team"] == "home"
    assert reasoning["efficiency_home"] > 1.3


def test_post_match_reasoning_xg_betrayal() -> None:
    tip = {"recommended_selection": "1", "was_correct": False, "actual_result": "0:1"}
    stats = {
        "cards_red_home": 0,
        "cards_red_away": 0,
        "xg_home": 2.0,
        "xg_away": 0.9,
        "shots_home": 15,
        "shots_away": 6,
    }

    reasoning = _compute_post_match_reasoning(tip, stats)

    assert reasoning is not None
    assert reasoning["type"] == "xg_betrayal"
    assert reasoning["expected_winner"] == "home"
    assert reasoning["actual_outcome"] == "away"
    assert reasoning["xg_delta"] > 0.8


def test_decision_trace_contains_xg_fields() -> None:
    tip = {
        "edge_pct": 5.0,
        "true_probability": 0.55,
        "confidence": 0.60,
        "implied_probability": 0.50,
        "recommended_selection": "1",
        "status": "active",
        "skip_reason": None,
    }
    strategy = {
        "_id": "test_strategy",
        "version": "v1",
        "generation": 1,
        "league_id": None,
        "is_active": True,
        "is_shadow": False,
        "dna": {
            "min_edge": 3.0,
            "min_confidence": 0.40,
            "home_bias": 1.0,
            "away_bias": 1.0,
            "draw_threshold": 0.0,
            "kelly_fraction": 0.25,
            "max_stake": 50.0,
            "volatility_buffer": 0.0,
            "bayes_trust_factor": 0.0,
        },
        "optimization_notes": {},
    }
    market_ctx = {
        "provider_count": 2,
        "spread_pct": 0.03,
        "volatility_dim": "volatile",
        "market_trust_factor": 0.9,
        "xg_home": 1.5,
        "xg_away": 1.2,
    }

    trace = _build_decision_trace(
        tip=tip,
        strategy=strategy,
        bayes_conf=0.55,
        stake_units=10.0,
        kelly_raw=0.2,
        market_ctx=market_ctx,
    )

    assert trace["market_context"]["xg_home"] == 1.5
    assert trace["market_context"]["xg_away"] == 1.2
