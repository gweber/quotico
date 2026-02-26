"""
backend/app/services/team_seed_service.py

Purpose:
    Canonical Team-Tower seeding service for well-known teams. Runs via startup
    and tooling, resolving league ObjectIds through LeagueRegistry before
    writing teams.

Dependencies:
    - app.database
    - app.services.league_service
    - app.services.team_registry_service
    - app.utils.utcnow
"""

from __future__ import annotations

import logging
from typing import Any

import app.database as _db
from app.services.league_service import LeagueRegistry
from app.services.team_registry_service import normalize_team_name
from app.utils import utcnow

logger = logging.getLogger("quotico.team_seed")


def _league(sport_key: str, teams: list[str]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for name in teams:
        docs.append(
            {
                "display_name": name,
                "sport_key": sport_key,
                "aliases": [name, f"{name} FC"],
            }
        )
    return docs


SEED_TEAMS: list[dict[str, Any]] = (
    _league(
        "soccer_epl",
        [
            "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton",
            "Chelsea", "Crystal Palace", "Everton", "Fulham", "Ipswich Town",
            "Leicester City", "Liverpool", "Manchester City", "Manchester United",
            "Newcastle United", "Nottingham Forest", "Southampton", "Tottenham Hotspur",
            "West Ham United", "Wolverhampton Wanderers",
        ],
    )
    + _league(
        "soccer_germany_bundesliga",
        [
            "Bayern München", "Borussia Dortmund", "RB Leipzig", "Bayer Leverkusen", "Eintracht Frankfurt",
            "VfB Stuttgart", "SC Freiburg", "Union Berlin", "TSG Hoffenheim", "VfL Wolfsburg",
            "Borussia Mönchengladbach", "Werder Bremen", "FC Augsburg", "FSV Mainz 05", "1. FC Köln",
            "VfL Bochum", "1. FC Heidenheim", "Holstein Kiel",
        ],
    )
    + _league(
        "soccer_germany_bundesliga2",
        [
            "Hamburger SV", "Hertha BSC", "Schalke 04", "Hannover 96", "Karlsruher SC",
            "Fortuna Düsseldorf", "1. FC Kaiserslautern", "SC Paderborn", "1. FC Nürnberg", "Greuther Fürth",
            "1. FC Magdeburg", "Eintracht Braunschweig", "SSV Ulm 1846", "SV Elversberg", "Jahn Regensburg",
            "SC Preußen Münster", "SV Darmstadt 98", "Hansa Rostock",
        ],
    )
    + _league(
        "soccer_spain_la_liga",
        [
            "Real Madrid", "Barcelona", "Atlético Madrid", "Athletic Club", "Real Sociedad",
            "Real Betis", "Sevilla", "Valencia", "Villarreal", "Celta Vigo",
            "Getafe", "Osasuna", "Mallorca", "Rayo Vallecano", "Girona",
            "Alavés", "Las Palmas", "Espanyol", "Leganés", "Real Valladolid",
        ],
    )
)


async def seed_core_teams(
    *,
    sport: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """
    Seed core teams and bind each team to resolved league ObjectIds.

    This function requires league resolution via LeagueRegistry; if no league
    can be resolved for a team's sport_key, the team is skipped.
    """
    if not dry_run:
        if sport:
            existing = await _db.db.teams.count_documents({"sport_key": sport})
            if existing > 0:
                return {
                    "processed": 0,
                    "upserted": 0,
                    "skipped_no_league": 0,
                    "skipped_existing": existing,
                }
        else:
            existing = await _db.db.teams.count_documents({})
            if existing > 0:
                return {
                    "processed": 0,
                    "upserted": 0,
                    "skipped_no_league": 0,
                    "skipped_existing": existing,
                }

    league_registry = LeagueRegistry.get()
    candidates = [team for team in SEED_TEAMS if not sport or team["sport_key"] == sport]
    now = utcnow()

    processed = 0
    upserted = 0
    skipped_no_league = 0

    for team in candidates:
        processed += 1
        sport_key = str(team.get("sport_key") or "").strip()
        league = await league_registry.get_league(sport_key)
        league_ids = []
        if league and league.get("_id") is not None:
            league_ids = [league["_id"]]
        else:
            skipped_no_league += 1
            logger.warning("Skipping seeded team without resolved league: %s (%s)", team.get("display_name"), sport_key)
            continue

        normalized = normalize_team_name(str(team["display_name"]))
        alias_docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alias in [team["display_name"], *team.get("aliases", [])]:
            alias_norm = normalize_team_name(str(alias))
            if not alias_norm or alias_norm in seen:
                continue
            seen.add(alias_norm)
            alias_docs.append(
                {
                    "name": str(alias),
                    "normalized": alias_norm,
                    "sport_key": sport_key,
                    "source": "seed",
                }
            )

        if dry_run:
            if verbose:
                logger.info("[dry-run] seed team %s (%s)", team["display_name"], sport_key)
            upserted += 1
            continue

        await _db.db.teams.update_one(
            {"normalized_name": normalized, "sport_key": sport_key},
            {
                "$set": {
                    "display_name": team["display_name"],
                    "normalized_name": normalized,
                    "sport_key": sport_key,
                    "league_ids": league_ids,
                    "source": "seed",
                    "needs_review": False,
                    "updated_at": now,
                },
                "$addToSet": {"aliases": {"$each": alias_docs}},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        upserted += 1

    return {
        "processed": processed,
        "upserted": upserted,
        "skipped_no_league": skipped_no_league,
        "skipped_existing": 0,
    }
