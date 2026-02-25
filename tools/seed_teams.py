"""Seed well-known teams into the teams collection.

Usage:
    python -m tools.seed_teams
    python -m tools.seed_teams --sport soccer_epl
    python -m tools.seed_teams --dry-run
"""

import argparse
import asyncio
import os
import sys
from datetime import timezone, datetime

sys.path.insert(0, "backend")

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

from app.services.team_registry_service import normalize_team_name
from app.services.league_service import LeagueRegistry


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _league(sport_key: str, teams: list[str]) -> list[dict]:
    docs = []
    for t in teams:
        docs.append({
            "display_name": t,
            "sport_key": sport_key,
            "aliases": [t, f"{t} FC"],
        })
    return docs


SEED_TEAMS = (
    _league("soccer_epl", [
        "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton",
        "Chelsea", "Crystal Palace", "Everton", "Fulham", "Ipswich Town",
        "Leicester City", "Liverpool", "Manchester City", "Manchester United",
        "Newcastle United", "Nottingham Forest", "Southampton", "Tottenham Hotspur",
        "West Ham United", "Wolverhampton Wanderers",
    ])
    + _league("soccer_germany_bundesliga", [
        "Bayern München", "Borussia Dortmund", "RB Leipzig", "Bayer Leverkusen", "Eintracht Frankfurt",
        "VfB Stuttgart", "SC Freiburg", "Union Berlin", "TSG Hoffenheim", "VfL Wolfsburg",
        "Borussia Mönchengladbach", "Werder Bremen", "FC Augsburg", "FSV Mainz 05", "1. FC Köln",
        "VfL Bochum", "1. FC Heidenheim", "Holstein Kiel",
    ])
    + _league("soccer_germany_bundesliga2", [
        "Hamburger SV", "Hertha BSC", "Schalke 04", "Hannover 96", "Karlsruher SC",
        "Fortuna Düsseldorf", "1. FC Kaiserslautern", "SC Paderborn", "1. FC Nürnberg", "Greuther Fürth",
        "1. FC Magdeburg", "Eintracht Braunschweig", "SSV Ulm 1846", "SV Elversberg", "Jahn Regensburg",
        "SC Preußen Münster", "SV Darmstadt 98", "Hansa Rostock",
    ])
    + _league("soccer_spain_la_liga", [
        "Real Madrid", "Barcelona", "Atlético Madrid", "Athletic Club", "Real Sociedad",
        "Real Betis", "Sevilla", "Valencia", "Villarreal", "Celta Vigo",
        "Getafe", "Osasuna", "Mallorca", "Rayo Vallecano", "Girona",
        "Alavés", "Las Palmas", "Espanyol", "Leganés", "Real Valladolid",
    ])
)


async def run(sport: str | None, dry_run: bool, verbose: bool) -> int:
    import app.database as _db

    await _db.connect_db()
    db = _db.db
    league_registry = LeagueRegistry.get()
    await league_registry.initialize()
    candidates = [t for t in SEED_TEAMS if not sport or t["sport_key"] == sport]
    now = utcnow()
    upserts = 0

    for team in candidates:
        normalized = normalize_team_name(team["display_name"])
        alias_docs = []
        seen = set()
        for alias in [team["display_name"]] + team.get("aliases", []):
            a_norm = normalize_team_name(alias)
            if not a_norm or a_norm in seen:
                continue
            seen.add(a_norm)
            alias_docs.append({
                "name": alias,
                "normalized": a_norm,
                "sport_key": team["sport_key"],
                "source": "seed",
            })

        if dry_run:
            if verbose:
                print(f"[dry-run] {team['sport_key']}: {team['display_name']} ({normalized})")
            upserts += 1
            continue

        await db.teams.update_one(
            {"normalized_name": normalized, "sport_key": team["sport_key"]},
            {
                "$set": {
                    "display_name": team["display_name"],
                    "normalized_name": normalized,
                    "sport_key": team["sport_key"],
                    "league_ids": [
                        league["_id"]
                        for league in [await league_registry.get_league(team["sport_key"])]
                        if league and league.get("_id") is not None
                    ],
                    "source": "seed",
                    "needs_review": False,
                    "updated_at": now,
                },
                "$addToSet": {"aliases": {"$each": alias_docs}},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if verbose:
            print(f"seeded {team['sport_key']}: {team['display_name']}")
        upserts += 1

    print(
        f"{'planned' if dry_run else 'completed'} seed upserts: {upserts} "
        f"(sport={sport or 'all'})"
    )
    return upserts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed well-known teams into teams collection.")
    parser.add_argument("--sport", type=str, default=None, help="Optional sport_key filter.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes.")
    parser.add_argument("--verbose", action="store_true", help="Print each upsert.")
    args = parser.parse_args()
    asyncio.run(run(args.sport, args.dry_run, args.verbose))


if __name__ == "__main__":
    main()
