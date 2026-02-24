"""Matchday mode: sync matchdays from providers into the unified matches collection."""

import logging
from datetime import datetime, timedelta

import app.database as _db
from app.utils import parse_utc, utcnow
from app.config_matchday import MATCHDAY_SPORTS
from app.providers.football_data import football_data_provider, teams_match
from app.providers.openligadb import openligadb_provider, _current_season
from app.services.team_mapping_service import (
    SPORT_KEY_TO_LEAGUE_CODE,
    derive_season_year,
    normalize_match_date,
    resolve_or_create_team,
    season_code,
    season_label,
)
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.matchday_sync")


def _get_season_for_sport(sport_key: str) -> int:
    """Determine the current season year for a sport."""
    return _current_season()


async def sync_matchdays() -> None:
    """Sync matchday data for all configured matchday sports.

    Syncs a window of 3 matchdays: previous, current, next.
    Runs every 30 min via scheduler.
    """
    for sport_key, config in MATCHDAY_SPORTS.items():
        try:
            await _sync_sport_matchdays(sport_key, config)
        except Exception as e:
            logger.error("Matchday sync failed for %s: %s", sport_key, e)


async def _sync_sport_matchdays(sport_key: str, config: dict) -> None:
    """Sync matchdays for a single sport.

    Smart sleep per sport: skips external API calls when no matchday
    activity is expected (all completed and next kickoff >48h away).
    Safety margin: syncs at least once every 6 hours.
    Bootstrap always runs when the full season isn't populated yet.
    """
    now = utcnow()
    max_matchdays = config["matchdays_per_season"]
    season = _get_season_for_sport(sport_key)
    state_key = f"matchday_sync:{sport_key}"

    # Check if full-season bootstrap is needed (first time this season)
    existing_count = await _db.db.matchdays.count_documents({
        "sport_key": sport_key,
        "season": season,
    })
    bootstrap_key = f"matchday_bootstrap:{sport_key}:{season}"
    bootstrap_done = await recently_synced(bootstrap_key, timedelta(days=7))
    needs_bootstrap = existing_count == 0 and not bootstrap_done

    if not needs_bootstrap:
        # Smart sleep: skip sync if no active/imminent matchdays
        window = now + timedelta(hours=48)
        active = await _db.db.matchdays.find_one({
            "sport_key": sport_key,
            "$or": [
                {"status": "in_progress"},
                {"status": "upcoming", "first_kickoff": {"$lte": window}},
            ],
        })

        if not active:
            if await recently_synced(state_key, timedelta(hours=6)):
                logger.debug("Smart sleep: %s has no active/upcoming matchdays, skipping sync", sport_key)
                return
            logger.info("Smart sleep safety: %s >6h since last sync, syncing anyway", sport_key)

    # Passed smart sleep gate — now fetch current matchday from provider
    provider_name = config["provider"]

    if provider_name == "openligadb":
        current_md = await openligadb_provider.get_current_matchday_number(sport_key)
    else:
        current_md = await football_data_provider.get_current_matchday_number(sport_key)

    if not current_md:
        logger.warning("Could not determine current matchday for %s", sport_key)
        return

    if needs_bootstrap:
        logger.info(
            "Full-season bootstrap for %s: syncing all %d matchdays",
            sport_key, max_matchdays,
        )
        for md_number in range(1, max_matchdays + 1):
            try:
                await _sync_single_matchday(sport_key, config, season, md_number)
            except Exception as e:
                logger.error("Full sync failed for %s matchday %d: %s", sport_key, md_number, e)
        await set_synced(bootstrap_key)
    else:
        # Incremental sync: previous, current, next matchday
        matchdays_to_sync = [
            md for md in [current_md - 1, current_md, current_md + 1]
            if 1 <= md <= max_matchdays
        ]
        for md_number in matchdays_to_sync:
            await _sync_single_matchday(sport_key, config, season, md_number)

    await set_synced(f"matchday_sync:{sport_key}")


