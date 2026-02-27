"""
backend/app/services/team_service.py

Purpose:
    Team profile service built on Team Tower identities. Provides search and
    team detail aggregates from teams + matches using ObjectId references.

Dependencies:
    - app.database
    - app.services.team_registry_service
    - app.utils
"""

import logging
import re

from bson import ObjectId

import app.database as _db
from app.services.team_registry_service import TeamRegistry
from app.utils import utcnow

logger = logging.getLogger("quotico.team_service")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _derive_season_year() -> int:
    now = utcnow()
    return now.year if now.month >= 7 else now.year - 1


def _serialize_team_ids(match_doc: dict) -> dict:
    """Convert Team Tower ObjectId refs in a match snippet to strings for JSON responses."""
    out = dict(match_doc)
    home_id = out.get("home_team_id")
    away_id = out.get("away_team_id")
    if isinstance(home_id, ObjectId):
        out["home_team_id"] = str(home_id)
    if isinstance(away_id, ObjectId):
        out["away_team_id"] = str(away_id)
    season = out.get("season")
    if season is not None and out.get("season_label") is None:
        out["season_label"] = str(season)
    return out


def _collect_league_ids(team_doc: dict) -> list[int]:
    league_ids: set[int] = set()
    primary_league_id = team_doc.get("league_id")
    if isinstance(primary_league_id, int):
        league_ids.add(primary_league_id)
    for alias in team_doc.get("aliases", []) or []:
        if not isinstance(alias, dict):
            continue
        alias_league_id = alias.get("league_id")
        if isinstance(alias_league_id, int):
            league_ids.add(alias_league_id)
    return sorted(league_ids)


