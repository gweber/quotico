"""
backend/app/workers/matchday_sync.py

Purpose:
    Sync matchday fixtures from league providers into MongoDB and keep matchday
    state current. Supports global scheduler runs and admin-triggered single
    league syncs.

Dependencies:
    - app.services.league_service.LeagueRegistry
    - app.providers.openligadb / app.providers.football_data
    - app.database
"""

import asyncio
import logging
from datetime import datetime, timedelta

import app.database as _db
from app.utils import parse_utc, utcnow
from app.config_matchday import MATCHDAY_SPORTS
from app.providers.football_data import football_data_provider
from app.providers.openligadb import openligadb_provider, _current_season
from app.providers.football_data import SPORT_TO_COMPETITION
from app.providers.openligadb import SPORT_TO_LEAGUE
from app.services.league_service import LeagueRegistry, league_feature_enabled
from app.services.provider_settings_service import provider_settings_service
from app.services.team_registry_service import TeamRegistry
from app.utils.team_matching import teams_match
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.matchday_sync")


def _get_season_for_sport(sport_key: str) -> int:
    """Determine the current season year for a sport."""
    return _current_season()


def _derive_season_start_year(match_date: datetime) -> int:
    return match_date.year if match_date.month >= 7 else match_date.year - 1


def _normalize_match_date(match_date: datetime) -> datetime:
    return match_date.replace(minute=0, second=0, microsecond=0)


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


async def sync_matchdays_for_sport(
    sport_key: str,
    season: int | None = None,
    full_season: bool = False,
) -> dict:
    """Admin entrypoint: sync one league and return execution metadata."""
    config = MATCHDAY_SPORTS.get(sport_key)
    if not config:
        raise ValueError(f"Sport key '{sport_key}' is not configured for matchday sync.")
    await _sync_sport_matchdays(
        sport_key,
        config,
        season_override=season,
        force_full_season=full_season,
    )
    return {
        "sport_key": sport_key,
        "status": "queued",
        "season": season,
        "full_season": full_season,
    }


async def _sync_sport_matchdays(
    sport_key: str,
    config: dict,
    *,
    season_override: int | None = None,
    force_full_season: bool = False,
) -> None:
    """Sync matchdays for a single sport.

    Smart sleep per sport: skips external API calls when no matchday
    activity is expected (all completed and next kickoff >48h away).
    Safety margin: syncs at least once every 6 hours.
    Bootstrap fills the complete season once.
    To avoid burst traffic, calls are paced by provider RPM settings.
    """
    now = utcnow()
    max_matchdays = config["matchdays_per_season"]
    season = int(season_override) if season_override is not None else _get_season_for_sport(sport_key)
    state_key = f"matchday_sync:{sport_key}"
    provider_name = config["provider"]
    provider_id = None
    if provider_name == "openligadb":
        provider_id = SPORT_TO_LEAGUE.get(sport_key)
    elif provider_name == "football_data":
        provider_id = SPORT_TO_COMPETITION.get(sport_key)

    league = await LeagueRegistry.get().ensure_for_import(
        sport_key,
        provider_name=provider_name,
        provider_id=provider_id or sport_key,
        auto_create_inactive=True,
    )
    if not league.get("is_active", False):
        logger.warning("Skipping matchday ingest for inactive league: %s", sport_key)
        return
    if not league_feature_enabled(league, "match_load", True):
        logger.info("Skipping matchday ingest for disabled match_load feature: %s", sport_key)
        return

    # Check if bootstrap is needed (first time this season)
    existing_count = await _db.db.matchdays.count_documents({
        "sport_key": sport_key,
        "season": season,
    })
    bootstrap_key = f"matchday_bootstrap:{sport_key}:{season}"
    bootstrap_done = await recently_synced(bootstrap_key, timedelta(days=7))
    needs_bootstrap = existing_count == 0 and not bootstrap_done

    if not needs_bootstrap and not force_full_season:
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

    min_interval_seconds = await _provider_min_interval_seconds(provider_name, sport_key)

    if force_full_season:
        logger.info(
            "Forced full-season sync for %s season=%d: syncing all %d matchdays (min_interval=%.2fs)",
            sport_key,
            season,
            max_matchdays,
            min_interval_seconds,
        )
        for idx, md_number in enumerate(range(1, max_matchdays + 1)):
            if idx > 0 and min_interval_seconds > 0:
                await asyncio.sleep(min_interval_seconds)
            try:
                await _sync_single_matchday(sport_key, config, season, md_number)
            except Exception as e:
                logger.error("Forced full sync failed for %s matchday %d: %s", sport_key, md_number, e)
        await set_synced(bootstrap_key)
    elif needs_bootstrap:
        logger.info(
            "Full-season bootstrap for %s: syncing all %d matchdays (min_interval=%.2fs)",
            sport_key,
            max_matchdays,
            min_interval_seconds,
        )
        for idx, md_number in enumerate(range(1, max_matchdays + 1)):
            if idx > 0 and min_interval_seconds > 0:
                await asyncio.sleep(min_interval_seconds)
            try:
                await _sync_single_matchday(sport_key, config, season, md_number)
            except Exception as e:
                logger.error("Full sync failed for %s matchday %d: %s", sport_key, md_number, e)
        await set_synced(bootstrap_key)
    else:
        # Passed smart sleep gate — now fetch current matchday from provider
        if provider_name == "openligadb":
            current_md = await openligadb_provider.get_current_matchday_number(sport_key)
        else:
            current_md = await football_data_provider.get_current_matchday_number(sport_key)

        if not current_md:
            logger.warning("Could not determine current matchday for %s", sport_key)
            return
        # Incremental sync: previous, current, next matchday
        matchdays_to_sync = [
            md for md in [current_md - 1, current_md, current_md + 1]
            if 1 <= md <= max_matchdays
        ]
        for idx, md_number in enumerate(matchdays_to_sync):
            if idx > 0 and min_interval_seconds > 0:
                await asyncio.sleep(min_interval_seconds)
            await _sync_single_matchday(sport_key, config, season, md_number)

    await set_synced(f"matchday_sync:{sport_key}")


