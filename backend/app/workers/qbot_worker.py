"""Q-Bot auto-tipper — places tips using QuoticoTip recommendations.

Runs every 30 minutes. Reads from the ``quotico_tips`` collection and
places real tips via ``tip_service.create_tip_internal()``.

Idempotency: The unique index on (user_id, match_id) in the tips
collection prevents double-tipping. DuplicateKeyError is caught and
silently skipped.
"""

import logging

from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.config import settings
from app.services.tip_service import create_tip_internal
from app.utils import ensure_utc, utcnow
from app.workers._state import get_synced_at, set_synced

logger = logging.getLogger("quotico.qbot")

_STATE_KEY = "qbot"


async def _get_qbot_user_id() -> str | None:
    """Fetch Q-Bot's user_id from the database."""
    user = await _db.db.users.find_one(
        {"email": "qbot@quotico.de", "is_bot": True},
        {"_id": 1},
    )
    return str(user["_id"]) if user else None


async def run_qbot() -> None:
    """Place tips for all high-confidence QuoticoTip recommendations.

    Smart sleep: only runs if quotico_tips were generated/updated since
    the last Q-Bot run.
    """
    now = utcnow()

    # Smart sleep: check if quotico_tips were updated since our last run
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        last_run = ensure_utc(last_run)
        recent_tip_gen = await _db.db.quotico_tips.find_one(
            {"generated_at": {"$gte": last_run}, "status": "active"},
        )
        if not recent_tip_gen:
            logger.debug("Smart sleep: no new QuoticoTips since last Q-Bot run")
            return

    # Get Q-Bot user ID
    qbot_id = await _get_qbot_user_id()
    if not qbot_id:
        logger.error("Q-Bot user not found in database — seed may have failed")
        return

    # Fetch all active QuoticoTips above confidence threshold
    min_conf = settings.QBOT_MIN_CONFIDENCE
    tips = await _db.db.quotico_tips.find(
        {
            "status": "active",
            "confidence": {"$gte": min_conf},
            "match_commence_time": {"$gt": now},
        },
    ).to_list(length=500)

    if not tips:
        await set_synced(_STATE_KEY)
        logger.debug("Q-Bot: no tips above confidence threshold %.2f", min_conf)
        return

    placed = 0
    skipped_dup = 0
    skipped_err = 0

    for qtip in tips:
        match_id = qtip["match_id"]
        prediction = qtip["recommended_selection"]

        try:
            await create_tip_internal(
                user_id=qbot_id,
                match_id=match_id,
                prediction=prediction,
            )
            placed += 1
        except DuplicateKeyError:
            skipped_dup += 1
        except Exception as e:
            logger.warning("Q-Bot failed to tip match %s: %s", match_id, e)
            skipped_err += 1

    await set_synced(_STATE_KEY)
    logger.info(
        "Q-Bot: %d tips placed, %d already existed, %d errors (threshold=%.2f)",
        placed, skipped_dup, skipped_err, min_conf,
    )
