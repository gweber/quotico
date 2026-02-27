"""
backend/app/routers/admin_ingest.py

Purpose:
    Admin ingest API for Sportmonks v3 discovery and season deep-ingest jobs
    with cache-first behavior and job-lock safety.

Dependencies:
    - app.services.auth_service
    - app.services.sportmonks_connector
    - app.utils
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

import numpy as np
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.services.league_service import invalidate_navigation_cache
from app.services.sportmonks_connector import sportmonks_connector
from app.utils import ensure_utc, utcnow

router = APIRouter(prefix="/api/admin/ingest", tags=["admin-ingest"])
logger = logging.getLogger("quotico.admin_ingest")

_JOB_TYPE = "sportmonks_deep_ingest"
_JOB_TYPE_METRICS = "sportmonks_metrics_sync"
_RATE_META_ID = "sportmonks_rate_limit_state"
_CACHE_METRICS_META_ID = "sportmonks_page_cache_metrics"
_GUARD_METRICS_META_ID = "sportmonks_guard_metrics"


class OverviewUserStats(BaseModel):
    total: int
    banned: int


class OverviewBetStats(BaseModel):
    total: int
    today: int


class OverviewMatchesV3Stats(BaseModel):
    total: int
    scheduled: int
    live: int
    finished: int
    postponed: int
    canceled: int
    walkover: int
    with_xg: int
    with_odds: int
    last_match_update: str | None


class OverviewIngestJobStats(BaseModel):
    queued: int
    running: int
    paused: int
    failed: int
    succeeded: int


class OverviewSportmonksApiStats(BaseModel):
    remaining: int | None
    reset_at: str | None
    reserve_credits: int


class AdminOverviewStatsResponse(BaseModel):
    users: OverviewUserStats
    bets: OverviewBetStats
    matches_v3: OverviewMatchesV3Stats
    ingest_jobs: OverviewIngestJobStats
    sportmonks_api: OverviewSportmonksApiStats
    generated_at: str


class ManualCheckAutoHealResponse(BaseModel):
    season_id: int | None = None
    healed_total: int
    healed_xg: int
    healed_odds: int


class IngestLeagueFeaturesPatchBody(BaseModel):
    tipping: bool | None = None
    match_load: bool | None = None
    xg_sync: bool | None = None
    odds_sync: bool | None = None


class IngestLeaguePatchBody(BaseModel):
    is_active: bool | None = None
    ui_order: int | None = None
    name: str | None = None
    features: IngestLeagueFeaturesPatchBody | None = None


def _serialize_discovery_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        features_raw = row.get("features")
        if not isinstance(features_raw, dict):
            features_raw = {}
        out.append(
            {
                "league_id": int(row.get("_id")),
                "sport_key": str(row.get("sport_key") or ""),
                "name": str(row.get("name") or ""),
                "country": str(row.get("country") or ""),
                "is_cup": bool(row.get("is_cup", False)),
                "is_active": bool(row.get("is_active", False)),
                "needs_review": bool(row.get("needs_review", False)),
                "ui_order": int(row.get("ui_order", 999)),
                "features": {
                    "tipping": bool(features_raw.get("tipping", False)),
                    "match_load": bool(features_raw.get("match_load", False)),
                    "xg_sync": bool(features_raw.get("xg_sync", False)),
                    "odds_sync": bool(features_raw.get("odds_sync", False)),
                },
                "available_seasons": row.get("available_seasons") or [],
                "last_synced_at": (
                    ensure_utc(row.get("last_synced_at")).isoformat()
                    if row.get("last_synced_at")
                    else None
                ),
            }
        )
    return out


def _read_updated_at(doc: dict[str, Any] | None):
    if not isinstance(doc, dict):
        return None
    value = doc.get("updated_at_utc")
    if value is None:
        value = doc.get("updated_at")
    return ensure_utc(value) if value is not None else None


@router.get("/overview/stats", response_model=AdminOverviewStatsResponse)
async def get_admin_overview_stats(admin=Depends(get_admin_user)):
    _ = admin
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    users_total = int(await _db.db.users.count_documents({}))
    users_banned = int(await _db.db.users.count_documents({"is_banned": True}))
    bets_total = int(await _db.db.betting_slips.count_documents({}))
    bets_today = int(await _db.db.betting_slips.count_documents({"created_at": {"$gte": today_start}}))

    match_total = int(await _db.db.matches_v3.count_documents({}))
    match_scheduled = int(await _db.db.matches_v3.count_documents({"status": "SCHEDULED"}))
    match_live = int(await _db.db.matches_v3.count_documents({"status": "LIVE"}))
    match_finished = int(await _db.db.matches_v3.count_documents({"status": "FINISHED"}))
    match_postponed = int(await _db.db.matches_v3.count_documents({"status": "POSTPONED"}))
    match_canceled = int(
        await _db.db.matches_v3.count_documents({"status": {"$in": ["CANCELED", "CANCELLED"]}})
    )
    match_walkover = int(await _db.db.matches_v3.count_documents({"status": "WALKOVER"}))
    match_with_xg = int(await _db.db.matches_v3.count_documents({"has_advanced_stats": True}))
    match_with_odds = int(
        await _db.db.matches_v3.count_documents(
            {
                "odds_meta.summary_1x2.home.avg": {"$exists": True, "$ne": None},
                "odds_meta.summary_1x2.draw.avg": {"$exists": True, "$ne": None},
                "odds_meta.summary_1x2.away.avg": {"$exists": True, "$ne": None},
            }
        )
    )

    latest_finished = await _db.db.matches_v3.find_one(
        {"status": "FINISHED", "updated_at_utc": {"$exists": True}},
        {"updated_at_utc": 1, "updated_at": 1},
        sort=[("updated_at_utc", -1)],
    )
    if not latest_finished:
        latest_finished = await _db.db.matches_v3.find_one(
            {"updated_at": {"$exists": True}},
            {"updated_at_utc": 1, "updated_at": 1},
            sort=[("updated_at", -1)],
        )
    last_updated = _read_updated_at(latest_finished if isinstance(latest_finished, dict) else None)
    last_match_update = last_updated.isoformat() if last_updated else None

    ingest_queued = int(await _db.db.admin_import_jobs.count_documents({"status": "queued"}))
    ingest_running = int(await _db.db.admin_import_jobs.count_documents({"status": "running"}))
    ingest_paused = int(await _db.db.admin_import_jobs.count_documents({"status": "paused"}))
    ingest_failed = int(await _db.db.admin_import_jobs.count_documents({"status": "failed"}))
    ingest_succeeded = int(await _db.db.admin_import_jobs.count_documents({"status": "succeeded"}))

    rate_meta = await _db.db.meta.find_one({"_id": _RATE_META_ID}, {"remaining": 1, "reset_at": 1})
    remaining = (
        int(rate_meta.get("remaining"))
        if isinstance(rate_meta, dict) and rate_meta.get("remaining") is not None
        else None
    )
    reset_at = None
    if isinstance(rate_meta, dict) and rate_meta.get("reset_at") is not None:
        try:
            reset_at = ensure_utc(rate_meta.get("reset_at")).isoformat()
        except Exception:
            reset_at = str(rate_meta.get("reset_at"))

    return AdminOverviewStatsResponse(
        users=OverviewUserStats(total=users_total, banned=users_banned),
        bets=OverviewBetStats(total=bets_total, today=bets_today),
        matches_v3=OverviewMatchesV3Stats(
            total=match_total,
            scheduled=match_scheduled,
            live=match_live,
            finished=match_finished,
            postponed=match_postponed,
            canceled=match_canceled,
            walkover=match_walkover,
            with_xg=match_with_xg,
            with_odds=match_with_odds,
            last_match_update=last_match_update,
        ),
        ingest_jobs=OverviewIngestJobStats(
            queued=ingest_queued,
            running=ingest_running,
            paused=ingest_paused,
            failed=ingest_failed,
            succeeded=ingest_succeeded,
        ),
        sportmonks_api=OverviewSportmonksApiStats(
            remaining=remaining,
            reset_at=reset_at,
            reserve_credits=int(settings.SPORTMONKS_RESERVE_CREDITS),
        ),
        generated_at=now.isoformat(),
    )


def _latest_synced_at(rows: list[dict[str, Any]]):
    latest = None
    for row in rows:
        value = row.get("last_synced_at")
        if value is None:
            continue
        current = ensure_utc(value)
        if latest is None or current > latest:
            latest = current
    return latest


def _error_summary(error_log: list[dict[str, Any]] | Any) -> dict[str, int]:
    warnings = 0
    errors = 0
    for row in error_log if isinstance(error_log, list) else []:
        msg = str((row or {}).get("error_msg") or "").strip().lower()
        if msg.startswith("warning:"):
            warnings += 1
        else:
            errors += 1
    return {"warnings": int(warnings), "errors": int(errors)}


async def _list_cached_leagues() -> list[dict[str, Any]]:
    rows = await _db.db.league_registry_v3.find({}).to_list(length=10_000)
    return sportmonks_connector._sort_discovery_items(rows)


@router.get("/discovery")
async def discover_leagues(
    force: bool = Query(False),
    admin=Depends(get_admin_user),
):
    _ = admin
    now = utcnow()
    ttl_minutes = int(settings.SPORTMONKS_DISCOVERY_TTL_MINUTES)
    cached = await _list_cached_leagues()
    last_synced_at = _latest_synced_at(cached)
    ttl_delta = timedelta(minutes=ttl_minutes)
    cache_valid = (
        bool(cached)
        and last_synced_at is not None
        and (now - last_synced_at) <= ttl_delta
    )

    if not force and cache_valid:
        rate_meta = await _db.db.meta.find_one({"_id": _RATE_META_ID})
        remaining = int(rate_meta.get("remaining")) if isinstance(rate_meta, dict) and rate_meta.get("remaining") is not None else None
        return {
            "source": "cache",
            "ttl_minutes": ttl_minutes,
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
            "rate_limit_remaining": remaining,
            "items": _serialize_discovery_items(cached),
        }

    rate_meta = await _db.db.meta.find_one({"_id": _RATE_META_ID})
    known_remaining = (
        int(rate_meta.get("remaining"))
        if isinstance(rate_meta, dict) and rate_meta.get("remaining") is not None
        else None
    )
    if known_remaining is not None and known_remaining <= 1:
        return {
            "source": "cache_fallback_rate_limited",
            "ttl_minutes": ttl_minutes,
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
            "rate_limit_remaining": known_remaining,
            "warning": "Discovery refresh skipped due to low remaining credits.",
            "items": _serialize_discovery_items(cached),
        }

    discovery = await sportmonks_connector.get_available_leagues()
    remaining = discovery.get("remaining")
    reset_at = discovery.get("reset_at")
    if remaining is not None and int(remaining) <= 1:
        await _db.db.meta.update_one(
            {"_id": _RATE_META_ID},
            {"$set": {"remaining": int(remaining), "reset_at": reset_at, "updated_at": now, "updated_at_utc": now}},
            upsert=True,
        )
        return {
            "source": "cache_fallback_rate_limited",
            "ttl_minutes": ttl_minutes,
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
            "rate_limit_remaining": int(remaining),
            "warning": "Discovery refresh skipped due to low remaining credits.",
            "items": _serialize_discovery_items(cached),
        }

    await sportmonks_connector.sync_leagues_to_registry(discovery.get("items") or [])
    await _db.db.meta.update_one(
        {"_id": _RATE_META_ID},
        {"$set": {"remaining": remaining, "reset_at": reset_at, "updated_at": now, "updated_at_utc": now}},
        upsert=True,
    )
    refreshed = await _list_cached_leagues()
    refreshed_last = _latest_synced_at(refreshed)
    return {
        "source": "sportmonks",
        "ttl_minutes": ttl_minutes,
        "last_synced_at": refreshed_last.isoformat() if refreshed_last else None,
        "rate_limit_remaining": remaining,
        "items": _serialize_discovery_items(refreshed),
    }


@router.patch("/leagues/{league_id}")
async def patch_discovery_league(
    league_id: int,
    body: IngestLeaguePatchBody,
    admin=Depends(get_admin_user),
):
    _ = admin
    if (
        body.is_active is None
        and body.ui_order is None
        and body.name is None
        and body.features is None
    ):
        raise HTTPException(status_code=400, detail="Nothing to update.")

    doc = await _db.db.league_registry_v3.find_one({"_id": int(league_id)})
    if not isinstance(doc, dict):
        raise HTTPException(status_code=404, detail="League not found.")

    stamp_now = utcnow()
    updates: dict[str, Any] = {"updated_at": stamp_now, "updated_at_utc": stamp_now}
    if body.is_active is not None:
        updates["is_active"] = bool(body.is_active)
    if body.ui_order is not None:
        updates["ui_order"] = int(body.ui_order)
    if body.name is not None:
        cleaned_name = str(body.name).strip()
        if not cleaned_name:
            raise HTTPException(status_code=400, detail="name must not be empty.")
        updates["name"] = cleaned_name
    if body.features is not None:
        existing_features = doc.get("features") if isinstance(doc.get("features"), dict) else {}
        next_features = {
            "tipping": bool(existing_features.get("tipping")),
            "match_load": bool(existing_features.get("match_load")),
            "xg_sync": bool(existing_features.get("xg_sync")),
            "odds_sync": bool(existing_features.get("odds_sync")),
        }
        if body.features.tipping is not None:
            next_features["tipping"] = bool(body.features.tipping)
        if body.features.match_load is not None:
            next_features["match_load"] = bool(body.features.match_load)
        if body.features.xg_sync is not None:
            next_features["xg_sync"] = bool(body.features.xg_sync)
        if body.features.odds_sync is not None:
            next_features["odds_sync"] = bool(body.features.odds_sync)
        updates["features"] = next_features

    await _db.db.league_registry_v3.update_one({"_id": int(league_id)}, {"$set": updates})
    await invalidate_navigation_cache()
    updated = await _db.db.league_registry_v3.find_one({"_id": int(league_id)})
    if not isinstance(updated, dict):
        raise HTTPException(status_code=500, detail="League update failed.")
    return {"item": _serialize_discovery_items([updated])[0]}


async def _run_sportmonks_ingest_job(job_id: ObjectId, season_id: int) -> None:
    try:
        await sportmonks_connector.ingest_season(int(season_id), job_id=job_id)
    except Exception as exc:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "phase": "failed",
                    "active_lock": False,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                    "updated_at": now,
                    "updated_at_utc": now,
                    "finished_at": now,
                }
            },
        )


async def _run_sportmonks_metrics_job(job_id: ObjectId, season_id: int) -> None:
    try:
        await sportmonks_connector.run_metrics_sync(int(season_id), job_id=job_id)
    except Exception as exc:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "phase": "failed",
                    "active_lock": False,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                    "updated_at": now,
                    "updated_at_utc": now,
                    "finished_at": now,
                }
            },
        )


@router.post("/season/{season_id}")
async def start_season_ingest(
    season_id: int,
    background_tasks: BackgroundTasks,
    admin=Depends(get_admin_user),
):
    admin_id = str(admin.get("_id"))
    existing = await _db.db.admin_import_jobs.find_one(
        {
            "type": _JOB_TYPE,
            "season_id": int(season_id),
            "status": {"$in": ["queued", "running", "paused"]},
        },
        {"_id": 1, "status": 1},
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "An active ingest job already exists for this season.",
                "active_job_id": str(existing["_id"]),
                "season_id": int(season_id),
                "status": str(existing.get("status") or ""),
            },
        )

    now = utcnow()
    doc = {
        "type": _JOB_TYPE,
        "source": "sportmonks",
        "season_id": int(season_id),
        "status": "queued",
        "phase": "queued",
        "active_lock": True,
        "rate_limit_paused": False,
        "rate_limit_remaining": None,
        "total_rounds": 0,
        "processed_rounds": 0,
        "current_round_name": None,
        "progress": {"processed": 0, "total": 0, "percent": 0.0},
        "timeout_at": None,
        "max_runtime_minutes": int(settings.SPORTMONKS_MAX_RUNTIME_DEEP_MINUTES),
        "page_requests_total": 0,
        "duplicate_page_blocks": 0,
        "phase_page_requests": {},
        "error_log": [],
        "retry_count": 0,
        "error": None,
        "results": None,
        "admin_id": admin_id,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "updated_at_utc": now,
        "finished_at": None,
    }
    try:
        inserted = await _db.db.admin_import_jobs.insert_one(doc)
    except DuplicateKeyError:
        existing_locked = await _db.db.admin_import_jobs.find_one(
            {"type": _JOB_TYPE, "season_id": int(season_id), "active_lock": True},
            {"_id": 1, "status": 1},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "An active ingest job already exists for this season.",
                "active_job_id": str((existing_locked or {}).get("_id")),
                "season_id": int(season_id),
                "status": str((existing_locked or {}).get("status") or ""),
            },
        )
    job_id = inserted.inserted_id
    background_tasks.add_task(_run_sportmonks_ingest_job, job_id, int(season_id))
    return {
        "accepted": True,
        "job_id": str(job_id),
        "season_id": int(season_id),
        "status": "queued",
    }


@router.post("/season/{season_id}/metrics-sync")
async def start_metrics_sync(
    season_id: int,
    background_tasks: BackgroundTasks,
    admin=Depends(get_admin_user),
):
    admin_id = str(admin.get("_id"))
    has_matches = await _db.db.matches_v3.count_documents({"season_id": int(season_id)}, limit=1)
    if int(has_matches) == 0:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Precondition Required: Perform Deep Ingest first.",
        )
    existing = await _db.db.admin_import_jobs.find_one(
        {
            "type": _JOB_TYPE_METRICS,
            "season_id": int(season_id),
            "status": {"$in": ["queued", "running", "paused"]},
        },
        {"_id": 1, "status": 1},
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "An active metrics-sync job already exists for this season.",
                "active_job_id": str(existing["_id"]),
                "season_id": int(season_id),
                "status": str(existing.get("status") or ""),
            },
        )
    now = utcnow()
    doc = {
        "type": _JOB_TYPE_METRICS,
        "source": "sportmonks",
        "season_id": int(season_id),
        "status": "queued",
        "phase": "queued",
        "active_lock": True,
        "rate_limit_paused": False,
        "rate_limit_remaining": None,
        "total_rounds": 0,
        "processed_rounds": 0,
        "current_round_name": None,
        "progress": {"processed": 0, "total": 0, "percent": 0.0},
        "timeout_at": None,
        "max_runtime_minutes": int(settings.SPORTMONKS_MAX_RUNTIME_METRICS_MINUTES),
        "page_requests_total": 0,
        "duplicate_page_blocks": 0,
        "phase_page_requests": {},
        "pages_processed": 0,
        "pages_total": None,
        "rows_processed": 0,
        "error_log": [],
        "retry_count": 0,
        "error": None,
        "results": None,
        "admin_id": admin_id,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "updated_at_utc": now,
        "finished_at": None,
    }
    try:
        inserted = await _db.db.admin_import_jobs.insert_one(doc)
    except DuplicateKeyError:
        existing_locked = await _db.db.admin_import_jobs.find_one(
            {"type": _JOB_TYPE_METRICS, "season_id": int(season_id), "active_lock": True},
            {"_id": 1, "status": 1},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": "An active metrics-sync job already exists for this season.",
                "active_job_id": str((existing_locked or {}).get("_id")),
                "season_id": int(season_id),
                "status": str((existing_locked or {}).get("status") or ""),
            },
        )
    job_id = inserted.inserted_id
    background_tasks.add_task(_run_sportmonks_metrics_job, job_id, int(season_id))
    return {
        "accepted": True,
        "job_id": str(job_id),
        "season_id": int(season_id),
        "status": "queued",
    }


@router.post("/manual-check/auto-heal", response_model=ManualCheckAutoHealResponse)
async def auto_heal_manual_checks(
    season_id: int | None = Query(None, ge=1),
    admin=Depends(get_admin_user),
):
    _ = admin
    result = await sportmonks_connector.auto_heal_manual_check_flags(season_id=season_id)
    logger.info(
        "Manual-check auto-heal completed season=%s healed_total=%d healed_xg=%d healed_odds=%d",
        str(season_id) if season_id is not None else "all",
        int(result.get("healed_total", 0)),
        int(result.get("healed_xg", 0)),
        int(result.get("healed_odds", 0)),
    )
    return ManualCheckAutoHealResponse(
        season_id=season_id,
        healed_total=int(result.get("healed_total", 0)),
        healed_xg=int(result.get("healed_xg", 0)),
        healed_odds=int(result.get("healed_odds", 0)),
    )


@router.get("/season/{season_id}/metrics-health")
async def get_metrics_health(season_id: int, admin=Depends(get_admin_user)):
    _ = admin
    total_matches = await _db.db.matches_v3.count_documents({"season_id": int(season_id)})
    xg_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": int(season_id),
            "has_advanced_stats": True,
        }
    )
    odds_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": int(season_id),
            "odds_meta.summary_1x2.home.avg": {"$exists": True},
            "odds_meta.summary_1x2.draw.avg": {"$exists": True},
            "odds_meta.summary_1x2.away.avg": {"$exists": True},
        }
    )
    total = int(total_matches)
    return {
        "season_id": int(season_id),
        "total_matches": total,
        "xg_covered_matches": int(xg_covered),
        "xg_coverage_percent": round((int(xg_covered) / total) * 100.0, 2) if total > 0 else 0.0,
        "odds_covered_matches": int(odds_covered),
        "odds_coverage_percent": round((int(odds_covered) / total) * 100.0, 2) if total > 0 else 0.0,
    }


@router.get("/season/{season_id}/integrity")
async def get_season_integrity(season_id: int, admin=Depends(get_admin_user)):
    _ = admin
    sid = int(season_id)
    total_matches = await _db.db.matches_v3.count_documents({"season_id": sid})
    by_status_cursor = _db.db.matches_v3.aggregate(
        [
            {"$match": {"season_id": sid}},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    )
    status_counts = {str(row.get("_id") or "UNKNOWN"): int(row.get("n") or 0) async for row in by_status_cursor}

    scores_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": sid,
            "teams.home.score": {"$ne": None},
            "teams.away.score": {"$ne": None},
        }
    )
    xg_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": sid,
            "teams.home.xg": {"$ne": None},
            "teams.away.xg": {"$ne": None},
        }
    )
    odds_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": sid,
            "odds_meta.summary_1x2.home.avg": {"$exists": True},
            "odds_meta.summary_1x2.draw.avg": {"$exists": True},
            "odds_meta.summary_1x2.away.avg": {"$exists": True},
        }
    )
    team_links_missing = await _db.db.matches_v3.count_documents(
        {
            "season_id": sid,
            "$or": [{"teams.home.sm_id": {"$exists": False}}, {"teams.away.sm_id": {"$exists": False}}],
        }
    )
    teams_without_name = await _db.db.teams_v3.count_documents(
        {"$or": [{"name": {"$exists": False}}, {"name": None}, {"name": ""}]}
    )
    max_timeline_row = await _db.db.matches_v3.aggregate(
        [
            {"$match": {"season_id": sid}},
            {"$project": {"len": {"$size": {"$ifNull": ["$odds_timeline", []]}}}},
            {"$sort": {"len": -1}},
            {"$limit": 1},
        ]
    ).to_list(length=1)
    max_timeline = int(max_timeline_row[0]["len"]) if max_timeline_row else 0

    finished_total = status_counts.get("FINISHED", 0)
    events_covered = await _db.db.matches_v3.count_documents(
        {"season_id": sid, "status": "FINISHED", "events.0": {"$exists": True}}
    )
    period_scores_covered = await _db.db.matches_v3.count_documents(
        {
            "season_id": sid,
            "status": "FINISHED",
            "scores.half_time.home": {"$ne": None},
            "scores.half_time.away": {"$ne": None},
            "scores.full_time.home": {"$ne": None},
            "scores.full_time.away": {"$ne": None},
        }
    )
    manual_check_count = await _db.db.matches_v3.count_documents(
        {"season_id": sid, "manual_check_required": True}
    )
    flagged_matches = await _db.db.matches_v3.find(
        {"season_id": sid, "manual_check_required": True},
        {
            "_id": 1, "status": 1, "start_at": 1,
            "teams.home.name": 1, "teams.away.name": 1,
            "teams.home.score": 1, "teams.away.score": 1,
            "manual_check_reasons": 1,
        },
    ).sort("start_at", -1).to_list(length=50)

    last_finished = await _db.db.matches_v3.find_one(
        {"season_id": sid, "status": "FINISHED"},
        {"round_id": 1, "start_at": 1},
        sort=[("start_at", -1)],
    )
    round_sample: list[dict[str, Any]] = []
    if isinstance(last_finished, dict) and last_finished.get("round_id") is not None:
        rid = int(last_finished["round_id"])
        rows = await _db.db.matches_v3.find(
            {"season_id": sid, "round_id": rid},
            {
                "_id": 1,
                "status": 1,
                "teams.home.name": 1,
                "teams.away.name": 1,
                "teams.home.score": 1,
                "teams.away.score": 1,
                "teams.home.xg": 1,
                "teams.away.xg": 1,
                "start_at": 1,
            },
        ).sort("start_at", 1).to_list(length=32)
        for row in rows:
            teams = row.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            round_sample.append(
                {
                    "match_id": int(row["_id"]),
                    "status": str(row.get("status") or ""),
                    "home": str(home.get("name") or ""),
                    "away": str(away.get("name") or ""),
                    "score": {"home": home.get("score"), "away": away.get("score")},
                    "xg": {"home": home.get("xg"), "away": away.get("xg")},
                    "start_at": ensure_utc(row.get("start_at")).isoformat() if row.get("start_at") else None,
                }
            )

    def _pct(value: int, total: int) -> float:
        return round((value / total) * 100.0, 2) if total > 0 else 0.0

    return {
        "season_id": sid,
        "total_matches": int(total_matches),
        "status_counts": status_counts,
        "coverage": {
            "scores": {"count": int(scores_covered), "percent": _pct(int(scores_covered), int(total_matches))},
            "xg_pair": {"count": int(xg_covered), "percent": _pct(int(xg_covered), int(total_matches))},
            "odds_1x2": {"count": int(odds_covered), "percent": _pct(int(odds_covered), int(total_matches))},
            "events": {"count": int(events_covered), "percent": _pct(int(events_covered), int(finished_total))},
            "period_scores": {"count": int(period_scores_covered), "percent": _pct(int(period_scores_covered), int(finished_total))},
        },
        "anomalies": {
            "team_links_missing": int(team_links_missing),
            "teams_without_name": int(teams_without_name),
            "max_odds_timeline_len": int(max_timeline),
            "manual_check_required": int(manual_check_count),
        },
        "manual_checks": {
            "count": int(manual_check_count),
            "items": [
                {
                    "match_id": int(row["_id"]),
                    "status": str(row.get("status") or ""),
                    "home": str((row.get("teams") or {}).get("home", {}).get("name") or ""),
                    "away": str((row.get("teams") or {}).get("away", {}).get("name") or ""),
                    "score": {
                        "home": (row.get("teams") or {}).get("home", {}).get("score"),
                        "away": (row.get("teams") or {}).get("away", {}).get("score"),
                    },
                    "start_at": ensure_utc(row.get("start_at")).isoformat() if row.get("start_at") else None,
                    "reasons": row.get("manual_check_reasons") or [],
                }
                for row in flagged_matches
            ],
        },
        "last_finished_round": {
            "round_id": int(last_finished.get("round_id")) if isinstance(last_finished, dict) and last_finished.get("round_id") is not None else None,
            "start_at": ensure_utc(last_finished.get("start_at")).isoformat() if isinstance(last_finished, dict) and last_finished.get("start_at") else None,
            "matches": round_sample,
        },
        "generated_at": utcnow().isoformat(),
    }


def _audit_check(key: str, label: str, value: str, status_val: str, detail: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "status": status_val, "detail": detail}


def _pct_status(pct: float, green: float = 90.0, yellow: float = 70.0) -> str:
    if pct >= green:
        return "green"
    if pct >= yellow:
        return "yellow"
    return "red"


def _count_status(count: int, green_max: int = 0, yellow_max: int = 10) -> str:
    if count <= green_max:
        return "green"
    if count <= yellow_max:
        return "yellow"
    return "red"


def _range_status(val: float, green_lo: float, green_hi: float, yellow_lo: float, yellow_hi: float) -> str:
    if green_lo <= val <= green_hi:
        return "green"
    if yellow_lo <= val <= yellow_hi:
        return "yellow"
    return "red"


def _fv(count: int, total: int) -> str:
    pct = round((count / total) * 100.0, 1) if total > 0 else 0.0
    return f"{count}/{total} ({pct}%)"


def _poisson_xp_audit(xg_home: float, xg_away: float, match_id: int, n_sims: int = 10_000) -> tuple[float, float]:
    rng = np.random.default_rng(seed=match_id)
    home_goals = rng.poisson(lam=max(xg_home, 0.01), size=n_sims)
    away_goals = rng.poisson(lam=max(xg_away, 0.01), size=n_sims)
    home_wins = int(np.sum(home_goals > away_goals))
    away_wins = int(np.sum(away_goals > home_goals))
    draws = n_sims - home_wins - away_wins
    xp_home = (3 * home_wins + draws) / n_sims
    xp_away = (3 * away_wins + draws) / n_sims
    return round(xp_home, 3), round(xp_away, 3)


@router.get("/season/{season_id}/audit")
async def get_season_audit(season_id: int, admin=Depends(get_admin_user)):
    _ = admin
    sid = int(season_id)

    total_matches = await _db.db.matches_v3.count_documents({"season_id": sid})
    if total_matches == 0:
        raise HTTPException(status_code=404, detail="No matches found for this season.")

    status_cursor = _db.db.matches_v3.aggregate([
        {"$match": {"season_id": sid}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    status_counts = {str(r.get("_id") or "UNKNOWN"): int(r.get("n") or 0) async for r in status_cursor}
    finished_total = status_counts.get("FINISHED", 0)

    def _pct(count: int, total: int) -> float:
        return round((count / total) * 100.0, 1) if total > 0 else 0.0

    # ---- MODULE 1: Hardening & Deep Event Check ----
    events_covered = await _db.db.matches_v3.count_documents(
        {"season_id": sid, "status": "FINISHED", "events.0": {"$exists": True}}
    )
    period_scores_covered = await _db.db.matches_v3.count_documents({
        "season_id": sid, "status": "FINISHED",
        "scores.half_time.home": {"$ne": None}, "scores.half_time.away": {"$ne": None},
        "scores.full_time.home": {"$ne": None}, "scores.full_time.away": {"$ne": None},
    })
    finish_type_covered = await _db.db.matches_v3.count_documents(
        {"season_id": sid, "status": "FINISHED", "finish_type": {"$ne": None}}
    )
    short_code_covered = await _db.db.matches_v3.count_documents({
        "season_id": sid,
        "teams.home.short_code": {"$exists": True, "$ne": None, "$ne": ""},
        "teams.away.short_code": {"$exists": True, "$ne": None, "$ne": ""},
    })
    image_path_covered = await _db.db.matches_v3.count_documents({
        "season_id": sid,
        "teams.home.image_path": {"$exists": True, "$ne": None, "$ne": ""},
        "teams.away.image_path": {"$exists": True, "$ne": None, "$ne": ""},
    })

    evt_pct = _pct(events_covered, finished_total)
    ps_pct = _pct(period_scores_covered, finished_total)
    ft_pct = _pct(finish_type_covered, finished_total)
    sc_pct = _pct(short_code_covered, total_matches)
    ip_pct = _pct(image_path_covered, total_matches)

    # Event type distribution
    evt_dist_cursor = _db.db.matches_v3.aggregate([
        {"$match": {"season_id": sid, "status": "FINISHED"}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.type", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    event_type_dist = {str(r["_id"]): int(r["n"]) async for r in evt_dist_cursor}
    evt_types_present = len(event_type_dist)

    hardening_checks = [
        _audit_check("events_coverage", "Events coverage", _fv(events_covered, finished_total), _pct_status(evt_pct)),
        _audit_check("period_scores", "Period scores", _fv(period_scores_covered, finished_total), _pct_status(ps_pct)),
        _audit_check("finish_type", "finish_type", _fv(finish_type_covered, finished_total), _pct_status(ft_pct)),
        _audit_check("team_short_code", "Team short_code", _fv(short_code_covered, total_matches), _pct_status(sc_pct)),
        _audit_check("team_image_path", "Team image_path", _fv(image_path_covered, total_matches), _pct_status(ip_pct)),
        _audit_check(
            "event_types", "Event types found",
            f"{evt_types_present}/4",
            "green" if evt_types_present >= 4 else ("yellow" if evt_types_present >= 2 else "red"),
        ),
    ]

    # ---- MODULE 2: Justice-Intelligence Pre-Flight ----
    xg_covered = await _db.db.matches_v3.count_documents({
        "season_id": sid, "status": "FINISHED",
        "teams.home.xg": {"$ne": None}, "teams.away.xg": {"$ne": None},
    })
    odds_covered = await _db.db.matches_v3.count_documents({
        "season_id": sid, "status": "FINISHED",
        "odds_meta.summary_1x2.home.avg": {"$exists": True},
        "odds_meta.summary_1x2.draw.avg": {"$exists": True},
        "odds_meta.summary_1x2.away.avg": {"$exists": True},
    })

    xg_pct = _pct(xg_covered, finished_total)
    odds_pct = _pct(odds_covered, finished_total)

    # xG distribution
    xg_stats_cursor = _db.db.matches_v3.aggregate([
        {"$match": {"season_id": sid, "status": "FINISHED", "teams.home.xg": {"$ne": None}, "teams.away.xg": {"$ne": None}}},
        {"$project": {"xgs": ["$teams.home.xg", "$teams.away.xg"]}},
        {"$unwind": "$xgs"},
        {"$group": {"_id": None, "avg": {"$avg": "$xgs"}, "min": {"$min": "$xgs"}, "max": {"$max": "$xgs"}}},
    ])
    xg_stats_rows = await xg_stats_cursor.to_list(length=1)
    xg_avg = round(float(xg_stats_rows[0]["avg"]), 2) if xg_stats_rows else 0.0
    xg_min = round(float(xg_stats_rows[0]["min"]), 2) if xg_stats_rows else 0.0
    xg_max = round(float(xg_stats_rows[0]["max"]), 2) if xg_stats_rows else 0.0

    # Odds overround (sample)
    odds_sample = await _db.db.matches_v3.find(
        {"season_id": sid, "status": "FINISHED", "odds_meta.summary_1x2.home.avg": {"$exists": True}},
        {"odds_meta.summary_1x2": 1},
    ).limit(20).to_list(length=20)
    overrounds = []
    for row in odds_sample:
        s = (row.get("odds_meta") or {}).get("summary_1x2") or {}
        h = (s.get("home") or {}).get("avg")
        d = (s.get("draw") or {}).get("avg")
        a = (s.get("away") or {}).get("avg")
        if h and d and a and h > 0 and d > 0 and a > 0:
            overrounds.append(round(1 / h + 1 / d + 1 / a, 4))
    avg_overround = round(sum(overrounds) / len(overrounds), 4) if overrounds else 0.0

    # Timeline depth
    timeline_stats_cursor = _db.db.matches_v3.aggregate([
        {"$match": {"season_id": sid}},
        {"$project": {"len": {"$size": {"$ifNull": ["$odds_timeline", []]}}}},
        {"$group": {"_id": None, "max_len": {"$max": "$len"}, "avg_len": {"$avg": "$len"}}},
    ])
    tl_rows = await timeline_stats_cursor.to_list(length=1)
    tl_max = int(tl_rows[0]["max_len"]) if tl_rows else 0
    tl_avg = round(float(tl_rows[0]["avg_len"]), 1) if tl_rows else 0.0

    # Analysis-eligible leagues (cross-season)
    all_registries = await _db.db.league_registry_v3.find({}, {"_id": 1, "available_seasons": 1}).to_list(length=1000)
    eligible_leagues = 0
    for lg in all_registries:
        seasons = lg.get("available_seasons") or []
        if not seasons:
            continue
        current = max(seasons, key=lambda s: s.get("id") or 0)
        cs_id = current.get("id")
        if not isinstance(cs_id, int):
            continue
        xg_cnt = await _db.db.matches_v3.count_documents(
            {"league_id": int(lg["_id"]), "season_id": cs_id, "status": "FINISHED", "has_advanced_stats": True}
        )
        if xg_cnt >= 10:
            eligible_leagues += 1

    justice_checks = [
        _audit_check("xg_coverage", "xG coverage", _fv(xg_covered, finished_total), _pct_status(xg_pct, 70.0, 40.0)),
        _audit_check(
            "xg_distribution", "xG distribution",
            f"avg={xg_avg} min={xg_min} max={xg_max}",
            _range_status(xg_avg, 0.5, 2.5, 0.2, 4.0) if xg_avg > 0 else "red",
        ),
        _audit_check("odds_coverage", "Odds coverage", _fv(odds_covered, finished_total), _pct_status(odds_pct, 60.0, 30.0)),
        _audit_check(
            "odds_overround", "Odds overround",
            f"avg={avg_overround}" if avg_overround > 0 else "n/a",
            _range_status(avg_overround, 1.00, 1.10, 0.95, 1.20) if avg_overround > 0 else "red",
        ),
        _audit_check(
            "timeline_depth", "Timeline depth",
            f"max={tl_max} avg={tl_avg}",
            "green" if tl_max >= 5 else ("yellow" if tl_max >= 1 else "red"),
        ),
        _audit_check(
            "analysis_leagues", "Analysis-eligible leagues",
            str(eligible_leagues),
            "green" if eligible_leagues >= 3 else ("yellow" if eligible_leagues >= 1 else "red"),
        ),
    ]

    # ---- MODULE 3: Entity & Registry Integrity ----
    league_count = await _db.db.league_registry_v3.count_documents({})
    leagues_with_seasons = await _db.db.league_registry_v3.count_documents({"available_seasons.0": {"$exists": True}})
    teams_total = await _db.db.teams_v3.count_documents({})
    teams_unnamed = await _db.db.teams_v3.count_documents(
        {"$or": [{"name": {"$exists": False}}, {"name": None}, {"name": ""}]}
    )
    unnamed_pct = _pct(teams_unnamed, teams_total) if teams_total > 0 else 0.0
    persons_cursor = _db.db.persons.aggregate([
        {"$group": {"_id": "$type", "n": {"$sum": 1}}},
    ])
    persons_by_type = {str(r["_id"]): int(r["n"]) async for r in persons_cursor}
    persons_total = sum(persons_by_type.values())

    team_links_missing = await _db.db.matches_v3.count_documents({
        "season_id": sid,
        "$or": [{"teams.home.sm_id": {"$exists": False}}, {"teams.away.sm_id": {"$exists": False}}],
    })

    # CDN image_path spot check
    img_samples = await _db.db.matches_v3.find(
        {"season_id": sid, "teams.home.image_path": {"$exists": True, "$ne": None}},
        {"teams.home.image_path": 1, "teams.away.image_path": 1},
    ).limit(5).to_list(length=5)
    cdn_pattern = re.compile(r"^https?://")
    valid_urls = 0
    total_urls = 0
    for row in img_samples:
        for side in ["home", "away"]:
            url = ((row.get("teams") or {}).get(side) or {}).get("image_path")
            if url:
                total_urls += 1
                if cdn_pattern.match(str(url)):
                    valid_urls += 1

    entity_checks = [
        _audit_check("league_registry", "League registry", str(league_count), "green" if league_count >= 5 else ("yellow" if league_count >= 1 else "red")),
        _audit_check("leagues_with_seasons", "Leagues with seasons", str(leagues_with_seasons), "green" if leagues_with_seasons >= 5 else ("yellow" if leagues_with_seasons >= 1 else "red")),
        _audit_check("teams_v3_health", "teams_v3 health", f"{teams_total} total, {teams_unnamed} unnamed ({unnamed_pct}%)", _pct_status(100.0 - unnamed_pct, 95.0, 80.0)),
        _audit_check("persons", "Persons", f"{persons_total} ({', '.join(f'{t}:{n}' for t, n in persons_by_type.items())})", "green" if persons_total >= 100 else ("yellow" if persons_total >= 10 else "red")),
        _audit_check("team_sm_id_linkage", "Team sm_id linkage", f"{team_links_missing} missing", _count_status(team_links_missing, 0, 5)),
        _audit_check("cdn_image_path", "CDN image_path", f"{valid_urls}/{total_urls} valid URLs", "green" if total_urls > 0 and valid_urls == total_urls else ("yellow" if valid_urls > 0 else "red")),
    ]

    # ---- MODULE 4: Data Guard & Anomaly Report ----
    manual_check_count = await _db.db.matches_v3.count_documents({"season_id": sid, "manual_check_required": True})

    reasons_cursor = _db.db.matches_v3.aggregate([
        {"$match": {"season_id": sid, "manual_check_reasons.0": {"$exists": True}}},
        {"$unwind": "$manual_check_reasons"},
        {"$group": {"_id": "$manual_check_reasons", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    reasons_breakdown = {str(r["_id"]): int(r["n"]) async for r in reasons_cursor}

    # Orphan season_ids
    all_season_ids_in_registry: set[int] = set()
    for lg in all_registries:
        for s in (lg.get("available_seasons") or []):
            if isinstance(s.get("id"), int):
                all_season_ids_in_registry.add(s["id"])
    distinct_season_ids = await _db.db.matches_v3.distinct("season_id")
    orphan_seasons = [s for s in distinct_season_ids if isinstance(s, int) and s not in all_season_ids_in_registry]

    flagged_matches = await _db.db.matches_v3.find(
        {"season_id": sid, "manual_check_required": True},
        {"_id": 1, "status": 1, "start_at": 1, "teams.home.name": 1, "teams.away.name": 1,
         "teams.home.score": 1, "teams.away.score": 1, "manual_check_reasons": 1},
    ).sort("start_at", -1).to_list(length=20)
    flagged_items = [
        {
            "match_id": int(r["_id"]),
            "home": str((r.get("teams") or {}).get("home", {}).get("name") or ""),
            "away": str((r.get("teams") or {}).get("away", {}).get("name") or ""),
            "status": str(r.get("status") or ""),
            "start_at": ensure_utc(r.get("start_at")).isoformat() if r.get("start_at") else None,
            "score": {
                "home": (r.get("teams") or {}).get("home", {}).get("score"),
                "away": (r.get("teams") or {}).get("away", {}).get("score"),
            },
            "reasons": r.get("manual_check_reasons") or [],
        }
        for r in flagged_matches
    ]

    guard_checks = [
        _audit_check("manual_check_flags", "Manual check flags", str(manual_check_count), _count_status(manual_check_count, 0, 10)),
        _audit_check("orphan_season_ids", "Orphan season_ids", str(len(orphan_seasons)), _count_status(len(orphan_seasons), 0, 3),
                      detail=", ".join(str(s) for s in orphan_seasons[:10]) if orphan_seasons else None),
        _audit_check("status_distribution", "Status distribution",
                      ", ".join(f"{k}:{v}" for k, v in status_counts.items()),
                      "green"),
    ]

    # ---- MODULE 5: Aggregation Performance Preview ----
    # Find the league_id for this season
    league_for_season = await _db.db.matches_v3.find_one({"season_id": sid}, {"league_id": 1})
    league_id = int(league_for_season["league_id"]) if league_for_season else None

    perf_checks: list[dict[str, Any]] = []
    if league_id is not None:
        t0 = time.monotonic()
        xg_rows = await _db.db.matches_v3.find(
            {"league_id": league_id, "season_id": sid, "status": "FINISHED", "has_advanced_stats": True},
            {"_id": 1, "teams": 1},
        ).sort("start_at", 1).to_list(length=5000)

        sum_xp = 0.0
        sum_real = 0
        team_ids: set[int] = set()
        for doc in xg_rows:
            teams = doc.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            xg_h, xg_a = home.get("xg"), away.get("xg")
            sc_h, sc_a = home.get("score"), away.get("score")
            h_id, a_id = home.get("sm_id"), away.get("sm_id")
            if not all(isinstance(v, (int, float)) for v in [xg_h, xg_a]):
                continue
            if not all(isinstance(v, int) for v in [sc_h, sc_a]):
                continue
            xp_h, xp_a = _poisson_xp_audit(float(xg_h), float(xg_a), int(doc["_id"]))
            sum_xp += xp_h + xp_a
            if sc_h > sc_a:
                sum_real += 3
            elif sc_a > sc_h:
                sum_real += 3
            else:
                sum_real += 2
            if h_id is not None:
                team_ids.add(int(h_id))
            if a_id is not None:
                team_ids.add(int(a_id))

        elapsed_ms = round((time.monotonic() - t0) * 1000)
        team_count = len(team_ids)
        xp_deviation = abs(sum_xp - sum_real) / sum_real if sum_real > 0 else 0.0

        perf_checks = [
            _audit_check("unjust_timing", "Unjust table dry-run", f"{elapsed_ms}ms ({len(xg_rows)} matches)",
                          "green" if elapsed_ms < 2000 else ("yellow" if elapsed_ms < 5000 else "red")),
            _audit_check("team_count", "Team count", str(team_count),
                          "green" if 15 <= team_count <= 25 else ("yellow" if 10 <= team_count <= 30 else "red")),
            _audit_check("xp_consistency", "xP vs real points deviation",
                          f"{round(xp_deviation * 100, 1)}%",
                          "green" if xp_deviation < 0.10 else ("yellow" if xp_deviation < 0.20 else "red")),
        ]
    else:
        perf_checks = [
            _audit_check("unjust_timing", "Unjust table dry-run", "n/a (no league found)", "red"),
        ]

    # ---- Build response ----
    modules = {
        "hardening": {"checks": hardening_checks, "extras": {"event_type_distribution": event_type_dist}},
        "justice_preflight": {"checks": justice_checks, "extras": {"overround_samples": len(overrounds)}},
        "entity_integrity": {"checks": entity_checks, "extras": {"persons_by_type": persons_by_type}},
        "data_guard": {"checks": guard_checks, "extras": {
            "check_reasons_breakdown": reasons_breakdown,
            "flagged_matches": flagged_items,
            "status_distribution": status_counts,
        }},
        "performance": {"checks": perf_checks, "extras": {}},
    }

    all_checks = []
    for mod in modules.values():
        all_checks.extend(mod["checks"])
    summary = {
        "green": sum(1 for c in all_checks if c["status"] == "green"),
        "yellow": sum(1 for c in all_checks if c["status"] == "yellow"),
        "red": sum(1 for c in all_checks if c["status"] == "red"),
        "total": len(all_checks),
    }

    return {
        "season_id": sid,
        "generated_at": utcnow().isoformat(),
        "summary": summary,
        "modules": modules,
    }


@router.get("/ops/snapshot")
async def get_ops_snapshot(admin=Depends(get_admin_user)):
    _ = admin
    now = utcnow()
    rate_meta = await _db.db.meta.find_one({"_id": _RATE_META_ID})
    remaining = int(rate_meta.get("remaining")) if isinstance(rate_meta, dict) and rate_meta.get("remaining") is not None else None
    reset_at = rate_meta.get("reset_at") if isinstance(rate_meta, dict) else None

    queued = await _db.db.admin_import_jobs.count_documents(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": "queued"}
    )
    running = await _db.db.admin_import_jobs.count_documents(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": "running"}
    )
    paused = await _db.db.admin_import_jobs.count_documents(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": "paused"}
    )
    active_docs = await _db.db.admin_import_jobs.find(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": {"$in": ["queued", "running", "paused"]}},
        {"_id": 1, "type": 1, "season_id": 1, "status": 1, "phase": 1, "updated_at": 1, "updated_at_utc": 1, "progress": 1},
    ).sort("updated_at_utc", -1).to_list(length=50)
    active_items = []
    stale_minutes = int(settings.SPORTMONKS_STALE_JOB_MINUTES)
    for doc in active_docs:
        updated_at = _read_updated_at(doc)
        is_stale = bool(updated_at is not None and (now - updated_at) > timedelta(minutes=stale_minutes))
        active_items.append(
            {
                "job_id": str(doc["_id"]),
                "type": str(doc.get("type") or ""),
                "season_id": int(doc.get("season_id") or 0),
                "status": str(doc.get("status") or ""),
                "phase": str(doc.get("phase") or ""),
                "progress": doc.get("progress") or {"processed": 0, "total": 0, "percent": 0.0},
                "updated_at": updated_at.isoformat() if updated_at else None,
                "is_stale": is_stale,
            }
        )

    metrics_job = await _db.db.admin_import_jobs.find_one(
        {"type": _JOB_TYPE_METRICS, "status": {"$in": ["running", "queued", "paused"]}},
        {"results": 1, "season_id": 1, "updated_at": 1, "updated_at_utc": 1},
        sort=[("updated_at_utc", -1)],
    )
    if not metrics_job:
        metrics_job = await _db.db.admin_import_jobs.find_one(
            {"type": _JOB_TYPE_METRICS, "status": {"$in": ["succeeded", "failed"]}},
            {"results": 1, "season_id": 1, "updated_at": 1, "updated_at_utc": 1},
            sort=[("updated_at", -1)],
        )
    results = (metrics_job or {}).get("results") if isinstance(metrics_job, dict) else {}
    total_fixtures = int((results or {}).get("total_fixtures") or 0)
    bulk_round_calls = int((results or {}).get("bulk_round_calls") or 0)
    repair_calls = int((results or {}).get("repair_calls") or 0)
    saved_calls_estimate = int((results or {}).get("saved_calls_estimate") or max(0, total_fixtures - (bulk_round_calls + repair_calls)))
    api_savings_ratio = float((results or {}).get("api_savings_ratio") or ((saved_calls_estimate / total_fixtures) if total_fixtures > 0 else 0.0))
    cache_meta = await _db.db.meta.find_one({"_id": _CACHE_METRICS_META_ID})
    cache_hits = int(cache_meta.get("hits") or 0) if isinstance(cache_meta, dict) else 0
    cache_misses = int(cache_meta.get("misses") or 0) if isinstance(cache_meta, dict) else 0
    cache_total = cache_hits + cache_misses
    guard_meta = await _db.db.meta.find_one({"_id": _GUARD_METRICS_META_ID})
    guard_blocks = int(guard_meta.get("page_guard_blocks") or 0) if isinstance(guard_meta, dict) else 0
    runtime_timeouts = int(guard_meta.get("runtime_timeouts") or 0) if isinstance(guard_meta, dict) else 0
    cache_entries_active = await _db.db.sportmonks_page_cache.count_documents({"expires_at": {"$gt": now}})

    return {
        "api_health": {
            "remaining": remaining,
            "reset_at": reset_at,
            "reserve_credits": int(settings.SPORTMONKS_RESERVE_CREDITS),
        },
        "queue_metrics": {
            "queued": int(queued),
            "running": int(running),
            "paused": int(paused),
            "active_by_type": {
                _JOB_TYPE: sum(1 for doc in active_docs if str(doc.get("type")) == _JOB_TYPE),
                _JOB_TYPE_METRICS: sum(1 for doc in active_docs if str(doc.get("type")) == _JOB_TYPE_METRICS),
            },
        },
        "efficiency": {
            "total_fixtures": total_fixtures,
            "bulk_round_calls": bulk_round_calls,
            "repair_calls": repair_calls,
            "saved_calls_estimate": saved_calls_estimate,
            "api_savings_ratio": round(api_savings_ratio, 4),
        },
        "cache_metrics": {
            "hits": cache_hits,
            "misses": cache_misses,
            "hit_ratio": round((cache_hits / cache_total), 4) if cache_total > 0 else 0.0,
            "entries_active": int(cache_entries_active),
        },
        "guard_metrics": {
            "page_guard_blocks": guard_blocks,
            "runtime_timeouts": runtime_timeouts,
        },
        "active_jobs": active_items,
        "generated_at": now.isoformat(),
    }


@router.get("/jobs/active")
async def list_active_jobs(admin=Depends(get_admin_user)):
    _ = admin
    docs = await _db.db.admin_import_jobs.find(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": {"$in": ["queued", "running", "paused"]}},
        {"_id": 1, "type": 1, "status": 1, "season_id": 1, "updated_at": 1, "updated_at_utc": 1, "processed_rounds": 1, "total_rounds": 1},
    ).sort("updated_at_utc", -1).to_list(length=200)
    items = []
    for doc in docs:
        total = int(doc.get("total_rounds") or 0)
        processed = int(doc.get("processed_rounds") or 0)
        items.append(
            {
                "job_id": str(doc["_id"]),
                "type": str(doc.get("type") or ""),
                "season_id": int(doc.get("season_id") or 0),
                "status": str(doc.get("status") or ""),
                "processed_rounds": processed,
                "total_rounds": total,
                "progress_percent": round((processed / total) * 100.0, 2) if total > 0 else 0.0,
                "updated_at": (_read_updated_at(doc).isoformat() if _read_updated_at(doc) else None),
            }
        )
    return {"items": items}


@router.get("/jobs/{job_id}")
async def get_ingest_job(job_id: str, admin=Depends(get_admin_user)):
    _ = admin
    try:
        oid = ObjectId(job_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid job_id.") from exc
    doc = await _db.db.admin_import_jobs.find_one({"_id": oid, "type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}})
    if not doc:
        raise HTTPException(status_code=404, detail="Ingest job not found.")
    now = utcnow()
    stale_minutes = int(settings.SPORTMONKS_STALE_JOB_MINUTES)
    started_at = ensure_utc(doc.get("started_at")) if doc.get("started_at") else None
    updated_at = _read_updated_at(doc)
    is_stale = bool(
        doc.get("status") == "running"
        and updated_at is not None
        and (now - updated_at) > timedelta(minutes=stale_minutes)
    )
    can_retry = bool(doc.get("status") == "failed" or is_stale)
    progress = doc.get("progress") if isinstance(doc.get("progress"), dict) else {}
    processed = int(progress.get("processed", doc.get("processed_rounds", 0)) or 0)
    total = int(progress.get("total", doc.get("total_rounds", 0)) or 0)
    progress_percent = round((processed / total) * 100.0, 2) if total > 0 else 0.0
    elapsed_seconds = int((now - started_at).total_seconds()) if started_at else None
    throughput_per_min = None
    eta_seconds = None
    if elapsed_seconds is not None and elapsed_seconds > 0 and processed > 0 and total > processed:
        throughput_per_min = round((processed / elapsed_seconds) * 60.0, 2)
        if throughput_per_min > 0:
            eta_seconds = int(((total - processed) / throughput_per_min) * 60.0)
    heartbeat_age_seconds = int((now - updated_at).total_seconds()) if updated_at else None
    error_log = doc.get("error_log", [])
    error_summary = _error_summary(error_log)
    return {
        "job_id": str(doc["_id"]),
        "type": str(doc.get("type") or ""),
        "status": str(doc.get("status") or "queued"),
        "phase": str(doc.get("phase") or "queued"),
        "season_id": int(doc.get("season_id") or 0),
        "total_rounds": int(doc.get("total_rounds") or total),
        "processed_rounds": int(doc.get("processed_rounds") or processed),
        "current_round_name": doc.get("current_round_name"),
        "rate_limit_remaining": doc.get("rate_limit_remaining"),
        "rate_limit_reset_at": doc.get("rate_limit_reset_at"),
        "rate_limit_paused": bool(doc.get("rate_limit_paused", False)),
        "progress": {"processed": processed, "total": total, "percent": progress_percent},
        "error": doc.get("error"),
        "error_log": error_log,
        "error_summary": error_summary,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "throughput_per_min": throughput_per_min,
        "eta_seconds": eta_seconds,
        "pages_processed": int(doc.get("pages_processed") or 0),
        "pages_total": doc.get("pages_total"),
        "rows_processed": int(doc.get("rows_processed") or 0),
        "timeout_at": ensure_utc(doc.get("timeout_at")).isoformat() if doc.get("timeout_at") else None,
        "max_runtime_minutes": int(doc.get("max_runtime_minutes") or 0) or None,
        "page_requests_total": int(doc.get("page_requests_total") or 0),
        "duplicate_page_blocks": int(doc.get("duplicate_page_blocks") or 0),
        "is_stale": is_stale,
        "can_retry": can_retry,
        "created_at": ensure_utc(doc.get("created_at")).isoformat() if doc.get("created_at") else None,
        "started_at": started_at.isoformat() if started_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "finished_at": ensure_utc(doc.get("finished_at")).isoformat() if doc.get("finished_at") else None,
        "results": doc.get("results"),
    }


@router.post("/jobs/{job_id}/resume")
async def resume_ingest_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    admin=Depends(get_admin_user),
):
    _ = admin
    try:
        oid = ObjectId(job_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid job_id.") from exc

    doc = await _db.db.admin_import_jobs.find_one(
        {"_id": oid, "type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}},
        {"_id": 1, "type": 1, "status": 1, "season_id": 1, "updated_at": 1, "updated_at_utc": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Ingest job not found.")

    now = utcnow()
    stale_minutes = int(settings.SPORTMONKS_STALE_JOB_MINUTES)
    updated_at = _read_updated_at(doc)
    is_stale = bool(
        doc.get("status") == "running"
        and updated_at is not None
        and (now - updated_at) > timedelta(minutes=stale_minutes)
    )
    status_value = str(doc.get("status") or "")
    if status_value == "running" and not is_stale:
        raise HTTPException(status_code=409, detail="Job is currently running.")
    if status_value == "queued":
        raise HTTPException(status_code=409, detail="Job is already queued.")
    if status_value == "succeeded":
        raise HTTPException(status_code=409, detail="Job already succeeded. Start a new run instead.")

    job_type = str(doc.get("type") or "")
    season_id = int(doc.get("season_id") or 0)
    active_other = await _db.db.admin_import_jobs.find_one(
        {
            "type": job_type,
            "season_id": season_id,
            "_id": {"$ne": oid},
            "status": {"$in": ["queued", "running", "paused"]},
        },
        {"_id": 1, "status": 1},
    )
    if active_other:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "Another active job exists for this season.",
                "active_job_id": str(active_other["_id"]),
                "season_id": season_id,
                "status": str(active_other.get("status") or ""),
            },
        )

    await _db.db.admin_import_jobs.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "queued",
                "phase": "queued",
                "active_lock": True,
                "rate_limit_paused": False,
                "error": None,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
                "updated_at_utc": now,
            },
            "$inc": {"retry_count": 1},
        },
    )
    if job_type == _JOB_TYPE_METRICS:
        background_tasks.add_task(_run_sportmonks_metrics_job, oid, season_id)
    else:
        background_tasks.add_task(_run_sportmonks_ingest_job, oid, season_id)
    return {
        "accepted": True,
        "job_id": str(oid),
        "season_id": season_id,
        "status": "queued",
        "resumed": True,
    }