async def search_teams(
    query: str,
    league_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search teams by display name or aliases, optionally league-scoped."""
    escaped = re.escape((query or "").strip())
    filt: dict = {
        "$or": [
            {"display_name": {"$regex": escaped, "$options": "i"}},
            {"aliases.name": {"$regex": escaped, "$options": "i"}},
        ],
    }
    if league_id is not None:
        filt["$or"] = [
            {"league_id": league_id},
            {"aliases": {"$elemMatch": {"league_id": league_id}}},
            {"aliases": {"$elemMatch": {"league_id": None}}},
        ]

    docs = await _db.db.teams.find(
        filt,
        {
            "display_name": 1,
            "league_ids": 1,
            "aliases.league_id": 1,
            "league_id": 1,
        },
    ).sort("display_name", 1).limit(limit).to_list(length=limit)

    return [
        {
            "team_id": str(doc["_id"]),
            "display_name": doc.get("display_name", ""),
            "slug": _slug(doc.get("display_name", "")),
            "league_ids": sorted(
                {
                    *(a.get("league_id") for a in doc.get("aliases", []) if isinstance(a.get("league_id"), int)),
                    doc.get("league_id") if isinstance(doc.get("league_id"), int) else None,
                }
                - {None}
            ),
            "current_league": None,
        }
        for doc in docs
    ]


async def get_team_profile(team_slug: str, league_id: int | None = None) -> dict | None:
    """Build team profile by normalized display-name slug."""
    slug = (team_slug or "").strip().lower()
    docs = await _db.db.teams.find({}, {"display_name": 1}).to_list(length=5000)
    team = next((d for d in docs if _slug(d.get("display_name", "")) == slug), None)
    if team and team.get("_id") is not None:
        full_team = await _db.db.teams.find_one({"_id": team["_id"]})
        if full_team:
            team = full_team

    if not team and league_id is not None:
        registry = TeamRegistry.get()
        team_id = await registry.resolve(slug.replace("-", " "), league_id)
        team = await _db.db.teams.find_one({"_id": team_id}) if team_id else None

    if not team:
        return None

    team_id = team["_id"]
    display_name = team.get("display_name", "")
    related_league_ids = [league_id] if league_id is not None else None

    season_stats = await get_team_season_stats(team_id, related_league_ids)
    recent = await _get_recent_results(team_id, related_league_ids, limit=15)
    form = _compute_form(recent, team_id)
    upcoming = await get_team_upcoming_matches(team_id, related_league_ids)

    return {
        "team_id": str(team_id),
        "display_name": display_name,
        "needs_review": bool(team.get("needs_review", False)),
        "aliases": team.get("aliases", []),
        "league_ids": _collect_league_ids(team),
        "form": form[:5],
        "recent_results": recent,
        "season_stats": season_stats,
        "upcoming_matches": upcoming,
    }


async def _get_recent_results(
    team_id,
    related_keys: list[int] | None,
    limit: int = 15,
) -> list[dict]:
    """Fetch recent finalized matches for a team by ObjectId."""
    query: dict = {
        "status": "FINISHED",
        "$or": [
            {"home_team_id": team_id},
            {"away_team_id": team_id},
        ],
    }
    if related_keys:
        query["league_id"] = {"$in": related_keys}

    matches = await _db.db.matches_v3.find(
        query,
        {
            "_id": 0,
            "start_at": 1,
            "home_team": 1,
            "away_team": 1,
            "home_team_id": 1,
            "away_team_id": 1,
            "result.home_score": 1,
            "result.away_score": 1,
            "result.outcome": 1,
            "league_id": 1,
            "season": 1,
        },
    ).sort("start_at", -1).to_list(length=limit)

    return [_serialize_team_ids(m) for m in matches]


def _compute_form(matches: list[dict], team_id) -> list[str]:
    """Derive W/D/L form string from recent results using team IDs."""
    form: list[str] = []
    for m in matches:
        r = m.get("result", {})
        hg = r.get("home_score", 0) or 0
        ag = r.get("away_score", 0) or 0
        is_home = m.get("home_team_id") == team_id

        if hg == ag:
            form.append("D")
        elif (hg > ag and is_home) or (ag > hg and not is_home):
            form.append("W")
        else:
            form.append("L")
    return form


async def get_team_season_stats(
    team_id,
    related_keys: list[int] | None,
) -> dict | None:
    """Aggregate current-season stats for a team by ObjectId."""
    season_year = _derive_season_year()
    query: dict = {
        "status": "FINISHED",
        "season": season_year,
        "$or": [
            {"home_team_id": team_id},
            {"away_team_id": team_id},
        ],
    }
    if related_keys:
        query["league_id"] = {"$in": related_keys}

    matches = await _db.db.matches_v3.find(
        query,
        {
            "_id": 0,
            "home_team_id": 1,
            "result.home_score": 1,
            "result.away_score": 1,
            "league_id": 1,
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
        is_home = m.get("home_team_id") == team_id

        if is_home:
            gf += hg
            ga += ag
            if hg > ag:
                wins += 1
                home_w += 1
            elif hg == ag:
                draws += 1
                home_d += 1
            else:
                losses += 1
                home_l += 1
        else:
            gf += ag
            ga += hg
            if ag > hg:
                wins += 1
                away_w += 1
            elif ag == hg:
                draws += 1
                away_d += 1
            else:
                losses += 1
                away_l += 1

    return {
        "season": season_year,
        "season_label": str(season_year),
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
    team_id,
    related_keys: list[int] | None,
    limit: int = 10,
) -> list[dict]:
    """Find upcoming matches where this team is playing."""
    query: dict = {
        "status": {"$in": ["SCHEDULED", "LIVE"]},
        "$or": [
            {"home_team_id": team_id},
            {"away_team_id": team_id},
        ],
    }
    if related_keys:
        query["league_id"] = {"$in": related_keys}

    matches = await _db.db.matches_v3.find(
        query,
        {
            "_id": 1,
            "league_id": 1,
            "home_team": 1,
            "away_team": 1,
            "start_at": 1,
            "odds": 1,
            "status": 1,
        },
    ).sort("start_at", 1).to_list(length=limit)

    return [
        {
            "id": str(m["_id"]),
            "league_id": m.get("league_id"),
            "home_team": m.get("home_team"),
            "away_team": m.get("away_team"),
            "start_at": m.get("start_at"),
            "odds": m.get("odds", {}),
            "status": m.get("status"),
        }
        for m in matches
    ]
