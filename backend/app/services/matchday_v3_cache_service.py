"""
backend/app/services/matchday_v3_cache_service.py

Purpose:
    Shared cache helpers for v3.1 matchday aggregation payloads.
    Backed by MongoDB to allow cross-process cache invalidation from ingest jobs.

Dependencies:
    - app.database
    - app.utils
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import app.database as _db
from app.utils import ensure_utc, utcnow

_COLLECTION = "v3_query_cache"


def build_matchday_cache_key(*, sport_key: str, season_id: int | str) -> str:
    return f"matchday:list:{str(sport_key).strip()}:{int(season_id)}"


async def get_matchday_list_cache(*, cache_key: str) -> list[dict[str, Any]] | None:
    doc = await getattr(_db.db, _COLLECTION).find_one({"_id": str(cache_key)})
    if not isinstance(doc, dict):
        return None
    expires_at = doc.get("expires_at")
    if expires_at is None:
        return None
    if ensure_utc(expires_at) <= utcnow():
        return None
    payload = doc.get("payload")
    if not isinstance(payload, list):
        return None
    return payload


async def set_matchday_list_cache(
    *,
    cache_key: str,
    payload: list[dict[str, Any]],
    ttl_seconds: int,
) -> None:
    now = utcnow()
    ttl = max(60, int(ttl_seconds))
    await getattr(_db.db, _COLLECTION).update_one(
        {"_id": str(cache_key)},
        {
            "$set": {
                "kind": "matchday_list",
                "payload": payload,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=ttl),
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def invalidate_matchday_list_cache_for_season(*, season_id: int) -> int:
    result = await getattr(_db.db, _COLLECTION).delete_many(
        {"kind": "matchday_list", "_id": {"$regex": f":{int(season_id)}$"}}
    )
    return int(result.deleted_count or 0)

