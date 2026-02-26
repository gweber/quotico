"""
backend/app/services/provider_rate_limiter.py

Purpose:
    Process-local provider-wide RPM limiter (shared across all endpoints of a
    provider). V1 is single-process by design.

Dependencies:
    - asyncio
    - time
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _BucketState:
    tokens: float
    updated_at: float
    capacity: float
    refill_per_second: float
    lock: asyncio.Lock


class ProviderRateLimiter:
    """Token-bucket limiter shared per provider inside one process."""

    def __init__(self) -> None:
        self._buckets: dict[str, _BucketState] = {}
        self._global_lock = asyncio.Lock()

    async def acquire(self, provider: str, rpm: int | None) -> None:
        if rpm is None or int(rpm) <= 0:
            return
        provider_key = str(provider or "").strip().lower()
        if not provider_key:
            return

        bucket = await self._get_or_create_bucket(provider_key, int(rpm))
        await self._acquire_bucket(bucket)

    async def _get_or_create_bucket(self, provider: str, rpm: int) -> _BucketState:
        async with self._global_lock:
            existing = self._buckets.get(provider)
            now = time.monotonic()
            capacity = max(1.0, float(rpm))
            refill_per_second = capacity / 60.0
            if existing is None:
                existing = _BucketState(
                    tokens=capacity,
                    updated_at=now,
                    capacity=capacity,
                    refill_per_second=refill_per_second,
                    lock=asyncio.Lock(),
                )
                self._buckets[provider] = existing
                return existing
            # Keep state, but apply new config immediately.
            existing.capacity = capacity
            existing.refill_per_second = refill_per_second
            if existing.tokens > capacity:
                existing.tokens = capacity
            return existing

    async def _acquire_bucket(self, bucket: _BucketState) -> None:
        while True:
            async with bucket.lock:
                now = time.monotonic()
                elapsed = max(0.0, now - bucket.updated_at)
                if elapsed > 0:
                    bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second)
                bucket.updated_at = now

                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return

                wait_seconds = (1.0 - bucket.tokens) / max(bucket.refill_per_second, 1e-9)

            await asyncio.sleep(wait_seconds)


provider_rate_limiter = ProviderRateLimiter()

