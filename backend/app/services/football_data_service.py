"""
backend/app/services/football_data_service.py

Purpose:
    Core import service for football-data.co.uk match statistics. Resolves teams
    via Team Tower, matches existing fixtures, and upserts stats into matches.

Dependencies:
    - app.providers.football_data_uk
    - app.services.league_service
    - app.services.team_registry_service
    - app.database
    - app.utils.utcnow
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import StringIO

from bson import ObjectId
from fastapi import HTTPException

import app.database as _db
from app.providers.football_data_uk import football_data_uk_provider
from app.services.league_service import LeagueRegistry
from app.services.team_registry_service import TeamRegistry
from app.utils import ensure_utc, utcnow

STATS_COLUMN_MAP = {
    "HC": "corners_home",
    "AC": "corners_away",
    "HS": "shots_home",
    "AS": "shots_away",
    "HF": "fouls_home",
    "AF": "fouls_away",
    "HY": "cards_yellow_home",
    "AY": "cards_yellow_away",
    "HR": "cards_red_home",
    "AR": "cards_red_away",
}


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _season_code_from_start_year(start_year: int) -> str:
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


async def import_football_data_stats(league_id: ObjectId, season: str | None = None) -> dict:
    """Import football-data.co.uk stats for one league+season."""
    league = await _db.db.leagues.find_one({"_id": league_id})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key.")

    # Ensure registry is warmed and league is known to the tower.
    registry = LeagueRegistry.get()
    await registry.ensure_for_import(sport_key, auto_create_inactive=True)

    external_ids = league.get("external_ids") or {}
    if not isinstance(external_ids, dict):
        external_ids = {}
    division_code = str(external_ids.get("football_data_uk") or "").strip()
    if not division_code:
        raise HTTPException(status_code=400, detail="League has no football_data_uk external_id.")

    season_code = season.strip() if season else _season_code_from_start_year(int(league.get("current_season") or utcnow().year))
    if len(season_code) != 4 or not season_code.isdigit():
        raise HTTPException(status_code=400, detail="season must be a football-data.co.uk code like '2324'.")

    csv_text = await football_data_uk_provider.fetch_season_csv(season_code, division_code)
    rows = list(csv.DictReader(StringIO(csv_text)))
    if not rows:
        return {"processed": 0, "matched": 0, "updated": 0, "season": season_code, "division": division_code}

    team_registry = TeamRegistry.get()
    now = utcnow()
    processed = 0
    matched = 0
    updated = 0

    for row in rows:
        home_team = (row.get("HomeTeam") or "").strip()
        away_team = (row.get("AwayTeam") or "").strip()
        date_raw = row.get("Date")
        if not home_team or not away_team or not date_raw:
            continue
        match_date = _parse_date(date_raw)
        if not match_date:
            continue

        processed += 1
        home_team_id = await team_registry.resolve(home_team, sport_key)
        away_team_id = await team_registry.resolve(away_team, sport_key)
        window_start = ensure_utc(match_date) - timedelta(hours=24)
        window_end = ensure_utc(match_date) + timedelta(hours=24)

        # Support historical string/object-id mixed docs during transition.
        home_values = [home_team_id, str(home_team_id)]
        away_values = [away_team_id, str(away_team_id)]
        match_doc = await _db.db.matches.find_one(
            {
                "sport_key": sport_key,
                "home_team_id": {"$in": home_values},
                "away_team_id": {"$in": away_values},
                "match_date": {"$gte": window_start, "$lte": window_end},
            },
            {"_id": 1},
        )
        if not match_doc:
            continue

        matched += 1
        stats: dict[str, int | str | datetime] = {
            "source": "football_data_co_uk",
            "updated_at": now,
        }
        for csv_key, target_key in STATS_COLUMN_MAP.items():
            parsed_value = _to_int(row.get(csv_key))
            if parsed_value is not None:
                stats[target_key] = parsed_value

        if len(stats) <= 2:
            continue

        result = await _db.db.matches.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {"stats": stats, "updated_at": now}},
        )
        if result.modified_count:
            updated += 1

    return {
        "processed": processed,
        "matched": matched,
        "updated": updated,
        "season": season_code,
        "division": division_code,
    }
