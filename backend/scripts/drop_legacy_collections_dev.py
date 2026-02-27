"""
backend/scripts/drop_legacy_collections_dev.py

Purpose:
    Safely drop legacy collections from a development MongoDB after the v3-only
    hard cut. Intended for local/dev environments only.

Usage:
    cd backend && python -m scripts.drop_legacy_collections_dev --dry-run
    cd backend && python -m scripts.drop_legacy_collections_dev --execute --force-dev
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

import app.database as _db
from app.config import settings

LEGACY_COLLECTIONS = ("matches", "teams", "leagues")
_DEV_ENV_VALUES = {"dev", "development", "local", "test"}
_DEV_DB_PATTERN = re.compile(r"(dev|local|test)", re.IGNORECASE)


def _guard_state() -> tuple[bool, bool]:
    env_candidates = {
        str(os.getenv("ENV") or "").strip().lower(),
        str(os.getenv("APP_ENV") or "").strip().lower(),
        str(os.getenv("FASTAPI_ENV") or "").strip().lower(),
    }
    env_ok = any(value in _DEV_ENV_VALUES for value in env_candidates if value)
    db_name = str(getattr(settings, "MONGO_DB", "") or "").strip()
    db_ok = bool(_DEV_DB_PATTERN.search(db_name))
    return env_ok, db_ok


async def _run(*, execute: bool, force_dev: bool) -> int:
    env_ok, db_ok = _guard_state()
    guard_ok = (env_ok and db_ok) or (force_dev and (env_ok or db_ok))
    if not guard_ok:
        print(
            {
                "ok": False,
                "reason": "guard_failed",
                "message": "Refusing to run outside explicit dev/local environment.",
                "env": {
                    "ENV": os.getenv("ENV"),
                    "APP_ENV": os.getenv("APP_ENV"),
                    "FASTAPI_ENV": os.getenv("FASTAPI_ENV"),
                    "mongo_db": getattr(settings, "MONGO_DB", None),
                    "env_ok": env_ok,
                    "db_ok": db_ok,
                },
            }
        )
        return 2

    await _db.connect_db()
    try:
        existing = set(await _db.db.list_collection_names())
        counts: dict[str, int | None] = {}
        for name in LEGACY_COLLECTIONS:
            if name in existing:
                counts[name] = int(await _db.db[name].count_documents({}))
            else:
                counts[name] = None

        print({"ok": True, "mode": "execute" if execute else "dry-run", "before": counts})

        if not execute:
            return 0

        dropped: list[str] = []
        for name in LEGACY_COLLECTIONS:
            if name in existing:
                await _db.db.drop_collection(name)
                dropped.append(name)

        after_existing = set(await _db.db.list_collection_names())
        post = {name: (name in after_existing) for name in LEGACY_COLLECTIONS}
        print({"ok": True, "dropped": dropped, "post_exists": post})
        return 0
    finally:
        await _db.close_db()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drop legacy collections in dev DB with hard guards.")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be dropped.")
    parser.add_argument("--execute", action="store_true", help="Actually drop legacy collections.")
    parser.add_argument(
        "--force-dev",
        action="store_true",
        help="Bypass env/db-name guard for explicit local maintenance only.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    execute = bool(args.execute)
    if not execute and not args.dry_run:
        # Default to dry-run for safety.
        execute = False
    return await _run(execute=execute, force_dev=bool(args.force_dev))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
