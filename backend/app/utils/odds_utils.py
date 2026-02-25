"""Fast canonical H2H odds normalization (High-Performance Version)."""

from __future__ import annotations
from typing import Any

# Konstanten für schnellen Lookup
_META_KEYS = {"updated_at", "h2h", "totals", "bookmakers", "spreads", "closing_line"}
_HOME_LOOKUP = ("1", "home", "Home", "H", "h")
_DRAW_LOOKUP = ("X", "draw", "Draw", "D", "x", "unentschieden", "Remis")
_AWAY_LOOKUP = ("2", "away", "Away", "A", "a")

def _to_float(value: Any) -> float | None:
    """Sichere Konvertierung in Float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _find_value(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Sucht Keys direkt ohne neue Speicherallokation (Case-Insensitive Ersatz)."""
    for k in keys:
        # Direkter Zugriff ist Nanosekunden-schnell
        if k in data:
            return _to_float(data[k])
    return None

def normalize_the_odds_api_market(market: dict, home_team: str, away_team: str) -> dict[str, float] | None:
    """Spezial-Adapter für das strukturierte Format von TheOddsAPI."""
    if "outcomes" not in market:
        return None
    
    res = {}
    for outcome in market["outcomes"]:
        name = outcome.get("name", "")
        price = _to_float(outcome.get("price"))
        if price is None: continue
        
        if name == home_team:
            res["1"] = price
        elif name == away_team:
            res["2"] = price
        elif name.lower() in ("draw", "unentschieden", "x", "remis"):
            res["X"] = price
            
    return res if ("1" in res and "2" in res) else None

def fast_normalize_h2h(raw_market: dict[str, Any] | None) -> dict[str, float] | None:
    """Normalisiert einen flachen Markt (CSV/Bookie-Entry) zu 1/X/2."""
    if not isinstance(raw_market, dict) or not raw_market:
        return None

    h = _find_value(raw_market, _HOME_LOOKUP)
    d = _find_value(raw_market, _DRAW_LOOKUP)
    a = _find_value(raw_market, _AWAY_LOOKUP)

    # Soccer pipeline expects strict 3-way odds (1/X/2), all > 0.
    if h is None or d is None or a is None:
        return None
    if h <= 0 or d <= 0 or a <= 0:
        return None

    return {
        "1": h,
        "X": d,
        "2": a
    }

def extract_h2h_from_payload(
    odds: dict[str, Any] | None,
    odds_h2h: dict[str, Any] | None = None,
) -> dict[str, float] | None:
    """
    Zentrale Logik: Extrahiert das kanonische odds.h2h aus jedem Payload.
    Wird vom Scraper (CSV) und Live-Sync (API) genutzt.
    """
    # 1. Prio: Explizit mitgegebene H2H Daten
    normalized = fast_normalize_h2h(odds_h2h)
    if normalized:
        return normalized

    if not isinstance(odds, dict) or not odds:
        return None

    # 2. Prio: Bereits vorhandene Struktur odds.h2h
    normalized = fast_normalize_h2h(odds.get("h2h"))
    if normalized:
        return normalized

    # 3. Prio: Suche in den Buchmachern (Pinnacle, Bet365 etc.)
    for key, market in odds.items():
        if str(key).lower() in _META_KEYS:
            continue
        if isinstance(market, dict):
            normalized = fast_normalize_h2h(market)
            if normalized:
                return normalized
    return None
