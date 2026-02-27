"""
backend/app/services/philosophical_guardrail_service.py

Purpose:
    Epistemic humility, moral trace, and absurdity filtering for decision
    integrity before emitting tips.

Dependencies:
    - dataclasses
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PhilosophicalCheckResult:
    allowed: bool
    reason_code: str = "OK_SIGNAL_EMITTED"
    meta_confidence: float = 1.0
    singularity_distance: float = 0.0
    absurdity_score: float = 0.0
    warnings: list[str] | None = None


def evaluate_epistemic_humility(
    *,
    case_similarity: float,
    data_integrity_score: float,
    model_agreement_score: float,
    meta_confidence_floor: float = 0.55,
    singularity_distance_max: float = 0.80,
    yield_on_epistemic_gap: bool = True,
) -> tuple[bool, float, float, str]:
    cs = min(1.0, max(0.0, float(case_similarity)))
    ds = min(1.0, max(0.0, float(data_integrity_score)))
    ms = min(1.0, max(0.0, float(model_agreement_score)))

    meta_confidence = 0.4 * cs + 0.3 * ds + 0.3 * ms
    singularity_distance = 1.0 - cs

    if yield_on_epistemic_gap and (
        meta_confidence < float(meta_confidence_floor)
        or singularity_distance > float(singularity_distance_max)
    ):
        return False, meta_confidence, singularity_distance, "ERR_EPISTEMIC_HUMILITY_YIELD"

    if meta_confidence < float(meta_confidence_floor):
        return True, meta_confidence, singularity_distance, "WARN_LOW_META_CONFIDENCE"

    return True, meta_confidence, singularity_distance, "OK_SIGNAL_EMITTED"


def evaluate_absurdity(
    *,
    z_score: float,
    z_score_max: float = 4.0,
) -> tuple[bool, float, str]:
    score = abs(float(z_score))
    if score > float(z_score_max):
        return False, score, "ERR_ABSURDITY_FILTER_BLOCK"
    if score > float(z_score_max) * 0.8:
        return True, score, "WARN_ABSURDITY_CAUTION"
    return True, score, "OK_SIGNAL_EMITTED"


def validate_moral_trace(moral_trace: dict[str, Any] | None) -> tuple[bool, str]:
    required = {
        "claim",
        "grounds",
        "counterfactual",
        "user_value_if_wrong",
        "limits_of_knowledge",
    }
    if not isinstance(moral_trace, dict):
        return False, "ERR_MORAL_TRACE_INCOMPLETE"
    missing = [k for k in required if not moral_trace.get(k)]
    if missing:
        return False, "ERR_MORAL_TRACE_INCOMPLETE"
    return True, "OK_SIGNAL_EMITTED"
