"""Team profile service.

Aggregates team data from historical_matches, matches, and team_aliases
for the team detail page.
"""

import logging
import re

import app.database as _db
from app.services.historical_service import (
    get_canonical_cache,
    resolve_team_key,
    sport_keys_for,
    team_name_key,
    _derive_season_year,
    _season_label,
    _season_code,
)
from app.utils import utcnow

logger = logging.getLogger("quotico.team_service")


async def search_teams(
    query: str,
    sport_key: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search teams by name across team_aliases, deduplicated by team_key."""
    escaped = re.escape(query)
    filt: dict = {"team_name": {"$regex": escaped, "$options": "i"}}
    if sport_key:
        filt["sport_key"] = sport_key

    pipeline = [
        {"$match": filt},
        {"$sort": {"updated_at": -1}},
        {
            "$group": {
                "_id": "$team_key",
                "team_name": {"$first": "$team_name"},
                "sport_keys": {"$addToSet": "$sport_key"},
                "current_sport_key": {"$first": "$sport_key"},
            },
        },
        {"$sort": {"team_name": 1}},
        {"$limit": limit},
    ]
    raw = await _db.db.team_aliases.aggregate(pipeline).to_list(length=limit)

    canonical_cache = get_canonical_cache()
    results = []
    for doc in raw:
        tk = doc["_id"]
        # Best display name: canonical cache first, then alias name
        display = doc["team_name"]
        for _provider, canonical in canonical_cache.items():
            if team_name_key(canonical) == tk:
                display = canonical
                break
        results.append({
            "team_key": tk,
            "display_name": display,
            "slug": tk.replace(" ", "-"),
            "sport_keys": doc["sport_keys"],
            "current_league": doc.get("current_sport_key", doc["sport_keys"][0] if doc["sport_keys"] else None),
        })
    return results


async def get_team_profile(team_slug: str, sport_key: str | None = None) -> dict | None:
    """Build a full team profile from a URL slug.

    Slug format: lowercase, hyphens (e.g. "bayern-munich").
    Backend converts hyphens to spaces for team_name_key resolution.
    """
    # Convert slug to searchable key
    search_key = team_slug.replace("-", " ").strip()

    # Determine sport keys to search across
    if sport_key:
        related_keys = sport_keys_for(sport_key)
    else:
        # Default to all soccer leagues
        related_keys = sport_keys_for("soccer_germany_bundesliga")

    # Try to resolve via the alias system first
    team_key = await resolve_team_key(search_key, related_keys)
    if not team_key:
        # Fallback: treat slug as a direct team_name_key
        team_key = team_name_key(search_key)
        # Verify it exists in the DB
        exists = await _db.db.team_aliases.find_one(
            {"sport_key": {"$in": related_keys}, "team_key": team_key},
        )
        if not exists:
            return None

    # Get display name from canonical map or aliases
    display_name = await _get_display_name(team_key, related_keys)

    # Determine which sport_keys this team appears in
    team_sport_keys = await _db.db.historical_matches.distinct(
        "sport_key",
        {"$or": [{"home_team_key": team_key}, {"away_team_key": team_key}]},
    )

    # Get season stats
    season_stats = await get_team_season_stats(team_key, related_keys)

    # Get recent results (last 15)
    recent = await _get_recent_results(team_key, related_keys, limit=15)

    # Compute form from recent results
    form = _compute_form(recent, team_key)

    # Get upcoming matches
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


async def _get_display_name(team_key: str, related_keys: list[str]) -> str:
    """Find the best display name for a team_key."""
    # Check canonical map first (most readable names)
    canonical_cache = get_canonical_cache()
    # Reverse lookup: find canonical name whose team_name_key matches
    for _provider, canonical in canonical_cache.items():
        if team_name_key(canonical) == team_key:
            return canonical

    # Fallback: use the most recent team_name from aliases
    alias = await _db.db.team_aliases.find_one(
        {"sport_key": {"$in": related_keys}, "team_key": team_key},
        {"team_name": 1},
        sort=[("updated_at", -1)],
    )
    if alias:
        return alias["team_name"]

    # Last resort: capitalize the key
    return team_key.title()


async def _get_recent_results(
    team_key: str, related_keys: list[str], limit: int = 15,
) -> list[dict]:
    """Fetch recent historical matches for a team."""
    proj = {
        "_id": 0,
        "match_date": 1, "home_team": 1, "away_team": 1,
        "home_team_key": 1, "away_team_key": 1,
        "home_goals": 1, "away_goals": 1, "result": 1,
        "season_label": 1, "sport_key": 1,
    }
    return await _db.db.historical_matches.find(
        {
            "sport_key": {"$in": related_keys},
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
        hg = m.get("home_goals", 0)
        ag = m.get("away_goals", 0)
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
    """Aggregate season stats from historical_matches for the current season."""
    now = utcnow()
    season_year = _derive_season_year(now)
    season = _season_code(season_year)

    matches = await _db.db.historical_matches.find(
        {
            "sport_key": {"$in": related_keys},
            "season": season,
            "$or": [
                {"home_team_key": team_key},
                {"away_team_key": team_key},
            ],
        },
        {
            "_id": 0, "home_team_key": 1, "home_goals": 1,
            "away_goals": 1, "sport_key": 1,
        },
    ).to_list(length=500)

    if not matches:
        return None

    wins = draws = losses = gf = ga = 0
    home_w = home_d = home_l = 0
    away_w = away_d = away_l = 0

    for m in matches:
        hg = m["home_goals"]
        ag = m["away_goals"]
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
        "season_label": _season_label(season_year),
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

    Reverse-lookups team_key → known display names from team_aliases,
    then searches the matches collection by those names.
    """
    # Get all known display names for this team
    aliases = await _db.db.team_aliases.find(
        {"sport_key": {"$in": related_keys}, "team_key": team_key},
        {"team_name": 1, "_id": 0},
    ).to_list(length=50)

    team_names = list({a["team_name"] for a in aliases})
    if not team_names:
        return []

    # Search matches by display names
    matches = await _db.db.matches.find(
        {
            "status": "upcoming",
            "$or": [
                {"teams.home": {"$in": team_names}},
                {"teams.away": {"$in": team_names}},
            ],
        },
        {
            "_id": 1, "sport_key": 1, "teams": 1,
            "commence_time": 1, "current_odds": 1, "status": 1,
        },
    ).sort("commence_time", 1).to_list(length=limit)

    return [
        {
            "id": str(m["_id"]),
            "sport_key": m["sport_key"],
            "teams": m["teams"],
            "commence_time": m["commence_time"],
            "current_odds": m.get("current_odds", {}),
            "status": m["status"],
        }
        for m in matches
    ]
