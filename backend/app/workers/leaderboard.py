import logging

import app.database as _db

logger = logging.getLogger("quotico.leaderboard")


async def materialize_leaderboard() -> None:
    """Recompute the materialized leaderboard collection from users.

    Runs after match resolution to keep leaderboard up to date.
    Stores alias (public name) instead of email — no PII in materialized view.
    """
    pipeline = [
        {"$match": {"is_deleted": False, "points": {"$gt": 0}}},
        {"$sort": {"points": -1}},
        {"$limit": 500},
        {"$project": {"alias": 1, "points": 1}},
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
        }
        for i, u in enumerate(users)
    ]

    await _db.db.leaderboard.insert_many(docs)
    logger.info("Leaderboard materialized: %d entries", len(docs))
