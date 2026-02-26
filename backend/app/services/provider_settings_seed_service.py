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
            "provider": "theoddsapi",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": settings.THEODDSAPI_BASE_URL,
            "timeout_seconds": 15.0,
            "max_retries": 3,
            "base_delay_seconds": 10.0,
            "rate_limit_rpm": int(settings.THEODDSAPI_RATE_LIMIT_RPM),
            "poll_interval_seconds": 900,
            "headers_override": {},
            "extra": {},
        },
        {
            "provider": "football_data",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": settings.FOOTBALL_DATA_ORG_BASE_URL,
            "timeout_seconds": 15.0,
            "max_retries": 3,
            "base_delay_seconds": 10.0,
            "rate_limit_rpm": int(settings.FOOTBALL_DATA_RATE_LIMIT_RPM),
            "poll_interval_seconds": 1800,
            "headers_override": {},
            "extra": {},
        },
        {
            "provider": "openligadb",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": settings.OPENLIGADB_BASE_URL,
            "timeout_seconds": 15.0,
            "max_retries": 3,
            "base_delay_seconds": 10.0,
            "rate_limit_rpm": int(settings.OPENLIGADB_RATE_LIMIT_RPM),
            "poll_interval_seconds": 1800,
            "headers_override": {},
            "extra": {},
        },
        {
            "provider": "football_data_uk",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": settings.FOOTBALL_DATA_UK_BASE_URL,
            "timeout_seconds": 30.0,
            "max_retries": 3,
            "base_delay_seconds": 5.0,
            "rate_limit_rpm": int(settings.FOOTBALL_DATA_UK_RATE_LIMIT_RPM),
            "poll_interval_seconds": None,
            "headers_override": {},
            "extra": {},
        },
        {
            "provider": "understat",
            "scope": "global",
            "league_id": None,
            "enabled": True,
            "base_url": settings.UNDERSTAT_BASE_URL,
            "timeout_seconds": 30.0,
            "max_retries": 3,
            "base_delay_seconds": 5.0,
            "rate_limit_rpm": int(settings.UNDERSTAT_RATE_LIMIT_RPM),
            "poll_interval_seconds": None,
            "headers_override": {},
            "extra": {},
        },
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

