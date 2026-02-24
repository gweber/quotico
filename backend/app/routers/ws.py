import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from jwt.exceptions import InvalidTokenError

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app.database as _db
from app.utils import parse_utc
from app.services.auth_service import decode_jwt
from app.services.match_service import sports_with_live_action, next_kickoff_in
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
    teams_match,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.providers.espn import espn_provider, SPORT_TO_ESPN

logger = logging.getLogger("quotico.ws")

router = APIRouter()

MAX_WS_CONNECTIONS = 500

# Adaptive polling intervals (seconds)
_INTERVAL_LIVE = 30         # matches in progress → 30s
_INTERVAL_PRE_GAME = 120    # kickoff within 30 min → 2 min
_INTERVAL_DORMANT = 900     # nothing for next 6h → 15 min heartbeat
_INTERVAL_DEAD_ZONE = None  # nothing today → stop polling (wake on next connect)


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
        """Adaptive poll loop: adjusts interval based on match schedule.

        Intervals:
        - Active Play (matches live):     30s, poll only active sports
        - Pre-Game (kickoff within 30m):  2 min, DB check only
        - Dormant (nothing for 6h):       15 min heartbeat
        - Dead Zone (nothing today):      stop polling entirely
        """
        while self.connections:
            try:
                live_sports = await sports_with_live_action()

                if live_sports:
                    # Active Play — poll only sports with live matches
                    await self._fetch_and_broadcast(live_sports)
                    interval = _INTERVAL_LIVE
                else:
                    # Nothing live — clear stale scores
                    if self._last_scores:
                        self._last_scores = {}
                        await self._broadcast_empty()

                    # Determine wake-up schedule
                    upcoming = await next_kickoff_in()
                    if upcoming and upcoming < timedelta(minutes=30):
                        interval = _INTERVAL_PRE_GAME
                        logger.debug("WS pre-game: kickoff in %s, checking every %ds", upcoming, interval)
                    elif upcoming and upcoming < timedelta(hours=6):
                        interval = _INTERVAL_DORMANT
                        logger.debug("WS dormant: next kickoff in %s, heartbeat every %ds", upcoming, interval)
                    else:
                        # Dead Zone — nothing today, stop polling
                        logger.info("WS dead zone: no matches upcoming, stopping poll loop")
                        self._poll_task = None
                        return  # exits the loop; _start_polling re-creates on next connect

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WS poll error: %s", e)
                interval = _INTERVAL_LIVE  # on error, keep trying at normal rate

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
                elif sport_key in SPORT_TO_ESPN:
                    live_data = await espn_provider.get_live_scores(sport_key)
            except Exception:
                logger.warning("WS live score provider failed for %s", sport_key, exc_info=True)
                continue

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

    async def _broadcast_empty(self) -> None:
        """Broadcast empty scores when all matches have ended."""
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

    async def broadcast_odds_updated(self, sport_key: str, odds_changed: int) -> None:
        """Notify clients that odds have been refreshed so they can re-fetch."""
        if not self.connections:
            return
        message = {
            "type": "odds_updated",
            "data": {"sport_key": sport_key, "odds_changed": odds_changed},
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
            match_time = parse_utc(utc_date)
        except ValueError:
            return None
    else:
        return None

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "match_date": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=50)

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


# Singleton manager
live_manager = LiveScoreManager()


@router.websocket("/ws/live-scores")
async def websocket_live_scores(ws: WebSocket):
    # Optional auth — live scores are public, but identify user if token present
    token = ws.cookies.get("access_token")
    if token:
        try:
            decode_jwt(token)
        except InvalidTokenError:
            pass  # proceed as guest

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
