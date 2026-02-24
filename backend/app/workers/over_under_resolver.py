"""Resolve over/under bets for completed matches."""

import logging

from bson import ObjectId

import app.database as _db
from app.models.wallet import BetStatus
from app.services import wallet_service
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.over_under_resolver")


async def resolve_over_under_bets() -> None:
    """Find pending O/U bets for completed matches and resolve them."""
    from datetime import timedelta

    state_key = "over_under_resolver"
    if await recently_synced(state_key, timedelta(hours=6)):
        has_pending = await _db.db.over_under_bets.find_one({"status": BetStatus.pending.value})
        if not has_pending:
            logger.debug("Smart sleep: no pending O/U bets")
            return

    now = utcnow()
    resolved_count = 0

    pending = await _db.db.over_under_bets.find(
        {"status": BetStatus.pending.value}
    ).to_list(length=5000)

    for bet in pending:
        match = await _db.db.matches.find_one({"_id": ObjectId(bet["match_id"])})
        if not match or match["status"] != "final":
            continue
        result = match.get("result", {})
        if result.get("home_score") is None or result.get("away_score") is None:
            continue

        total_goals = result["home_score"] + result["away_score"]
        line = bet["line"]
        prediction = bet["prediction"]

        if total_goals > line:
            actual = "over"
        elif total_goals < line:
            actual = "under"
        else:
            # Push — void the bet, refund stake
            if bet.get("wallet_id") and bet.get("stake"):
                await wallet_service.credit_win(
                    wallet_id=bet["wallet_id"],
                    user_id=bet["user_id"],
                    squad_id=bet["squad_id"],
                    amount=bet["stake"],
                    reference_type="over_under_bet",
                    reference_id=str(bet["_id"]),
                    description=f"Push (refund): {total_goals} goals = line {line}",
                )
            await _db.db.over_under_bets.update_one(
                {"_id": bet["_id"]},
                {"$set": {"status": BetStatus.void.value, "resolved_at": now}},
            )
            resolved_count += 1
            continue

        is_won = prediction == actual

        if is_won and bet.get("wallet_id") and bet.get("stake"):
            win_amount = bet["stake"] * bet["locked_odds"]
            await wallet_service.credit_win(
                wallet_id=bet["wallet_id"],
                user_id=bet["user_id"],
                squad_id=bet["squad_id"],
                amount=win_amount,
                reference_type="over_under_bet",
                reference_id=str(bet["_id"]),
                description=f"O/U win: {prediction} {line} ({total_goals} goals)",
            )

        points_earned = bet["locked_odds"] if is_won else 0.0
        await _db.db.over_under_bets.update_one(
            {"_id": bet["_id"]},
            {"$set": {
                "status": BetStatus.won.value if is_won else BetStatus.lost.value,
                "points_earned": points_earned,
                "resolved_at": now,
            }},
        )

        if not is_won and bet.get("wallet_id"):
            await wallet_service.mark_bankrupt_if_needed(bet["wallet_id"])

        resolved_count += 1

    if resolved_count:
        logger.info("Resolved %d O/U bets", resolved_count)
    await set_synced(state_key)
