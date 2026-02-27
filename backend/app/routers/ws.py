"""
backend/app/routers/ws.py

Purpose:
    WebSocket router exposing legacy live-score streaming and the new
    authenticated qbus realtime stream endpoint.

Dependencies:
    - app.services.auth_service
    - app.services.websocket_manager
    - app.services.match_service
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Optional

from bson import ObjectId
from jwt.exceptions import InvalidTokenError

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app.database as _db
from app.config import settings
from app.services.auth_service import decode_jwt
from app.services.match_service import sports_with_live_action, next_kickoff_in
from app.services.websocket_manager import websocket_manager

logger = logging.getLogger("quotico.ws")

router = APIRouter()

MAX_WS_CONNECTIONS = 500

# Adaptive polling intervals (seconds)
_INTERVAL_LIVE = 30         # matches in progress -> 30s
_INTERVAL_PRE_GAME = 120    # kickoff within 30 min -> 2 min
_INTERVAL_DORMANT = 900     # nothing for next 6h -> 15 min heartbeat


class LiveScoreManager:
    """Manages WebSocket connections and broadcasts live score updates."""

    def __init__(self):
        self.connections: list[WebSocket] = []
        self._last_scores: dict[str, dict] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._odds_buffer_match_ids: set[str] = set()
        self._odds_buffer_league_id: int | None = None
        self._odds_buffer_count: int = 0
        self._odds_flush_task: Optional[asyncio.Task] = None

    @property
    def is_full(self) -> bool:
        return len(self.connections) >= MAX_WS_CONNECTIONS

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)
        logger.info("WS client connected (%d total)", len(self.connections))

        if len(self.connections) == 1:
            self._start_polling()

        if self._last_scores:
            try:
                await ws.send_json(
                    {
                        "type": "live_scores",
                        "data": list(self._last_scores.values()),
                    }
                )
            except Exception:
                pass

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info("WS client disconnected (%d remaining)", len(self.connections))
        if not self.connections and self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while self.connections:
            try:
                live_sports = await sports_with_live_action()
                if live_sports:
                    await self._fetch_and_broadcast(live_sports)
                    interval = _INTERVAL_LIVE
                else:
                    if self._last_scores:
                        self._last_scores = {}
                        await self._broadcast_empty()
                    upcoming = await next_kickoff_in()
                    if upcoming and upcoming < timedelta(minutes=30):
                        interval = _INTERVAL_PRE_GAME
                    elif upcoming and upcoming < timedelta(hours=6):
                        interval = _INTERVAL_DORMANT
                    else:
                        self._poll_task = None
                        return
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("WS poll error: %s", exc)
                interval = _INTERVAL_LIVE
            await asyncio.sleep(interval)

    async def _fetch_and_broadcast(self, live_sports: set[int]) -> None:
        # Legacy providers removed — live score polling disabled pending Sportmonks integration
        pass

    async def _broadcast_empty(self) -> None:
        if not self.connections:
            return
        message = {"type": "live_scores", "data": []}
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_match_resolved(self, match_id: str, result: str) -> None:
        if not self.connections:
            return
        message = {"type": "match_resolved", "data": {"match_id": match_id, "result": result}}
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def _flush_odds_updates(self) -> None:
        await asyncio.sleep(0.35)
        if not self.connections:
            self._odds_buffer_match_ids.clear()
            self._odds_buffer_league_id = None
            self._odds_buffer_count = 0
            self._odds_flush_task = None
            return
        match_ids = sorted(self._odds_buffer_match_ids)
        message = {
            "type": "odds_updated",
            "data": {
                "league_id": self._odds_buffer_league_id,
                "odds_changed": self._odds_buffer_count if self._odds_buffer_count > 0 else len(match_ids),
                "match_ids": match_ids,
            },
        }
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
        self._odds_buffer_match_ids.clear()
        self._odds_buffer_league_id = None
        self._odds_buffer_count = 0
        self._odds_flush_task = None

    async def broadcast_odds_updated(self, league_id: int | None, odds_changed: int, match_ids: list[str] | None = None) -> None:
        if not self.connections:
            return
        if league_id is not None:
            self._odds_buffer_league_id = int(league_id)
        self._odds_buffer_count += max(0, int(odds_changed))
        if match_ids:
            self._odds_buffer_match_ids.update(str(mid) for mid in match_ids if str(mid).strip())
        if self._odds_flush_task is None or self._odds_flush_task.done():
            self._odds_flush_task = asyncio.create_task(self._flush_odds_updates())


def _token_from_ws(websocket: WebSocket) -> str | None:
    cookie_token = websocket.cookies.get("access_token")
    if cookie_token:
        return str(cookie_token)
    query_token = websocket.query_params.get("token")
    if query_token:
        return str(query_token)
    return None


async def _resolve_ws_user(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        payload = decode_jwt(token)
    except InvalidTokenError:
        return None
    if payload.get("type") != "access":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        oid = ObjectId(str(user_id))
    except Exception:
        return None
    jti = payload.get("jti")
    if jti:
        blocked = await _db.db.access_blocklist.find_one({"jti": jti}, {"_id": 1})
        if blocked:
            return None
    user = await _db.db.users.find_one({"_id": oid, "is_deleted": False}, {"_id": 1, "is_banned": 1})
    if not user or user.get("is_banned"):
        return None
    return user


live_manager = LiveScoreManager()


@router.websocket("/ws/live-scores")
async def websocket_live_scores(ws: WebSocket):
    token = ws.cookies.get("access_token")
    if token:
        try:
            decode_jwt(token)
        except InvalidTokenError:
            pass

    if live_manager.is_full:
        await ws.close(code=4002, reason="Too many connections")
        return

    await live_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        live_manager.disconnect(ws)
    except Exception:
        live_manager.disconnect(ws)


@router.websocket("/ws")
async def websocket_event_stream(ws: WebSocket):
    """
    Authenticated realtime event stream for qbus events.

    Auth:
        - Cookie `access_token` (preferred)
        - Query parameter `token` (fallback)
    """
    if not settings.WS_EVENTS_ENABLED:
        await ws.close(code=4403, reason="WS events disabled")
        return
    token = _token_from_ws(ws)
    user = await _resolve_ws_user(token)
    if not user:
        await ws.close(code=4401, reason="Unauthorized")
        return

    try:
        connection_id = await websocket_manager.connect(ws, user_id=str(user["_id"]))
    except RuntimeError:
        await ws.close(code=4002, reason="Too many connections")
        return

    try:
        await ws.send_json({"type": "connected", "data": {"connection_id": connection_id}})
        while True:
            raw = await ws.receive_text()
            await websocket_manager.touch(connection_id)
            if raw == "ping":
                await ws.send_json({"type": "pong"})
                continue

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid_json"})
                continue
            command_type = str(message.get("type") or "").strip()
            if command_type == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if command_type not in {"subscribe", "unsubscribe", "replace_subscriptions"}:
                await ws.send_json({"type": "error", "error": "unsupported_command"})
                continue

            try:
                filters = await websocket_manager.update_filters(connection_id, command_type, message)
                await ws.send_json({"type": "subscribed", "data": {"filters": filters}})
            except Exception:
                await ws.send_json({"type": "error", "error": "invalid_subscription_payload"})
    except WebSocketDisconnect:
        await websocket_manager.disconnect(connection_id)
    except Exception:
        await websocket_manager.disconnect(connection_id)
