"""
backend/app/services/provider_settings_seed_service.py

Purpose:
    Bootstrap default provider runtime settings into MongoDB when the
    provider_settings collection is empty.

Dependencies:
    - app.database
    - app.config
    - app.utils.utcnow
"""

from __future__ import annotations

import app.database as _db
from app.config import settings
from app.utils import utcnow


def _default_docs() -> list[dict]:
    return [
        {
            "provider": "sportmonks",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": str(settings.SPORTMONKS_BASE_URL or ""),
            "timeout_seconds": 90.0,
            "max_retries": 3,
            "base_delay_seconds": 2.0,
            "rate_limit_rpm": 180,
            "poll_interval_seconds": 900,
            "headers_override": {},
            "extra": {},
        }
    ]


async def seed_provider_settings_defaults() -> dict[str, int]:
    """Seed provider settings once for bootstrap-only environments."""
    count = await _db.db.provider_settings.count_documents({})
    if count > 0:
        return {"created": 0, "skipped": count}

    now = utcnow()
    created = 0
    for doc in _default_docs():
        payload = {**doc, "created_at": now, "updated_at": now, "updated_by": "SYSTEM"}
        await _db.db.provider_settings.insert_one(payload)
        created += 1
    return {"created": created, "skipped": 0}

