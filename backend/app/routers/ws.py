"""
backend/app/routers/ws.py

Purpose:
    WebSocket router exposing legacy live-score streaming and the new
    authenticated qbus realtime stream endpoint.

Dependencies:
    - app.services.auth_service
    - app.services.websocket_manager
    - app.services.match_service
    - app.providers.football_data
    - app.providers.openligadb
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
from app.utils import parse_utc
from app.services.auth_service import decode_jwt
from app.services.match_service import sports_with_live_action, next_kickoff_in
from app.services.websocket_manager import websocket_manager
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.utils.team_matching import teams_match

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

    async def _fetch_and_broadcast(self, live_sports: set[str]) -> None:
        new_scores: dict[str, dict] = {}
        for sport_key in live_sports:
            live_data: list[dict] = []
            try:
                if sport_key in SPORT_TO_LEAGUE:
                    live_data = await openligadb_provider.get_live_scores(sport_key)
                    if not live_data:
                        live_data = await football_data_provider.get_live_scores(sport_key)
                elif sport_key in SPORT_TO_COMPETITION:
                    live_data = await football_data_provider.get_live_scores(sport_key)
            except Exception:
                logger.warning("WS live score provider failed for %s", sport_key, exc_info=True)
                continue

            for score in live_data:
                matched = await _match_to_db(sport_key, score)
                if matched:
                    new_scores[matched["match_id"]] = matched

        changed = new_scores != self._last_scores
        self._last_scores = new_scores
        if changed and self.connections:
            message = {"type": "live_scores", "data": list(new_scores.values())}
            dead: list[WebSocket] = []
            for ws in self.connections:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(ws)

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

    async def broadcast_odds_updated(self, sport_key: str, odds_changed: int) -> None:
        if not self.connections:
            return
        message = {"type": "odds_updated", "data": {"sport_key": sport_key, "odds_changed": odds_changed}}
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def _match_to_db(sport_key: str, score: dict) -> Optional[dict]:
    utc_date = score.get("utc_date", "")
    if not isinstance(utc_date, str) or not utc_date:
        return None
    try:
        match_time = parse_utc(utc_date)
    except ValueError:
        return None

    candidates = await _db.db.matches.find(
        {
            "sport_key": sport_key,
            "match_date": {
                "$gte": match_time - timedelta(hours=6),
                "$lte": match_time + timedelta(hours=6),
            },
        }
    ).to_list(length=50)
    for candidate in candidates:
        home = candidate.get("home_team", "")
        if teams_match(home, score.get("home_team", "")):
            return {
                "match_id": str(candidate["_id"]),
                "home_score": score["home_score"],
                "away_score": score["away_score"],
                "minute": score.get("minute"),
                "sport_key": sport_key,
            }
    return None


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

