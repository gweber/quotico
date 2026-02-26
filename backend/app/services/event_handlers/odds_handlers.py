"""
backend/app/services/event_handlers/odds_handlers.py

Purpose:
    Odds event subscribers. V1 keeps this lightweight and focuses on observability.

Dependencies:
    - app.services.event_models
"""

from __future__ import annotations

import logging

from app.services.event_models import BaseEvent

logger = logging.getLogger("quotico.event_handlers.odds")


async def handle_odds_ingested(event: BaseEvent) -> None:
    provider = str(getattr(event, "provider", "") or "")
    match_ids = list(getattr(event, "match_ids", []) or [])
    markets_updated = int(getattr(event, "markets_updated", 0) or 0)
    logger.info(
        "Processed odds.ingested event provider=%s matches=%d markets_updated=%d",
        provider,
        len(match_ids),
        markets_updated,
    )

