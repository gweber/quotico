"""Persistent worker state — tracks synced_at per worker across restarts.

Prevents unnecessary API calls after dev restarts and deploys.
Uses a lightweight `worker_state` collection in MongoDB.
"""

from datetime import datetime, timedelta

import app.database as _db
from app.utils import ensure_utc, utcnow


async def get_synced_at(worker_id: str) -> datetime | None:
    """Get the last synced_at timestamp for a worker."""
    doc = await _db.db.worker_state.find_one({"_id": worker_id})
    return doc["synced_at"] if doc else None


async def set_synced(worker_id: str) -> None:
    """Mark a worker as just synced."""
    now = utcnow()
    await _db.db.worker_state.update_one(
        {"_id": worker_id},
        {"$set": {"synced_at": now}},
        upsert=True,
    )


async def recently_synced(worker_id: str, max_age: timedelta) -> bool:
    """Check if a worker synced within the given time window."""
    last = await get_synced_at(worker_id)
    if not last:
        return False
    last = ensure_utc(last)
    return (utcnow() - last) < max_age
