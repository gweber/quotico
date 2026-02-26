"""
backend/app/services/historical_service.py

Purpose:
    Historical context service (H2H + form) using Team Tower IDs only.

Dependencies:
    - app.database
    - app.services.team_registry_service
"""

import logging
import time
from datetime import datetime

from bson import ObjectId

import app.database as _db
from app.services.team_registry_service import TeamRegistry

logger = logging.getLogger("quotico.historical_service")

_context_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600


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


_ALL_SOCCER_KEYS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
]
RELATED_SPORT_KEYS: dict[str, list[str]] = {k: _ALL_SOCCER_KEYS for k in _ALL_SOCCER_KEYS}


def sport_keys_for(sport_key: str) -> list[str]:
    return RELATED_SPORT_KEYS.get(sport_key, [sport_key])


def _serialize_match_team_ids(match_doc: dict) -> dict:
    """Return a shallow match copy with team ids converted to strings for API safety."""
    out = dict(match_doc)
    home_id = out.get("home_team_id")
    away_id = out.get("away_team_id")
    if isinstance(home_id, ObjectId):
        out["home_team_id"] = str(home_id)
    if isinstance(away_id, ObjectId):
        out["away_team_id"] = str(away_id)
    return out


async def build_match_context(
    home_team: str,
    away_team: str,
    sport_key: str,
    h2h_limit: int = 10,
    form_limit: int = 10,
    *,
    before_date: datetime | None = None,
) -> dict:
    if not before_date:
        cache_key = f"{home_team}|{away_team}|{sport_key}|{h2h_limit}|{form_limit}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    related_keys = sport_keys_for(sport_key)
    team_registry = TeamRegistry.get()
    home_team_id = await team_registry.resolve(home_team, sport_key)
    away_team_id = await team_registry.resolve(away_team, sport_key)

    proj = {
        "_id": 0,
        "match_date": 1,
        "home_team": 1,
        "away_team": 1,
        "home_team_id": 1,
        "away_team_id": 1,
        "result.home_score": 1,
        "result.away_score": 1,
        "result.outcome": 1,
        "result.home_xg": 1,
        "result.away_xg": 1,
        "sport_key": 1,
    }

    h2h_query: dict = {
        "sport_key": {"$in": related_keys},
        "status": "final",
        "result.outcome": {"$ne": None},
        "$or": [
            {"home_team_id": home_team_id, "away_team_id": away_team_id},
            {"home_team_id": away_team_id, "away_team_id": home_team_id},
        ],
    }
    if before_date:
        h2h_query["match_date"] = {"$lt": before_date}

    h2h_matches = await _db.db.matches.find(h2h_query, proj).sort("match_date", -1).to_list(length=h2h_limit)
    h2h_all = await _db.db.matches.find(
        h2h_query,
        {
            "_id": 0,
            "home_team_id": 1,
            "away_team_id": 1,
            "result.home_score": 1,
            "result.away_score": 1,
            "result.home_xg": 1,
            "result.away_xg": 1,
        },
    ).to_list(length=500)

    h2h_summary = None
    if h2h_all:
        total = len(h2h_all)
        home_wins = 0
        away_wins = 0
        draws = 0
        total_goals = 0
        over_2_5 = 0
        btts = 0
        sum_home_xg = 0.0
        sum_away_xg = 0.0
        xg_count = 0

        for m in h2h_all:
            r = m.get("result", {})
            hg = r.get("home_score", 0) or 0
            ag = r.get("away_score", 0) or 0
            total_goals += hg + ag
            if hg + ag > 2:
                over_2_5 += 1
            if hg > 0 and ag > 0:
                btts += 1

            h_xg = r.get("home_xg")
            a_xg = r.get("away_xg")
            if h_xg is not None and a_xg is not None:
                if m.get("home_team_id") == home_team_id:
                    sum_home_xg += h_xg
                    sum_away_xg += a_xg
                else:
                    sum_home_xg += a_xg
                    sum_away_xg += h_xg
                xg_count += 1

            if m.get("home_team_id") == home_team_id:
                if hg > ag:
                    home_wins += 1
                elif hg < ag:
                    away_wins += 1
                else:
                    draws += 1
            else:
                if ag > hg:
                    home_wins += 1
                elif ag < hg:
                    away_wins += 1
                else:
                    draws += 1

        h2h_summary = {
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
            h2h_summary["avg_home_xg"] = round(sum_home_xg / xg_count, 2)
            h2h_summary["avg_away_xg"] = round(sum_away_xg / xg_count, 2)

    async def get_form(team_id) -> list[dict]:
        query: dict = {
            "sport_key": {"$in": related_keys},
            "status": "final",
            "result.outcome": {"$ne": None},
            "$or": [{"home_team_id": team_id}, {"away_team_id": team_id}],
        }
        if before_date:
            query["match_date"] = {"$lt": before_date}
        return await _db.db.matches.find(query, proj).sort("match_date", -1).to_list(length=form_limit)

    home_form = await get_form(home_team_id)
    away_form = await get_form(away_team_id)

    response = {
        "h2h": {
            "summary": h2h_summary,
            "matches": [_serialize_match_team_ids(m) for m in h2h_matches],
        } if h2h_summary else None,
        "home_form": [_serialize_match_team_ids(m) for m in home_form],
        "away_form": [_serialize_match_team_ids(m) for m in away_form],
        "home_team_id": str(home_team_id) if isinstance(home_team_id, ObjectId) else home_team_id,
        "away_team_id": str(away_team_id) if isinstance(away_team_id, ObjectId) else away_team_id,
    }
    if not before_date:
        cache_set(cache_key, response)
    return response
