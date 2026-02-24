import logging
from datetime import datetime

import app.database as _db
from app.models.badge import BADGE_DEFINITIONS
from app.utils import utcnow
from app.workers._state import get_synced_at, set_synced

logger = logging.getLogger("quotico.badge_engine")

_STATE_KEY = "badge_engine"


async def check_badges() -> None:
    """Background task: check all users for badge eligibility.

    Smart sleep: skips if no bets resolved and no squads created since last run.
    """
    last_run = await get_synced_at(_STATE_KEY)
    if last_run:
        recent_bet = await _db.db.betting_slips.find_one(
            {"resolved_at": {"$gte": last_run}},
        )
        recent_squad = await _db.db.squads.find_one(
            {"created_at": {"$gte": last_run}},
        )
        if not recent_bet and not recent_squad:
            logger.debug("Smart sleep: no activity since last run, skipping badge check")
            return

    total_awarded = 0
    async for user in _db.db.users.find({"is_deleted": False}, {"_id": 1, "points": 1}):
        user_id = str(user["_id"])
        awarded = await _check_user_badges(user_id, user)
        total_awarded += awarded

    await set_synced(_STATE_KEY)
    if total_awarded > 0:
        logger.info("Badge engine: awarded %d new badges", total_awarded)


async def _check_user_badges(user_id: str, user: dict) -> int:
    """Check and award badges for a single user. Returns count of new badges."""
    # Get existing badges
    existing = await _db.db.badges.find(
        {"user_id": user_id}
    ).to_list(length=100)
    existing_keys = {b["badge_key"] for b in existing}

    now = utcnow()
    awarded = 0

    # --- Bet count badges (classic single/parlay slips) ---
    slip_filter = {"user_id": user_id, "type": {"$in": ["single", "parlay"]}}
    bet_count = await _db.db.betting_slips.count_documents(slip_filter)

    if bet_count >= 1 and "first_bet" not in existing_keys:
        await _award(user_id, "first_bet", now)
        awarded += 1

    if bet_count >= 10 and "ten_bets" not in existing_keys:
        await _award(user_id, "ten_bets", now)
        awarded += 1

    if bet_count >= 50 and "fifty_bets" not in existing_keys:
        await _award(user_id, "fifty_bets", now)
        awarded += 1

    # --- Win badges ---
    won_count = await _db.db.betting_slips.count_documents(
        {"user_id": user_id, "status": "won", "type": {"$in": ["single", "parlay"]}}
    )

    if won_count >= 1 and "first_win" not in existing_keys:
        await _award(user_id, "first_win", now)
        awarded += 1

    # --- Underdog King: won a single bet with locked_odds > 4.0 ---
    if "underdog_king" not in existing_keys:
        underdog = await _db.db.betting_slips.find_one({
            "user_id": user_id,
            "status": "won",
            "type": "single",
            "selections.locked_odds": {"$gt": 4.0},
        })
        if underdog:
            await _award(user_id, "underdog_king", now)
            awarded += 1

    # --- Hot Streak: 3 consecutive wins ---
    if "hot_streak_3" not in existing_keys:
        recent_slips = await _db.db.betting_slips.find(
            {"user_id": user_id, "status": {"$in": ["won", "lost"]}, "type": {"$in": ["single", "parlay"]}}
        ).sort("resolved_at", -1).limit(20).to_list(length=20)

        streak = 0
        for slip in recent_slips:
            if slip["status"] == "won":
                streak += 1
                if streak >= 3:
                    await _award(user_id, "hot_streak_3", now)
                    awarded += 1
                    break
            else:
                streak = 0

    # --- Squad Leader: created a squad ---
    if "squad_leader" not in existing_keys:
        squad = await _db.db.squads.find_one({"admin_id": user_id})
        if squad:
            await _award(user_id, "squad_leader", now)
            awarded += 1

    # --- Battle Victor: participated in a battle ---
    if "battle_victor" not in existing_keys:
        participation = await _db.db.battle_participations.find_one({"user_id": user_id})
        if participation:
            await _award(user_id, "battle_victor", now)
            awarded += 1

    # --- Century Points: 100+ points ---
    if "century_points" not in existing_keys and user.get("points", 0) >= 100:
        await _award(user_id, "century_points", now)
        awarded += 1

    # --- Matchday badges ---
    if "matchday_debut" not in existing_keys:
        matchday_pred = await _db.db.matchday_predictions.find_one({
            "user_id": user_id, "status": "resolved",
        })
        if matchday_pred:
            await _award(user_id, "matchday_debut", now)
            awarded += 1

    if "oracle" not in existing_keys:
        exact_pred = await _db.db.matchday_predictions.find_one({
            "user_id": user_id,
            "predictions.points_earned": 3,
        })
        if exact_pred:
            await _award(user_id, "oracle", now)
            awarded += 1

    if "perfect_matchday" not in existing_keys:
        # Find a resolved prediction where ALL predictions scored 3
        perfect = await _db.db.matchday_predictions.find_one({
            "user_id": user_id,
            "status": "resolved",
            "predictions": {"$not": {"$elemMatch": {"points_earned": {"$ne": 3}}}},
            "predictions.0": {"$exists": True},  # At least 1 prediction
        })
        if perfect:
            await _award(user_id, "perfect_matchday", now)
            awarded += 1

    return awarded


async def _award(user_id: str, badge_key: str, now: datetime) -> None:
    """Award a badge to a user."""
    await _db.db.badges.insert_one({
        "user_id": user_id,
        "badge_key": badge_key,
        "awarded_at": now,
    })
    badge = BADGE_DEFINITIONS.get(badge_key, {})
    logger.info(
        "Badge awarded: %s → %s (%s)",
        user_id, badge_key, badge.get("name", "?"),
    )
