"""Historical match context service.

Provides H2H and form context by querying the unified ``matches`` collection
for matches with ``status: "final"``.  Team name resolution is delegated to
``team_mapping_service``.

The old ``historical_matches`` collection and ``archive_resolved_match()``
function are no longer needed — resolved matches stay in the same collection.
"""

import logging
import time
from datetime import datetime

import app.database as _db
from app.services.team_mapping_service import resolve_team, team_name_key

logger = logging.getLogger("quotico.historical_service")

# ---------------------------------------------------------------------------
# In-memory cache for match-context responses (historical data rarely changes)
# ---------------------------------------------------------------------------
_context_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600  # 1 hour


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


# ---------------------------------------------------------------------------
# Related sport keys: H2H spans across divisions (e.g. BL1 + BL2)
# ---------------------------------------------------------------------------
_ALL_SOCCER_KEYS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_uefa_champs_league",
]

RELATED_SPORT_KEYS: dict[str, list[str]] = {k: _ALL_SOCCER_KEYS for k in _ALL_SOCCER_KEYS}


def sport_keys_for(sport_key: str) -> list[str]:
    """Return the sport_key(s) to query for H2H/form (spans related leagues)."""
    return RELATED_SPORT_KEYS.get(sport_key, [sport_key])


# ---------------------------------------------------------------------------
# Match context builder (H2H + form) — queries unified matches collection
# ---------------------------------------------------------------------------

async def build_match_context(
    home_team: str,
    away_team: str,
    sport_key: str,
    h2h_limit: int = 10,
    form_limit: int = 10,
    *,
    before_date: datetime | None = None,
) -> dict:
    """Core logic: resolve team names, fetch H2H + form from unified collection."""
    # Skip cache when backfilling (queries are unique per date)
    if not before_date:
        cache_key = f"{home_team}|{away_team}|{sport_key}|{h2h_limit}|{form_limit}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    related_keys = sport_keys_for(sport_key)

    # Resolve via team_mapping_service
    home_result = await resolve_team(home_team, sport_key)
    away_result = await resolve_team(away_team, sport_key)

    home_key = home_result[2] if home_result else team_name_key(home_team)
    away_key = away_result[2] if away_result else team_name_key(away_team)

    if not home_key or not away_key:
        result: dict = {"h2h": None, "home_form": None, "away_form": None}
        if not before_date:
            cache_set(cache_key, result)
        return result

    proj = {
        "_id": 0,
        "match_date": 1, "home_team": 1, "away_team": 1,
        "home_team_key": 1, "away_team_key": 1,
        "result.home_score": 1, "result.away_score": 1, "result.outcome": 1,
        "season_label": 1, "sport_key": 1,
    }

    # H2H query: finalized matches between these two teams across related leagues
    h2h_query: dict = {
        "sport_key": {"$in": related_keys},
        "status": "final",
        "result.outcome": {"$ne": None},
        "$or": [
            {"home_team_key": home_key, "away_team_key": away_key},
            {"home_team_key": away_key, "away_team_key": home_key},
        ],
    }
    if before_date:
        h2h_query["match_date"] = {"$lt": before_date}

    h2h_matches = await _db.db.matches.find(
        h2h_query, proj,
    ).sort("match_date", -1).to_list(length=h2h_limit)

    # Compute H2H summary from ALL matches
    h2h_summary = None
    h2h_all = await _db.db.matches.find(
        h2h_query,
        {"_id": 0, "home_team_key": 1, "result.home_score": 1, "result.away_score": 1},
    ).to_list(length=500)

    if h2h_all:
        total = len(h2h_all)
        home_wins = 0
        away_wins = 0
        draws = 0
        total_goals = 0
        over_2_5 = 0
        btts = 0

        for m in h2h_all:
            r = m.get("result", {})
            hg = r.get("home_score", 0) or 0
            ag = r.get("away_score", 0) or 0
            total_goals += hg + ag
            if hg + ag > 2:
                over_2_5 += 1
            if hg > 0 and ag > 0:
                btts += 1

            if m["home_team_key"] == home_key:
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
        }

    # Form: last N finalized matches for each team
    async def get_form(t_key: str) -> list[dict]:
        form_query: dict = {
            "sport_key": {"$in": related_keys},
            "status": "final",
            "result.outcome": {"$ne": None},
            "$or": [
                {"home_team_key": t_key},
                {"away_team_key": t_key},
            ],
        }
        if before_date:
            form_query["match_date"] = {"$lt": before_date}
        return await _db.db.matches.find(
            form_query,
            proj,
        ).sort("match_date", -1).to_list(length=form_limit)

    home_form = await get_form(home_key)
    away_form = await get_form(away_key)

    response = {
        "h2h": {
            "summary": h2h_summary,
            "matches": h2h_matches,
        } if h2h_summary else None,
        "home_form": home_form,
        "away_form": away_form,
        "home_team_key": home_key,
        "away_team_key": away_key,
    }
    if not before_date:
        cache_set(cache_key, response)
    return response
