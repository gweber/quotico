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


async def handle_odds_raw_ingested(event: BaseEvent) -> None:
    fixture_ids = list(getattr(event, "fixture_ids", []) or [])
    cached = int(getattr(event, "cached_count", 0) or 0)
    source_method = str(getattr(event, "source_method", "") or "")
    logger.info(
        "odds.raw_ingested fixtures=%d cached=%d method=%s",
        len(fixture_ids),
        cached,
        source_method,
    )

