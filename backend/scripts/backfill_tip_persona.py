"""
backend/scripts/backfill_tip_persona.py

Purpose:
    Idempotent migration helper for persona governance rollout.
    - Backfills missing users.tip_persona fields.
    - Seeds active tip_persona_policy v1 when no policy exists yet.

Usage:
    cd backend && python -m scripts.backfill_tip_persona
"""

from __future__ import annotations

import asyncio

import app.database as _db
from app.utils import utcnow


async def run() -> None:
    now = utcnow()
    users = _db.db.users
    policy = _db.db.tip_persona_policy

    result = await users.update_many(
        {"$or": [{"tip_persona": {"$exists": False}}, {"tip_persona": None}]},
        {
            "$set": {
                "tip_persona": "casual",
                "tip_persona_updated_at": now,
                "tip_override_persona": None,
                "tip_override_updated_at": None,
                "updated_at": now,
            }
        },
    )

    active = await policy.find_one({"is_active": True}, sort=[("version", -1)])
    seeded = False
    if not isinstance(active, dict):
        await policy.insert_one(
            {
                "version": 1,
                "is_active": True,
                "rules": [],
                "note": "Initial seed",
                "updated_by": "migration",
                "updated_at": now,
                "created_at": now,
            }
        )
        seeded = True

    print(
        {
            "users_backfilled": int(result.modified_count),
            "policy_seeded": seeded,
        }
    )


if __name__ == "__main__":
    asyncio.run(run())
