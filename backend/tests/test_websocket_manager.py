"""
backend/tests/test_websocket_manager.py

Purpose:
    Unit tests for websocket manager connection lifecycle, filter matching, and
    heartbeat cleanup.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "backend")

from app.services.websocket_manager import WebSocketManager


class _FakeWebSocket:
    def __init__(self, *, fail_send: bool = False):
        self.accepted = False
        self.fail_send = fail_send
        self.messages: list[dict] = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_connect_subscribe_and_filtered_broadcast():
    manager = WebSocketManager(max_connections=10, heartbeat_seconds=30)
    ws = _FakeWebSocket()
    conn_id = await manager.connect(ws, user_id="u1")
    filters = await manager.update_filters(
        conn_id,
        "subscribe",
        {"match_ids": ["m1"], "event_types": ["odds.ingested"]},
    )
    assert filters["match_ids"] == ["m1"]
    assert filters["event_types"] == ["odds.ingested"]

    sent = await manager.broadcast(
        event_type="odds.ingested",
        data={"ok": True},
        selectors={"match_ids": ["m1"]},
        meta={"event_id": "e1"},
    )
    assert sent == 1
    assert ws.messages[-1]["type"] == "odds.ingested"

    sent_no_match = await manager.broadcast(
        event_type="match.updated",
        data={"ok": True},
        selectors={"match_ids": ["m1"]},
        meta={"event_id": "e2"},
    )
    assert sent_no_match == 0


@pytest.mark.asyncio
async def test_heartbeat_removes_dead_connections():
    manager = WebSocketManager(max_connections=10, heartbeat_seconds=1)
    ws_ok = _FakeWebSocket()
    ws_fail = _FakeWebSocket(fail_send=True)
    await manager.connect(ws_ok, user_id="u-ok")
    await manager.connect(ws_fail, user_id="u-fail")
    await manager.start()
    await asyncio.sleep(2.2)
    await manager.stop()
    stats = manager.stats()
    assert stats["dropped_connections"] >= 1
