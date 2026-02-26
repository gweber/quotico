"""
backend/app/services/event_handlers/match_handlers.py

Purpose:
    Subscriber logic for match-domain events. Finalized matches trigger resolver
    and leaderboard recomputation in an idempotent fashion.

Dependencies:
    - app.database
    - app.services.event_models
    - app.workers.match_resolver
    - app.workers.leaderboard
    - app.workers.matchday_leaderboard
"""

from __future__ import annotations

import logging

from bson import ObjectId

import app.database as _db
from app.services.event_models import BaseEvent
from app.workers.leaderboard import materialize_leaderboard
from app.workers.match_resolver import resolve_single_match
from app.workers.matchday_leaderboard import materialize_matchday_leaderboard

logger = logging.getLogger("quotico.event_handlers.match")


async def handle_match_finalized(event: BaseEvent) -> None:
    match_id = str(getattr(event, "match_id", "") or "")
    sport_key = str(getattr(event, "sport_key", "") or "")
    season = getattr(event, "season", None)
    if not match_id:
        return
    try:
        match_oid = ObjectId(match_id)
    except Exception:
        return

    # Idempotency guard: if no unresolved dependent slips exist, skip heavy resolver path.
    unresolved = await _db.db.betting_slips.find_one(
        {
            "selections.match_id": match_id,
            "status": {"$in": ["pending", "partial", "draft"]},
        },
        {"_id": 1},
    )
    if unresolved:
        await resolve_single_match(match_oid)

    await materialize_leaderboard()
    await materialize_matchday_leaderboard(
        sport_key=sport_key or None,
        season=int(season) if isinstance(season, int) else None,
    )
    logger.info("Processed match.finalized event for match_id=%s", match_id)
