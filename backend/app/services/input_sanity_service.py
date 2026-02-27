"""
backend/app/services/input_sanity_service.py

Purpose:
    Input sanity checks for match/tip contexts before Shared Gate Logic.
    Prevents pathological data from polluting downstream math.

Dependencies:
    - dataclasses
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InputSanityResult:
    allowed: bool
    reason_code: str = "OK_SIGNAL_EMITTED"
    warnings: list[str] = field(default_factory=list)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def run_input_sanity_checks(
    context: dict[str, Any],
    *,
    max_xg_per_team: float = 8.0,
    max_odds_jump_pct: float = 0.60,
    min_expected_event_count: int = 1,
) -> InputSanityResult:
    """Validate raw context values with hard safety bounds."""
    xg_home = _safe_float(context.get("xg_home"))
    xg_away = _safe_float(context.get("xg_away"))

    if xg_home is not None and xg_home > max_xg_per_team:
        return InputSanityResult(allowed=False, reason_code="ERR_SANITY_XG_OUTLIER")
    if xg_away is not None and xg_away > max_xg_per_team:
        return InputSanityResult(allowed=False, reason_code="ERR_SANITY_XG_OUTLIER")

    opening = _safe_float(context.get("odds_opening"))
    current = _safe_float(context.get("odds_current"))
    if opening and current and opening > 0:
        jump = abs(current - opening) / opening
        if jump > max_odds_jump_pct:
            return InputSanityResult(allowed=False, reason_code="ERR_SANITY_ODDS_DEVIATION")

    event_count = int(context.get("event_count") or 0)
    status = str(context.get("match_status") or "").upper()
    if status == "FINISHED" and event_count < min_expected_event_count:
        return InputSanityResult(
            allowed=True,
            reason_code="OK_SIGNAL_EMITTED",
            warnings=["WARN_SANITY_LOW_EVENT_DENSITY"],
        )

    return InputSanityResult(allowed=True)
