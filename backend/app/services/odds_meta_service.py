"""
backend/app/services/odds_meta_service.py

Purpose:
    Read helpers for odds_meta market values from match documents. Provides a
    stable access layer for services that need h2h/totals/spreads and updated
    timestamps without touching raw odds events.

Dependencies:
    - app.utils
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils import ensure_utc


def _market_node(match: dict[str, Any], market: str) -> dict[str, Any]:
    return (
        (match.get("odds_meta") or {}).get("markets", {}).get(market, {})
        if isinstance(match, dict)
        else {}
    )


def get_current_market(match: dict[str, Any], market: str) -> dict[str, Any]:
    node = _market_node(match, market)
    current = node.get("current")
    return dict(current) if isinstance(current, dict) else {}


def get_market_updated_at(match: dict[str, Any], market: str) -> datetime | None:
    node = _market_node(match, market)
    ts = node.get("updated_at")
    if ts:
        return ensure_utc(ts)
    root = (match.get("odds_meta") or {}).get("updated_at") if isinstance(match, dict) else None
    return ensure_utc(root) if root else None


def build_legacy_like_odds(match: dict[str, Any]) -> dict[str, Any]:
    """Compatibility payload with h2h/totals/spreads + updated_at from odds_meta."""
    h2h = get_current_market(match, "h2h")
    totals = get_current_market(match, "totals")
    spreads = get_current_market(match, "spreads")
    ts = get_market_updated_at(match, "h2h") or get_market_updated_at(match, "totals") or get_market_updated_at(match, "spreads")
    return {
        "h2h": h2h,
        "totals": totals,
        "spreads": spreads,
        "updated_at": ts,
    }
