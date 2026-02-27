"""
backend/app/services/policy_service.py

Purpose:
    Runtime policy access for v3.1/v3.2 engine controls with short-lived cache
    and audit-friendly snapshot retrieval.

Dependencies:
    - app.database
    - app.utils
"""

from __future__ import annotations

import time as _time
from typing import Any

import app.database as _db

_POLICY_TTL_SECONDS = 30.0


class PolicyService:
    """Load policy values from ``engine_policies_v3`` with in-memory cache."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._expires_at: float = 0.0
        self._version: str = "runtime"

    async def _refresh(self) -> None:
        now = _time.time()
        if now < self._expires_at and self._cache:
            return

        docs = await _db.db.engine_policies_v3.find({}).to_list(length=5000)
        cache: dict[str, Any] = {}
        latest_version = "runtime"
        for doc in docs:
            key = str(doc.get("policy_key") or doc.get("_id") or "").strip()
            if not key:
                continue
            cache[key] = doc.get("value")
            v = str(doc.get("schema_version") or "").strip()
            if v:
                latest_version = v

        self._cache = cache
        self._version = latest_version
        self._expires_at = now + _POLICY_TTL_SECONDS

    async def get(self, key: str, default: Any = None) -> Any:
        await self._refresh()
        return self._cache.get(key, default)

    async def get_snapshot(self, keys: list[str]) -> dict[str, Any]:
        await self._refresh()
        out: dict[str, Any] = {"policy_version_used": self._version}
        for key in keys:
            if key in self._cache:
                out[key] = self._cache[key]
        return out


_policy_service_singleton: PolicyService | None = None


def get_policy_service() -> PolicyService:
    global _policy_service_singleton
    if _policy_service_singleton is None:
        _policy_service_singleton = PolicyService()
    return _policy_service_singleton
