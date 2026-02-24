"""Match service — unified lifecycle: scheduled → live → final.

Handles odds sync from TheOddsAPI, match queries, and status transitions.
Team names are resolved to canonical keys at insert time via team_mapping_service.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import app.database as _db
from app.models.match import MatchStatus
from app.providers.odds_api import odds_provider, SUPPORTED_SPORTS
from app.services.team_mapping_service import (
    SPORT_KEY_TO_LEAGUE_CODE,
    derive_season_year,
    normalize_match_date,
    resolve_or_create_team,
    season_code,
    season_label,
)
from app.utils import ensure_utc, parse_utc, utcnow

logger = logging.getLogger("quotico.match_service")

# Max match duration — used for status computation and smart sleep.
_MAX_DURATION: dict[str, timedelta] = {}
_DEFAULT_DURATION = timedelta(minutes=190)  # soccer


def _compute_status(
    match_date: datetime,
    now: datetime,
    sport_key: str,
    existing: dict | None,
) -> str | None:
    """Compute the status for an existing or new match.

    Returns a status string, or None if the existing status should not be changed
    (i.e. already in a terminal state).
    """
    if existing:
        cur_status = existing.get("status")
        if cur_status in (MatchStatus.final, MatchStatus.cancelled):
            return None  # never downgrade terminal states
        # Preserve live/final for matches that already had results
        if cur_status == MatchStatus.final:
            return None

    max_dur = _MAX_DURATION.get(sport_key, _DEFAULT_DURATION)
    if match_date > now:
        return MatchStatus.scheduled
    elif (now - match_date) <= max_dur:
        return MatchStatus.live
    else:
        # Past expected duration — don't override to final (let resolver handle it)
        return None if existing else MatchStatus.final


async def sports_with_live_action() -> set[str]:
    """Return sport_keys that likely have matches in progress right now.

    Checks our own DB — zero external API calls.
    """
    now = utcnow()
    live_sports: set[str] = set()

    for sport_key in SUPPORTED_SPORTS:
        max_dur = _MAX_DURATION.get(sport_key, _DEFAULT_DURATION)
        has_live = await _db.db.matches.find_one({
            "sport_key": sport_key,
            "status": {"$in": [MatchStatus.scheduled, MatchStatus.live]},
            "match_date": {
                "$lte": now,
                "$gte": now - max_dur,
            },
        })
        if has_live:
            live_sports.add(sport_key)

    return live_sports


async def next_kickoff_in() -> timedelta | None:
    """How long until the next scheduled match kicks off?"""
    now = utcnow()
    nxt = await _db.db.matches.find_one(
        {
            "status": MatchStatus.scheduled,
            "match_date": {"$gt": now},
        },
        sort=[("match_date", 1)],
        projection={"match_date": 1},
    )
    if nxt:
        return ensure_utc(nxt["match_date"]) - now
    return None


async def sync_matches_for_sport(sport_key: str) -> dict:
    """Fetch odds from provider and upsert matches in DB.

    Key changes from old version:
    - Resolves team names via team_mapping_service at insert time
    - Dual lookup: metadata.theoddsapi_id (fast path) → compound key (fallback)
    - Writes to nested odds structure
    - Stores season, season_label, league_code, team keys

    Returns {"matches": count, "odds_changed": changed_count}.
    """
    matches_data = await odds_provider.get_odds(sport_key)
    if not matches_data:
        return {"matches": 0, "odds_changed": 0}

    now = utcnow()
    count = 0
    odds_changed = 0

    for m in matches_data:
        match_date_raw = m["commence_time"]
        if isinstance(match_date_raw, str):
            match_date_raw = parse_utc(match_date_raw)
        match_date_normalized = normalize_match_date(match_date_raw)

        # Resolve team names to canonical keys
        home_display, home_key = await resolve_or_create_team(
            m["teams"]["home"], sport_key,
        )
        away_display, away_key = await resolve_or_create_team(
            m["teams"]["away"], sport_key,
        )

        sy = derive_season_year(match_date_raw)
        sc = season_code(sy)
        sl = season_label(sy)

        # Build nested odds
        odds_h2h = m["odds"]
        odds_totals = m.get("totals_odds", {})
        odds_spreads = m.get("spreads_odds", {})

        # --- Find existing match ---

        # Fast path: by TheOddsAPI external_id
        existing = await _db.db.matches.find_one(
            {"metadata.theoddsapi_id": m["external_id"]},
            projection={"status": 1, "odds": 1},
        )

        if not existing:
            # Fallback: compound key with ±6h date window
            # (handles matchday-created matches that later get odds)
            existing = await _db.db.matches.find_one({
                "sport_key": sport_key,
                "home_team_key": home_key,
                "away_team_key": away_key,
                "match_date": {
                    "$gte": match_date_normalized - timedelta(hours=6),
                    "$lte": match_date_normalized + timedelta(hours=6),
                },
            }, projection={"status": 1, "odds": 1})

        # Detect odds changes
        if existing:
            old_odds = existing.get("odds", {})
            if (old_odds.get("h2h") != odds_h2h
                    or old_odds.get("totals") != odds_totals
                    or old_odds.get("spreads") != odds_spreads):
                odds_changed += 1

        # Determine status
        status = _compute_status(match_date_raw, now, sport_key, existing)

        # Freeze closing line on scheduled → live transition
        closing_line = None
        if (
            status == MatchStatus.live
            and existing
            and existing.get("status") == MatchStatus.scheduled
        ):
            old_odds = existing.get("odds", {})
            closing_line = {
                "h2h": old_odds.get("h2h", {}),
                "totals": old_odds.get("totals", {}),
                "spreads": old_odds.get("spreads", {}),
                "frozen_at": now,
            }

        # Build update — match_date is the raw accurate time (display/countdown),
        # match_date_hour is floored to the hour (compound unique index only)
        set_fields: dict = {
            "sport_key": sport_key,
            "match_date": match_date_raw,
            "match_date_hour": match_date_normalized,
            "home_team": home_display,
            "away_team": away_display,
            "home_team_key": home_key,
            "away_team_key": away_key,
            "season": sc,
            "season_label": sl,
            "league_code": SPORT_KEY_TO_LEAGUE_CODE.get(sport_key),
            "odds.h2h": odds_h2h,
            "odds.updated_at": now,
            "metadata.theoddsapi_id": m["external_id"],
            "metadata.source": "theoddsapi",
            "updated_at": now,
        }
        if odds_totals:
            set_fields["odds.totals"] = odds_totals
        if odds_spreads:
            set_fields["odds.spreads"] = odds_spreads
        if closing_line:
            set_fields["odds.closing_line"] = closing_line
        if status:
            set_fields["status"] = status

        # Choose upsert filter
        if existing:
            upsert_filter = {"_id": existing["_id"]}
        else:
            upsert_filter = {
                "sport_key": sport_key,
                "season": sc,
                "home_team_key": home_key,
                "away_team_key": away_key,
                "match_date_hour": match_date_normalized,
            }

        set_on_insert: dict = {
            "result": {
                "home_score": None, "away_score": None,
                "outcome": None, "half_time": None,
            },
            "created_at": now,
        }
        # Only set status in $setOnInsert when it's not already in $set
        if "status" not in set_fields:
            set_on_insert["status"] = MatchStatus.scheduled

        await _db.db.matches.update_one(
            upsert_filter,
            {
                "$set": set_fields,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )
        count += 1

    # Sweep: touch odds.updated_at for scheduled matches of this sport that
    # have valid odds but weren't in the API response (lines pulled near kickoff).
    seen_theoddsapi_ids = [m["external_id"] for m in matches_data]
    sweep_result = await _db.db.matches.update_many(
        {
            "sport_key": sport_key,
            "status": MatchStatus.scheduled,
            "odds.h2h": {"$ne": {}},
            "metadata.theoddsapi_id": {"$nin": seen_theoddsapi_ids},
        },
        {"$set": {"odds.updated_at": now}},
    )
    if sweep_result.modified_count:
        logger.info(
            "Swept %d unfound %s matches (odds.updated_at refreshed)",
            sweep_result.modified_count, sport_key,
        )

    logger.info("Synced %d matches for %s (%d odds changed)", count, sport_key, odds_changed)
    return {"matches": count, "odds_changed": odds_changed}


async def get_match_by_id(match_id: str) -> Optional[dict]:
    """Get a single match by its MongoDB _id."""
    from bson import ObjectId

    try:
        return await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    except Exception:
        return None


async def get_matches(
    sport_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Get matches with optional filters, sorted by match_date."""
    query: dict = {}
    if sport_key:
        query["sport_key"] = sport_key
    if status:
        # Accept both old (upcoming/completed) and new (scheduled/final) status names
        status_map = {"upcoming": MatchStatus.scheduled, "completed": MatchStatus.final}
        query["status"] = status_map.get(status, status)
    else:
        query["status"] = {"$in": [MatchStatus.scheduled, MatchStatus.live]}

    cursor = _db.db.matches.find(query).sort("match_date", 1).limit(limit)
    return await cursor.to_list(length=limit)
