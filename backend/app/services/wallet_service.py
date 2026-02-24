"""Virtual Wallet Engine — atomic balance operations with MongoDB sessions."""

import logging
from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.models.game_mode import GAME_MODE_DEFAULTS
from app.models.wallet import TransactionType, WalletStatus
from app.utils import utcnow

logger = logging.getLogger("quotico.wallet_service")


async def get_or_create_wallet(
    user_id: str, squad_id: str, sport_key: str, season: int
) -> dict:
    """Get existing wallet or create a new one with initial balance."""
    wallet = await _db.db.wallets.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
    })
    if wallet:
        return wallet

    # Get squad config for initial balance
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")

    # Try league_configs first, fall back to legacy game_mode_config
    from app.services.squad_league_service import get_active_league_config
    league_config = get_active_league_config(squad, sport_key)
    if league_config:
        config = league_config.get("config", {})
    else:
        config = squad.get("game_mode_config", {})
    initial_balance = config.get(
        "initial_balance",
        GAME_MODE_DEFAULTS["bankroll"]["initial_balance"],
    )

    now = utcnow()
    wallet_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
        "balance": float(initial_balance),
        "initial_balance": float(initial_balance),
        "total_wagered": 0.0,
        "total_won": 0.0,
        "status": WalletStatus.active.value,
        "bankrupt_since": None,
        "consecutive_bonus_days": 0,
        "last_daily_bonus_at": None,
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.wallets.insert_one(wallet_doc)
    wallet_doc["_id"] = result.inserted_id

    # Log initial credit transaction
    await _log_transaction(
        wallet_id=str(result.inserted_id),
        user_id=user_id,
        squad_id=squad_id,
        tx_type=TransactionType.INITIAL_CREDIT,
        amount=float(initial_balance),
        balance_after=float(initial_balance),
        description=f"Initial balance {sport_key} season {season}",
    )

    logger.info(
        "Wallet created: user=%s squad=%s sport=%s season=%d balance=%.0f",
        user_id, squad_id, sport_key, season, initial_balance,
    )
    return wallet_doc


async def deduct_stake(
    wallet_id: str, user_id: str, squad_id: str, stake: float,
    reference_type: str, reference_id: str, description: str,
) -> dict:
    """Atomically deduct stake from wallet. Returns updated wallet.

    Uses find_one_and_update with balance >= stake guard to prevent overdraft.
    """
    now = utcnow()
    wallet = await _db.db.wallets.find_one_and_update(
        {
            "_id": ObjectId(wallet_id),
            "user_id": user_id,
            "balance": {"$gte": stake},
            "status": {"$ne": WalletStatus.frozen.value},
        },
        {
            "$inc": {"balance": -stake, "total_wagered": stake},
            "$set": {"updated_at": now},
        },
        return_document=True,
    )
    if not wallet:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Not enough coins or wallet frozen.",
        )

    await _log_transaction(
        wallet_id=wallet_id,
        user_id=user_id,
        squad_id=squad_id,
        tx_type=TransactionType.BET_PLACED,
        amount=-stake,
        balance_after=wallet["balance"],
        reference_type=reference_type,
        reference_id=reference_id,
        description=description,
    )

    return wallet


async def credit_win(
    wallet_id: str, user_id: str, squad_id: str, amount: float,
    reference_type: str, reference_id: str, description: str,
) -> dict:
    """Credit winnings to wallet."""
    now = utcnow()
    wallet = await _db.db.wallets.find_one_and_update(
        {"_id": ObjectId(wallet_id), "user_id": user_id},
        {
            "$inc": {"balance": amount, "total_won": amount},
            "$set": {
                "status": WalletStatus.active.value,
                "bankrupt_since": None,
                "updated_at": now,
            },
        },
        return_document=True,
    )
    if not wallet:
        logger.error("Wallet not found for credit: %s", wallet_id)
        return {}

    await _log_transaction(
        wallet_id=wallet_id,
        user_id=user_id,
        squad_id=squad_id,
        tx_type=TransactionType.BET_WON,
        amount=amount,
        balance_after=wallet["balance"],
        reference_type=reference_type,
        reference_id=reference_id,
        description=description,
    )

    return wallet


