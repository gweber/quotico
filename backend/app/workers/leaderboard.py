import logging
from datetime import datetime, timedelta, timezone

import app.database as _db
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.leaderboard")

_STATE_KEY = "leaderboard"


async def materialize_leaderboard() -> None:
    """Recompute the materialized leaderboard collection from users.

    Smart sleep: skips if no points were awarded since last materialization.
    Runs after match resolution to keep leaderboard up to date.
    Stores alias (public name) instead of email — no PII in materialized view.
    """
    # Check if any points were awarded since last run
    from app.workers._state import get_synced_at
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        recent_activity = await _db.db.points_transactions.find_one(
            {"created_at": {"$gte": last_run}},
        )
        if not recent_activity:
            logger.debug("Smart sleep: no new points since last run, skipping leaderboard")
            return

    pipeline = [
        {"$match": {"is_deleted": False, "points": {"$gt": 0}}},
        {"$sort": {"points": -1}},
        {"$limit": 500},
        {"$project": {"alias": 1, "points": 1, "is_bot": 1}},
    ]

    users = await _db.db.users.aggregate(pipeline).to_list(length=500)

    if not users:
        return

    # Replace entire leaderboard collection
    await _db.db.leaderboard.delete_many({})

    docs = [
        {
            "user_id": str(u["_id"]),
            "alias": u.get("alias", "Anonymous"),
            "points": u["points"],
            "rank": i + 1,
            "is_bot": u.get("is_bot", False),
        }
        for i, u in enumerate(users)
    ]

    await _db.db.leaderboard.insert_many(docs)
    await set_synced(_STATE_KEY)
    logger.info("Leaderboard materialized: %d entries", len(docs))