async def _sync_single_matchday(
    sport_key: str, config: dict, season: int, matchday_number: int
) -> None:
    """Sync a single matchday: fetch matches, upsert matchday doc, link matches."""
    provider_name = config["provider"]

    # Fetch matchday data from provider
    if provider_name == "openligadb":
        provider_matches = await openligadb_provider.get_matchday_data(
            sport_key, season, matchday_number
        )
    else:
        provider_matches = await football_data_provider.get_matchday_data(
            sport_key, season, matchday_number
        )

    if not provider_matches:
        return

    now = utcnow()
    match_ids: list[str] = []
    kickoffs: list[datetime] = []

    for pm in provider_matches:
        match_doc = await _find_or_create_match(sport_key, season, matchday_number, pm, now)
        if match_doc:
            match_ids.append(str(match_doc["_id"]))
            md = match_doc.get("match_date")
            if md:
                kickoffs.append(md)

    if not match_ids:
        return

    # Determine matchday status
    all_matches = await _db.db.matches.find(
        {"_id": {"$in": [__import__("bson").ObjectId(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))

    statuses = [m.get("status", "scheduled") for m in all_matches]
    all_resolved = all(s == "final" for s in statuses)
    any_live = any(s == "live" for s in statuses)

    if all_resolved:
        md_status = "completed"
    elif any_live or any(s == "final" for s in statuses):
        md_status = "in_progress"
    else:
        md_status = "upcoming"

    label = config["label_template"].format(n=matchday_number)

    # Upsert matchday document
    await _db.db.matchdays.update_one(
        {
            "sport_key": sport_key,
            "season": season,
            "matchday_number": matchday_number,
        },
        {
            "$set": {
                "label": label,
                "match_ids": match_ids,
                "match_count": len(match_ids),
                "first_kickoff": min(kickoffs) if kickoffs else None,
                "last_kickoff": max(kickoffs) if kickoffs else None,
                "status": md_status,
                "all_resolved": all_resolved,
                "updated_at": now,
            },
            "$setOnInsert": {
                "sport_key": sport_key,
                "season": season,
                "matchday_number": matchday_number,
                "created_at": now,
            },
        },
        upsert=True,
    )

    logger.info(
        "Synced matchday %s %d/%d: %d matches, status=%s",
        sport_key, season, matchday_number, len(match_ids), md_status,
    )


async def _find_or_create_match(
    sport_key: str, season: int, matchday_number: int,
    provider_match: dict, now: datetime,
) -> dict | None:
    """Find existing match by team key + date, or create a new one.

    Resolves team names to canonical keys. Uses compound key for dedup.
    """
    home_team = provider_match["home_team"]
    away_team = provider_match["away_team"]
    utc_date = provider_match["utc_date"]

    if not home_team or not away_team:
        return None

    # Parse commence time (always tz-aware)
    if not utc_date or not isinstance(utc_date, (str, datetime)):
        return None
    try:
        match_date_raw = parse_utc(utc_date)
    except (ValueError, TypeError):
        return None

    match_date_normalized = normalize_match_date(match_date_raw)

    # Resolve team names to canonical keys
    home_display, home_key = await resolve_or_create_team(home_team, sport_key)
    away_display, away_key = await resolve_or_create_team(away_team, sport_key)

    sy = derive_season_year(match_date_raw)
    sc = season_code(sy)
    sl = season_label(sy)

    # Find existing match by compound key with ±6h date window
    existing = await _db.db.matches.find_one({
        "sport_key": sport_key,
        "home_team_key": home_key,
        "away_team_key": away_key,
        "match_date": {
            "$gte": match_date_normalized - timedelta(hours=6),
            "$lte": match_date_normalized + timedelta(hours=6),
        },
    })

    if existing:
        # Update matchday fields + accurate match_date from provider
        update: dict = {
            "matchday_number": matchday_number,
            "matchday_season": season,
            "match_date": match_date_raw,
            "match_date_hour": match_date_normalized,
            "updated_at": now,
        }
        if provider_match.get("is_finished") and provider_match.get("home_score") is not None:
            hs, aws = provider_match["home_score"], provider_match["away_score"]
            update["result.home_score"] = hs
            update["result.away_score"] = aws
            update["result.outcome"] = "1" if hs > aws else ("X" if hs == aws else "2")
            update["status"] = "final"

        await _db.db.matches.update_one(
            {"_id": existing["_id"]}, {"$set": update}
        )
        return await _db.db.matches.find_one({"_id": existing["_id"]})

    # No match found — create a new one
    status = "scheduled"
    if provider_match.get("is_finished"):
        status = "final"
    elif match_date_raw <= now:
        status = "live"

    result_data: dict = {
        "home_score": provider_match.get("home_score"),
        "away_score": provider_match.get("away_score"),
        "outcome": None,
        "half_time": None,
    }
    if provider_match.get("is_finished") and provider_match.get("home_score") is not None:
        hs, aws = provider_match["home_score"], provider_match["away_score"]
        result_data["outcome"] = "1" if hs > aws else ("X" if hs == aws else "2")

    doc = {
        "sport_key": sport_key,
        "match_date": match_date_raw,
        "match_date_hour": match_date_normalized,
        "status": status,
        "season": sc,
        "season_label": sl,
        "league_code": SPORT_KEY_TO_LEAGUE_CODE.get(sport_key),
        "home_team": home_display,
        "away_team": away_display,
        "home_team_key": home_key,
        "away_team_key": away_key,
        "metadata": {"source": "matchday_sync"},
        "matchday_number": matchday_number,
        "matchday_season": season,
        "odds": {"h2h": {}, "totals": {}, "spreads": {}, "updated_at": None},
        "result": result_data,
        "created_at": now,
        "updated_at": now,
    }

    try:
        insert_result = await _db.db.matches.insert_one(doc)
        doc["_id"] = insert_result.inserted_id
        return doc
    except Exception as e:
        # Duplicate key — race condition, fetch existing
        logger.debug("Match insert race condition for %s vs %s: %s", home_display, away_display, e)
        return await _db.db.matches.find_one({
            "sport_key": sport_key,
            "season": sc,
            "home_team_key": home_key,
            "away_team_key": away_key,
            "match_date_hour": match_date_normalized,
        })
