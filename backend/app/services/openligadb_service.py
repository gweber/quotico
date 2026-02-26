"""
backend/app/services/openligadb_service.py

Purpose:
    Season import service for OpenLigaDB using the unified match ingest
    service. Emits admin-job friendly counters and conflict previews.

Dependencies:
    - app.database
    - app.services.league_service
    - app.services.match_ingest_service
    - app.services.match_ingest_adapters.openligadb_adapter
    - app.services.team_registry_service
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from bson import ObjectId
from fastapi import HTTPException

import app.database as _db
from app.services.league_service import LeagueRegistry
from app.services.match_ingest_adapters.openligadb_adapter import build_openligadb_matches
from app.services.match_ingest_service import match_ingest_service

logger = logging.getLogger("quotico.openligadb_service")


async def import_season(
    league_id: ObjectId,
    season_year: int,
    dry_run: bool = False,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Import all season matches for one league from OpenLigaDB."""
    league = await _db.db.leagues.find_one({"_id": league_id})
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key")

    league_doc = await LeagueRegistry.get().ensure_for_import(sport_key, auto_create_inactive=True)
    if not league_doc.get("is_active", False):
        raise HTTPException(status_code=400, detail="League is inactive")

    ext_ids = league.get("external_ids") if isinstance(league.get("external_ids"), dict) else {}
    league_shortcut = str(ext_ids.get("openligadb") or "").strip()
    if not league_shortcut:
        raise HTTPException(status_code=400, detail="League has no openligadb external ID set")

    if progress_cb:
        await progress_cb(
            {
                "phase": "fetching_matches",
                "progress": {"processed": 0, "total": 0, "percent": 0.0},
                "counters": {
                    "processed": 0,
                    "created": 0,
                    "updated": 0,
                    "matched": 0,
                    "skipped_conflicts": 0,
                    "unresolved_teams": 0,
                    "skipped_season_mismatch": 0,
                    "would_create": 0,
                    "would_update": 0,
                },
            }
        )

    transformed, skipped_season_mismatch = await build_openligadb_matches(
        league_shortcut=league_shortcut,
        season_year=int(season_year),
        sport_key=sport_key,
    )

    if progress_cb:
        total = len(transformed)
        await progress_cb(
            {
                "phase": "upserting_matches",
                "progress": {"processed": total, "total": total, "percent": 100.0 if total else 0.0},
            }
        )

    ingest_result = await match_ingest_service.process_matches(
        transformed,
        league_id=league_id,
        dry_run=dry_run,
    )

    created = int(ingest_result.get("created", 0))
    updated = int(ingest_result.get("updated", 0))
    processed = int(ingest_result.get("processed", 0))
    conflicts = int(ingest_result.get("conflicts", 0))
    unresolved_teams = int(ingest_result.get("unresolved_team", 0)) + int(ingest_result.get("team_name_conflict", 0))

    result: dict[str, Any] = {
        "processed": processed,
        "created": created,
        "updated": updated,
        "matched": created + updated,
        "skipped_conflicts": conflicts,
        "unresolved_teams": unresolved_teams,
        "skipped_season_mismatch": int(skipped_season_mismatch),
        "season": int(season_year),
        "league_id": str(league_id),
        "conflicts_preview": ingest_result.get("conflicts_preview", [])[:20],
        "match_ingest": {
            "matched_by_external_id": int(ingest_result.get("matched_by_external_id", 0)),
            "matched_by_identity_window": int(ingest_result.get("matched_by_identity_window", 0)),
            "team_name_conflict": int(ingest_result.get("team_name_conflict", 0)),
            "other_conflicts": int(ingest_result.get("other_conflicts", 0)),
        },
    }
    if dry_run:
        result["dry_run_preview"] = {
            "matches_found": processed,
            "would_create": created,
            "would_update": updated,
            "items": ingest_result.get("items_preview", [])[:10],
        }

    if progress_cb:
        await progress_cb(
            {
                "phase": "finalizing",
                "progress": {"processed": processed, "total": processed, "percent": 100.0 if processed else 0.0},
                "counters": {
                    "processed": processed,
                    "created": created,
                    "updated": updated,
                    "matched": created + updated,
                    "skipped_conflicts": conflicts,
                    "unresolved_teams": unresolved_teams,
                    "skipped_season_mismatch": int(skipped_season_mismatch),
                    "would_create": created if dry_run else 0,
                    "would_update": updated if dry_run else 0,
                },
            }
        )

    return result
