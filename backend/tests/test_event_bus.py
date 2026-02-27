"""
backend/tests/test_event_bus.py

Purpose:
    Unit tests for the in-memory event bus implementation.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "backend")

from app.services.event_bus import InMemoryEventBus
from app.services.event_models import MatchCreatedEvent


@pytest.mark.asyncio
async def test_event_bus_fanout_and_correlation() -> None:
    bus = InMemoryEventBus(ingress_maxsize=10, handler_maxsize=10, default_concurrency=1, error_buffer_size=10)
    seen: list[tuple[str, str]] = []

    async def handler_a(event):
        seen.append(("a", event.correlation_id))

    async def handler_b(event):
        seen.append(("b", event.correlation_id))

    bus.subscribe("match.created", handler_a, handler_name="a", concurrency=1)
    bus.subscribe("match.created", handler_b, handler_name="b", concurrency=1)
    await bus.start()

    bus.publish(
        MatchCreatedEvent(
            source="test",
            correlation_id="corr-1",
            match_id="m1",
            league_id=8,
            season=2025,
            status="scheduled",
            ingest_source="football_data",
            external_id="ext-1",
        )
    )
    await asyncio.sleep(0.05)
    await bus.stop()

    assert ("a", "corr-1") in seen
    assert ("b", "corr-1") in seen
    stats = bus.stats()
    assert stats["published_total"] == 1
    assert stats["failed_total"] == 0
    assert stats["per_event_type"]["match.created"]["published_total"] == 1
    assert stats["per_source"]["test"]["published_total"] == 1


@pytest.mark.asyncio
async def test_event_bus_handler_failure_isolated() -> None:
    bus = InMemoryEventBus(ingress_maxsize=10, handler_maxsize=10, default_concurrency=1, error_buffer_size=10)
    success_calls = 0

    async def failing(_event):
        raise RuntimeError("boom")

    async def success(_event):
        nonlocal success_calls
        success_calls += 1

    bus.subscribe("match.created", failing, handler_name="failing", concurrency=1)
    bus.subscribe("match.created", success, handler_name="success", concurrency=1)
    await bus.start()
    bus.publish(
        MatchCreatedEvent(
            source="test",
            correlation_id="corr-2",
            match_id="m2",
            league_id=8,
            season=2025,
            status="scheduled",
            ingest_source="football_data",
            external_id="ext-2",
        )
    )
    await asyncio.sleep(0.05)
    await bus.stop()

    stats = bus.stats()
    assert success_calls == 1
    assert stats["failed_total"] >= 1
    assert len(stats["recent_errors"]) >= 1


@pytest.mark.asyncio
async def test_event_bus_overflow_drops() -> None:
    bus = InMemoryEventBus(ingress_maxsize=1, handler_maxsize=1, default_concurrency=1, error_buffer_size=10)
    event = MatchCreatedEvent(
        source="test",
        correlation_id="corr-3",
        match_id="m3",
        league_id=8,
        season=2025,
        status="scheduled",
        ingest_source="football_data",
        external_id="ext-3",
    )
    bus.publish(event)
    bus.publish(event.model_copy(update={"event_id": "evt-2"}))

    stats = bus.stats()
    assert stats["published_total"] >= 1
    assert stats["dropped_total"] >= 1
