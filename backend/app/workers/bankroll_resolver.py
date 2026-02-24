"""Resolve bankroll bets for completed matches."""

import logging

from bson import ObjectId

import app.database as _db
from app.models.wallet import BetStatus, TransactionType
from app.services import wallet_service
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.bankroll_resolver")


async def resolve_bankroll_bets() -> None:
    """Find pending bankroll bets for completed matches and resolve them.

    Smart sleep: skips if no bankroll_bets were recently created.
    """
    from datetime import timedelta

    state_key = "bankroll_resolver"
    if await recently_synced(state_key, timedelta(hours=6)):
        # Check if there's actually pending work
        has_pending = await _db.db.bankroll_bets.find_one({"status": BetStatus.pending.value})
        if not has_pending:
            logger.debug("Smart sleep: no pending bankroll bets")
            return

    now = utcnow()
    resolved_count = 0
    won_count = 0

    # Find pending bets whose matches are completed
    pending_bets = await _db.db.bankroll_bets.find(
        {"status": BetStatus.pending.value}
    ).to_list(length=5000)

    for bet in pending_bets:
        match = await _db.db.matches.find_one({"_id": ObjectId(bet["match_id"])})
        if not match or match["status"] != "final" or not match.get("result", {}).get("outcome"):
            continue

        prediction = bet["prediction"]
        result = match["result"]["outcome"]
        is_won = prediction == result

        if is_won:
            win_amount = bet["stake"] * bet["locked_odds"]
            await wallet_service.credit_win(
                wallet_id=bet["wallet_id"],
                user_id=bet["user_id"],
                squad_id=bet["squad_id"],
                amount=win_amount,
                reference_type="bankroll_bet",
                reference_id=str(bet["_id"]),
                description=f"Win: {match['home_team']} vs {match['away_team']} -> {prediction} ({win_amount:.0f} coins)",
            )
            won_count += 1

        # Update bet status
        await _db.db.bankroll_bets.update_one(
            {"_id": bet["_id"]},
            {"$set": {
                "status": BetStatus.won.value if is_won else BetStatus.lost.value,
                "points_earned": bet["potential_win"] if is_won else 0.0,
                "resolved_at": now,
            }},
        )

        # Check if wallet is now bankrupt
        if not is_won:
            await wallet_service.mark_bankrupt_if_needed(bet["wallet_id"])

        # Log loss transaction (informational, amount=0)
        if not is_won:
            await _db.db.wallet_transactions.insert_one({
                "wallet_id": bet["wallet_id"],
                "user_id": bet["user_id"],
                "squad_id": bet["squad_id"],
                "type": TransactionType.BET_LOST.value,
                "amount": 0.0,
                "balance_after": (await _db.db.wallets.find_one(
                    {"_id": ObjectId(bet["wallet_id"])}
                ) or {}).get("balance", 0),
                "reference_type": "bankroll_bet",
                "reference_id": str(bet["_id"]),
                "description": f"Lost: {match['home_team']} vs {match['away_team']}",
                "created_at": now,
            })

        resolved_count += 1

    if resolved_count:
        logger.info(
            "Resolved %d bankroll bets (%d winners)",
            resolved_count, won_count,
        )
    await set_synced(state_key)
