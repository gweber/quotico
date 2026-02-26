"""Matchday mode: materialize season leaderboard from betting_slips."""

import logging
import app.database as _db
from app.utils import utcnow
from app.workers._state import get_synced_at, set_synced

logger = logging.getLogger("quotico.matchday_leaderboard")

_STATE_KEY = "matchday_leaderboard"


async def materialize_matchday_leaderboard(*, sport_key: str | None = None, season: int | None = None) -> None:
    """Aggregate resolved matchday_round slips into per-sport/season leaderboard.

    Smart sleep: skips if no slips were resolved since last materialization.
    """
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        recent_resolution = await _db.db.betting_slips.find_one(
            {"type": "matchday_round", "status": "resolved", "updated_at": {"$gte": last_run}},
        )
        if not recent_resolution:
            logger.debug("Smart sleep: no new matchday resolutions since last run, skipping")
            return

    match_filter = {"type": "matchday_round", "status": "resolved", "total_points": {"$ne": None}}
    if sport_key:
        match_filter["sport_key"] = str(sport_key)
    if season is not None:
        match_filter["season"] = int(season)

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": {
                    "sport_key": "$sport_key",
                    "season": "$season",
                    "user_id": "$user_id",
                    "squad_id": "$squad_id",
                },
                "total_points": {"$sum": "$total_points"},
                "matchdays_played": {"$sum": 1},
                "selections": {"$push": "$selections"},
            }
        },
    ]

    results = await _db.db.betting_slips.aggregate(pipeline).to_list(length=10000)
    now = utcnow()

    # Collect all user IDs for alias lookup
    user_ids = list({r["_id"]["user_id"] for r in results})
    if not user_ids:
        return

    from bson import ObjectId
    users = await _db.db.users.find(
        {"_id": {"$in": [ObjectId(uid) for uid in user_ids]}},
        {"alias": 1},
    ).to_list(length=len(user_ids))
    alias_map = {str(u["_id"]): u.get("alias", "Anonymous") for u in users}

    updated = 0
    for r in results:
        sport_key = r["_id"]["sport_key"]
        season = r["_id"]["season"]
        user_id = r["_id"]["user_id"]
        squad_id = r["_id"].get("squad_id")

        # Count point categories across all selections
        exact_count = 0
        diff_count = 0
        tendency_count = 0
        for sel_list in r["selections"]:
            for sel in sel_list:
                pts = sel.get("points_earned")
                if pts == 3:
                    exact_count += 1
                elif pts == 2:
                    diff_count += 1
                elif pts == 1:
                    tendency_count += 1

        await _db.db.matchday_leaderboard.update_one(
            {"sport_key": sport_key, "season": season, "user_id": user_id, "squad_id": squad_id},
            {
                "$set": {
                    "total_points": r["total_points"],
                    "matchdays_played": r["matchdays_played"],
                    "exact_count": exact_count,
                    "diff_count": diff_count,
                    "tendency_count": tendency_count,
                    "alias": alias_map.get(user_id, "Anonymous"),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "sport_key": sport_key,
                    "season": season,
                    "user_id": user_id,
                    "squad_id": squad_id,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        updated += 1

    await set_synced(_STATE_KEY)
    if updated:
        logger.info("Materialized matchday leaderboard: %d entries", updated)
