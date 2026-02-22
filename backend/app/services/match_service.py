import logging
from datetime import datetime, timedelta
from typing import Optional

import app.database as _db
from app.models.match import MatchStatus
from app.providers.odds_api import odds_provider, SUPPORTED_SPORTS
from app.utils import utcnow

logger = logging.getLogger("quotico.match_service")

# Max match duration by sport type — used to determine the polling window.
# After commence_time + duration, we stop expecting live data.
# Soccer: 90 min + 15 min halftime + ~25 min extra/VAR/injury = 190 min
_MAX_DURATION: dict[str, timedelta] = {
    "americanfootball_nfl": timedelta(hours=4),
    "basketball_nba": timedelta(hours=3),
}
_DEFAULT_DURATION = timedelta(minutes=190)  # soccer, tennis


async def sports_with_live_action() -> set[str]:
    """Return the set of sport_keys that likely have matches in progress right now.

    Checks our own DB — zero external API calls.
    A match is "possibly live" if it has started (commence_time <= now)
    but hasn't been completed yet and is within its expected duration window.
    """
    now = utcnow()
    live_sports: set[str] = set()

    for sport_key in SUPPORTED_SPORTS:
        max_dur = _MAX_DURATION.get(sport_key, _DEFAULT_DURATION)
        has_live = await _db.db.matches.find_one({
            "sport_key": sport_key,
            "status": {"$in": ["upcoming", "live"]},
            "commence_time": {
                "$lte": now,                  # has kicked off
                "$gte": now - max_dur,        # not ancient
            },
        })
        if has_live:
            live_sports.add(sport_key)

    return live_sports


async def next_kickoff_in() -> timedelta | None:
    """How long until the next upcoming match kicks off? None if nothing scheduled."""
    now = utcnow()
    nxt = await _db.db.matches.find_one(
        {
            "status": "upcoming",
            "commence_time": {"$gt": now},
        },
        sort=[("commence_time", 1)],
        projection={"commence_time": 1},
    )
    if nxt:
        return nxt["commence_time"] - now
    return None


async def sync_matches_for_sport(sport_key: str) -> int:
    """Fetch odds from provider and upsert matches in DB.

    Returns the number of matches upserted.
    """
    matches_data = await odds_provider.get_odds(sport_key)
    if not matches_data:
        return 0

    now = utcnow()
    count = 0

    for m in matches_data:
        commence_time = m["commence_time"]
        if isinstance(commence_time, str):
            commence_time = datetime.fromisoformat(
                commence_time.replace("Z", "+00:00")
            )

        # Determine status for NEW inserts only
        max_dur = _MAX_DURATION.get(m["sport_key"], _DEFAULT_DURATION)
        if commence_time <= now:
            initial_status = MatchStatus.completed if (now - commence_time) > max_dur else MatchStatus.live
        else:
            initial_status = MatchStatus.upcoming

        # Always update odds/times — but NOT status blindly
        set_fields = {
            "sport_key": m["sport_key"],
            "teams": m["teams"],
            "commence_time": commence_time,
            "current_odds": m["odds"],
            "odds_updated_at": now,
            "updated_at": now,
        }
        if "totals_odds" in m:
            set_fields["totals_odds"] = m["totals_odds"]

        # Only update status for non-terminal matches
        existing = await _db.db.matches.find_one(
            {"external_id": m["external_id"]},
            projection={"status": 1},
        )
        if existing:
            cur_status = existing.get("status")
            if cur_status not in (MatchStatus.completed, MatchStatus.cancelled):
                if commence_time > now:
                    set_fields["status"] = MatchStatus.upcoming
                elif (now - commence_time) <= max_dur:
                    set_fields["status"] = MatchStatus.live
                # Past duration but not resolved → leave as-is for match_resolver

        await _db.db.matches.update_one(
            {"external_id": m["external_id"]},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "external_id": m["external_id"],
                    "status": initial_status,
                    "result": None,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        count += 1

    logger.info("Synced %d matches for %s", count, sport_key)
    return count


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
    """Get matches with optional filters, sorted by commence_time."""
    query: dict = {}
    if sport_key:
        query["sport_key"] = sport_key
    if status:
        query["status"] = status
    else:
        # Default: show upcoming and live matches
        query["status"] = {"$in": [MatchStatus.upcoming, MatchStatus.live]}

    cursor = _db.db.matches.find(query).sort("commence_time", 1).limit(limit)
    return await cursor.to_list(length=limit)
