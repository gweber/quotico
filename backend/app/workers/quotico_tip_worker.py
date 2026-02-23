"""QuoticoTip pre-computation worker.

Runs every 30 minutes to generate/refresh value-bet tips for all upcoming
soccer matches. Uses smart sleep to skip when no odds have been updated.
"""

import logging

import app.database as _db
from app.services.quotico_tip_service import generate_quotico_tip
from app.utils import ensure_utc, utcnow
from app.workers._state import get_synced_at, set_synced

logger = logging.getLogger("quotico.quotico_tip_worker")

_STATE_KEY = "quotico_tips"


async def generate_quotico_tips() -> None:
    """Pre-compute QuoticoTips for all upcoming matches.

    Smart sleep: only runs if odds have been updated since last tip generation.
    """
    now = utcnow()

    # Smart sleep: check if any odds were polled since our last run
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        last_run = ensure_utc(last_run)
        recent_odds_update = await _db.db.matches.find_one(
            {"odds_updated_at": {"$gte": last_run}, "status": "upcoming"},
        )
        if not recent_odds_update:
            logger.debug("Smart sleep: no odds updates since last tip generation")
            return

    # Fetch all upcoming matches
    matches = await _db.db.matches.find(
        {"status": "upcoming"},
    ).to_list(length=500)

    if not matches:
        await set_synced(_STATE_KEY)
        return

    generated = 0
    no_signal = 0
    fresh = 0

    for match in matches:
        match_id = str(match["_id"])

        # Check if existing tip is still fresh (generated after last odds update)
        existing = await _db.db.quotico_tips.find_one(
            {"match_id": match_id},
            {"generated_at": 1},
        )
        if existing:
            odds_updated = ensure_utc(match.get("odds_updated_at", now))
            tip_generated = ensure_utc(existing["generated_at"])
            if tip_generated >= odds_updated:
                fresh += 1
                continue

        try:
            tip = await generate_quotico_tip(match)
            await _db.db.quotico_tips.update_one(
                {"match_id": match_id},
                {"$set": tip},
                upsert=True,
            )
            if tip.get("status") == "active":
                generated += 1
            else:
                no_signal += 1
        except Exception as e:
            logger.error("Failed to generate tip for %s: %s", match_id, e)

    # Expire stale tips (match has started)
    expired = await _db.db.quotico_tips.update_many(
        {"status": "active", "match_commence_time": {"$lte": now}},
        {"$set": {"status": "expired"}},
    )

    await set_synced(_STATE_KEY)
    logger.info(
        "QuoticoTips: %d generated, %d no_signal, %d fresh, %d expired",
        generated, no_signal, fresh, expired.modified_count,
    )
