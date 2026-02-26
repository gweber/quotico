"""
backend/app/routers/persons.py

Purpose:
    Read-only batch lookup API for Sportmonks persons (referees/players) used
    by frontend cards to resolve IDs to display names.

Dependencies:
    - app.database
    - app.services.auth_service
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import app.database as _db
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/persons", tags=["persons"])


class PersonsBatchRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


@router.post("/batch")
async def persons_batch_lookup(
    body: PersonsBatchRequest,
    user=Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    _ = user
    ids = [int(pid) for pid in body.ids if isinstance(pid, int)]
    ids = sorted(set(ids))
    if not ids:
        return {"items": []}

    rows = await _db.db.persons.find(
        {"_id": {"$in": ids}},
        {"_id": 1, "type": 1, "name": 1, "common_name": 1, "image_path": 1},
    ).to_list(length=max(50, len(ids)))

    items = []
    for row in rows:
        pid = row.get("_id")
        if not isinstance(pid, int):
            continue
        items.append(
            {
                "id": pid,
                "type": str(row.get("type") or ""),
                "name": str(row.get("name") or ""),
                "common_name": str(row.get("common_name") or ""),
                "image_path": str(row.get("image_path") or ""),
            }
        )

    return {"items": items}

