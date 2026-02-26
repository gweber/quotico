#!/usr/bin/env python3
"""
Test Qbot Intelligence 2.3 implementation - market synergy & temporal bias.
"""

import sys
sys.path.insert(0, "backend")

from app.services.qbot_intelligence_service import (
    _build_decision_trace,
    _get_market_synergy,
    _get_temporal_dim,
    _extract_market_context,
)

def test_temporal_dim():
    """Test temporal dimension binning."""
    assert _get_temporal_dim(None) == "day"
    assert _get_temporal_dim(10) == "day"      # < 18:00
    assert _get_temporal_dim(17) == "day"      # < 18:00
    assert _get_temporal_dim(18) == "prime"    # 18:00 - 22:00
    assert _get_temporal_dim(20) == "prime"
    assert _get_temporal_dim(21) == "prime"
    assert _get_temporal_dim(22) == "late"     # >= 22:00
    assert _get_temporal_dim(23) == "late"
    print("✓ _get_temporal_dim() works")

def test_market_synergy():
    """Test market synergy multiplier calculation."""
    # No totals data -> no synergy
    market_ctx = {"totals_provider_count": 0}
    assert _get_market_synergy("1", market_ctx) == 1.0
    
    # With totals data but missing line/over
    market_ctx = {"totals_provider_count": 2}
    assert _get_market_synergy("1", market_ctx) == 1.0
    
    # Positive synergy: line >= 2.5 and over < 1.70
    market_ctx = {
        "totals_provider_count": 3,
        "totals_line": "2.75",
        "totals_over": "1.65"
    }
    assert _get_market_synergy("1", market_ctx) == 1.05  # SYNERGY_POSITIVE_MULTIPLIER
    assert _get_market_synergy("2", market_ctx) == 1.05
    
    # Negative synergy: line <= 2.0 (low-scoring environment)
    market_ctx = {
        "totals_provider_count": 2,
        "totals_line": "1.75",
        "totals_over": "2.10"
    }
    assert _get_market_synergy("1", market_ctx) == 0.95  # SYNERGY_NEGATIVE_MULTIPLIER
    assert _get_market_synergy("2", market_ctx) == 0.95
    
    # Draw picks don't get synergy
    market_ctx = {
        "totals_provider_count": 3,
        "totals_line": "2.75",
        "totals_over": "1.65"
    }
    assert _get_market_synergy("X", market_ctx) == 1.0
    print("✓ _get_market_synergy() works")

def test_market_context_extraction():
    """Test market context extraction with totals data."""
    # Minimal match dict with odds_meta
    match = {
        "odds_meta": {
            "markets": {
                "h2h": {
                    "provider_count": 3,
                    "current": {"1": "2.10", "X": "3.40", "2": "3.80"}
                },
                "totals": {
                    "provider_count": 2,
                    "current": {"line": "2.5", "over": "1.85", "under": "1.95"}
                }
            }
        },
        "match_date_hour": None  # No datetime for simplicity
    }
    
    market_ctx = _extract_market_context(match, "1")
    
    # Check totals data
    assert market_ctx["totals_line"] == "2.5"
    assert market_ctx["totals_over"] == "1.85"
    assert market_ctx["totals_provider_count"] == 2
    
    # Check temporal defaults
    assert market_ctx["match_hour"] is None
    assert market_ctx["temporal_dim"] == "day"
    assert market_ctx["is_weekend"] == True  # default conservative
    assert market_ctx["is_midweek"] == False
    
    print("✓ _extract_market_context() extracts Qbot 2.3 fields")

def test_decision_trace_inclusion():
    """Test that decision trace includes Qbot 2.3 metrics."""
    # Create a minimal tip
    tip = {
        "edge_pct": 5.0,
        "true_probability": 0.55,
        "confidence": 0.60,
        "implied_probability": 0.50,
        "recommended_selection": "1",
        "status": "active",
        "skip_reason": None,
    }
    
    # Create a mock strategy
    strategy = {
        "_id": "test_strategy",
        "version": "v1",
        "generation": 1,
        "sport_key": "all",
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
        "optimization_notes": {}
    }
    
    # Create market context with Qbot 2.3 data
    market_ctx = {
        "provider_count": 3,
        "spread_pct": 0.02,
        "volatility_dim": "stable",
        "market_trust_factor": 1.0,
        # Qbot 2.3 additions
        "totals_line": "2.75",
        "totals_over": "1.65",
        "totals_provider_count": 2,
        "match_hour": 20,  # prime time
        "temporal_dim": "prime",
        "is_weekend": False,
        "is_midweek": True,
    }
    
    trace = _build_decision_trace(
        tip=tip,
        strategy=strategy,
        bayes_conf=0.55,
        stake_units=10.0,
        kelly_raw=0.2,
        market_ctx=market_ctx,
    )
    
    # Check market_context section exists
    assert "market_context" in trace
    mc = trace["market_context"]
    
    # Check Qbot 2.3 fields are present
    assert mc["totals_line"] == "2.75"
    assert mc["totals_over"] == "1.65"
    assert mc["totals_provider_count"] == 2
    assert mc["match_hour"] == 20
    assert mc["temporal_dim"] == "prime"
    assert mc["is_weekend"] == False
    assert mc["is_midweek"] == True
    assert "synergy_factor" in mc
    # synergy_factor should be calculated based on pick and market_ctx
    # For totals_line=2.75 and totals_over=1.65, pick="1" -> positive synergy 1.05
    # But _get_market_synergy expects market_ctx with totals_provider_count
    # The function is called with market_ctx, which has totals_provider_count=2
    # So synergy should be 1.05
    assert mc["synergy_factor"] == 1.05
    
    print("✓ Decision trace includes Qbot 2.3 metrics")
    
    # Also test with null market_ctx
    trace_no_market = _build_decision_trace(
        tip=tip,
        strategy=strategy,
        bayes_conf=0.55,
        stake_units=10.0,
        kelly_raw=0.2,
        market_ctx=None,
    )
    assert trace_no_market["market_context"] is None
    print("✓ Decision trace handles null market context")

def main():
    """Run all tests."""
    print("Testing Qbot Intelligence 2.3 implementation...")
    print("-" * 50)
    
    test_temporal_dim()
    test_market_synergy()
    test_market_context_extraction()
    test_decision_trace_inclusion()
    
    print("-" * 50)
    print("All tests passed! ✅")
    print("\nAcceptance criteria verified:")
    print("1. Decision trace contains totals line, totals_over, totals_provider_count")
    print("2. Decision trace contains match_hour and temporal_dim")
    print("3. Decision trace contains is_weekend and is_midweek flags")
    print("4. Decision trace contains synergy_factor showing market support")
    print("5. Temporal dimension correctly binned (day/prime/late)")
    print("6. Market synergy correctly calculated with ghost protection")

if __name__ == "__main__":
    main()