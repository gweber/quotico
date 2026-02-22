"""Resolve parlay (Kombi-Joker) bets for completed matches."""

import logging

from bson import ObjectId

import app.database as _db
from app.models.wallet import BetStatus
from app.services import wallet_service
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.parlay_resolver")


async def resolve_parlays() -> None:
    """Find pending parlays and resolve once all legs' matches are completed.

    Smart sleep: skips if no pending parlays exist.
    """
    from datetime import timedelta

    state_key = "parlay_resolver"
    if await recently_synced(state_key, timedelta(hours=6)):
        has_pending = await _db.db.parlays.find_one({"status": BetStatus.pending.value})
        if not has_pending:
            logger.debug("Smart sleep: no pending parlays")
            return

    now = utcnow()
    resolved_count = 0

    pending = await _db.db.parlays.find(
        {"status": BetStatus.pending.value}
    ).to_list(length=5000)

    for parlay in pending:
        legs = parlay.get("legs", [])
        all_resolved = True
        any_lost = False
        updated_legs = []

        for leg in legs:
            if leg["result"] != "pending":
                updated_legs.append(leg)
                if leg["result"] == "lost":
                    any_lost = True
                continue

            match = await _db.db.matches.find_one({"_id": ObjectId(leg["match_id"])})
            if not match or match["status"] != "completed":
                all_resolved = False
                updated_legs.append(leg)
                continue

            prediction = leg["prediction"]

            if prediction in ("over", "under"):
                # Over/Under leg
                if match.get("home_score") is None or match.get("away_score") is None:
                    all_resolved = False
                    updated_legs.append(leg)
                    continue

                total_goals = match["home_score"] + match["away_score"]
                totals = match.get("totals_odds", {})
                line = totals.get("line", 2.5)

                if total_goals > line:
                    actual = "over"
                elif total_goals < line:
                    actual = "under"
                else:
                    # Push on this leg — void the entire parlay
                    leg["result"] = "void"
                    updated_legs.append(leg)
                    await _void_parlay(parlay, updated_legs, now)
                    resolved_count += 1
                    break

                leg["result"] = "won" if prediction == actual else "lost"
            else:
                # 1/X/2 leg
                if not match.get("result"):
                    all_resolved = False
                    updated_legs.append(leg)
                    continue
                leg["result"] = "won" if prediction == match["result"] else "lost"

            if leg["result"] == "lost":
                any_lost = True
            updated_legs.append(leg)
        else:
            # Loop completed without break (no void)
            if not all_resolved:
                # Save any leg updates we made
                if updated_legs != legs:
                    await _db.db.parlays.update_one(
                        {"_id": parlay["_id"]},
                        {"$set": {"legs": updated_legs}},
                    )
                continue

            # All legs resolved
            is_won = not any_lost

            if is_won and parlay.get("stake") and parlay.get("squad_id"):
                # Bankroll mode — credit winnings
                wallet = await _db.db.wallets.find_one({
                    "user_id": parlay["user_id"],
                    "squad_id": parlay["squad_id"],
                    "sport_key": parlay.get("sport_key"),
                    "season": parlay.get("season"),
                })
                if wallet:
                    await wallet_service.credit_win(
                        wallet_id=str(wallet["_id"]),
                        user_id=parlay["user_id"],
                        squad_id=parlay["squad_id"],
                        amount=parlay["potential_win"],
                        reference_type="parlay",
                        reference_id=str(parlay["_id"]),
                        description=f"Kombi-Joker Gewinn: Quote {parlay['combined_odds']:.2f} ({parlay['potential_win']:.0f} Coins)",
                    )

            points_earned = parlay["potential_win"] if is_won else 0.0
            new_status = BetStatus.won.value if is_won else BetStatus.lost.value

            await _db.db.parlays.update_one(
                {"_id": parlay["_id"]},
                {"$set": {
                    "legs": updated_legs,
                    "status": new_status,
                    "points_earned": points_earned,
                    "resolved_at": now,
                }},
            )

            if not is_won and parlay.get("stake"):
                wallet = await _db.db.wallets.find_one({
                    "user_id": parlay["user_id"],
                    "squad_id": parlay["squad_id"],
                    "sport_key": parlay.get("sport_key"),
                    "season": parlay.get("season"),
                })
                if wallet:
                    await wallet_service.mark_bankrupt_if_needed(str(wallet["_id"]))

            resolved_count += 1

    if resolved_count:
        logger.info("Resolved %d parlays", resolved_count)
    await set_synced(state_key)


async def _void_parlay(parlay: dict, updated_legs: list, now) -> None:
    """Void a parlay (push on a leg) — refund stake if applicable."""
    if parlay.get("stake") and parlay.get("squad_id"):
        wallet = await _db.db.wallets.find_one({
            "user_id": parlay["user_id"],
            "squad_id": parlay["squad_id"],
            "sport_key": parlay.get("sport_key"),
            "season": parlay.get("season"),
        })
        if wallet:
            await wallet_service.credit_win(
                wallet_id=str(wallet["_id"]),
                user_id=parlay["user_id"],
                squad_id=parlay["squad_id"],
                amount=parlay["stake"],
                reference_type="parlay",
                reference_id=str(parlay["_id"]),
                description=f"Kombi-Joker Push (Rückerstattung): {parlay['stake']:.0f} Coins",
            )

    await _db.db.parlays.update_one(
        {"_id": parlay["_id"]},
        {"$set": {
            "legs": updated_legs,
            "status": BetStatus.void.value,
            "points_earned": 0.0,
            "resolved_at": now,
        }},
    )
