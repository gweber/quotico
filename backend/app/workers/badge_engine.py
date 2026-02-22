import logging
from datetime import datetime, timezone

import app.database as _db
from app.models.badge import BADGE_DEFINITIONS

logger = logging.getLogger("quotico.badge_engine")


async def check_badges() -> None:
    """Background task: check all users for badge eligibility."""
    users = await _db.db.users.find(
        {"is_deleted": False}
    ).to_list(length=10000)

    total_awarded = 0
    for user in users:
        user_id = str(user["_id"])
        awarded = await _check_user_badges(user_id, user)
        total_awarded += awarded

    if total_awarded > 0:
        logger.info("Badge engine: awarded %d new badges", total_awarded)


async def _check_user_badges(user_id: str, user: dict) -> int:
    """Check and award badges for a single user. Returns count of new badges."""
    # Get existing badges
    existing = await _db.db.badges.find(
        {"user_id": user_id}
    ).to_list(length=100)
    existing_keys = {b["badge_key"] for b in existing}

    now = datetime.now(timezone.utc)
    awarded = 0

    # --- Tip count badges ---
    tip_count = await _db.db.tips.count_documents({"user_id": user_id})

    if tip_count >= 1 and "first_tip" not in existing_keys:
        await _award(user_id, "first_tip", now)
        awarded += 1

    if tip_count >= 10 and "ten_tips" not in existing_keys:
        await _award(user_id, "ten_tips", now)
        awarded += 1

    if tip_count >= 50 and "fifty_tips" not in existing_keys:
        await _award(user_id, "fifty_tips", now)
        awarded += 1

    # --- Win badges ---
    won_count = await _db.db.tips.count_documents({"user_id": user_id, "status": "won"})

    if won_count >= 1 and "first_win" not in existing_keys:
        await _award(user_id, "first_win", now)
        awarded += 1

    # --- Underdog King: won a tip with locked_odds > 4.0 ---
    if "underdog_king" not in existing_keys:
        underdog = await _db.db.tips.find_one({
            "user_id": user_id,
            "status": "won",
            "locked_odds": {"$gt": 4.0},
        })
        if underdog:
            await _award(user_id, "underdog_king", now)
            awarded += 1

    # --- Hot Streak: 3 consecutive wins ---
    if "hot_streak_3" not in existing_keys:
        recent_tips = await _db.db.tips.find(
            {"user_id": user_id, "status": {"$in": ["won", "lost"]}}
        ).sort("resolved_at", -1).limit(20).to_list(length=20)

        streak = 0
        for tip in recent_tips:
            if tip["status"] == "won":
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