async def _provider_min_interval_seconds(provider_name: str, sport_key: str) -> float:
    """Return pacing interval derived from provider RPM config."""
    try:
        effective = await provider_settings_service.get_effective(
            provider_name,
            sport_key=sport_key,
            include_secret=False,
        )
        cfg = effective.get("effective_config") or {}
        rpm = int(cfg.get("rate_limit_rpm") or 0)
        if rpm <= 0:
            return 0.0
        return max(0.0, 60.0 / float(rpm))
    except Exception:
        logger.exception("Failed to resolve provider RPM for %s/%s", provider_name, sport_key)
        return 0.0


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
    """Find existing match by team IDs + date, or create a new one."""
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

    match_date_normalized = _normalize_match_date(match_date_raw)

    registry = TeamRegistry.get()
    home_team_id = await registry.resolve(home_team, sport_key)
    away_team_id = await registry.resolve(away_team, sport_key)

    season_start = _derive_season_start_year(match_date_raw)

    # Find existing match by canonical IDs with ±24h window
    existing = await _db.db.matches.find_one({
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "match_date": {
            "$gte": match_date_raw - timedelta(hours=24),
            "$lte": match_date_raw + timedelta(hours=24),
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
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
        }
        if provider_match.get("round_name") is not None:
            update["round_name"] = str(provider_match.get("round_name"))
        if provider_match.get("group_name") is not None:
            update["group_name"] = str(provider_match.get("group_name"))
        if provider_match.get("score_extra_time") is not None:
            update["score_extra_time"] = provider_match.get("score_extra_time")
        if provider_match.get("score_penalties") is not None:
            update["score_penalties"] = provider_match.get("score_penalties")
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
        "season": season_start,
        "home_team": home_team,
        "away_team": away_team,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "metadata": {"source": "matchday_sync"},
        "matchday_number": matchday_number,
        "matchday_season": season,
        "round_name": provider_match.get("round_name"),
        "group_name": provider_match.get("group_name"),
        "score_extra_time": provider_match.get("score_extra_time"),
        "score_penalties": provider_match.get("score_penalties"),
        "odds_meta": {
            "updated_at": None,
            "version": 0,
            "markets": {},
        },
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
        logger.debug("Match insert race condition for %s vs %s: %s", home_team, away_team, e)
        return await _db.db.matches.find_one({
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "match_date": {
                "$gte": match_date_raw - timedelta(hours=24),
                "$lte": match_date_raw + timedelta(hours=24),
            },
        })