async def mark_bankrupt_if_needed(wallet_id: str) -> None:
    """Check if wallet balance is <= 0 and mark as bankrupt."""
    now = utcnow()
    await _db.db.wallets.update_one(
        {
            "_id": ObjectId(wallet_id),
            "balance": {"$lte": 0},
            "status": {"$ne": WalletStatus.bankrupt.value},
        },
        {
            "$set": {
                "status": WalletStatus.bankrupt.value,
                "bankrupt_since": now,
                "consecutive_bonus_days": 0,
                "updated_at": now,
            },
        },
    )


async def apply_progressive_daily_bonus() -> int:
    """Apply progressive daily bonus to bankrupt wallets.

    - Day 1 after bankrupt: +50 coins
    - Day 2 (no bet): +50 more (=100 total)
    - Day 3 (no bet): +50 more (=150 total)
    - Resets when user places a bet

    Returns number of bonuses applied.
    """
    now = utcnow()
    one_day_ago = now - timedelta(days=1)
    bonus_amount = 50.0
    applied = 0

    # Find bankrupt wallets eligible for bonus
    # Must be bankrupt for at least 24h, and last bonus was >24h ago (or never)
    cursor = _db.db.wallets.find({
        "status": WalletStatus.bankrupt.value,
        "bankrupt_since": {"$lte": one_day_ago},
        "$or": [
            {"last_daily_bonus_at": None},
            {"last_daily_bonus_at": {"$lte": one_day_ago}},
        ],
    })

    async for wallet in cursor:
        wallet_id = str(wallet["_id"])
        user_id = wallet["user_id"]
        squad_id = wallet["squad_id"]
        days = wallet.get("consecutive_bonus_days", 0)

        # Cap at 3 consecutive bonus days
        if days >= 3:
            continue

        new_balance = wallet["balance"] + bonus_amount
        new_days = days + 1

        await _db.db.wallets.update_one(
            {"_id": wallet["_id"]},
            {
                "$inc": {"balance": bonus_amount},
                "$set": {
                    "status": WalletStatus.active.value if new_balance > 0 else WalletStatus.bankrupt.value,
                    "consecutive_bonus_days": new_days,
                    "last_daily_bonus_at": now,
                    "updated_at": now,
                },
            },
        )

        await _log_transaction(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            tx_type=TransactionType.DAILY_BONUS,
            amount=bonus_amount,
            balance_after=new_balance,
            description=f"Progressive daily bonus (day {new_days})",
        )
        applied += 1

    if applied:
        logger.info("Applied daily bonus to %d bankrupt wallets", applied)
    return applied


async def reset_bonus_counter(wallet_id: str) -> None:
    """Reset consecutive bonus days when a user places a bet."""
    await _db.db.wallets.update_one(
        {"_id": ObjectId(wallet_id)},
        {"$set": {"consecutive_bonus_days": 0}},
    )


async def get_wallet_transactions(
    wallet_id: str, limit: int = 50, skip: int = 0,
) -> list[dict]:
    """Get transaction history for a wallet."""
    return await _db.db.wallet_transactions.find(
        {"wallet_id": wallet_id},
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def _log_transaction(
    wallet_id: str, user_id: str, squad_id: str,
    tx_type: TransactionType, amount: float, balance_after: float,
    description: str, reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
) -> None:
    """Insert an immutable wallet transaction record."""
    await _db.db.wallet_transactions.insert_one({
        "wallet_id": wallet_id,
        "user_id": user_id,
        "squad_id": squad_id,
        "type": tx_type.value,
        "amount": amount,
        "balance_after": balance_after,
        "reference_type": reference_type,
        "reference_id": reference_id,
        "description": description,
        "created_at": utcnow(),
    })
