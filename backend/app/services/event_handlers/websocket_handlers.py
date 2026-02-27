"""
backend/app/services/event_handlers/websocket_handlers.py

Purpose:
    qbus subscribers that fan out selected match/odds events to authenticated
    WebSocket clients through the realtime WebSocket manager.

Dependencies:
    - app.database
    - app.services.event_models
    - app.services.websocket_manager
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.services.event_models import BaseEvent
from app.services.websocket_manager import websocket_manager
from app.utils import ensure_utc

logger = logging.getLogger("quotico.event_handlers.websocket")


async def handle_match_updated_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    try:
        fixture_id = int(getattr(event, "match_id", ""))
    except (TypeError, ValueError):
        return
    import app.database as _db

    match_doc = await _db.db.matches_v3.find_one({"_id": int(fixture_id)}, {"_id": 1, "league_id": 1, "status": 1, "scores": 1, "start_at": 1, "teams": 1, "updated_at": 1, "updated_at_utc": 1})
    if not match_doc:
        return

    raw_league_id = match_doc.get("league_id")
    league_id = raw_league_id if isinstance(raw_league_id, int) else None
    if raw_league_id is not None and not isinstance(raw_league_id, int):
        logger.warning("Legacy websocket match payload has non-int league_id=%r", raw_league_id)
    teams = match_doc.get("teams") if isinstance(match_doc.get("teams"), dict) else {}
    home_team = ((teams or {}).get("home") or {}).get("name") if isinstance((teams or {}).get("home"), dict) else ""
    away_team = ((teams or {}).get("away") or {}).get("name") if isinstance((teams or {}).get("away"), dict) else ""

    await websocket_manager.broadcast(
        event_type="match.updated",
        data={
            "match_id": str(int(match_doc["_id"])),
            "league_id": league_id,
            "league_codes": [],
            "status": str(match_doc.get("status") or ""),
            "score": (match_doc.get("scores") or {}).get("full_time") if isinstance(match_doc.get("scores"), dict) else {},
            "home_team": str(home_team or ""),
            "away_team": str(away_team or ""),
            "match_date": ensure_utc(match_doc.get("start_at")).isoformat() if match_doc.get("start_at") else None,
            "updated_at": ensure_utc(match_doc.get("updated_at_utc") or match_doc.get("updated_at")).isoformat() if (match_doc.get("updated_at_utc") or match_doc.get("updated_at")) else None,
            "changed_fields": list(getattr(event, "changed_fields", []) or []),
        },
        selectors={
            "match_ids": [str(int(match_doc["_id"]))],
            "league_ids": [league_id] if league_id is not None else [],
            "league_codes": [],
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )


async def handle_match_finalized_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    try:
        fixture_id = int(getattr(event, "match_id", ""))
    except (TypeError, ValueError):
        return

    import app.database as _db

    match_doc = await _db.db.matches_v3.find_one({"_id": int(fixture_id)}, {"_id": 1, "league_id": 1, "status": 1, "scores": 1, "updated_at": 1, "updated_at_utc": 1})
    if not match_doc:
        return
    raw_league_id = match_doc.get("league_id")
    league_id = raw_league_id if isinstance(raw_league_id, int) else None
    if raw_league_id is not None and not isinstance(raw_league_id, int):
        logger.warning("Legacy websocket match payload has non-int league_id=%r", raw_league_id)

    await websocket_manager.broadcast(
        event_type="match.finalized",
        data={
            "match_id": str(int(match_doc["_id"])),
            "league_id": league_id,
            "league_codes": [],
            "status": str(match_doc.get("status") or "FINISHED"),
            "score": (match_doc.get("scores") or {}).get("full_time") if isinstance(match_doc.get("scores"), dict) else {},
            "result": {"outcome": None},
            "final_score": dict(getattr(event, "final_score", {}) or {}),
            "updated_at": ensure_utc(match_doc.get("updated_at_utc") or match_doc.get("updated_at")).isoformat() if (match_doc.get("updated_at_utc") or match_doc.get("updated_at")) else None,
        },
        selectors={
            "match_ids": [str(int(match_doc["_id"]))],
            "league_ids": [league_id] if league_id is not None else [],
            "league_codes": [],
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )


async def handle_odds_ingested_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    match_ids = sorted({str(item).strip() for item in (getattr(event, "match_ids", []) or []) if str(item).strip()})
    if not match_ids:
        return
    event_league_id = getattr(event, "league_id", None)
    league_id: int | None = event_league_id if isinstance(event_league_id, int) else None
    if event_league_id is not None and not isinstance(event_league_id, int):
        logger.warning("Legacy websocket odds payload has non-int league_id=%r", event_league_id)

    await websocket_manager.broadcast(
        event_type="odds.ingested",
        data={
            "provider": str(getattr(event, "provider", "") or ""),
            "league_id": league_id,
            "inserted": int(getattr(event, "inserted", 0) or 0),
            "deduplicated": int(getattr(event, "deduplicated", 0) or 0),
            "markets_updated": int(getattr(event, "markets_updated", 0) or 0),
            "match_ids": match_ids,
        },
        selectors={
            "match_ids": match_ids,
            "league_ids": [league_id] if league_id is not None else [],
            "league_codes": [],
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )
    # Legacy live-scores socket still powers startpage odds refresh triggers.
    try:
        from app.routers.ws import live_manager

        await live_manager.broadcast_odds_updated(
            league_id=league_id,
            odds_changed=len(match_ids),
            match_ids=match_ids,
        )
    except Exception:
        logger.warning("Failed to broadcast legacy odds_updated websocket event", exc_info=True)
