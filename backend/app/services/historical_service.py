"""
backend/app/services/historical_service.py

Purpose:
    H2H and form service for v3 matches.
    All queries target matches_v3 using Sportmonks sm_id team identity.

Dependencies:
    - app.database
"""

import logging
import time
from datetime import datetime

import app.database as _db

logger = logging.getLogger("quotico.historical_service")

# ---------------------------------------------------------------------------
# v3 H2H cache
# ---------------------------------------------------------------------------
_context_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600


def _cache_key(id_a: int, id_b: int) -> str:
    """Symmetric cache key — A vs B == B vs A."""
    lo, hi = (id_a, id_b) if id_a <= id_b else (id_b, id_a)
    return f"v3|{lo}|{hi}"


def cache_get(key: str) -> dict | None:
    entry = _context_cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    if entry:
        del _context_cache[key]
    return None


def cache_set(key: str, value: dict) -> None:
    _context_cache[key] = (time.monotonic() + _CACHE_TTL, value)


def clear_context_cache() -> None:
    _context_cache.clear()
    logger.info("Match-context cache cleared")


def _map_match(doc: dict, home_sm_id: int) -> dict:
    """Map a matches_v3 document to the frontend-compatible H2H match shape."""
    teams = doc.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    hs = home.get("score")
    as_ = away.get("score")
    if hs is not None and as_ is not None:
        outcome = "1" if hs > as_ else "2" if as_ > hs else "X"
    else:
        outcome = None
    start_at = doc.get("start_at")
    return {
        "match_date": start_at.isoformat() if isinstance(start_at, datetime) else str(start_at or ""),
        "home_team": home.get("name") or "",
        "away_team": away.get("name") or "",
        "home_team_id": home.get("sm_id"),
        "away_team_id": away.get("sm_id"),
        "finish_type": doc.get("finish_type"),
        "result": {
            "home_score": hs,
            "away_score": as_,
            "outcome": outcome,
            "home_xg": home.get("xg"),
            "away_xg": away.get("xg"),
        },
    }


def _compute_summary(matches: list[dict], home_sm_id: int) -> dict | None:
    """Compute H2H summary stats from mapped match dicts."""
    if not matches:
        return None
    total = len(matches)
    home_wins = 0
    away_wins = 0
    draws = 0
    total_goals = 0
    over_2_5 = 0
    btts = 0
    sum_home_xg = 0.0
    sum_away_xg = 0.0
    sum_xg_diff = 0.0
    xg_count = 0

    for m in matches:
        r = m.get("result") or {}
        hs = r.get("home_score") or 0
        as_ = r.get("away_score") or 0
        total_goals += hs + as_
        if hs + as_ > 2:
            over_2_5 += 1
        if hs > 0 and as_ > 0:
            btts += 1

        h_xg = r.get("home_xg")
        a_xg = r.get("away_xg")
        is_home = m.get("home_team_id") == home_sm_id
        if h_xg is not None and a_xg is not None:
            if is_home:
                sum_home_xg += h_xg
                sum_away_xg += a_xg
                sum_xg_diff += h_xg - a_xg
            else:
                sum_home_xg += a_xg
                sum_away_xg += h_xg
                sum_xg_diff += a_xg - h_xg
            xg_count += 1

        if is_home:
            if hs > as_:
                home_wins += 1
            elif hs < as_:
                away_wins += 1
            else:
                draws += 1
        else:
            if as_ > hs:
                home_wins += 1
            elif as_ < hs:
                away_wins += 1
            else:
                draws += 1

    summary: dict = {
        "total": total,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "avg_goals": round(total_goals / total, 1),
        "over_2_5_pct": round(over_2_5 / total, 2),
        "btts_pct": round(btts / total, 2),
        "xg_samples_used": xg_count,
        "xg_samples_total": total,
    }
    if xg_count > 0:
        summary["avg_home_xg"] = round(sum_home_xg / xg_count, 2)
        summary["avg_away_xg"] = round(sum_away_xg / xg_count, 2)
        summary["avg_xg_diff"] = round(sum_xg_diff / xg_count, 2)
    return summary


_H2H_QUERY_PROJ = {
    "_id": 0,
    "start_at": 1,
    "teams.home.name": 1,
    "teams.home.sm_id": 1,
    "teams.home.score": 1,
    "teams.home.xg": 1,
    "teams.away.name": 1,
    "teams.away.sm_id": 1,
    "teams.away.score": 1,
    "teams.away.xg": 1,
    "finish_type": 1,
}


async def build_h2h(
    home_sm_id: int,
    away_sm_id: int,
    *,
    limit: int = 10,
    skip: int = 0,
) -> dict | None:
    """Build H2H data for two teams identified by Sportmonks sm_id."""
    query = {
        "status": "FINISHED",
        "$or": [
            {"teams.home.sm_id": home_sm_id, "teams.away.sm_id": away_sm_id},
            {"teams.home.sm_id": away_sm_id, "teams.away.sm_id": home_sm_id},
        ],
    }
    # Fetch paginated slice for display
    docs = await _db.db.matches_v3.find(
        query, _H2H_QUERY_PROJ,
    ).sort("start_at", -1).skip(skip).limit(limit).to_list(length=limit)
    matches = [_map_match(d, home_sm_id) for d in docs]

    # For summary: fetch all (up to 500) to compute accurate stats
    if skip == 0:
        all_docs = await _db.db.matches_v3.find(
            query, _H2H_QUERY_PROJ,
        ).to_list(length=500)
        all_matches = [_map_match(d, home_sm_id) for d in all_docs]
        summary = _compute_summary(all_matches, home_sm_id)
    else:
        summary = None

    if not matches and not summary:
        return None
    return {"summary": summary, "matches": matches}


async def build_form(team_sm_id: int, *, limit: int = 5) -> list[dict]:
    """Get recent form for a team (last N finished matches)."""
    query = {
        "status": "FINISHED",
        "$or": [
            {"teams.home.sm_id": team_sm_id},
            {"teams.away.sm_id": team_sm_id},
        ],
    }
    docs = await _db.db.matches_v3.find(
        query, _H2H_QUERY_PROJ,
    ).sort("start_at", -1).limit(limit).to_list(length=limit)
    return [_map_match(d, team_sm_id) for d in docs]


async def build_match_context(
    home_sm_id: int,
    away_sm_id: int,
    *,
    h2h_limit: int = 10,
    form_limit: int = 5,
) -> dict:
    """Build complete match context: H2H + form for both teams."""
    key = _cache_key(home_sm_id, away_sm_id)
    cached = cache_get(key)
    if cached is not None:
        return cached

    import asyncio
    h2h, home_form, away_form = await asyncio.gather(
        build_h2h(home_sm_id, away_sm_id, limit=h2h_limit),
        build_form(home_sm_id, limit=form_limit),
        build_form(away_sm_id, limit=form_limit),
    )
    result = {
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "home_team_id": home_sm_id,
        "away_team_id": away_sm_id,
    }
    cache_set(key, result)
    return result
