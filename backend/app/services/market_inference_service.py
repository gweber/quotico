"""
backend/app/services/market_inference_service.py

Purpose:
    Market inference utilities for model-vs-market drift detection and Bayesian
    posterior blending with implied probabilities.

Dependencies:
    - dataclasses
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MarketInferenceResult:
    p_model: float
    p_market: float
    p_post: float
    latency_gap: float
    drift_alert: bool
    bayes_weight_used: float


def implied_probability_from_odds(odds: dict[str, float]) -> dict[str, float]:
    """Convert 1X2 odds to vig-inclusive implied probs and normalize."""
    vals: dict[str, float] = {}
    inv_sum = 0.0
    for key in ("1", "X", "2"):
        odd = odds.get(key)
        if not odd or odd <= 1.0:
            vals[key] = 0.0
            continue
        inv = 1.0 / float(odd)
        vals[key] = inv
        inv_sum += inv

    if inv_sum <= 0:
        return {"1": 1 / 3, "X": 1 / 3, "2": 1 / 3}
    return {k: (v / inv_sum) for k, v in vals.items()}


def bayesian_market_update(
    *,
    p_model: float,
    p_market: float,
    weight: float,
) -> float:
    w = min(1.0, max(0.0, float(weight)))
    p = (1.0 - w) * float(p_model) + w * float(p_market)
    return min(1.0, max(0.0, p))


def infer_market_context(
    *,
    p_model: float,
    odds_h2h: dict[str, float],
    selection: str,
    drift_alert_pct: float = 0.12,
    bayes_weight_min: float = 0.05,
    bayes_weight_max: float = 0.35,
    xg_delta: float = 0.0,
    has_event_update: bool = False,
) -> MarketInferenceResult:
    probs = implied_probability_from_odds(odds_h2h)
    p_market = float(probs.get(selection, 1 / 3))
    p_model = min(1.0, max(0.0, float(p_model)))
    latency_gap = p_market - p_model

    # Strengthen prior when drift is large and not explained by hard updates.
    base = min(1.0, abs(latency_gap) / max(float(drift_alert_pct), 1e-9))
    w = bayes_weight_min + (bayes_weight_max - bayes_weight_min) * base
    if abs(float(xg_delta)) > 1e-6 or has_event_update:
        w *= 0.75

    p_post = bayesian_market_update(p_model=p_model, p_market=p_market, weight=w)
    drift_alert = abs(latency_gap) >= float(drift_alert_pct) and abs(float(xg_delta)) <= 1e-6 and not has_event_update

    return MarketInferenceResult(
        p_model=p_model,
        p_market=p_market,
        p_post=p_post,
        latency_gap=latency_gap,
        drift_alert=drift_alert,
        bayes_weight_used=w,
    )
