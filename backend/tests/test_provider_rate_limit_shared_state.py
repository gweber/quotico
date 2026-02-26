"""
backend/tests/test_provider_rate_limit_shared_state.py

Purpose:
    Validate provider-wide (shared) in-process RPM limiting semantics.

Dependencies:
    - app.services.provider_rate_limiter
"""

from __future__ import annotations

import pytest

from app.services import provider_rate_limiter as limiter_module
from app.services.provider_rate_limiter import ProviderRateLimiter


@pytest.mark.asyncio
async def test_provider_rate_limit_shared_state(monkeypatch):
    timeline = {"now": 0.0}
    sleeps: list[float] = []

    def _fake_monotonic() -> float:
        return timeline["now"]

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        timeline["now"] += seconds

    monkeypatch.setattr(limiter_module.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(limiter_module.asyncio, "sleep", _fake_sleep)

    limiter = ProviderRateLimiter()

    # Endpoint A call (consumes initial token).
    await limiter.acquire("football_data", 1)
    # Endpoint B call for same provider should wait due shared provider bucket.
    await limiter.acquire("football_data", 1)

    assert sleeps, "second call should have been throttled"
    assert round(sleeps[0], 2) == 60.0

