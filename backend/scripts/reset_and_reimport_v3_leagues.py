"""
backend/scripts/reset_and_reimport_v3_leagues.py

Purpose:
    Dev-only reset tool for v3 league-centric rebuilds. Clears v3 runtime
    collections, drops legacy collections, re-runs Sportmonks discovery, and
    optionally re-ingests selected seasons.

Usage:
    cd backend && python -m scripts.reset_and_reimport_v3_leagues --dry-run
    cd backend && python -m scripts.reset_and_reimport_v3_leagues --execute --force-dev
    cd backend && python -m scripts.reset_and_reimport_v3_leagues --execute --season-id 23614
    cd backend && python -m scripts.reset_and_reimport_v3_leagues --execute --all-active-seasons
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import app.database as _db
from app.config import settings
from app.services.sportmonks_connector import sportmonks_connector

RESET_COLLECTIONS = [
    "league_registry_v3",
    "matches_v3",
    "teams_v3",
    "matchdays",
    "engine_config",
    "engine_calibration",
    "qbot_strategies",
    "qbot_backtests",
    "qtip_cache",
    "provider_settings",
    "provider_secrets",
]

LEGACY_DROP_COLLECTIONS = ["matches", "teams", "leagues"]


def _is_dev_safe_db_name(name: str) -> bool:
    raw = str(name or "").lower()
    return any(token in raw for token in ("dev", "local", "test"))


def _is_local_mongo(uri: str) -> bool:
    raw = str(uri or "").lower()
    return any(token in raw for token in ("localhost", "127.0.0.1", "mongodb://mongo"))


def _guard_or_raise(force_dev: bool) -> None:
    db_name = str(settings.MONGO_DB or "")
    mongo_uri = str(settings.MONGO_URI or "")
    if _is_dev_safe_db_name(db_name):
        return
    if force_dev and _is_local_mongo(mongo_uri):
        return
    raise RuntimeError(
        f"Guard rejected. Refusing to run on MONGO_DB={db_name!r}. "
        "Use a dev/local/test DB name, or --force-dev with a local Mongo URI."
    )


async def _collection_count(name: str) -> int:
    try:
        return int(await _db.db[name].count_documents({}))
    except Exception:
        return -1


async def _resolve_active_seasons() -> list[int]:
    rows = await _db.db.league_registry_v3.find(
        {"is_active": True},
        {"available_seasons": 1},
    ).to_list(length=10_000)
    season_ids: set[int] = set()
    for row in rows:
        for season in row.get("available_seasons") or []:
            sid = season.get("id") if isinstance(season, dict) else None
            if isinstance(sid, int):
                season_ids.add(sid)
    return sorted(season_ids)


async def _run(args: argparse.Namespace) -> int:
    _guard_or_raise(force_dev=bool(args.force_dev))

    await _db.connect_db()
    try:
        before = {
            name: await _collection_count(name)
            for name in (RESET_COLLECTIONS + LEGACY_DROP_COLLECTIONS)
        }
        print("[reset] before_counts:", before)

        if not args.execute:
            print("[reset] dry-run only (no writes)")
            return 0

        for name in RESET_COLLECTIONS:
            await _db.db[name].delete_many({})
        for name in LEGACY_DROP_COLLECTIONS:
            try:
                await _db.db[name].drop()
            except Exception:
                pass

        discovery = await sportmonks_connector.get_available_leagues()
        await sportmonks_connector.sync_leagues_to_registry(discovery.get("items") or [])
        print("[reset] discovery synced:", len(discovery.get("items") or []))

        season_ids: set[int] = set(int(sid) for sid in (args.season_id or []))
        if args.all_active_seasons:
            for sid in await _resolve_active_seasons():
                season_ids.add(int(sid))

        for season_id in sorted(season_ids):
            print(f"[reset] ingest season {season_id}")
            await sportmonks_connector.ingest_season(int(season_id))
            await sportmonks_connector.run_metrics_sync(int(season_id))

        after = {
            name: await _collection_count(name)
            for name in (RESET_COLLECTIONS + LEGACY_DROP_COLLECTIONS)
        }
        print("[reset] after_counts:", after)
        return 0
    finally:
        await _db.close_db()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset v3 league data and re-import via Sportmonks")
    parser.add_argument("--dry-run", action="store_true", help="Show counts only (default behavior)")
    parser.add_argument("--execute", action="store_true", help="Perform reset and re-import")
    parser.add_argument("--force-dev", action="store_true", help="Allow execution on local Mongo URI when DB name is not dev-like")
    parser.add_argument("--season-id", action="append", type=int, default=[], help="Season id to deep-ingest (repeatable)")
    parser.add_argument("--all-active-seasons", action="store_true", help="Ingest all season ids discovered on active leagues")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.execute:
        args.dry_run = True
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
