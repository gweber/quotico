"""Wallet maintenance — progressive daily bonus for bankrupt wallets."""

import logging

from app.services.wallet_service import apply_progressive_daily_bonus
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.wallet_maintenance")


async def run_wallet_maintenance() -> None:
    """Daily maintenance: apply progressive bonus to bankrupt wallets.

    Runs once per day (smart sleep with 20h window).
    """
    from datetime import timedelta

    state_key = "wallet_maintenance"
    if await recently_synced(state_key, timedelta(hours=20)):
        logger.debug("Smart sleep: wallet maintenance ran recently")
        return

    bonus_count = await apply_progressive_daily_bonus()

    if bonus_count:
        logger.info("Wallet maintenance complete: %d bonuses applied", bonus_count)
    else:
        logger.debug("Wallet maintenance complete: no bonuses needed")

    await set_synced(state_key)
