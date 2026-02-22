from typing import Any

from fastapi import APIRouter, Query

import app.database as _db

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])


@router.get("/")
async def get_leaderboard(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Get the global leaderboard, sorted by points descending.

    Uses the materialized leaderboard collection for performance.
    Falls back to users collection if materialized view is empty.
    Public endpoint: returns alias only, never email.
    """
    # Try materialized leaderboard first
    entries = await _db.db.leaderboard.find().sort("points", -1).limit(limit).to_list(
        length=limit
    )

    if not entries:
        # Fallback: read directly from users
        entries = (
            await _db.db.users.find(
                {"is_deleted": False, "points": {"$gt": 0}},
                {"alias": 1, "points": 1},
            )
            .sort("points", -1)
            .limit(limit)
            .to_list(length=limit)
        )

    return [
        {
            "rank": i + 1,
            "alias": e.get("alias", "Anonymous"),
            "points": round(e.get("points", 0), 2),
        }
        for i, e in enumerate(entries)
    ]
