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

from datetime import timedelta
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.services.sportmonks_connector import sportmonks_connector
from app.utils import ensure_utc, utcnow

router = APIRouter(prefix="/api/admin/ingest", tags=["admin-ingest"])

_JOB_TYPE = "sportmonks_deep_ingest"
_JOB_TYPE_METRICS = "sportmonks_metrics_sync"
_RATE_META_ID = "sportmonks_rate_limit_state"


def _serialize_discovery_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "league_id": int(row.get("_id")),
                "name": str(row.get("name") or ""),
                "country": str(row.get("country") or ""),
                "is_cup": bool(row.get("is_cup", False)),
                "available_seasons": row.get("available_seasons") or [],
                "last_synced_at": (
                    ensure_utc(row.get("last_synced_at")).isoformat()
                    if row.get("last_synced_at")
                    else None
                ),
            }
        )
    return out


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
            {"$set": {"remaining": int(remaining), "reset_at": reset_at, "updated_at": now}},
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
        {"$set": {"remaining": remaining, "reset_at": reset_at, "updated_at": now}},
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
        "error_log": [],
        "retry_count": 0,
        "error": None,
        "results": None,
        "admin_id": admin_id,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
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
        {"_id": 1, "type": 1, "season_id": 1, "status": 1, "phase": 1, "updated_at": 1, "progress": 1},
    ).sort("updated_at", -1).to_list(length=50)
    active_items = []
    stale_minutes = int(settings.SPORTMONKS_STALE_JOB_MINUTES)
    for doc in active_docs:
        updated_at = ensure_utc(doc.get("updated_at")) if doc.get("updated_at") else None
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
        {"results": 1, "season_id": 1, "updated_at": 1},
        sort=[("updated_at", -1)],
    )
    if not metrics_job:
        metrics_job = await _db.db.admin_import_jobs.find_one(
            {"type": _JOB_TYPE_METRICS, "status": {"$in": ["succeeded", "failed"]}},
            {"results": 1, "season_id": 1, "updated_at": 1},
            sort=[("updated_at", -1)],
        )
    results = (metrics_job or {}).get("results") if isinstance(metrics_job, dict) else {}
    total_fixtures = int((results or {}).get("total_fixtures") or 0)
    bulk_round_calls = int((results or {}).get("bulk_round_calls") or 0)
    repair_calls = int((results or {}).get("repair_calls") or 0)
    saved_calls_estimate = int((results or {}).get("saved_calls_estimate") or max(0, total_fixtures - (bulk_round_calls + repair_calls)))
    api_savings_ratio = float((results or {}).get("api_savings_ratio") or ((saved_calls_estimate / total_fixtures) if total_fixtures > 0 else 0.0))

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
        "active_jobs": active_items,
        "generated_at": now.isoformat(),
    }


@router.get("/jobs/active")
async def list_active_jobs(admin=Depends(get_admin_user)):
    _ = admin
    docs = await _db.db.admin_import_jobs.find(
        {"type": {"$in": [_JOB_TYPE, _JOB_TYPE_METRICS]}, "status": {"$in": ["queued", "running", "paused"]}},
        {"_id": 1, "type": 1, "status": 1, "season_id": 1, "updated_at": 1, "processed_rounds": 1, "total_rounds": 1},
    ).sort("updated_at", -1).to_list(length=200)
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
                "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
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
    updated_at = ensure_utc(doc.get("updated_at")) if doc.get("updated_at") else None
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
        "rate_limit_paused": bool(doc.get("rate_limit_paused", False)),
        "progress": {"processed": processed, "total": total, "percent": progress_percent},
        "error": doc.get("error"),
        "error_log": doc.get("error_log", []),
        "is_stale": is_stale,
        "can_retry": can_retry,
        "created_at": ensure_utc(doc.get("created_at")).isoformat() if doc.get("created_at") else None,
        "started_at": ensure_utc(doc.get("started_at")).isoformat() if doc.get("started_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
        "finished_at": ensure_utc(doc.get("finished_at")).isoformat() if doc.get("finished_at") else None,
        "results": doc.get("results"),
    }
