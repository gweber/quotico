"""
backend/app/services/event_handlers/matchday_handlers.py

Purpose:
    Targeted matchday projection refresh subscribers.

Dependencies:
    - app.database
    - app.services.event_models
    - app.utils
"""

from __future__ import annotations

import logging

from bson import ObjectId

import app.database as _db
from app.services.event_models import BaseEvent
from app.utils import utcnow

logger = logging.getLogger("quotico.event_handlers.matchday")


async def handle_match_updated(event: BaseEvent) -> None:
    # Idempotency/workload guard: only projection-affecting changes should trigger recalculation.
    changed_fields = set(getattr(event, "changed_fields", []) or [])
    if changed_fields and changed_fields.isdisjoint({"status", "score", "matchday", "match_date"}):
        return

    match_id = str(getattr(event, "match_id", "") or "")
    if not match_id:
        return

    linked = await _db.db.matchdays.find(
        {"match_ids": match_id},
        {"_id": 1, "match_ids": 1, "status": 1},
    ).to_list(length=100)
    if not linked:
        return

    for matchday in linked:
        ids = [mid for mid in matchday.get("match_ids", []) if isinstance(mid, str)]
        object_ids: list[ObjectId] = []
        for item in ids:
            try:
                object_ids.append(ObjectId(item))
            except Exception:
                continue
        if not object_ids:
            continue

        matches = await _db.db.matches.find(
            {"_id": {"$in": object_ids}},
            {"status": 1},
        ).to_list(length=len(object_ids))
        statuses = [str(doc.get("status") or "scheduled") for doc in matches]
        if not statuses:
            continue
        all_final = all(status == "final" for status in statuses)
        any_live = any(status == "live" for status in statuses)

        next_status = "completed" if all_final else ("in_progress" if any_live or any(s == "final" for s in statuses) else "upcoming")
        if next_status == matchday.get("status"):
            continue

        await _db.db.matchdays.update_one(
            {"_id": matchday["_id"]},
            {"$set": {"status": next_status, "updated_at": utcnow()}},
        )
        logger.info("Updated matchday status via event matchday_id=%s status=%s", str(matchday["_id"]), next_status)
