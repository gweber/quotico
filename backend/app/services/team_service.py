"""Team profile service.

Aggregates team data from the unified ``matches`` collection and
``team_mappings`` for the team detail page.
"""

import logging
import re

import app.database as _db
from app.services.historical_service import sport_keys_for
from app.services.team_mapping_service import (
    team_name_key,
    derive_season_year,
    season_code,
    season_label,
)
from app.utils import utcnow

logger = logging.getLogger("quotico.team_service")


async def search_teams(
    query: str,
    sport_key: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search teams by name across team_mappings."""
    escaped = re.escape(query)
    filt: dict = {
        "$or": [
            {"display_name": {"$regex": escaped, "$options": "i"}},
            {"names": {"$regex": escaped, "$options": "i"}},
        ],
    }
    if sport_key:
        filt["sport_keys"] = sport_key

    docs = await _db.db.team_mappings.find(filt).sort(
        "display_name", 1,
    ).limit(limit).to_list(length=limit)

    return [
        {
            "team_key": team_name_key(doc["display_name"]),
            "display_name": doc["display_name"],
            "slug": team_name_key(doc["display_name"]).replace(" ", "-"),
            "sport_keys": doc.get("sport_keys", []),
            "current_league": doc.get("sport_keys", [None])[0],
        }
        for doc in docs
    ]


async def get_team_profile(team_slug: str, sport_key: str | None = None) -> dict | None:
    """Build a full team profile from a URL slug.

    Slug format: lowercase, hyphens (e.g. "bayern-munich").
    Backend converts hyphens to spaces for team_name_key resolution.
    """
    search_key = team_slug.replace("-", " ").strip()

    if sport_key:
        related_keys = sport_keys_for(sport_key)
    else:
        related_keys = sport_keys_for("soccer_germany_bundesliga")

    # Try to find a mapping whose team_name_key matches
    team_key = team_name_key(search_key)
    display_name = await _get_display_name(team_key)

    if not display_name:
        # No team mapping found — check if any match uses this key
        exists = await _db.db.matches.find_one(
            {
                "$or": [{"home_team_key": team_key}, {"away_team_key": team_key}],
            },
            {"_id": 1},
        )
        if not exists:
            return None
        display_name = team_key.title()

    # Determine which sport_keys this team appears in
    team_sport_keys = await _db.db.matches.distinct(
        "sport_key",
        {
            "status": "final",
            "$or": [{"home_team_key": team_key}, {"away_team_key": team_key}],
        },
    )

    season_stats = await get_team_season_stats(team_key, related_keys)
    recent = await _get_recent_results(team_key, related_keys, limit=15)
    form = _compute_form(recent, team_key)
    upcoming = await get_team_upcoming_matches(team_key, related_keys)

    return {
        "team_key": team_key,
        "display_name": display_name,
        "sport_keys": team_sport_keys or (related_keys[:1] if related_keys else []),
        "form": form[:5],
        "recent_results": recent,
        "season_stats": season_stats,
        "upcoming_matches": upcoming,
    }


async def _get_display_name(team_key: str) -> str | None:
    """Find the best display name for a team_key from team_mappings."""
    # Fetch all mappings and find the one whose display_name normalizes to team_key
    docs = await _db.db.team_mappings.find(
        {}, {"display_name": 1},
    ).to_list(length=5000)

    for doc in docs:
        if team_name_key(doc["display_name"]) == team_key:
            return doc["display_name"]

    return None


async def _get_recent_results(
    team_key: str, related_keys: list[str], limit: int = 15,
) -> list[dict]:
    """Fetch recent finalized matches for a team."""
    proj = {
        "_id": 0,
        "match_date": 1, "home_team": 1, "away_team": 1,
        "home_team_key": 1, "away_team_key": 1,
        "result.home_score": 1, "result.away_score": 1, "result.outcome": 1,
        "season_label": 1, "sport_key": 1,
    }
    return await _db.db.matches.find(
        {
            "sport_key": {"$in": related_keys},
            "status": "final",
            "$or": [
                {"home_team_key": team_key},
                {"away_team_key": team_key},
            ],
        },
        proj,
    ).sort("match_date", -1).to_list(length=limit)


def _compute_form(matches: list[dict], team_key: str) -> list[str]:
    """Derive W/D/L form string from recent results."""
    form: list[str] = []
    for m in matches:
        r = m.get("result", {})
        hg = r.get("home_score", 0) or 0
        ag = r.get("away_score", 0) or 0
        is_home = m.get("home_team_key") == team_key

        if hg == ag:
            form.append("D")
        elif (hg > ag and is_home) or (ag > hg and not is_home):
            form.append("W")
        else:
            form.append("L")
    return form


async def get_team_season_stats(
    team_key: str, related_keys: list[str],
) -> dict | None:
    """Aggregate season stats from finalized matches for the current season."""
    now = utcnow()
    sy = derive_season_year(now)
    sc = season_code(sy)

    matches = await _db.db.matches.find(
        {
            "sport_key": {"$in": related_keys},
            "status": "final",
            "season": sc,
            "$or": [
                {"home_team_key": team_key},
                {"away_team_key": team_key},
            ],
        },
        {
            "_id": 0, "home_team_key": 1,
            "result.home_score": 1, "result.away_score": 1,
            "sport_key": 1,
        },
    ).to_list(length=500)

    if not matches:
        return None

    wins = draws = losses = gf = ga = 0
    home_w = home_d = home_l = 0
    away_w = away_d = away_l = 0

    for m in matches:
        r = m.get("result", {})
        hg = r.get("home_score", 0) or 0
        ag = r.get("away_score", 0) or 0
        is_home = m["home_team_key"] == team_key

        if is_home:
            gf += hg
            ga += ag
            if hg > ag:
                wins += 1; home_w += 1
            elif hg == ag:
                draws += 1; home_d += 1
            else:
                losses += 1; home_l += 1
        else:
            gf += ag
            ga += hg
            if ag > hg:
                wins += 1; away_w += 1
            elif ag == hg:
                draws += 1; away_d += 1
            else:
                losses += 1; away_l += 1

    return {
        "season_label": season_label(sy),
        "matches_played": len(matches),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_scored": gf,
        "goals_conceded": ga,
        "goal_difference": gf - ga,
        "points": wins * 3 + draws,
        "home_record": {"w": home_w, "d": home_d, "l": home_l},
        "away_record": {"w": away_w, "d": away_d, "l": away_l},
    }


async def get_team_upcoming_matches(
    team_key: str, related_keys: list[str], limit: int = 10,
) -> list[dict]:
    """Find upcoming matches where this team is playing.

    Uses home_team_key/away_team_key directly — no alias lookup needed.
    """
    matches = await _db.db.matches.find(
        {
            "status": "scheduled",
            "$or": [
                {"home_team_key": team_key},
                {"away_team_key": team_key},
            ],
        },
        {
            "_id": 1, "sport_key": 1, "home_team": 1, "away_team": 1,
            "match_date": 1, "odds": 1, "status": 1,
        },
    ).sort("match_date", 1).to_list(length=limit)

    return [
        {
            "id": str(m["_id"]),
            "sport_key": m["sport_key"],
            "home_team": m["home_team"],
            "away_team": m["away_team"],
            "match_date": m["match_date"],
            "odds": m.get("odds", {}),
            "status": m["status"],
        }
        for m in matches
    ]
