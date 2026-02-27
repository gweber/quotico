"""
backend/app/services/websocket_manager.py

Purpose:
    Process-local WebSocket connection manager for qbus-driven realtime events.
    Manages authenticated client connections, subscription filters, heartbeat,
    and filtered broadcast delivery.

Dependencies:
    - fastapi.WebSocket
    - app.config
    - app.utils
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import WebSocket

from app.config import settings
from app.utils import utcnow

logger = logging.getLogger("quotico.websocket_manager")

_FILTER_KEYS = ("match_ids", "league_ids", "league_codes", "event_types")
_EVENT_FILTER_KEYS = ("match_ids", "league_ids", "league_codes")


def _normalize_str_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    out: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        if value:
            out.add(value)
    return out


def _normalize_league_id_set(values: Any) -> set[int]:
    """Normalize league-id filters as int-only tokens."""
    if not isinstance(values, list):
        return set()
    out: set[int] = set()
    for item in values:
        if isinstance(item, bool):
            logger.warning("Legacy websocket payload rejected bool league_id=%s", item)
            continue
        if isinstance(item, int):
            out.add(item)
            continue
        if isinstance(item, str):
            logger.warning("Legacy websocket payload uses string league_id='%s'; expected int", item)
            continue
        logger.warning("Legacy websocket payload rejected unsupported league_id type=%s", type(item).__name__)
    return out


@dataclass
class ManagedConnection:
    connection_id: str
    user_id: str
    websocket: WebSocket
    filters: dict[str, set[Any]]
    connected_at: datetime
    last_seen_at: datetime


class WebSocketManager:
    def __init__(
        self,
        *,
        max_connections: int,
        heartbeat_seconds: int,
    ) -> None:
        self._max_connections = max(1, int(max_connections))
        self._heartbeat_seconds = max(1, int(heartbeat_seconds))
        self._connections: dict[str, ManagedConnection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._broadcast_total = 0
        self._send_failures = 0
        self._dropped_connections = 0
        self._last_errors: list[dict[str, Any]] = []

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ws_heartbeat")
            logger.info("WebSocket manager started")

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return
            self._running = False
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None
            self._connections.clear()
            logger.info("WebSocket manager stopped")

    async def connect(self, websocket: WebSocket, *, user_id: str, initial_filters: dict[str, Any] | None = None) -> str:
        await websocket.accept()
        async with self._lock:
            if len(self._connections) >= self._max_connections:
                raise RuntimeError("max_connections_exceeded")
            connection_id = str(uuid.uuid4())
            now = utcnow()
            self._connections[connection_id] = ManagedConnection(
                connection_id=connection_id,
                user_id=str(user_id),
                websocket=websocket,
                filters=self._filters_from_payload(initial_filters or {}),
                connected_at=now,
                last_seen_at=now,
            )
            return connection_id

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            self._connections.pop(connection_id, None)

    async def touch(self, connection_id: str) -> None:
        async with self._lock:
            conn = self._connections.get(connection_id)
            if conn:
                conn.last_seen_at = utcnow()

    async def update_filters(self, connection_id: str, command_type: str, payload: dict[str, Any]) -> dict[str, list[Any]]:
        async with self._lock:
            conn = self._connections.get(connection_id)
            if not conn:
                raise RuntimeError("connection_not_found")

            incoming = self._filters_from_payload(payload)
            if command_type == "replace_subscriptions":
                conn.filters = incoming
            elif command_type == "subscribe":
                for key in _FILTER_KEYS:
                    conn.filters[key].update(incoming[key])
            elif command_type == "unsubscribe":
                for key in _FILTER_KEYS:
                    conn.filters[key].difference_update(incoming[key])
            else:
                raise ValueError("unsupported_command")

            conn.last_seen_at = utcnow()
            return self._filters_to_jsonable(conn.filters)

    async def broadcast(
        self,
        *,
        event_type: str,
        data: dict[str, Any],
        selectors: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        selectors = selectors or {}
        if selectors.get("sport_keys"):
            logger.warning("Legacy websocket selectors use deprecated 'sport_keys'")
        normalized_selectors: dict[str, set[Any]] = {}
        for key in _EVENT_FILTER_KEYS:
            if key == "league_ids":
                normalized_selectors[key] = _normalize_league_id_set(selectors.get(key, []))
            else:
                normalized_selectors[key] = _normalize_str_set(selectors.get(key, []))
        message = {
            "type": str(event_type),
            "data": data,
            "meta": meta or {},
        }

        async with self._lock:
            connections = list(self._connections.values())

        delivered = 0
        dead_ids: list[str] = []
        for conn in connections:
            if not self._matches(conn.filters, event_type=str(event_type), selectors=normalized_selectors):
                continue
            try:
                await conn.websocket.send_json(message)
                delivered += 1
            except Exception as exc:
                dead_ids.append(conn.connection_id)
                self._send_failures += 1
                self._append_error(
                    {
                        "ts": utcnow().isoformat(),
                        "connection_id": conn.connection_id,
                        "event_type": str(event_type),
                        "error": str(exc),
                    }
                )

        for conn_id in dead_ids:
            await self.disconnect(conn_id)
            self._dropped_connections += 1

        self._broadcast_total += 1
        return delivered

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "active_connections": len(self._connections),
            "max_connections": self._max_connections,
            "heartbeat_seconds": self._heartbeat_seconds,
            "broadcast_total": self._broadcast_total,
            "send_failures": self._send_failures,
            "dropped_connections": self._dropped_connections,
            "last_errors": list(self._last_errors),
        }

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._lock:
                connections = list(self._connections.values())
            dead_ids: list[str] = []
            for conn in connections:
                try:
                    await conn.websocket.send_json({"type": "ping", "data": {"ts": utcnow().isoformat()}})
                except Exception:
                    dead_ids.append(conn.connection_id)
            for conn_id in dead_ids:
                await self.disconnect(conn_id)
                self._dropped_connections += 1

    @staticmethod
    def _filters_from_payload(payload: dict[str, Any]) -> dict[str, set[Any]]:
        out = {key: set() for key in _FILTER_KEYS}
        if not isinstance(payload, dict):
            return out
        for key in _FILTER_KEYS:
            if key == "league_ids":
                out[key] = _normalize_league_id_set(payload.get(key, []))
            else:
                out[key] = _normalize_str_set(payload.get(key, []))
        if payload.get("sport_keys"):
            logger.warning("Legacy websocket payload uses deprecated 'sport_keys' filter")
        return out

    @staticmethod
    def _filters_to_jsonable(filters: dict[str, set[Any]]) -> dict[str, list[Any]]:
        return {key: sorted(filters.get(key, set())) for key in _FILTER_KEYS}

    @staticmethod
    def _matches(filters: dict[str, set[Any]], *, event_type: str, selectors: dict[str, set[Any]]) -> bool:
        event_types = filters.get("event_types", set())
        if event_types and event_type not in event_types:
            return False

        has_dimension_filters = any(bool(filters.get(key, set())) for key in _EVENT_FILTER_KEYS)
        if not has_dimension_filters:
            return True

        for key in _EVENT_FILTER_KEYS:
            conn_values = filters.get(key, set())
            if not conn_values:
                continue
            event_values = selectors.get(key, set())
            if conn_values.intersection(event_values):
                return True
        return False

    def _append_error(self, error: dict[str, Any]) -> None:
        self._last_errors.append(error)
        if len(self._last_errors) > 200:
            self._last_errors = self._last_errors[-200:]


websocket_manager = WebSocketManager(
    max_connections=settings.WS_MAX_CONNECTIONS,
    heartbeat_seconds=settings.WS_HEARTBEAT_SECONDS,
)
