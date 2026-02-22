import logging
from datetime import datetime, timezone
from typing import Optional

import app.database as _db
from app.models.match import MatchStatus
from app.providers.odds_api import odds_provider

logger = logging.getLogger("quotico.match_service")


async def sync_matches_for_sport(sport_key: str) -> int:
    """Fetch odds from provider and upsert matches in DB.

    Returns the number of matches upserted.
    """
    matches_data = await odds_provider.get_odds(sport_key)
    if not matches_data:
        return 0

    now = datetime.now(timezone.utc)
    count = 0

    for m in matches_data:
        commence_time = m["commence_time"]
        if isinstance(commence_time, str):
            commence_time = datetime.fromisoformat(
                commence_time.replace("Z", "+00:00")
            )

        # Determine status based on time
        status = MatchStatus.upcoming
        if commence_time <= now:
            status = MatchStatus.live

        await _db.db.matches.update_one(
            {"external_id": m["external_id"]},
            {
                "$set": {
                    "sport_key": m["sport_key"],
                    "teams": m["teams"],
                    "commence_time": commence_time,
                    "status": status,
                    "current_odds": m["odds"],
                    "odds_updated_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "external_id": m["external_id"],
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
