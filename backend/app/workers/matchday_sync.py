"""
backend/app/workers/matchday_sync.py

Purpose:
    Build and refresh matchday documents from Sportmonks-native v3 data
    (`matches_v3`) only. No legacy provider ingestion and no legacy
    `matches` writes are allowed.

Dependencies:
    - app.database
    - app.services.league_service
"""

import logging
from datetime import timedelta

import app.database as _db
from app.services.league_service import league_feature_enabled
from app.services.sportmonks_connector import sportmonks_connector
from app.utils import ensure_utc, utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.matchday_sync")


async def _list_active_tippable_leagues() -> list[dict]:
    return await _db.db.league_registry_v3.find(
        {"is_active": True, "features.tipping": True},
        {"_id": 1, "name": 1, "features": 1},
    ).to_list(length=500)


async def sync_matchdays() -> None:
    """Sync matchday snapshots for all active+tippable leagues."""
    leagues = await _list_active_tippable_leagues()
    for league in leagues:
        league_id = int(league["_id"])
        try:
            await _sync_league_matchdays(league_id=league_id, league=league)
        except Exception as exc:
            logger.error("Matchday sync failed for league_id=%s: %s", league_id, exc)


async def sync_matchdays_for_sport(
    league_id: int,
    season: int | None = None,
    full_season: bool = False,
) -> dict:
    """Admin entrypoint: sync one league."""
    league = await _db.db.league_registry_v3.find_one({"_id": int(league_id)})
    if not league:
        raise ValueError(f"League id '{league_id}' was not found in league_registry_v3.")

    await _sync_league_matchdays(
        league_id=int(league_id),
        league=league,
        season_override=season,
        force_full_season=full_season,
    )
    return {
        "league_id": int(league_id),
        "status": "queued",
        "season": season,
        "full_season": full_season,
    }


async def _resolve_v3_season_id(league_id: int) -> int | None:
    """Resolve current season id from matches_v3 for league."""
    base_query = {
        "league_id": int(league_id),
        "season_id": {"$ne": None},
    }

    now = utcnow()
    upcoming = await _db.db.matches_v3.find_one(
        {
            **base_query,
            "status": {"$in": ["SCHEDULED", "LIVE"]},
            "start_at": {"$gte": now - timedelta(days=7)},
        },
        sort=[("start_at", 1)],
        projection={"season_id": 1},
    )
    if upcoming and upcoming.get("season_id") is not None:
        return int(upcoming["season_id"])

    latest = await _db.db.matches_v3.find_one(
        base_query,
        sort=[("start_at", -1)],
        projection={"season_id": 1},
    )
    if latest and latest.get("season_id") is not None:
        return int(latest["season_id"])
    return None


async def _resolve_max_matchdays(league_id: int, season: int) -> int:
    rounds = await _db.db.matches_v3.distinct(
        "round_id",
        {"league_id": int(league_id), "season_id": int(season), "round_id": {"$ne": None}},
    )
    valid_rounds = [int(r) for r in rounds if isinstance(r, int) or str(r).isdigit()]
    return max(valid_rounds) if valid_rounds else 0


async def _sync_league_matchdays(
    league_id: int,
    league: dict,
    *,
    season_override: int | None = None,
    force_full_season: bool = False,
) -> None:
    """Sync matchdays for one league using only matches_v3."""
    now = utcnow()

    if not league.get("is_active", False):
        logger.warning("Skipping matchday ingest for inactive league_id=%s", league_id)
        return
    if not league_feature_enabled(league, "match_load", True):
        logger.info("Skipping matchday ingest for disabled match_load feature: league_id=%s", league_id)
        return

    season = int(season_override) if season_override is not None else await _resolve_v3_season_id(int(league_id))
    if season is None:
        logger.warning("Skipping league_id=%s: no season_id resolvable from matches_v3", league_id)
        return

    max_matchdays = await _resolve_max_matchdays(int(league_id), int(season))
    if max_matchdays <= 0:
        logger.warning("Skipping league_id=%s: no round_id data in matches_v3", league_id)
        return

    try:
        await sportmonks_connector.run_metrics_sync(int(season))
    except Exception:
        logger.warning("Matchday refresh metrics sync failed for league_id=%s season=%s", league_id, season, exc_info=True)

    state_key = f"matchday_sync:{int(league_id)}"
    existing_count = await _db.db.matchdays.count_documents({"league_id": int(league_id), "season": int(season)})
    bootstrap_key = f"matchday_bootstrap:{int(league_id)}:{int(season)}"
    bootstrap_done = await recently_synced(bootstrap_key, timedelta(days=7))
    needs_bootstrap = existing_count == 0 and not bootstrap_done

    if not needs_bootstrap and not force_full_season:
        window = now + timedelta(hours=48)
        active = await _db.db.matchdays.find_one(
            {
                "league_id": int(league_id),
                "season": int(season),
                "$or": [
                    {"status": "in_progress"},
                    {"status": "upcoming", "first_kickoff": {"$lte": window}},
                ],
            }
        )
        if not active and await recently_synced(state_key, timedelta(hours=6)):
            logger.debug("Smart sleep: league_id=%s has no active/upcoming matchdays, skipping sync", league_id)
            return

    if force_full_season or needs_bootstrap:
        for round_number in range(1, int(max_matchdays) + 1):
            try:
                await _sync_single_matchday(int(league_id), int(season), round_number)
            except Exception as exc:
                logger.error("Full sync failed for league_id=%s round=%d: %s", league_id, round_number, exc)
        await set_synced(bootstrap_key)
    else:
        latest_md = await _db.db.matchdays.find_one(
            {"league_id": int(league_id), "season": int(season)},
            sort=[("matchday_number", -1)],
            projection={"matchday_number": 1},
        )
        current_md = int(latest_md["matchday_number"]) if latest_md and latest_md.get("matchday_number") else None
        if current_md is None:
            logger.warning("Could not determine current matchday for league_id=%s", league_id)
            return

        for round_number in [current_md - 1, current_md, current_md + 1]:
            if 1 <= int(round_number) <= int(max_matchdays):
                await _sync_single_matchday(int(league_id), int(season), int(round_number))

    await set_synced(state_key)


async def _sync_single_matchday(league_id: int, season: int, matchday_number: int) -> None:
    """Project one v3 round into the matchdays collection."""
    now = utcnow()
    rows = await _db.db.matches_v3.find(
        {
            "league_id": int(league_id),
            "season_id": int(season),
            "round_id": int(matchday_number),
        },
        {"_id": 1, "start_at": 1, "status": 1},
    ).to_list(length=400)

    if not rows:
        return

    match_ids = [str(int(row["_id"])) for row in rows if row.get("_id") is not None]
    kickoffs = [ensure_utc(row.get("start_at")) for row in rows if row.get("start_at")]
    statuses = [str((row.get("status") or "SCHEDULED")).upper() for row in rows]

    all_resolved = all(status == "FINISHED" for status in statuses)
    any_live = any(status == "LIVE" for status in statuses)
    any_finished = any(status == "FINISHED" for status in statuses)

    if all_resolved:
        md_status = "completed"
    elif any_live or any_finished:
        md_status = "in_progress"
    else:
        md_status = "upcoming"

    label = f"Matchday {int(matchday_number)}"
    matchday_id = f"v3:{int(league_id)}:{int(season)}:{int(matchday_number)}"

    await _db.db.matchdays.update_one(
        {"_id": matchday_id},
        {
            "$set": {
                "league_id": int(league_id),
                "season": int(season),
                "matchday_number": int(matchday_number),
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
                "created_at": now,
            },
        },
        upsert=True,
    )
