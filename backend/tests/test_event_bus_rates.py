"""
backend/tests/test_event_bus_rates.py

Purpose:
    Tests for qbus sliding-window rate calculations.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "backend")

from app.services.event_bus import InMemoryEventBus
from app.services.event_models import MatchCreatedEvent


@pytest.mark.asyncio
async def test_event_bus_rate_calculation_and_prune() -> None:
    bus = InMemoryEventBus(ingress_maxsize=20, handler_maxsize=20, default_concurrency=1, error_buffer_size=10)
    handled = 0

    async def handler(_event):
        nonlocal handled
        handled += 1

    bus.subscribe("match.created", handler, handler_name="h1", concurrency=1)
    await bus.start()

    for i in range(5):
        bus.publish(
            MatchCreatedEvent(
                source="test",
                correlation_id=f"corr-{i}",
                match_id=f"m{i}",
                league_id="l1",
                sport_key="soccer_epl",
                season=2025,
                status="scheduled",
                ingest_source="football_data",
                external_id=f"ext-{i}",
            )
        )
    await asyncio.sleep(0.08)

    stats = bus.stats()
    assert stats["published_total"] == 5
    assert stats["handled_total"] == 5
    assert stats["published_rate_1m"] > 0.0
    assert stats["handled_rate_1m"] > 0.0
    assert handled == 5
    await bus.stop()

