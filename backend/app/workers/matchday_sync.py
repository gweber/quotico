"""Spieltag-Modus: sync matchdays from providers into MongoDB."""

import asyncio
import logging
from datetime import datetime, timedelta

import app.database as _db
from app.utils import ensure_utc, utcnow
from app.config_spieltag import SPIELTAG_SPORTS
from app.providers.football_data import football_data_provider, teams_match
from app.providers.openligadb import openligadb_provider, _current_season
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.matchday_sync")

# Rate-limit pause between API calls for football-data.org (free tier: 10 req/min)
_FOOTBALL_DATA_DELAY = 7  # seconds between matchday fetches


def _get_season_for_sport(sport_key: str) -> int:
    """Determine the current season year for a sport."""
    # All soccer leagues follow Jul–Jun season pattern
    return _current_season()


async def sync_matchdays() -> None:
    """Sync matchday data for all configured Spieltag sports.

    Syncs a window of 3 matchdays: previous, current, next.
    Runs every 30 min via scheduler.
    """
    for sport_key, config in SPIELTAG_SPORTS.items():
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

    # Check if full-season bootstrap is needed (always runs — skip smart sleep)
    existing_count = await _db.db.matchdays.count_documents({
        "sport_key": sport_key,
        "season": season,
    })
    needs_bootstrap = existing_count < max_matchdays

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
            state_key = f"matchday_sync:{sport_key}"
            if await recently_synced(state_key, timedelta(hours=6)):
                logger.debug("Smart sleep: %s has no active/upcoming matchdays, skipping sync", sport_key)
                return
            logger.info("Smart sleep safety: %s >6h since last sync, syncing anyway", sport_key)

    provider_name = config["provider"]

    # Get current matchday number from provider
    if provider_name == "openligadb":
        current_md = await openligadb_provider.get_current_matchday_number(sport_key)
    else:
        current_md = await football_data_provider.get_current_matchday_number(sport_key)

    if not current_md:
        logger.warning("Could not determine current matchday for %s", sport_key)
        return

    if needs_bootstrap:
        # Full-season sync: fetch all matchdays so the nav shows 1–34 (or 1–38, etc.)
        logger.info(
            "Full-season sync for %s: %d/%d matchdays exist, syncing all",
            sport_key, existing_count, max_matchdays,
        )
        delay = _FOOTBALL_DATA_DELAY if provider_name == "football_data" else 0
        for md_number in range(1, max_matchdays + 1):
            try:
                await _sync_single_matchday(sport_key, config, season, md_number)
            except Exception as e:
                logger.error("Full sync failed for %s matchday %d: %s", sport_key, md_number, e)
            if delay and md_number < max_matchdays:
                await asyncio.sleep(delay)
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
        # Find or create the match in our matches collection
        match_doc = await _find_or_create_match(sport_key, season, matchday_number, pm, now)
        if match_doc:
            match_ids.append(str(match_doc["_id"]))
            ct = match_doc.get("commence_time")
            if ct:
                kickoffs.append(ct)

    if not match_ids:
        return

    # Determine matchday status
    all_matches = await _db.db.matches.find(
        {"_id": {"$in": [__import__("bson").ObjectId(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))

    statuses = [m.get("status", "upcoming") for m in all_matches]
    all_resolved = all(s == "completed" for s in statuses)
    any_live = any(s == "live" for s in statuses)

    if all_resolved:
        md_status = "completed"
    elif any_live or any(s == "completed" for s in statuses):
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
    """Find existing match by team name + date, or create a new one.

    Links the match to the matchday via matchday_number/season fields.
    """
    home_team = provider_match["home_team"]
    away_team = provider_match["away_team"]
    utc_date = provider_match["utc_date"]

    if not home_team or not away_team:
        return None

    # Parse commence time
    if isinstance(utc_date, str) and utc_date:
        try:
            commence_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(utc_date, datetime):
        commence_time = ensure_utc(utc_date)
    else:
        return None

    # Try to find existing match by sport + matchday fields + team name
    existing = await _db.db.matches.find_one({
        "sport_key": sport_key,
        "matchday_season": season,
        "matchday_number": matchday_number,
        "teams.home": home_team,
    })

    if existing:
        # Update matchday fields + scores if completed
        update: dict = {
            "matchday_number": matchday_number,
            "matchday_season": season,
            "updated_at": now,
        }
        if provider_match.get("is_finished") and provider_match.get("home_score") is not None:
            update["home_score"] = provider_match["home_score"]
            update["away_score"] = provider_match["away_score"]
            if provider_match["home_score"] > provider_match["away_score"]:
                update["result"] = "1"
            elif provider_match["home_score"] == provider_match["away_score"]:
                update["result"] = "X"
            else:
                update["result"] = "2"
            update["status"] = "completed"

        await _db.db.matches.update_one(
            {"_id": existing["_id"]}, {"$set": update}
        )
        return await _db.db.matches.find_one({"_id": existing["_id"]})

    # Also try matching by team name fuzzy match (teams from OddsAPI may differ)
    from datetime import timedelta
    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "commence_time": {
            "$gte": commence_time - timedelta(hours=6),
            "$lte": commence_time + timedelta(hours=6),
        },
    }).to_list(length=50)

    for candidate in candidates:
        c_home = candidate.get("teams", {}).get("home", "")
        if teams_match(c_home, home_team):
            # Link existing match to this matchday
            update = {
                "matchday_number": matchday_number,
                "matchday_season": season,
                "updated_at": now,
            }
            if provider_match.get("is_finished") and provider_match.get("home_score") is not None:
                update["home_score"] = provider_match["home_score"]
                update["away_score"] = provider_match["away_score"]
                if provider_match["home_score"] > provider_match["away_score"]:
                    update["result"] = "1"
                elif provider_match["home_score"] == provider_match["away_score"]:
                    update["result"] = "X"
                else:
                    update["result"] = "2"
                update["status"] = "completed"

            await _db.db.matches.update_one(
                {"_id": candidate["_id"]}, {"$set": update}
            )
            return await _db.db.matches.find_one({"_id": candidate["_id"]})

    # No match found — create a new one (Spieltag-only match without OddsAPI data)
    status = "upcoming"
    if provider_match.get("is_finished"):
        status = "completed"
    elif commence_time <= now:
        status = "live"

    doc = {
        "external_id": f"spieltag:{sport_key}:{season}:{matchday_number}:{home_team}",
        "sport_key": sport_key,
        "teams": {"home": home_team, "away": away_team},
        "commence_time": commence_time,
        "status": status,
        "current_odds": {},
        "odds_updated_at": now,
        "result": None,
        "home_score": provider_match.get("home_score"),
        "away_score": provider_match.get("away_score"),
        "matchday_number": matchday_number,
        "matchday_season": season,
        "created_at": now,
        "updated_at": now,
    }

    if provider_match.get("is_finished") and provider_match.get("home_score") is not None:
        hs, aws = provider_match["home_score"], provider_match["away_score"]
        if hs > aws:
            doc["result"] = "1"
        elif hs == aws:
            doc["result"] = "X"
        else:
            doc["result"] = "2"

    result = await _db.db.matches.update_one(
        {"external_id": doc["external_id"]},
        {"$set": {k: v for k, v in doc.items() if k != "external_id"},
         "$setOnInsert": {"external_id": doc["external_id"]}},
        upsert=True,
    )

    return await _db.db.matches.find_one({"external_id": doc["external_id"]})
