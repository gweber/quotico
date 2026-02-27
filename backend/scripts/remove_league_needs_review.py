"""
backend/scripts/remove_league_needs_review.py

Purpose:
    Dev/local maintenance tool that removes the deprecated `needs_review`
    field from league collections after the hard-cut to v3 league governance.

Usage:
    cd backend && python -m scripts.remove_league_needs_review --dry-run
    cd backend && python -m scripts.remove_league_needs_review --execute --force-dev
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from typing import Any

import app.database as _db
from app.config import settings

TARGET_COLLECTIONS = ("league_registry_v3", "leagues")
DEV_ENV_VALUES = {"dev", "development", "local", "test"}
DEV_DB_PATTERN = re.compile(r"(dev|local|test)", re.IGNORECASE)


def _is_local_mongo(uri: str) -> bool:
    raw = (uri or "").lower()
    return "localhost" in raw or "127.0.0.1" in raw


def _guard_ok(force_dev: bool) -> tuple[bool, dict[str, Any]]:
    env_candidates = [
        os.getenv("APP_ENV", ""),
        os.getenv("ENV", ""),
        os.getenv("PYTHON_ENV", ""),
    ]
    db_name = str(settings.MONGO_DB or "")
    mongo_uri = str(settings.MONGO_URI or "")
    env_ok = any(value.strip().lower() in DEV_ENV_VALUES for value in env_candidates if value)
    db_ok = bool(DEV_DB_PATTERN.search(db_name))
    uri_ok = _is_local_mongo(mongo_uri)
    ok = (env_ok and db_ok) or (force_dev and uri_ok and (env_ok or db_ok))
    return ok, {
        "env_candidates": env_candidates,
        "db_name": db_name,
        "uri_is_local": uri_ok,
        "force_dev": force_dev,
    }


async def _run(execute: bool, force_dev: bool) -> int:
    ok, meta = _guard_ok(force_dev=force_dev)
    if not ok:
        print(
            {
                "ok": False,
                "reason": "guard_failed",
                "message": "Refusing to run outside explicit dev/local context.",
                **meta,
            }
        )
        return 2

    await _db.connect_db()
    try:
        before: dict[str, int] = {}
        for name in TARGET_COLLECTIONS:
            before[name] = int(
                await _db.db[name].count_documents({"needs_review": {"$exists": True}})
            )

        if not execute:
            print({"ok": True, "mode": "dry-run", "before": before})
            return 0

        modified: dict[str, int] = {}
        for name in TARGET_COLLECTIONS:
            result = await _db.db[name].update_many(
                {"needs_review": {"$exists": True}},
                {"$unset": {"needs_review": ""}},
            )
            modified[name] = int(result.modified_count)

        after: dict[str, int] = {}
        for name in TARGET_COLLECTIONS:
            after[name] = int(
                await _db.db[name].count_documents({"needs_review": {"$exists": True}})
            )

        print(
            {
                "ok": True,
                "mode": "execute",
                "before": before,
                "modified": modified,
                "after": after,
            }
        )
        return 0
    finally:
        await _db.close_db()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Remove league `needs_review` field in dev/local DB.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts only (default behavior).")
    parser.add_argument("--execute", action="store_true", help="Apply $unset on target collections.")
    parser.add_argument(
        "--force-dev",
        action="store_true",
        help="Allow execution when either env or DB guard is missing, only with local Mongo URI.",
    )
    args = parser.parse_args()

    execute = bool(args.execute)
    if not execute and not args.dry_run:
        execute = False

    return await _run(execute=execute, force_dev=bool(args.force_dev))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
