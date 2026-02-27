"""
backend/app/services/shared_gate_logic.py

Purpose:
    Shared gate logic used by live tip generation and backtests to enforce
    consistent signal emission decisions.

Dependencies:
    - app.services.policy_service
    - app.services.input_sanity_service
    - app.services.market_inference_service
    - app.services.philosophical_guardrail_service
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.input_sanity_service import run_input_sanity_checks
from app.services.market_inference_service import infer_market_context
from app.services.philosophical_guardrail_service import (
    evaluate_absurdity,
    evaluate_epistemic_humility,
    validate_moral_trace,
)
from app.services.policy_service import get_policy_service


@dataclass
class GateDecision:
    allowed: bool
    reason_code: str
    damping_factor: float
    policy_version_used: str
    gate_results: dict[str, Any]


async def can_signal_be_emitted(match_ctx: dict[str, Any], policy_ctx: dict[str, Any] | None = None) -> GateDecision:
    """Evaluate sanity/risk/philosophical checks and return unified decision."""
    policy_service = get_policy_service()

    policy_keys = [
        "MAX_XG_PER_TEAM",
        "MAX_ODDS_JUMP_PCT",
        "MIN_EXPECTED_EVENT_COUNT",
        "MARKET_DRIFT_ALERT_PCT",
        "MARKET_BAYES_PRIOR_WEIGHT_MIN",
        "MARKET_BAYES_PRIOR_WEIGHT_MAX",
        "META_CONFIDENCE_FLOOR",
        "SINGULARITY_DISTANCE_MAX",
        "YIELD_ON_EPISTEMIC_GAP",
        "ABSURDITY_ZSCORE_MAX",
        "MORAL_TRACE_REQUIRED",
    ]
    snapshot = policy_ctx or await policy_service.get_snapshot(policy_keys)

    max_xg_per_team = float(snapshot.get("MAX_XG_PER_TEAM", 8.0))
    max_odds_jump_pct = float(snapshot.get("MAX_ODDS_JUMP_PCT", 0.60))
    min_expected_event_count = int(snapshot.get("MIN_EXPECTED_EVENT_COUNT", 1))

    sanity = run_input_sanity_checks(
        match_ctx,
        max_xg_per_team=max_xg_per_team,
        max_odds_jump_pct=max_odds_jump_pct,
        min_expected_event_count=min_expected_event_count,
    )
    gate_results: dict[str, Any] = {
        "sanity": {
            "allowed": sanity.allowed,
            "reason_code": sanity.reason_code,
            "warnings": sanity.warnings,
        }
    }
    if not sanity.allowed:
        return GateDecision(
            allowed=False,
            reason_code=sanity.reason_code,
            damping_factor=0.0,
            policy_version_used=str(snapshot.get("policy_version_used", "runtime")),
            gate_results=gate_results,
        )

    market = infer_market_context(
        p_model=float(match_ctx.get("p_model", 1 / 3)),
        odds_h2h=match_ctx.get("odds_h2h") or {},
        selection=str(match_ctx.get("selection") or "1"),
        drift_alert_pct=float(snapshot.get("MARKET_DRIFT_ALERT_PCT", 0.12)),
        bayes_weight_min=float(snapshot.get("MARKET_BAYES_PRIOR_WEIGHT_MIN", 0.05)),
        bayes_weight_max=float(snapshot.get("MARKET_BAYES_PRIOR_WEIGHT_MAX", 0.35)),
        xg_delta=float(match_ctx.get("xg_delta", 0.0)),
        has_event_update=bool(match_ctx.get("has_event_update", False)),
    )
    gate_results["market"] = {
        "p_model": market.p_model,
        "p_market": market.p_market,
        "p_post": market.p_post,
        "latency_gap": market.latency_gap,
        "drift_alert": market.drift_alert,
        "bayes_weight_used": market.bayes_weight_used,
    }

    epi_allowed, meta_conf, singularity_distance, epi_reason = evaluate_epistemic_humility(
        case_similarity=float(match_ctx.get("case_similarity", 0.7)),
        data_integrity_score=float(match_ctx.get("data_integrity_score", 1.0)),
        model_agreement_score=float(match_ctx.get("model_agreement_score", 0.7)),
        meta_confidence_floor=float(snapshot.get("META_CONFIDENCE_FLOOR", 0.55)),
        singularity_distance_max=float(snapshot.get("SINGULARITY_DISTANCE_MAX", 0.80)),
        yield_on_epistemic_gap=bool(snapshot.get("YIELD_ON_EPISTEMIC_GAP", True)),
    )
    gate_results["epistemic"] = {
        "meta_confidence": meta_conf,
        "singularity_distance": singularity_distance,
        "reason_code": epi_reason,
    }
    if not epi_allowed:
        return GateDecision(
            allowed=False,
            reason_code=epi_reason,
            damping_factor=0.0,
            policy_version_used=str(snapshot.get("policy_version_used", "runtime")),
            gate_results=gate_results,
        )

    absurd_allowed, absurd_score, absurd_reason = evaluate_absurdity(
        z_score=float(match_ctx.get("absurdity_z_score", 0.0)),
        z_score_max=float(snapshot.get("ABSURDITY_ZSCORE_MAX", 4.0)),
    )
    gate_results["absurdity"] = {
        "score": absurd_score,
        "reason_code": absurd_reason,
    }
    if not absurd_allowed:
        return GateDecision(
            allowed=False,
            reason_code=absurd_reason,
            damping_factor=0.0,
            policy_version_used=str(snapshot.get("policy_version_used", "runtime")),
            gate_results=gate_results,
        )

    if bool(snapshot.get("MORAL_TRACE_REQUIRED", True)):
        moral_ok, moral_reason = validate_moral_trace(match_ctx.get("moral_trace"))
        gate_results["moral_trace"] = {
            "ok": moral_ok,
            "reason_code": moral_reason,
        }
        if not moral_ok:
            return GateDecision(
                allowed=False,
                reason_code=moral_reason,
                damping_factor=0.0,
                policy_version_used=str(snapshot.get("policy_version_used", "runtime")),
                gate_results=gate_results,
            )

    damping_factor = 1.0
    reason_code = "OK_SIGNAL_EMITTED"
    if market.drift_alert:
        damping_factor = 0.75
        reason_code = "WARN_MARKET_DRIFT_WITHOUT_DATA_TRIGGER"

    return GateDecision(
        allowed=True,
        reason_code=reason_code,
        damping_factor=damping_factor,
        policy_version_used=str(snapshot.get("policy_version_used", "runtime")),
        gate_results=gate_results,
    )
