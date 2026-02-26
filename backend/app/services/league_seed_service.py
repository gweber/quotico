"""
backend/app/services/league_seed_service.py

Purpose:
    Core league seeding for League Tower. Isolated from league registry runtime
    logic to keep startup/bootstrap concerns separated.

Dependencies:
    - app.database
    - app.utils.utcnow
"""

from __future__ import annotations

import app.database as _db
from app.utils import utcnow

CORE_LEAGUES = [
    {
        "sport_key": "soccer_germany_bundesliga",
        "name": "Bundesliga",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "Germany",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_germany_bundesliga",
            "openligadb": "bl1",
            "football_data_uk": "D1",
            "football_data": "BL1",
            "understat": "GER-Bundesliga",
        },
    },
    {
        "sport_key": "soccer_germany_bundesliga2",
        "name": "2. Bundesliga",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "Germany",
        "level": 2,
        "external_ids": {
            "theoddsapi": "soccer_germany_bundesliga2",
            "openligadb": "bl2",
        },
    },
    {
        "sport_key": "soccer_epl",
        "name": "Premier League",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "England",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_epl",
            "football_data_uk": "E0",
            "football_data": "PL",
            "understat": "ENG-Premier League",
        },
    },
    {
        "sport_key": "soccer_spain_la_liga",
        "name": "La Liga",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "Spain",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_spain_la_liga",
            "football_data": "PD",
            "understat": "ESP-La Liga",
        },
    },
    {
        "sport_key": "soccer_italy_serie_a",
        "name": "Serie A",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "Italy",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_italy_serie_a",
            "football_data": "SA",
            "understat": "ITA-Serie A",
        },
    },
    {
        "sport_key": "soccer_france_ligue_one",
        "name": "Ligue 1",
        "structure_type": "league",
        "season_start_month": 8,
        "country": "France",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_france_ligue_one",
            "football_data": "FL1",
            "understat": "FRA-Ligue 1",
        },
    },
    {
        "sport_key": "soccer_uefa_champs_league",
        "name": "Champions League",
        "structure_type": "tournament",
        "season_start_month": 7,
        "country": "Europe",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_uefa_champs_league",
            "football_data": "CL",
        },
    },
    {
        "sport_key": "soccer_germany_dfb_pokal",
        "name": "DFB-Pokal",
        "structure_type": "cup",
        "season_start_month": 8,
        "country": "Germany",
        "level": 1,
        "external_ids": {
            "theoddsapi": "soccer_germany_dfb_pokal",
        },
    },
]


def _tier_from_level(level: int | None) -> str:
    if level is None:
        return "unknown"
    if level <= 1:
        return "tier1"
    if level == 2:
        return "tier2"
    return "tier3"


def _country_code_from_name(country: str | None) -> str | None:
    mapping = {
        "germany": "DE",
        "england": "GB",
        "spain": "ES",
        "italy": "IT",
        "france": "FR",
        "europe": "EU",
    }
    if not country:
        return None
    return mapping.get(country.strip().lower())


def _default_current_season(season_start_month: int = 7) -> int:
    now = utcnow()
    return now.year if now.month >= int(season_start_month) else now.year - 1


def _default_season_start_month(sport_key: str) -> int:
    normalized = (sport_key or "").strip().lower()
    return 7 if normalized.startswith("soccer_") else 1


def _default_features() -> dict:
    """Default feature set for seeded core leagues.

    Core leagues are active on creation, therefore tipping must be enabled by
    default. Runtime-created review leagues still use league_service defaults.
    """
    return {
        "tipping": True,
        "match_load": True,
        "xg_sync": False,
        "odds_sync": False,
    }


def _default_structure_type() -> str:
    return "league"


async def seed_core_leagues() -> dict[str, int]:
    """Bootstrap core leagues only when the leagues collection is empty."""
    # Imported lazily to avoid circular dependency with league_service facade.
    from app.services.league_service import LeagueRegistry, invalidate_navigation_cache

    existing_count = await _db.db.leagues.count_documents({})
    if existing_count > 0:
        await LeagueRegistry.get().initialize()
        return {"created": 0, "updated": 0, "total": len(CORE_LEAGUES), "skipped": existing_count}

    created = 0
    updated = 0
    now = utcnow()

    for league in CORE_LEAGUES:
        sport_key = str(league["sport_key"]).strip()
        name = str(league["name"]).strip()
        country = str(league["country"]).strip()
        level = int(league.get("level") or 1)
        structure_type = str(league.get("structure_type") or _default_structure_type()).strip().lower()
        if structure_type not in {"league", "cup", "tournament"}:
            structure_type = _default_structure_type()
        external_ids = {
            str(provider).strip().lower(): str(external_id).strip()
            for provider, external_id in (league.get("external_ids") or {}).items()
            if str(provider).strip() and str(external_id).strip()
        }
        season_start_month = int(
            league.get("season_start_month")
            or _default_season_start_month(sport_key)
        )

        update_doc = {
            "name": name,
            "country": country,
            "level": level,
            "display_name": name,
            "country_code": _country_code_from_name(country),
            "tier": _tier_from_level(level),
            "structure_type": structure_type,
            "season_start_month": season_start_month,
            "external_ids": external_ids,
            "updated_at": now,
        }
        insert_defaults = {
            "sport_key": sport_key,
            "current_season": _default_current_season(season_start_month),
            "is_active": True,
            "needs_review": False,
            "ui_order": 999,
            "features": _default_features(),
            "created_at": now,
        }

        existing = await _db.db.leagues.find_one({"sport_key": sport_key}, {"_id": 1})
        result = await _db.db.leagues.update_one(
            {"sport_key": sport_key},
            {"$set": update_doc, "$setOnInsert": insert_defaults},
            upsert=True,
        )
        if result.upserted_id is not None:
            created += 1
        elif existing:
            updated += 1

    await invalidate_navigation_cache()
    await LeagueRegistry.get().initialize()
    return {"created": created, "updated": updated, "total": len(CORE_LEAGUES)}
