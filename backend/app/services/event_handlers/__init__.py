"""
backend/app/services/event_handlers/__init__.py

Purpose:
    Central registration entrypoint for event bus subscribers.

Dependencies:
    - app.services.event_bus
    - app.services.event_handlers.match_handlers
    - app.services.event_handlers.matchday_handlers
    - app.services.event_handlers.odds_handlers
"""

from __future__ import annotations

from app.config import settings
from app.services.event_bus import InMemoryEventBus
from app.services.event_handlers.match_handlers import handle_match_finalized
from app.services.event_handlers.matchday_handlers import handle_match_updated
from app.services.event_handlers.odds_handlers import handle_odds_ingested
from app.services.event_handlers.websocket_handlers import (
    handle_match_finalized_ws,
    handle_match_updated_ws,
    handle_odds_ingested_ws,
)


def register_event_handlers(bus: InMemoryEventBus) -> None:
    if settings.EVENT_HANDLER_MATCH_FINALIZED_ENABLED:
        bus.subscribe("match.finalized", handle_match_finalized, handler_name="match_finalized", concurrency=1)
    if settings.EVENT_HANDLER_MATCH_UPDATED_ENABLED:
        bus.subscribe("match.updated", handle_match_updated, handler_name="match_updated", concurrency=1)
    if settings.EVENT_HANDLER_ODDS_INGESTED_ENABLED:
        bus.subscribe("odds.ingested", handle_odds_ingested, handler_name="odds_ingested", concurrency=1)
    if settings.EVENT_HANDLER_WS_BROADCAST_ENABLED:
        bus.subscribe("match.updated", handle_match_updated_ws, handler_name="ws_match_updated", concurrency=1)
        bus.subscribe("match.finalized", handle_match_finalized_ws, handler_name="ws_match_finalized", concurrency=1)
        bus.subscribe("odds.ingested", handle_odds_ingested_ws, handler_name="ws_odds_ingested", concurrency=1)
