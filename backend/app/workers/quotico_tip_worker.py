"""QuoticoTip pre-computation worker.

Runs every 30 minutes to generate/refresh value-bet recommendations for all
upcoming soccer matches. Uses smart sleep to skip when no odds have been updated.
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

    Smart sleep: only runs if odds have been updated since last bet generation.
    """
    now = utcnow()

    # Smart sleep: check if any odds were polled since our last run
    target_statuses = ["scheduled", "live"]
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        last_run = ensure_utc(last_run)
        recent_odds_update = await _db.db.matches.find_one(
            {"odds.updated_at": {"$gte": last_run}, "status": {"$in": target_statuses}},
        )
        if not recent_odds_update:
            # Still generate tips for live matches that have none
            missing_tip = await _db.db.matches.find_one(
                {"status": "live", "odds.h2h": {"$ne": {}}},
            )
            if missing_tip:
                existing_tip = await _db.db.quotico_tips.find_one(
                    {"match_id": str(missing_tip["_id"])},
                )
                if not existing_tip:
                    logger.info("Smart sleep bypassed: live match without tip found")
                else:
                    logger.debug("Smart sleep: no odds updates since last bet generation")
                    return
            else:
                logger.debug("Smart sleep: no odds updates since last bet generation")
                return

    # Fetch all scheduled and live matches
    matches = await _db.db.matches.find(
        {"status": {"$in": target_statuses}},
    ).to_list(length=500)

    if not matches:
        await set_synced(_STATE_KEY)
        return

    generated = 0
    no_signal = 0
    fresh = 0

    for match in matches:
        match_id = str(match["_id"])

        # Check if existing bet is still fresh (generated after last odds update)
        existing = await _db.db.quotico_tips.find_one(
            {"match_id": match_id},
            {"generated_at": 1},
        )
        if existing:
            odds_updated = ensure_utc(match.get("odds", {}).get("updated_at") or now)
            bet_generated = ensure_utc(existing["generated_at"])
            if bet_generated >= odds_updated:
                fresh += 1
                continue

        try:
            bet = await generate_quotico_tip(match)
            await _db.db.quotico_tips.update_one(
                {"match_id": match_id},
                {"$set": bet},
                upsert=True,
            )
            if bet.get("status") == "active":
                generated += 1
            else:
                no_signal += 1
        except Exception as e:
            logger.error("Failed to generate bet for %s: %s", match_id, e)

    # Expire tips only for matches that reached a terminal state (final/cancelled)
    final_match_ids = await _db.db.matches.distinct(
        "_id", {"status": {"$in": ["final", "cancelled"]}},
    )
    final_match_id_strs = [str(mid) for mid in final_match_ids]
    expired = await _db.db.quotico_tips.update_many(
        {"status": {"$in": ["active", "no_signal"]}, "match_id": {"$in": final_match_id_strs}},
        {"$set": {"status": "expired"}},
    )

    await set_synced(_STATE_KEY)
    logger.info(
        "QuoticoTips: %d generated, %d no_signal, %d fresh, %d expired",
        generated, no_signal, fresh, expired.modified_count,
    )
