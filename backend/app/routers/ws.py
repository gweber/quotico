import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from jwt.exceptions import InvalidTokenError

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app.database as _db
from app.services.auth_service import decode_jwt
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
    teams_match,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.providers.espn import espn_provider, SPORT_TO_ESPN
from app.providers.odds_api import SUPPORTED_SPORTS

logger = logging.getLogger("quotico.ws")

router = APIRouter()

MAX_WS_CONNECTIONS = 500


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

        # Start polling if first connection
        if len(self.connections) == 1:
            self._start_polling()

        # Send current scores immediately
        if self._last_scores:
            try:
                await ws.send_json({
                    "type": "live_scores",
                    "data": list(self._last_scores.values()),
                })
            except Exception:
                pass

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info("WS client disconnected (%d remaining)", len(self.connections))

        # Stop polling if no connections
        if not self.connections and self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll providers every 30s and broadcast changes."""
        while self.connections:
            try:
                await self._fetch_and_broadcast()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WS poll error: %s", e)
            await asyncio.sleep(30)

    async def _fetch_and_broadcast(self) -> None:
        new_scores: dict[str, dict] = {}

        for sport_key in SUPPORTED_SPORTS:
            live_data: list[dict] = []

            if sport_key in SPORT_TO_LEAGUE:
                live_data = await openligadb_provider.get_live_scores(sport_key)
                if not live_data:
                    live_data = await football_data_provider.get_live_scores(sport_key)
            elif sport_key in SPORT_TO_COMPETITION:
                live_data = await football_data_provider.get_live_scores(sport_key)
            elif sport_key in SPORT_TO_ESPN:
                live_data = await espn_provider.get_live_scores(sport_key)

            for score in live_data:
                matched = await _match_to_db(sport_key, score)
                if matched:
                    new_scores[matched["match_id"]] = matched

        # Detect changes
        changed = new_scores != self._last_scores
        self._last_scores = new_scores

        if changed and self.connections:
            message = {
                "type": "live_scores",
                "data": list(new_scores.values()),
            }
            dead: list[WebSocket] = []
            for ws in self.connections:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(ws)

    async def broadcast_match_resolved(self, match_id: str, result: str) -> None:
        """Notify clients when a match is resolved."""
        if not self.connections:
            return
        message = {
            "type": "match_resolved",
            "data": {"match_id": match_id, "result": result},
        }
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def _match_to_db(sport_key: str, score: dict) -> Optional[dict]:
    """Match a live score to a DB match."""
    utc_date = score.get("utc_date", "")
    if isinstance(utc_date, str) and utc_date:
        try:
            match_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "commence_time": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=50)

    for candidate in candidates:
        home = candidate.get("teams", {}).get("home", "")
        if teams_match(home, score.get("home_team", "")):
            return {
                "match_id": str(candidate["_id"]),
                "home_score": score["home_score"],
                "away_score": score["away_score"],
                "minute": score.get("minute"),
                "sport_key": sport_key,
            }

    return None


# Singleton manager
live_manager = LiveScoreManager()


@router.websocket("/ws/live-scores")
async def websocket_live_scores(ws: WebSocket):
    # Authenticate via access_token cookie
    token = ws.cookies.get("access_token")
    if not token:
        await ws.close(code=4001, reason="Unauthorized")
        return

    try:
        payload = decode_jwt(token)
        if payload.get("type") != "access":
            await ws.close(code=4001, reason="Invalid token type")
            return
    except InvalidTokenError:
        await ws.close(code=4001, reason="Invalid token")
        return

    # Connection cap
    if live_manager.is_full:
        await ws.close(code=4002, reason="Too many connections")
        return

    await live_manager.connect(ws)
    try:
        while True:
            # Keep connection alive, handle client messages
            data = await ws.receive_text()
            # Client can send "ping" to keep alive
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        live_manager.disconnect(ws)
    except Exception:
        live_manager.disconnect(ws)
