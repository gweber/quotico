"""
backend/app/routers/admin.py

Purpose:
    Admin HTTP router for operational controls across users, matches, workers,
    Team Tower, League Tower, and Qbot tooling.

Dependencies:
    - app.services.auth_service
    - app.services.audit_service
    - app.services.admin_service
    - app.services.league_service
"""

import csv
import io
import logging
import re
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.services.alias_service import generate_default_alias
from app.services.admin_service import (
    cleanup_same_day_duplicate_matches,
    list_same_day_duplicate_matches,
    merge_teams,
)
from app.services.auth_service import get_admin_user, invalidate_user_tokens
from app.services.audit_service import log_audit
from app.services.league_service import (
    LeagueRegistry,
    default_current_season_for_sport,
    default_season_start_month_for_sport,
    invalidate_navigation_cache,
    seed_core_leagues,
    update_league_order,
)
from app.services.qbot_backtest_service import simulate_strategy_backtest
from app.services.football_data_service import import_football_data_stats
from app.services.football_data_org_service import import_season as import_football_data_org_season
from app.services.openligadb_service import import_season as import_openligadb_season
from app.services.xg_enrichment_service import (
    enrich_matches as enrich_xg_matches,
    list_xg_target_sport_keys,
    parse_season_spec as parse_xg_season_spec,
)
from app.services.event_bus import event_bus
from app.services.event_bus_monitor import event_bus_monitor
from app.services.provider_settings_service import provider_settings_service
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.providers.odds_api import odds_provider
from app.utils import ensure_utc, utcnow
from app.workers._state import get_synced_at, get_worker_state

logger = logging.getLogger("quotico.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])
FOOTBALL_DATA_IMPORT_RATE_LIMIT_SECONDS = 10
ADMIN_MATCHES_CACHE_TTL_SECONDS = 30
_ADMIN_MATCHES_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}


def _admin_matches_cache_invalidate() -> None:
    _ADMIN_MATCHES_CACHE.clear()


def _admin_matches_cache_key(
    *,
    page: int,
    page_size: int,
    league_id: str | None,
    status_filter: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    needs_review: bool | None,
    odds_available: bool | None,
    search: str | None,
) -> str:
    return "|".join(
        [
            f"p={page}",
            f"ps={page_size}",
            f"l={league_id or ''}",
            f"s={status_filter or ''}",
            f"df={ensure_utc(date_from).isoformat() if date_from else ''}",
            f"dt={ensure_utc(date_to).isoformat() if date_to else ''}",
            f"nr={needs_review if needs_review is not None else ''}",
            f"oa={odds_available if odds_available is not None else ''}",
            f"q={(search or '').strip().lower()}",
        ]
    )


# --- Request models ---

class PointsAdjust(BaseModel):
    delta: float
    reason: str


class ResultOverride(BaseModel):
    result: str  # "1", "X", "2"
    home_score: int
    away_score: int


class BattleCreateAdmin(BaseModel):
    squad_a_id: str
    squad_b_id: str
    start_time: datetime
    end_time: datetime


# --- Dashboard ---

@router.get("/stats")
async def admin_stats(admin=Depends(get_admin_user)):
    """Admin dashboard stats."""
    now = utcnow()

    user_count = await _db.db.users.count_documents({"is_deleted": False})
    active_today = await _db.db.betting_slips.count_documents({
        "submitted_at": {"$gte": now.replace(hour=0, minute=0, second=0)},
    })
    total_bets = await _db.db.betting_slips.count_documents({})
    total_matches = await _db.db.matches.count_documents({})
    pending_matches = await _db.db.matches.count_documents({"status": {"$in": ["scheduled", "live"]}})
    completed_matches = await _db.db.matches.count_documents({"status": "final"})
    squad_count = await _db.db.squads.count_documents({})
    battle_count = await _db.db.battles.count_documents({})
    banned_count = await _db.db.users.count_documents({"is_banned": True})

    return {
        "users": {
            "total": user_count,
            "banned": banned_count,
        },
        "bets": {
            "total": total_bets,
            "today": active_today,
        },
        "matches": {
            "total": total_matches,
            "pending": pending_matches,
            "completed": completed_matches,
        },
        "squads": squad_count,
        "battles": battle_count,
        "api_usage": await odds_provider.load_usage(),
        "circuit_open": odds_provider.circuit_open,
    }


# --- Provider Status ---

# Worker definitions: id -> (label, provider, import path)
_WORKER_REGISTRY: dict[str, dict] = {
    "odds_poller": {"label": "Odds Poller", "provider": "odds_api"},
    "calibration_eval": {"label": "Calibration: Daily Eval", "provider": None},
    "calibration_refine": {"label": "Calibration: Weekly Refine", "provider": None},
    "calibration_explore": {"label": "Calibration: Monthly Explore", "provider": None},
    "reliability_check": {"label": "Reliability Check", "provider": None},
    "match_resolver": {"label": "Match Resolver", "provider": "multiple"},
    "matchday_sync": {"label": "Matchday Sync", "provider": "multiple"},
    "leaderboard": {"label": "Leaderboard", "provider": None},
    "badge_engine": {"label": "Badge Engine", "provider": None},
    "matchday_resolver": {"label": "Matchday Resolver", "provider": "multiple"},
    "matchday_leaderboard": {"label": "Matchday Leaderboard", "provider": None},
    "bankroll_resolver": {"label": "Bankroll Resolver", "provider": None},
    "survivor_resolver": {"label": "Survivor Resolver", "provider": None},
    "over_under_resolver": {"label": "Over/Under Resolver", "provider": None},
    "fantasy_resolver": {"label": "Fantasy Resolver", "provider": None},
    "parlay_resolver": {"label": "Parlay Resolver", "provider": None},
    "wallet_maintenance": {"label": "Wallet Maintenance", "provider": None},
    "quotico_tip_worker": {"label": "QuoticoTip Engine", "provider": None},
}

# Workers that can be triggered manually
_TRIGGERABLE_WORKERS = {
    "odds_poller", "match_resolver", "matchday_sync",
    "leaderboard", "matchday_resolver", "matchday_leaderboard",
    "quotico_tip_worker",
    "calibration_eval", "calibration_refine", "calibration_explore", "reliability_check"
}

_SUPPORTED_PROVIDER_SETTINGS = {
    "theoddsapi",
    "football_data",
    "openligadb",
    "football_data_uk",
    "understat",
}


@router.get("/provider-status")
async def provider_status(admin=Depends(get_admin_user)):
    """Aggregated status of all providers and background workers."""
    from app.main import scheduler, automation_enabled, automated_job_count

    # Provider health
    usage = await odds_provider.load_usage()
    providers = {
        "odds_api": {
            "label": "TheOddsAPI",
            "status": "circuit_open" if odds_provider.circuit_open else "ok",
            "requests_used": usage.get("requests_used"),
            "requests_remaining": usage.get("requests_remaining"),
        },
        "football_data": {"label": "football-data.org", "status": "ok"},
        "openligadb": {"label": "OpenLigaDB", "status": "ok"},
        "espn": {"label": "ESPN", "status": "ok"},
    }

    # Worker state from DB + scheduler
    jobs_by_id = {job.id: job for job in scheduler.get_jobs()}
    workers = []
    for wid, meta in _WORKER_REGISTRY.items():
        state = await get_worker_state(wid)
        last = state["synced_at"] if state else None
        job = jobs_by_id.get(wid)
        workers.append({
            "id": wid,
            "label": meta["label"],
            "provider": meta["provider"],
            "triggerable": wid in _TRIGGERABLE_WORKERS,
            "last_synced": ensure_utc(last).isoformat() if last else None,
            "last_metrics": state.get("last_metrics") if state else None,
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        })

    return {
        "providers": providers,
        "workers": workers,
        "automated_workers_enabled": automation_enabled(),
        "automated_workers_scheduled_jobs": automated_job_count(),
        "scheduler_running": bool(scheduler.running),
    }


class ProviderSettingsPatchBody(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    base_delay_seconds: Optional[float] = None
    rate_limit_rpm: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    headers_override: Optional[dict[str, str]] = None
    extra: Optional[dict[str, Any]] = None


class ProviderSecretSetBody(BaseModel):
    scope: str = "global"
    league_id: Optional[str] = None
    api_key: str


class ProviderSecretClearBody(BaseModel):
    scope: str = "global"
    league_id: Optional[str] = None


class ProviderProbeBody(BaseModel):
    sport_key: Optional[str] = None
    league_id: Optional[str] = None


def _ensure_supported_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized not in _SUPPORTED_PROVIDER_SETTINGS:
        raise HTTPException(status_code=400, detail="Unsupported provider.")
    return normalized


def _mask_effective(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    effective = dict(safe.get("effective_config") or {})
    effective.pop("api_key", None)
    safe["effective_config"] = effective
    return safe


@router.get("/provider-settings")
async def list_provider_settings(
    league_id: Optional[str] = None,
    sport_key: Optional[str] = None,
    admin=Depends(get_admin_user),
):
    """List effective runtime provider settings."""
    items: list[dict[str, Any]] = []
    for provider in sorted(_SUPPORTED_PROVIDER_SETTINGS):
        resolved = await provider_settings_service.get_effective(
            provider,
            league_id=league_id,
            sport_key=sport_key,
            include_secret=True,
        )
        items.append(_mask_effective(resolved))
    return {
        "items": items,
        "league_id": league_id,
        "sport_key": sport_key,
    }


@router.get("/provider-settings/{provider}")
async def get_provider_settings(
    provider: str,
    league_id: Optional[str] = None,
    sport_key: Optional[str] = None,
    admin=Depends(get_admin_user),
):
    """Get effective runtime settings for one provider."""
    normalized = _ensure_supported_provider(provider)
    resolved = await provider_settings_service.get_effective(
        normalized,
        league_id=league_id,
        sport_key=sport_key,
        include_secret=True,
    )
    return _mask_effective(resolved)


@router.patch("/provider-settings/{provider}/global")
async def patch_provider_settings_global(
    provider: str,
    body: ProviderSettingsPatchBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Update global non-secret provider settings."""
    normalized = _ensure_supported_provider(provider)
    admin_id = str(admin["_id"])
    patch = body.model_dump(exclude_none=True)
    result = await provider_settings_service.set_settings(
        normalized,
        patch,
        scope="global",
        actor_id=admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=f"provider:{normalized}:global",
        action="PROVIDER_SETTINGS_UPDATE",
        metadata={
            "provider": normalized,
            "scope": "global",
            "league_id": None,
            "changed_fields": result.get("updated_fields", []),
        },
        request=request,
    )
    return {"ok": True, **result}


@router.patch("/provider-settings/{provider}/leagues/{league_id}")
async def patch_provider_settings_league(
    provider: str,
    league_id: str,
    body: ProviderSettingsPatchBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Update league-scoped non-secret provider overrides."""
    normalized = _ensure_supported_provider(provider)
    admin_id = str(admin["_id"])
    patch = body.model_dump(exclude_none=True)
    result = await provider_settings_service.set_settings(
        normalized,
        patch,
        scope="league",
        league_id=league_id,
        actor_id=admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=f"provider:{normalized}:league:{league_id}",
        action="PROVIDER_SETTINGS_UPDATE",
        metadata={
            "provider": normalized,
            "scope": "league",
            "league_id": league_id,
            "changed_fields": result.get("updated_fields", []),
        },
        request=request,
    )
    return {"ok": True, **result}


@router.put("/provider-settings/{provider}/secret")
async def set_provider_secret(
    provider: str,
    body: ProviderSecretSetBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Set or rotate encrypted provider API key (DB-first)."""
    normalized = _ensure_supported_provider(provider)
    if not str(body.api_key or "").strip():
        raise HTTPException(status_code=400, detail="api_key is required.")
    admin_id = str(admin["_id"])
    result = await provider_settings_service.set_secret(
        normalized,
        api_key=str(body.api_key).strip(),
        scope=body.scope,
        league_id=body.league_id,
        actor_id=admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=f"provider:{normalized}:{body.scope}:{body.league_id or 'global'}",
        action="PROVIDER_SECRET_SET",
        metadata={
            "provider": normalized,
            "scope": body.scope,
            "league_id": body.league_id,
            "key_version": result.get("key_version"),
        },
        request=request,
    )
    return {
        "provider": normalized,
        "scope": result["scope"],
        "league_id": result["league_id"],
        "configured": True,
        "key_version": result["key_version"],
        "updated_at": ensure_utc(result["updated_at"]).isoformat() if result.get("updated_at") else None,
    }


@router.delete("/provider-settings/{provider}/secret")
async def clear_provider_secret(
    provider: str,
    body: ProviderSecretClearBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Remove encrypted provider API key override."""
    normalized = _ensure_supported_provider(provider)
    admin_id = str(admin["_id"])
    result = await provider_settings_service.clear_secret(
        normalized,
        scope=body.scope,
        league_id=body.league_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=f"provider:{normalized}:{body.scope}:{body.league_id or 'global'}",
        action="PROVIDER_SECRET_CLEAR",
        metadata={
            "provider": normalized,
            "scope": body.scope,
            "league_id": body.league_id,
        },
        request=request,
    )
    return {"ok": True, **result}


@router.get("/provider-settings/{provider}/secret-status")
async def provider_secret_status(
    provider: str,
    scope: str = Query("global"),
    league_id: Optional[str] = None,
    admin=Depends(get_admin_user),
):
    """Return masked secret status for one provider scope."""
    normalized = _ensure_supported_provider(provider)
    status_payload = await provider_settings_service.get_secret_status(
        normalized,
        scope=scope,
        league_id=league_id,
    )
    if status_payload.get("updated_at"):
        status_payload["updated_at"] = ensure_utc(status_payload["updated_at"]).isoformat()
    return status_payload


@router.post("/provider-settings/{provider}/probe")
async def probe_provider_config(
    provider: str,
    body: ProviderProbeBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Run lightweight config probe for operator feedback."""
    normalized = _ensure_supported_provider(provider)
    admin_id = str(admin["_id"])
    resolved = await provider_settings_service.get_effective(
        normalized,
        sport_key=body.sport_key,
        league_id=body.league_id,
        include_secret=True,
    )
    effective = dict(resolved.get("effective_config") or {})
    enabled = bool(effective.get("enabled", True))
    api_key = str(effective.get("api_key") or "")
    base_url = str(effective.get("base_url") or "")

    if not enabled:
        status_text = "warn"
        message = "Provider disabled."
    elif normalized in {"theoddsapi", "football_data"} and not api_key:
        status_text = "warn"
        message = "Missing API key."
    elif not base_url.startswith("http"):
        status_text = "error"
        message = "Invalid base_url."
    else:
        status_text = "ok"
        message = "Configuration looks valid."

    await log_audit(
        actor_id=admin_id,
        target_id=f"provider:{normalized}:probe",
        action="PROVIDER_CONFIG_PROBE",
        metadata={
            "provider": normalized,
            "sport_key": body.sport_key,
            "league_id": body.league_id,
            "status": status_text,
        },
        request=request,
    )
    return {
        "provider": normalized,
        "status": status_text,
        "message": message,
        "effective_config": _mask_effective(resolved).get("effective_config"),
        "source_map": resolved.get("source_map", {}),
    }


@router.get("/event-bus/status")
async def event_bus_status(admin=Depends(get_admin_user)):
    """Operational event bus status for debugging and monitoring."""
    from app.main import automation_enabled, scheduler

    payload = await event_bus_monitor.get_current_health()
    fallback_ids = {"match_resolver", "leaderboard", "matchday_leaderboard"}
    scheduled_fallback = []
    for job in scheduler.get_jobs():
        if job.id not in fallback_ids:
            continue
        scheduled_fallback.append(
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
        )
    payload["fallback_polling"] = {
        "automation_enabled": bool(automation_enabled()),
        "scheduled_jobs": sorted(scheduled_fallback, key=lambda row: row["id"]),
    }
    return payload


@router.get("/event-bus/history")
async def event_bus_history(
    window: str = Query("24h", pattern="^(1h|6h|24h)$"),
    bucket_seconds: int = Query(10, ge=10, le=60),
    admin=Depends(get_admin_user),
):
    """Historical qbus stats for charts."""
    return await event_bus_monitor.get_recent_stats(window=window, bucket_seconds=bucket_seconds)


@router.get("/event-bus/handlers")
async def event_bus_handlers(
    window: str = Query("1h", pattern="^(1h|6h|24h)$"),
    admin=Depends(get_admin_user),
):
    """Handler rollups aggregated from historical qbus snapshots."""
    rows = await event_bus_monitor.get_handler_rollups(window=window)
    recent_errors = event_bus.stats().get("recent_errors", []) or []
    last_error_by_handler: dict[str, dict[str, Any]] = {}
    for item in recent_errors:
        handler_name = str(item.get("handler_name") or "")
        if not handler_name:
            continue
        if handler_name not in last_error_by_handler:
            last_error_by_handler[handler_name] = item
    for row in rows:
        row["last_error"] = last_error_by_handler.get(row["name"])
    return {"window": window, "items": rows}


@router.get("/time-machine/justice")
async def list_time_machine_justice(
    sport_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    days: int = Query(default=0, ge=0, le=3650),
    admin=Depends(get_admin_user),
):
    """List Engine Time Machine justice snapshots for admin analytics UI."""
    query: dict[str, Any] = {}
    normalized_sport_key = (sport_key or "").strip()
    if normalized_sport_key:
        query["sport_key"] = normalized_sport_key
    if days > 0:
        query["snapshot_date"] = {"$gte": utcnow() - timedelta(days=days)}

    projection = {
        "sport_key": 1,
        "snapshot_date": 1,
        "window_start": 1,
        "window_end": 1,
        "table": 1,
        "meta": 1,
    }
    rows = await _db.db.engine_time_machine_justice.find(
        query,
        projection,
    ).sort("snapshot_date", -1).limit(int(limit)).to_list(length=int(limit))

    items: list[dict[str, Any]] = []
    for row in rows:
        table = row.get("table") if isinstance(row.get("table"), list) else []
        top3 = table[:3]
        items.append(
            {
                "id": str(row.get("_id")),
                "sport_key": str(row.get("sport_key") or ""),
                "snapshot_date": ensure_utc(row.get("snapshot_date")).isoformat()
                if row.get("snapshot_date")
                else None,
                "window_start": ensure_utc(row.get("window_start")).isoformat()
                if row.get("window_start")
                else None,
                "window_end": ensure_utc(row.get("window_end")).isoformat()
                if row.get("window_end")
                else None,
                "meta": row.get("meta") if isinstance(row.get("meta"), dict) else {},
                "table_size": len(table),
                "top3": top3,
                "table": table,
            }
        )

    available_sports = await _db.db.engine_time_machine_justice.distinct("sport_key")
    return {
        "items": items,
        "count": len(items),
        "available_sports": sorted(str(x) for x in available_sports if x),
        "filters": {
            "sport_key": normalized_sport_key or None,
            "limit": int(limit),
            "days": int(days),
        },
    }


class TriggerSyncRequest(BaseModel):
    worker_id: str


class AutomationToggleRequest(BaseModel):
    enabled: bool
    run_initial_sync: bool = False


@router.post("/trigger-sync")
async def trigger_sync(
    body: TriggerSyncRequest, request: Request, admin=Depends(get_admin_user),
):
    """Manually trigger a background worker."""
    if body.worker_id not in _TRIGGERABLE_WORKERS:
        raise HTTPException(status_code=400, detail=f"Worker '{body.worker_id}' cannot be triggered manually.")

    # Lazy-import the worker function
    worker_fn = _get_worker_fn(body.worker_id)
    if not worker_fn:
        raise HTTPException(status_code=400, detail=f"Unknown worker: {body.worker_id}")

    admin_id = str(admin["_id"])
    logger.info("Admin %s triggered manual sync: %s", admin_id, body.worker_id)

    t0 = _time.monotonic()
    try:
        await worker_fn()
    except Exception as e:
        logger.error("Manual sync %s failed: %s", body.worker_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync failed for {body.worker_id}. Check server logs.")
    duration_ms = int((_time.monotonic() - t0) * 1000)

    await log_audit(
        actor_id=admin_id, target_id=body.worker_id, action="MANUAL_SYNC",
        metadata={"duration_ms": duration_ms}, request=request,
    )

    label = _WORKER_REGISTRY[body.worker_id]["label"]
    return {"message": f"{label} completed", "duration_ms": duration_ms}


@router.get("/workers/automation")
async def get_automation_state(admin=Depends(get_admin_user)):
    """Return automatic worker scheduler state."""
    from app.main import automation_enabled, automated_job_count, scheduler

    return {
        "enabled": automation_enabled(),
        "scheduled_jobs": automated_job_count(),
        "scheduler_running": bool(scheduler.running),
    }


@router.post("/workers/automation")
async def set_automation_state(
    body: AutomationToggleRequest,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Enable/disable automatic worker jobs at runtime."""
    from app.main import set_automation_enabled

    result = await set_automation_enabled(
        body.enabled,
        run_initial_sync=body.run_initial_sync and body.enabled,
        persist=True,
    )
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id="worker_automation",
        action="WORKER_AUTOMATION_TOGGLE",
        metadata={
            "enabled": result["enabled"],
            "changed": result["changed"],
            "added_jobs": result["added_jobs"],
            "removed_jobs": result["removed_jobs"],
            "run_initial_sync": bool(body.run_initial_sync and body.enabled),
        },
        request=request,
    )
    return result


def _get_worker_fn(worker_id: str):
    """Lazy-import worker functions to avoid circular imports."""
    if worker_id == "odds_poller":
        from app.workers.odds_poller import poll_odds
        return poll_odds
    if worker_id == "match_resolver":
        from app.workers.match_resolver import resolve_matches
        return resolve_matches
    if worker_id == "matchday_sync":
        from app.workers.matchday_sync import sync_matchdays
        return sync_matchdays
    if worker_id == "leaderboard":
        from app.workers.leaderboard import materialize_leaderboard
        return materialize_leaderboard
    if worker_id == "matchday_resolver":
        from app.workers.matchday_resolver import resolve_matchday_predictions
        return resolve_matchday_predictions
    if worker_id == "matchday_leaderboard":
        from app.workers.matchday_leaderboard import materialize_matchday_leaderboard
        return materialize_matchday_leaderboard
    if worker_id == "quotico_tip_worker":
        from app.workers.quotico_tip_worker import generate_quotico_tips
        return generate_quotico_tips
    if worker_id == "calibration_eval":
        from app.workers.calibration_worker import run_daily_evaluation
        return run_daily_evaluation
    if worker_id == "calibration_refine":
        from app.workers.calibration_worker import run_weekly_refinement
        return run_weekly_refinement
    if worker_id == "calibration_explore":
        from app.workers.calibration_worker import run_monthly_exploration
        return run_monthly_exploration
    if worker_id == "reliability_check":
        from app.workers.calibration_worker import run_reliability_check
        return run_reliability_check
    return None


# --- User Management ---

@router.get("/users")
async def list_users(
    request: Request,
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    admin=Depends(get_admin_user),
):
    """List users, optionally filtered by email or alias search."""
    query: dict = {"is_deleted": False}
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"email": {"$regex": escaped, "$options": "i"}},
            {"alias": {"$regex": escaped, "$options": "i"}},
        ]

    users = await _db.db.users.find(query).sort("created_at", -1).limit(limit).to_list(length=limit)

    # GDPR: log data access when admin views user profiles
    admin_id = str(admin["_id"])
    for u in users:
        await log_audit(
            actor_id=admin_id,
            target_id=str(u["_id"]),
            action="USER_PROFILE_VIEWED",
            request=request,
        )

    return [
        {
            "id": str(u["_id"]),
            "email": u["email"],
            "alias": u.get("alias", ""),
            "has_custom_alias": u.get("has_custom_alias", False),
            "points": u.get("points", 0),
            "is_admin": u.get("is_admin", False),
            "is_banned": u.get("is_banned", False),
            "is_2fa_enabled": u.get("is_2fa_enabled", False),
            "created_at": ensure_utc(u["created_at"]).isoformat(),
            "bet_count": await _db.db.betting_slips.count_documents({"user_id": str(u["_id"])}),
        }
        for u in users
    ]


@router.post("/users/{user_id}/points")
async def adjust_points(
    user_id: str, body: PointsAdjust, request: Request, admin=Depends(get_admin_user)
):
    """Manually adjust a user's points."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    now = utcnow()
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$inc": {"points": body.delta}, "$set": {"updated_at": now}},
    )
    await _db.db.points_transactions.insert_one({
        "user_id": user_id,
        "bet_id": "admin_adjustment",
        "delta": body.delta,
        "scoring_version": 0,
        "reason": body.reason,
        "admin_id": str(admin["_id"]),
        "created_at": now,
    })

    admin_id = str(admin["_id"])
    logger.info("Admin %s adjusted points for %s: %+.1f (%s)", admin_id, user_id, body.delta, body.reason)
    await log_audit(
        actor_id=admin_id, target_id=user_id, action="MANUAL_SCORE_ADJUSTMENT",
        metadata={"delta": body.delta, "reason": body.reason}, request=request,
    )
    return {"message": f"Points adjusted: {body.delta:+.1f}", "new_total": user.get("points", 0) + body.delta}


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: str, request: Request, admin=Depends(get_admin_user)):
    """Ban a user."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.get("is_admin"):
        raise HTTPException(status_code=400, detail="Cannot ban an admin.")

    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_banned": True, "updated_at": utcnow()}},
    )
    await invalidate_user_tokens(user_id)
    admin_id = str(admin["_id"])
    logger.info("Admin %s banned user %s", admin_id, user_id)
    await log_audit(actor_id=admin_id, target_id=user_id, action="USER_BAN", request=request)
    return {"message": f"{user['email']} has been banned."}


@router.post("/users/{user_id}/unban")
async def unban_user(user_id: str, request: Request, admin=Depends(get_admin_user)):
    """Unban a user."""
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_banned": False, "updated_at": utcnow()}},
    )
    await log_audit(
        actor_id=str(admin["_id"]), target_id=user_id, action="USER_UNBAN", request=request,
    )
    return {"message": "Ban lifted."}


@router.post("/users/{user_id}/reset-alias")
async def reset_alias(user_id: str, request: Request, admin=Depends(get_admin_user)):
    """Reset a user's alias back to a default User#XXXXXX tag."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    old_alias = user.get("alias", "")
    alias, alias_slug = await generate_default_alias(_db.db)
    now = utcnow()

    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "alias": alias,
                "alias_slug": alias_slug,
                "has_custom_alias": False,
                "updated_at": now,
            }
        },
    )

    admin_id = str(admin["_id"])
    logger.info("Admin %s reset alias for %s: %s -> %s", admin_id, user_id, old_alias, alias)
    await log_audit(
        actor_id=admin_id, target_id=user_id, action="ALIAS_RESET",
        metadata={"old_alias": old_alias, "new_alias": alias}, request=request,
    )
    return {"message": f"Alias reset: {old_alias} -> {alias}"}


# --- Match Management ---

class MatchSyncBody(BaseModel):
    league_id: str


class MatchDuplicateCleanupBody(BaseModel):
    league_id: Optional[str] = None
    sport_key: Optional[str] = None
    limit_groups: int = 500
    dry_run: bool = False

@router.get("/matches")
async def list_all_matches(
    response: Response,
    league_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    needs_review: Optional[bool] = Query(None),
    odds_available: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin=Depends(get_admin_user),
):
    """List matches with league/status/date/review filters (admin view)."""
    if search and len(search.strip()) > 64:
        raise HTTPException(status_code=400, detail="search must be at most 64 chars.")

    cache_key = _admin_matches_cache_key(
        page=page,
        page_size=page_size,
        league_id=league_id,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        needs_review=needs_review,
        odds_available=odds_available,
        search=search,
    )
    cache_now = utcnow()
    cache_item = _ADMIN_MATCHES_CACHE.get(cache_key)
    if cache_item:
        cached_at, cached_payload = cache_item
        if (cache_now - cached_at).total_seconds() < ADMIN_MATCHES_CACHE_TTL_SECONDS:
            response.headers["X-Admin-Cache"] = "HIT"
            return cached_payload
        _ADMIN_MATCHES_CACHE.pop(cache_key, None)

    and_filters: list[dict[str, Any]] = []
    if league_id:
        and_filters.append({"league_id": _parse_object_id(league_id, "league_id")})
    if status_filter:
        and_filters.append({"status": status_filter})
    if date_from or date_to:
        date_filter: dict[str, datetime] = {}
        if date_from:
            date_filter["$gte"] = ensure_utc(date_from)
        if date_to:
            date_filter["$lte"] = ensure_utc(date_to)
        and_filters.append({"match_date": date_filter})
    if odds_available is True:
        and_filters.append({"odds_meta.updated_at": {"$ne": None}})
    elif odds_available is False:
        and_filters.append(
            {
                "$or": [
                    {"odds_meta.updated_at": None},
                    {"odds_meta.updated_at": {"$exists": False}},
                ]
            }
        )
    if search and search.strip():
        escaped = re.escape(search.strip())
        and_filters.append(
            {
                "$or": [
                    {"home_team": {"$regex": escaped, "$options": "i"}},
                    {"away_team": {"$regex": escaped, "$options": "i"}},
                ]
            }
        )
    if needs_review is True:
        review_team_ids = await _db.db.teams.distinct("_id", {"needs_review": True})
        if not review_team_ids:
            empty_payload = {"items": [], "page": page, "page_size": page_size, "total": 0}
            _ADMIN_MATCHES_CACHE[cache_key] = (cache_now, empty_payload)
            response.headers["X-Admin-Cache"] = "MISS"
            return empty_payload
        and_filters.append(
            {
                "$or": [
                    {"home_team_id": {"$in": review_team_ids}},
                    {"away_team_id": {"$in": review_team_ids}},
                ]
            }
        )

    query: dict[str, Any] = {"$and": and_filters} if and_filters else {}

    total = await _db.db.matches.count_documents(query)
    skip = (page - 1) * page_size
    matches = await _db.db.matches.find(query).sort("match_date", -1).skip(skip).limit(page_size).to_list(length=page_size)

    league_ids = [m.get("league_id") for m in matches if m.get("league_id")]
    leagues_by_id: dict[ObjectId, str] = {}
    if league_ids:
        league_docs = await _db.db.leagues.find(
            {"_id": {"$in": league_ids}},
            {"display_name": 1, "name": 1},
        ).to_list(length=500)
        leagues_by_id = {
            d["_id"]: str(d.get("display_name") or d.get("name") or "")
            for d in league_docs
        }

    match_ids = [str(m["_id"]) for m in matches]
    bet_count_map: dict[str, int] = {}
    if match_ids:
        bet_counts = await _db.db.betting_slips.aggregate(
            [
                {"$match": {"selections.match_id": {"$in": match_ids}}},
                {"$unwind": "$selections"},
                {"$match": {"selections.match_id": {"$in": match_ids}}},
                {"$group": {"_id": "$selections.match_id", "count": {"$sum": 1}}},
            ]
        ).to_list(length=10_000)
        bet_count_map = {str(doc["_id"]): int(doc.get("count", 0)) for doc in bet_counts}

    items = []
    for m in matches:
        odds_meta = m.get("odds_meta") if isinstance(m.get("odds_meta"), dict) else {}
        odds_updated_at = odds_meta.get("updated_at") if isinstance(odds_meta, dict) else None
        raw_matchday = m.get("matchday")
        matchday = None
        if isinstance(raw_matchday, (int, float)):
            matchday = int(raw_matchday)
        elif isinstance(raw_matchday, str):
            text = raw_matchday.strip()
            if text.isdigit():
                matchday = int(text)
        items.append(
            {
                "id": str(m["_id"]),
                "league_id": str(m["league_id"]) if m.get("league_id") else None,
                "league_name": leagues_by_id.get(m.get("league_id"), ""),
                "sport_key": m["sport_key"],
                "home_team": m.get("home_team", ""),
                "away_team": m.get("away_team", ""),
                "home_team_id": str(m.get("home_team_id")) if m.get("home_team_id") else None,
                "away_team_id": str(m.get("away_team_id")) if m.get("away_team_id") else None,
                "match_date": ensure_utc(m["match_date"]).isoformat(),
                "status": m["status"],
                "score": m.get("score", {}),
                "result": m.get("result", {}),
                "matchday": matchday,
                "external_ids": m.get("external_ids", {}),
                "has_odds": bool(odds_updated_at),
                "odds_updated_at": (ensure_utc(odds_updated_at).isoformat() if odds_updated_at else None),
                "bet_count": int(bet_count_map.get(str(m["_id"]), 0)),
            }
        )

    payload = {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }
    _ADMIN_MATCHES_CACHE[cache_key] = (cache_now, payload)
    response.headers["X-Admin-Cache"] = "MISS"
    return payload


@router.get("/matches/{match_id}")
async def get_admin_match_detail(match_id: str, admin=Depends(get_admin_user)):
    oid = _parse_object_id(match_id, "match_id")
    match = await _db.db.matches.find_one({"_id": oid})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")

    league_name = ""
    if match.get("league_id"):
        league = await _db.db.leagues.find_one({"_id": match["league_id"]}, {"display_name": 1, "name": 1})
        if league:
            league_name = str(league.get("display_name") or league.get("name") or "")

    odds_meta = match.get("odds_meta") if isinstance(match.get("odds_meta"), dict) else {}
    odds_updated_at = odds_meta.get("updated_at") if isinstance(odds_meta, dict) else None
    raw_matchday = match.get("matchday")
    matchday = None
    if isinstance(raw_matchday, (int, float)):
        matchday = int(raw_matchday)
    elif isinstance(raw_matchday, str):
        text = raw_matchday.strip()
        if text.isdigit():
            matchday = int(text)
    return {
        "id": str(match["_id"]),
        "league_id": str(match.get("league_id")) if match.get("league_id") else None,
        "league_name": league_name,
        "sport_key": match.get("sport_key"),
        "home_team": match.get("home_team", ""),
        "away_team": match.get("away_team", ""),
        "home_team_id": str(match.get("home_team_id")) if match.get("home_team_id") else None,
        "away_team_id": str(match.get("away_team_id")) if match.get("away_team_id") else None,
        "match_date": ensure_utc(match.get("match_date")).isoformat() if match.get("match_date") else None,
        "status": match.get("status"),
        "score": match.get("score", {}),
        "result": match.get("result", {}),
        "matchday": matchday,
        "stats": match.get("stats", {}),
        "external_ids": match.get("external_ids", {}),
        "odds_meta": odds_meta,
        "has_odds": bool(odds_updated_at),
        "odds_updated_at": ensure_utc(odds_updated_at).isoformat() if odds_updated_at else None,
    }


@router.get("/match-duplicates")
async def list_match_duplicates_admin(
    league_id: Optional[str] = Query(None),
    sport_key: Optional[str] = Query(None),
    limit_groups: int = Query(200, ge=1, le=2000),
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(league_id, "league_id") if league_id else None
    result = await list_same_day_duplicate_matches(
        league_id=league_oid,
        sport_key=sport_key,
        limit_groups=limit_groups,
    )
    league_ids: set[ObjectId] = set()
    for group in result.get("groups", []):
        raw = str(group.get("league_id") or "").strip()
        if not raw:
            continue
        try:
            league_ids.add(ObjectId(raw))
        except Exception:
            continue

    leagues_by_id: dict[str, str] = {}
    if league_ids:
        league_docs = await _db.db.leagues.find({"_id": {"$in": list(league_ids)}}, {"display_name": 1, "name": 1}).to_list(length=5000)
        leagues_by_id = {
            str(doc["_id"]): str(doc.get("display_name") or doc.get("name") or "")
            for doc in league_docs
        }

    for group in result.get("groups", []):
        group["league_name"] = leagues_by_id.get(str(group.get("league_id") or ""), "")

    return result


@router.post("/match-duplicates/cleanup")
async def cleanup_match_duplicates_admin(
    body: MatchDuplicateCleanupBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(body.league_id, "league_id") if body.league_id else None
    result = await cleanup_same_day_duplicate_matches(
        league_id=league_oid,
        sport_key=body.sport_key,
        limit_groups=int(body.limit_groups or 500),
        dry_run=bool(body.dry_run),
    )
    _admin_matches_cache_invalidate()
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=body.league_id or (body.sport_key or "all"),
        action="MATCH_DUPLICATE_CLEANUP",
        metadata={
            "league_id": body.league_id,
            "sport_key": body.sport_key,
            "limit_groups": int(body.limit_groups or 500),
            **result,
        },
        request=request,
    )
    return result


async def _run_matches_sync_for_league(sport_key: str) -> None:
    from app.services.match_service import sync_matches_for_sport

    try:
        await sync_matches_for_sport(sport_key)
    except Exception:
        logger.exception("Manual match sync failed for %s", sport_key)


@router.post("/matches/sync")
async def trigger_matches_sync(
    body: MatchSyncBody,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(body.league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid}, {"sport_key": 1})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key.")

    background_tasks.add_task(_run_matches_sync_for_league, sport_key)
    _admin_matches_cache_invalidate()
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=body.league_id,
        action="MATCH_SYNC_TRIGGER",
        metadata={"sport_key": sport_key},
        request=request,
    )
    return {"message": "Match sync queued.", "sport_key": sport_key}


@router.post("/matches/{match_id}/override")
async def override_result(
    match_id: str, body: ResultOverride, request: Request, admin=Depends(get_admin_user)
):
    """Override a match result (force settle)."""
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")

    now = utcnow()
    before = {
        "status": match.get("status"),
        "result": match.get("result", {}),
        "match_date": ensure_utc(match.get("match_date")).isoformat() if match.get("match_date") else None,
    }
    old_result = before.get("result", {}).get("outcome")

    # Update match
    await _db.db.matches.update_one(
        {"_id": ObjectId(match_id)},
        {
            "$set": {
                "status": "final",
                "result.outcome": body.result,
                "result.home_score": body.home_score,
                "result.away_score": body.away_score,
                "updated_at": now,
            }
        },
    )

    # Re-resolve bets if needed
    if old_result != body.result:
        await _re_resolve_bets(match_id, body.result, now, admin)
    _admin_matches_cache_invalidate()

    # No separate archive step needed — resolved matches stay in the
    # unified ``matches`` collection and are queried directly for H2H/form.

    admin_id = str(admin["_id"])
    logger.info(
        "Admin %s overrode match %s: %s -> %s (%d-%d)",
        admin_id, match_id, old_result, body.result,
        body.home_score, body.away_score,
    )
    await log_audit(
        actor_id=admin_id, target_id=match_id, action="MATCH_OVERRIDE",
        metadata={
            "old_result": old_result, "new_result": body.result,
            "home_score": body.home_score, "away_score": body.away_score,
            "before": before,
            "after": {
                "status": "final",
                "result": {
                    "outcome": body.result,
                    "home_score": body.home_score,
                    "away_score": body.away_score,
                },
                "match_date": before.get("match_date"),
            },
        },
        request=request,
    )
    return {"message": f"Result overridden: {body.result} ({body.home_score}-{body.away_score})"}


@router.post("/matches/{match_id}/force-settle")
async def force_settle(
    match_id: str, body: ResultOverride, request: Request, admin=Depends(get_admin_user)
):
    """Force settle a match that hasn't been resolved yet."""
    return await override_result(match_id, body, request, admin)


async def _re_resolve_bets(match_id: str, new_result: str, now: datetime, admin: dict) -> None:
    """Re-resolve all betting slips containing this match via the Universal Resolver."""
    from app.workers.match_resolver import resolve_selection, recalculate_slip

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    home_score = match.get("result", {}).get("home_score", 0) if match else 0
    away_score = match.get("result", {}).get("away_score", 0) if match else 0

    slips = await _db.db.betting_slips.find(
        {"selections.match_id": match_id}
    ).to_list(length=10000)

    for slip in slips:
        old_status = slip["status"]
        # Calculate old payout to reverse
        old_payout = 0.0
        if old_status == "won" and slip.get("funding", "virtual") == "virtual":
            old_payout = slip.get("potential_payout", 0) or 0

        # Re-resolve each selection for this match
        for sel in slip.get("selections", []):
            if sel.get("match_id") == match_id:
                resolve_selection(sel, match or {}, new_result, home_score, away_score)

        # Recalculate slip-level status
        recalculate_slip(slip, now)

        new_status = slip["status"]
        new_payout = 0.0
        if new_status == "won" and slip.get("funding", "virtual") == "virtual":
            new_payout = slip.get("potential_payout", 0) or 0

        # Update slip in DB
        await _db.db.betting_slips.update_one(
            {"_id": slip["_id"]},
            {"$set": {
                "selections": slip["selections"],
                "status": slip["status"],
                "total_odds": slip.get("total_odds"),
                "potential_payout": slip.get("potential_payout"),
                "resolved_at": slip.get("resolved_at"),
                "updated_at": now,
            }},
        )

        # Adjust user points (reverse old, apply new)
        points_delta = new_payout - old_payout
        if points_delta != 0:
            await _db.db.users.update_one(
                {"_id": ObjectId(slip["user_id"])},
                {"$inc": {"points": points_delta}},
            )
            await _db.db.points_transactions.insert_one({
                "user_id": slip["user_id"],
                "bet_id": str(slip["_id"]),
                "delta": points_delta,
                "scoring_version": 0,
                "reason": f"Admin override: {old_status} -> {new_status}",
                "admin_id": str(admin["_id"]),
                "created_at": now,
            })


# --- Battle Management ---

@router.post("/battles")
async def create_battle_admin(
    body: BattleCreateAdmin, request: Request, admin=Depends(get_admin_user)
):
    """Admin creates a battle between any two squads."""
    squad_a = await _db.db.squads.find_one({"_id": ObjectId(body.squad_a_id)})
    squad_b = await _db.db.squads.find_one({"_id": ObjectId(body.squad_b_id)})

    if not squad_a or not squad_b:
        raise HTTPException(status_code=404, detail="Squad not found.")
    if body.squad_a_id == body.squad_b_id:
        raise HTTPException(status_code=400, detail="A squad cannot battle itself.")

    now = utcnow()
    start_time = ensure_utc(body.start_time)
    battle_doc = {
        "squad_a_id": body.squad_a_id,
        "squad_b_id": body.squad_b_id,
        "start_time": start_time,
        "end_time": body.end_time,
        "status": "upcoming" if start_time > now else "active",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.battles.insert_one(battle_doc)

    battle_id = str(result.inserted_id)
    admin_id = str(admin["_id"])
    logger.info("Admin %s created battle %s: %s vs %s", admin_id, battle_id, squad_a["name"], squad_b["name"])
    await log_audit(
        actor_id=admin_id, target_id=battle_id, action="BATTLE_CREATE",
        metadata={"squad_a_id": body.squad_a_id, "squad_b_id": body.squad_b_id},
        request=request,
    )
    return {
        "id": battle_id,
        "message": f"Battle created: {squad_a['name']} vs {squad_b['name']}",
    }


@router.get("/squads")
async def list_squads(
    limit: int = Query(50, ge=1, le=200),
    admin=Depends(get_admin_user),
):
    """List all squads for battle creation."""
    squads = await _db.db.squads.find().sort("created_at", -1).limit(limit).to_list(length=limit)
    return [
        {
            "id": str(s["_id"]),
            "name": s["name"],
            "member_count": len(s.get("members", [])),
            "admin_id": s["admin_id"],
            "invite_code": s["invite_code"],
        }
        for s in squads
    ]


# --- Audit Log Viewer ---

@router.get("/audit-logs")
async def list_audit_logs(
    action: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    """List audit logs with filtering and pagination (admin only)."""
    query: dict = {}
    if action:
        query["action"] = action
    if actor_id:
        query["actor_id"] = actor_id
    if target_id:
        query["target_id"] = target_id
    if date_from or date_to:
        ts_query: dict = {}
        if date_from:
            try:
                ts_query["$gte"] = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if date_to:
            try:
                ts_query["$lte"] = datetime.strptime(date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                pass
        if ts_query:
            query["timestamp"] = ts_query

    total = await _db.db.audit_logs.count_documents(query)
    logs = await _db.db.audit_logs.find(query).sort("timestamp", -1).skip(offset).limit(limit).to_list(length=limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(entry["_id"]),
                "timestamp": ensure_utc(entry["timestamp"]).isoformat(),
                "actor_id": entry["actor_id"],
                "target_id": entry["target_id"],
                "action": entry["action"],
                "metadata": entry.get("metadata", {}),
                "ip_truncated": entry.get("ip_truncated", ""),
            }
            for entry in logs
        ],
    }


@router.get("/audit-logs/actions")
async def list_audit_actions(admin=Depends(get_admin_user)):
    """List all distinct action types in the audit log."""
    actions = await _db.db.audit_logs.distinct("action")
    return sorted(actions)


@router.get("/audit-logs/export")
async def export_audit_logs(
    action: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    admin=Depends(get_admin_user),
):
    """Export audit logs as CSV for regulatory requests (admin only)."""
    query: dict = {}
    if action:
        query["action"] = action
    if actor_id:
        query["actor_id"] = actor_id
    if target_id:
        query["target_id"] = target_id
    if date_from or date_to:
        ts_query: dict = {}
        if date_from:
            try:
                ts_query["$gte"] = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if date_to:
            try:
                ts_query["$lte"] = datetime.strptime(date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                pass
        if ts_query:
            query["timestamp"] = ts_query

    logs = await _db.db.audit_logs.find(query).sort("timestamp", -1).to_list(length=50000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "actor_id", "target_id", "action", "metadata", "ip_truncated"])
    for entry in logs:
        writer.writerow([
            ensure_utc(entry["timestamp"]).isoformat(),
            entry["actor_id"],
            entry["target_id"],
            entry["action"],
            str(entry.get("metadata", {})),
            entry.get("ip_truncated", ""),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=quotico-audit-logs.csv"},
    )


# --- Team Management (Team-Tower) ---

# --- League Management (League-Tower) ---


def _league_to_dict(doc: dict) -> dict:
    external_ids = doc.get("external_ids")
    if not isinstance(external_ids, dict):
        external_ids = doc.get("provider_mappings", {})
    if not isinstance(external_ids, dict):
        external_ids = {}
    features = doc.get("features", {})
    if not isinstance(features, dict):
        features = {}
    tipping_default = bool(doc.get("is_active", False))
    sport_key = str(doc.get("sport_key", ""))
    season_start_month = int(
        doc.get("season_start_month")
        or default_season_start_month_for_sport(sport_key)
    )

    return {
        "id": str(doc["_id"]),
        "sport_key": sport_key,
        "display_name": doc.get("display_name", ""),
        "structure_type": str(doc.get("structure_type") or "league"),
        "country_code": doc.get("country_code"),
        "tier": doc.get("tier"),
        "season_start_month": season_start_month,
        "current_season": int(
            doc.get("current_season")
            or default_current_season_for_sport(
                sport_key,
                season_start_month=season_start_month,
            )
        ),
        "ui_order": int(doc.get("ui_order", 999)),
        "is_active": bool(doc.get("is_active", False)),
        "needs_review": bool(doc.get("needs_review", False)),
        "features": {
            "tipping": bool(features.get("tipping", tipping_default)),
            "match_load": bool(features.get("match_load", True)),
            "xg_sync": bool(features.get("xg_sync", False)),
            "odds_sync": bool(features.get("odds_sync", False)),
        },
        "external_ids": {
            str(provider).strip().lower(): str(external_id).strip()
            for provider, external_id in external_ids.items()
            if str(provider).strip() and str(external_id).strip()
        },
        "football_data_last_import_at": (
            ensure_utc(doc.get("football_data_last_import_at")).isoformat()
            if doc.get("football_data_last_import_at")
            else None
        ),
        "football_data_last_import_season": doc.get("football_data_last_import_season"),
        "football_data_last_import_by": doc.get("football_data_last_import_by"),
        "created_at": ensure_utc(doc.get("created_at")).isoformat() if doc.get("created_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
    }


async def _refresh_league_registry() -> None:
    await invalidate_navigation_cache()
    await LeagueRegistry.get().initialize()


class LeagueFeaturesUpdateBody(BaseModel):
    tipping: Optional[bool] = None
    match_load: Optional[bool] = None
    xg_sync: Optional[bool] = None
    odds_sync: Optional[bool] = None


class LeagueUpdateBody(BaseModel):
    display_name: Optional[str] = None
    structure_type: Optional[str] = None
    current_season: Optional[int] = None
    season_start_month: Optional[int] = None
    ui_order: Optional[int] = None
    is_active: Optional[bool] = None
    external_ids: Optional[dict[str, str]] = None
    features: Optional[LeagueFeaturesUpdateBody] = None


class LeagueOrderBody(BaseModel):
    league_ids: list[str]


class LeagueStatsImportBody(BaseModel):
    season: str | None = None


class FootballDataImportRequest(BaseModel):
    season: str | None = None
    dry_run: bool = False


class UnifiedMatchIngestRequest(BaseModel):
    source: str
    season: int | str | None = None
    dry_run: bool = True


class LeagueSyncRequest(BaseModel):
    season: int | None = None
    full_season: bool = False


class XGEnrichmentRequest(BaseModel):
    sport_key: str | None = None
    season: str | None = None
    dry_run: bool = False
    force: bool = False


def _serialize_import_job(doc: dict) -> dict:
    return {
        "job_id": str(doc["_id"]),
        "type": str(doc.get("type") or ""),
        "source": str(doc.get("source") or ""),
        "status": doc.get("status", "queued"),
        "phase": doc.get("phase", "queued"),
        "progress": doc.get("progress", {"processed": 0, "total": 0, "percent": 0.0}),
        "counters": doc.get("counters", {}),
        "league_id": str(doc.get("league_id")) if doc.get("league_id") else None,
        "season": doc.get("season"),
        "dry_run": bool(doc.get("dry_run", False)),
        "started_at": ensure_utc(doc.get("started_at")).isoformat() if doc.get("started_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
        "finished_at": ensure_utc(doc.get("finished_at")).isoformat() if doc.get("finished_at") else None,
        "error": doc.get("error"),
        "results": doc.get("results"),
    }


async def _run_football_data_import_job(
    job_id: ObjectId,
    league_id: ObjectId,
    season: str | None,
    dry_run: bool,
    admin_id: str,
) -> None:
    now = utcnow()
    await _db.db.admin_import_jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "running",
                "phase": "fetching_csv",
                "started_at": now,
                "updated_at": now,
            }
        },
    )

    async def _progress_update(payload: dict[str, Any]) -> None:
        update_doc: dict[str, Any] = {"updated_at": utcnow()}
        if payload.get("phase"):
            update_doc["phase"] = payload["phase"]
        if payload.get("progress"):
            update_doc["progress"] = payload["progress"]
        await _db.db.admin_import_jobs.update_one({"_id": job_id}, {"$set": update_doc})

    try:
        result = await import_football_data_stats(
            league_id=league_id,
            season=season,
            dry_run=dry_run,
            progress_cb=_progress_update,
        )
        now = utcnow()
        counters = {
            "matched": int(result.get("matched", 0)),
            "existing_matches": int(result.get("existing_matches", 0)),
            "new_matches": int(result.get("new_matches", 0)),
            "updated": int(result.get("updated", 0)),
            "odds_snapshots_total": int(result.get("odds_snapshots_total", 0)),
            "odds_ingest_inserted": int(result.get("odds_ingest_inserted", 0)),
            "odds_ingest_deduplicated": int(result.get("odds_ingest_deduplicated", 0)),
            "odds_ingest_markets_updated": int(result.get("odds_ingest_markets_updated", 0)),
            "odds_ingest_errors": int(result.get("odds_ingest_errors", 0)),
        }
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "succeeded",
                    "phase": "finalizing",
                    "counters": counters,
                    "results": result,
                    "updated_at": now,
                    "finished_at": now,
                }
            },
        )
        if not dry_run:
            await _db.db.leagues.update_one(
                {"_id": league_id},
                {
                    "$set": {
                        "football_data_last_import_at": now,
                        "football_data_last_import_season": result.get("season"),
                        "football_data_last_import_by": admin_id,
                        "football_data_last_import_summary": {
                            "matched": counters["matched"],
                            "updated": counters["updated"],
                            "odds_snapshots_total": counters["odds_snapshots_total"],
                        },
                        "updated_at": now,
                    }
                },
            )
    except Exception as exc:
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": utcnow(),
                    "finished_at": utcnow(),
                    "error": {
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                }
            },
        )
        logger.exception("Async football-data import job failed: %s", str(job_id))


async def _run_unified_match_ingest_job(
    job_id: ObjectId,
    league_id: ObjectId,
    source: str,
    season: int | str | None,
    dry_run: bool,
    admin_id: str,
) -> None:
    now = utcnow()
    await _db.db.admin_import_jobs.update_one(
        {"_id": job_id},
        {"$set": {"status": "running", "phase": "initializing", "started_at": now, "updated_at": now}},
    )

    async def _progress_update(payload: dict[str, Any]) -> None:
        update_doc: dict[str, Any] = {"updated_at": utcnow()}
        if payload.get("phase"):
            update_doc["phase"] = payload["phase"]
        if payload.get("progress"):
            update_doc["progress"] = payload["progress"]
        if payload.get("counters"):
            update_doc["counters"] = payload["counters"]
        await _db.db.admin_import_jobs.update_one({"_id": job_id}, {"$set": update_doc})

    try:
        source_key = str(source or "").strip().lower()
        if source_key == "football_data_uk":
            if season is not None and not isinstance(season, str):
                season = str(season)
            result = await import_football_data_stats(
                league_id=league_id,
                season=season,
                dry_run=dry_run,
                progress_cb=_progress_update,
            )
            counters = {
                "processed": int(result.get("processed", 0)),
                "created": int(result.get("new_matches", 0)),
                "updated": int(result.get("updated", 0)),
                "matched": int(result.get("matched", 0)),
                "skipped": 0,
                "conflicts": 0,
                "matched_by_external_id": 0,
                "matched_by_identity_window": 0,
                "unresolved_league": 0,
                "unresolved_team": 0,
                "team_name_conflict": 0,
                "other_conflicts": 0,
            }
        elif source_key == "football_data":
            if season is None:
                raise ValueError("season is required for football_data")
            result = await import_football_data_org_season(
                league_id=league_id,
                season_year=int(season),
                dry_run=dry_run,
                progress_cb=_progress_update,
            )
            ingest_meta = result.get("match_ingest") if isinstance(result.get("match_ingest"), dict) else {}
            counters = {
                "processed": int(result.get("processed", 0)),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "matched": int(result.get("matched", 0)),
                "skipped": int(result.get("skipped_conflicts", 0)),
                "conflicts": int(result.get("skipped_conflicts", 0)),
                "matched_by_external_id": int(ingest_meta.get("matched_by_external_id", 0)),
                "matched_by_identity_window": int(ingest_meta.get("matched_by_identity_window", 0)),
                "unresolved_league": 0,
                "unresolved_team": int(result.get("unresolved_teams", 0)),
                "team_name_conflict": int(ingest_meta.get("team_name_conflict", 0)),
                "other_conflicts": int(ingest_meta.get("other_conflicts", 0)),
            }
        elif source_key == "openligadb":
            if season is None:
                raise ValueError("season is required for openligadb")
            result = await import_openligadb_season(
                league_id=league_id,
                season_year=int(season),
                dry_run=dry_run,
                progress_cb=_progress_update,
            )
            ingest_meta = result.get("match_ingest") if isinstance(result.get("match_ingest"), dict) else {}
            counters = {
                "processed": int(result.get("processed", 0)),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "matched": int(result.get("matched", 0)),
                "skipped": int(result.get("skipped_conflicts", 0)),
                "conflicts": int(result.get("skipped_conflicts", 0)),
                "matched_by_external_id": int(ingest_meta.get("matched_by_external_id", 0)),
                "matched_by_identity_window": int(ingest_meta.get("matched_by_identity_window", 0)),
                "unresolved_league": 0,
                "unresolved_team": int(result.get("unresolved_teams", 0)),
                "team_name_conflict": int(ingest_meta.get("team_name_conflict", 0)),
                "other_conflicts": int(ingest_meta.get("other_conflicts", 0)),
            }
        else:
            raise ValueError(f"Unsupported ingest source: {source_key}")

        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "succeeded",
                    "phase": "finalizing",
                    "counters": counters,
                    "results": result,
                    "updated_at": now,
                    "finished_at": now,
                }
            },
        )
        if not dry_run:
            await _db.db.leagues.update_one(
                {"_id": league_id},
                {
                    "$set": {
                        "match_ingest_last_run_at": now,
                        "match_ingest_last_source": source_key,
                        "match_ingest_last_run_by": admin_id,
                        "updated_at": now,
                    }
                },
            )
    except Exception as exc:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": now,
                    "finished_at": now,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                }
            },
        )
        logger.exception("Async unified match ingest job failed: %s", str(job_id))


async def _run_xg_enrichment_job(
    job_id: ObjectId,
    sport_key: str | None,
    season_spec: str | None,
    dry_run: bool,
    force: bool,
    _admin_id: str,
) -> None:
    now = utcnow()
    await _db.db.admin_import_jobs.update_one(
        {"_id": job_id},
        {"$set": {"status": "running", "phase": "resolving_scope", "started_at": now, "updated_at": now}},
    )

    try:
        seasons = parse_xg_season_spec(season_spec)
    except Exception as exc:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": now,
                    "finished_at": now,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                }
            },
        )
        logger.exception("Async xG enrichment job invalid season spec: %s", str(job_id))
        return

    try:
        target_sports = await list_xg_target_sport_keys(sport_key)
        if sport_key and not target_sports:
            raise ValueError(
                f"sport_key={sport_key} is not eligible (requires active league + xg_sync + understat mapping)."
            )
        if not target_sports:
            raise ValueError("No eligible leagues found for xG enrichment.")

        total_runs = len(target_sports) * len(seasons)
        runs_done = 0
        counters = {
            "processed": 0,
            "total": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped": 0,
            "already_enriched": 0,
            "alias_suggestions_recorded": 0,
            "leagues_processed": 0,
            "seasons_processed": 0,
            "runs_total": total_runs,
            "runs_completed": 0,
        }
        results_items: list[dict[str, Any]] = []
        unmatched_teams_seen: set[str] = set()
        raw_rows_preview: list[dict[str, Any]] = []

        for current_sport in target_sports:
            for season_year in seasons:
                await _db.db.admin_import_jobs.update_one(
                    {"_id": job_id},
                    {
                        "$set": {
                            "phase": "enriching_matches",
                            "updated_at": utcnow(),
                            "progress": {
                                "processed": runs_done,
                                "total": total_runs,
                                "percent": round((runs_done / total_runs) * 100.0, 2) if total_runs else 0.0,
                            },
                            "counters": counters,
                        }
                    },
                )

                result = await enrich_xg_matches(
                    current_sport,
                    int(season_year),
                    dry_run=dry_run,
                    force=force,
                )
                runs_done += 1
                counters["total"] += int(result.get("total", 0))
                counters["processed"] += int(result.get("total", 0))
                counters["matched"] += int(result.get("matched", 0))
                counters["unmatched"] += int(result.get("unmatched", 0))
                counters["skipped"] += int(result.get("skipped", 0))
                counters["already_enriched"] += int(result.get("already_enriched", 0))
                counters["alias_suggestions_recorded"] += int(result.get("alias_suggestions_recorded", 0))
                counters["runs_completed"] = runs_done

                if all(item.get("sport_key") != current_sport for item in results_items):
                    counters["leagues_processed"] += 1
                counters["seasons_processed"] += 1

                team_samples = list(result.get("unmatched_teams", []) or [])
                for name in team_samples:
                    if len(unmatched_teams_seen) >= 500:
                        break
                    unmatched_teams_seen.add(str(name))

                for raw in list(result.get("raw_rows_preview", []) or []):
                    if len(raw_rows_preview) >= 500:
                        break
                    if not isinstance(raw, dict):
                        continue
                    row = dict(raw)
                    row["sport_key"] = str(result.get("sport_key") or current_sport)
                    row["season_year"] = int(result.get("season_year", season_year))
                    raw_rows_preview.append(row)

                results_items.append(
                    {
                        "sport_key": str(result.get("sport_key") or current_sport),
                        "season_year": int(result.get("season_year", season_year)),
                        "provider": str(result.get("provider") or "understat"),
                        "total": int(result.get("total", 0)),
                        "matched": int(result.get("matched", 0)),
                        "unmatched": int(result.get("unmatched", 0)),
                        "skipped": int(result.get("skipped", 0)),
                        "already_enriched": int(result.get("already_enriched", 0)),
                        "alias_suggestions_recorded": int(result.get("alias_suggestions_recorded", 0)),
                        "unmatched_teams_sample": team_samples[:50],
                        "raw_rows_preview_sample": list(result.get("raw_rows_preview", []) or [])[:20],
                    }
                )

                await _db.db.admin_import_jobs.update_one(
                    {"_id": job_id},
                    {
                        "$set": {
                            "updated_at": utcnow(),
                            "progress": {
                                "processed": runs_done,
                                "total": total_runs,
                                "percent": round((runs_done / total_runs) * 100.0, 2) if total_runs else 100.0,
                            },
                            "counters": counters,
                        }
                    },
                )

        finished_at = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "succeeded",
                    "phase": "finalizing",
                    "updated_at": finished_at,
                    "finished_at": finished_at,
                    "counters": counters,
                    "results": {
                        "sport_key": sport_key,
                        "season": season_spec,
                        "seasons": seasons,
                        "dry_run": bool(dry_run),
                        "force": bool(force),
                        "provider": "understat",
                        "summary": counters,
                        "items": results_items,
                        "raw_rows_preview": raw_rows_preview,
                        "unmatched_teams": sorted(unmatched_teams_seen),
                    },
                }
            },
        )
    except Exception as exc:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": now,
                    "finished_at": now,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                }
            },
        )
        logger.exception("Async xG enrichment job failed: %s", str(job_id))


@router.get("/leagues")
async def list_leagues_admin(admin=Depends(get_admin_user)):
    docs = await _db.db.leagues.find({}).sort([("ui_order", 1), ("display_name", 1)]).to_list(length=10_000)
    return {"items": [_league_to_dict(doc) for doc in docs]}


@router.post("/leagues/seed")
async def seed_leagues_admin(request: Request, admin=Depends(get_admin_user)):
    result = await seed_core_leagues()
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id="leagues",
        action="LEAGUES_SEEDED",
        metadata=result,
        request=request,
    )
    return result


@router.put("/leagues/order")
async def update_leagues_order_admin(
    body: LeagueOrderBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    if not body.league_ids:
        raise HTTPException(status_code=400, detail="league_ids must not be empty.")

    ordered_ids: list[ObjectId] = []
    seen: set[str] = set()
    for league_id in body.league_ids:
        if league_id in seen:
            continue
        seen.add(league_id)
        ordered_ids.append(_parse_object_id(league_id, "league_id"))

    result = await update_league_order(ordered_ids)
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id="leagues",
        action="LEAGUE_ORDER_UPDATE",
        metadata=result,
        request=request,
    )
    return result


@router.patch("/leagues/{league_id}")
async def update_league_admin(
    league_id: str,
    body: LeagueUpdateBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    if (
        body.display_name is None
        and body.structure_type is None
        and body.current_season is None
        and body.season_start_month is None
        and body.ui_order is None
        and body.is_active is None
        and body.external_ids is None
        and body.features is None
    ):
        raise HTTPException(status_code=400, detail="Nothing to update.")

    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    updates: dict = {"updated_at": utcnow()}
    if body.display_name is not None:
        updates["display_name"] = body.display_name.strip()
    if body.structure_type is not None:
        structure_type = str(body.structure_type).strip().lower()
        if structure_type not in {"league", "cup", "tournament"}:
            raise HTTPException(status_code=400, detail="Invalid structure_type.")
        updates["structure_type"] = structure_type
    if body.current_season is not None:
        updates["current_season"] = int(body.current_season)
    if body.season_start_month is not None:
        season_start_month = int(body.season_start_month)
        if season_start_month < 1 or season_start_month > 12:
            raise HTTPException(status_code=400, detail="Invalid season_start_month.")
        updates["season_start_month"] = season_start_month
    if body.ui_order is not None:
        updates["ui_order"] = int(body.ui_order)
    if body.is_active is not None:
        updates["is_active"] = bool(body.is_active)
    if body.external_ids is not None:
        updates["external_ids"] = {
            str(provider).strip().lower(): str(ext_id).strip()
            for provider, ext_id in body.external_ids.items()
            if str(provider).strip() and str(ext_id).strip()
        }
    if body.features is not None:
        existing_features = league.get("features")
        if not isinstance(existing_features, dict):
            existing_features = {}
        next_features = {
            "tipping": bool(existing_features.get("tipping", bool(league.get("is_active", False)))),
            "match_load": bool(existing_features.get("match_load", True)),
            "xg_sync": bool(existing_features.get("xg_sync", False)),
            "odds_sync": bool(existing_features.get("odds_sync", False)),
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

    await _db.db.leagues.update_one({"_id": league_oid}, {"$set": updates})
    await _refresh_league_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=league_id,
        action="LEAGUE_UPDATE",
        metadata={"updates": updates},
        request=request,
    )
    updated = await _db.db.leagues.find_one({"_id": league_oid})
    return {"message": "League updated.", "item": _league_to_dict(updated or league)}


async def _run_single_league_sync(
    sport_key: str,
    season: int | None = None,
    full_season: bool = False,
) -> None:
    from app.workers.matchday_sync import sync_matchdays_for_sport

    try:
        await sync_matchdays_for_sport(
            sport_key,
            season=season,
            full_season=full_season,
        )
    except Exception:
        logger.exception("Manual league sync failed for %s", sport_key)


async def _check_football_data_import_rate_limit(
    admin_id: str,
    league_id: str,
) -> None:
    await _check_admin_import_rate_limit("football_data_import", admin_id, league_id)


async def _check_admin_import_rate_limit(
    import_key: str,
    admin_id: str,
    league_id: str,
) -> None:
    now = utcnow()
    limit_key = f"{import_key}:{admin_id}:{league_id}"
    existing = await _db.db.meta.find_one({"_id": limit_key}, {"last_requested_at": 1})
    if existing and existing.get("last_requested_at"):
        last_requested_at = ensure_utc(existing["last_requested_at"])
        retry_after = int(
            FOOTBALL_DATA_IMPORT_RATE_LIMIT_SECONDS
            - (now - last_requested_at).total_seconds()
        )
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "success": False,
                    "error": "rate_limited",
                    "retry_after_seconds": retry_after,
                    "last_request_at": last_requested_at.isoformat(),
                },
            )
    try:
        if existing:
            await _db.db.meta.update_one(
                {"_id": limit_key},
                {"$set": {"last_requested_at": now, "updated_at": now}},
            )
        else:
            await _db.db.meta.insert_one(
                {
                    "_id": limit_key,
                    "last_requested_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    except DuplicateKeyError:
        # Another request created the key concurrently; enforce by re-checking once.
        existing = await _db.db.meta.find_one({"_id": limit_key}, {"last_requested_at": 1})
        if existing and existing.get("last_requested_at"):
            last_requested_at = ensure_utc(existing["last_requested_at"])
            retry_after = int(
                FOOTBALL_DATA_IMPORT_RATE_LIMIT_SECONDS
                - (utcnow() - last_requested_at).total_seconds()
            )
            if retry_after > 0:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "success": False,
                        "error": "rate_limited",
                        "retry_after_seconds": retry_after,
                        "last_request_at": last_requested_at.isoformat(),
                    },
                )


@router.post("/leagues/{league_id}/sync")
async def trigger_league_sync_admin(
    league_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    body: LeagueSyncRequest | None = None,
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key.")

    season = int(body.season) if body and body.season is not None else None
    full_season = bool(body.full_season) if body else False
    background_tasks.add_task(_run_single_league_sync, sport_key, season, full_season)
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=league_id,
        action="LEAGUE_SYNC_TRIGGER",
        metadata={"sport_key": sport_key, "season": season, "full_season": full_season},
        request=request,
    )
    return {
        "message": "League sync queued.",
        "sport_key": sport_key,
        "season": season,
        "full_season": full_season,
    }


@router.post("/leagues/{league_id}/import-football-data")
async def trigger_league_football_data_import_admin(
    league_id: str,
    body: FootballDataImportRequest,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Run football-data.co.uk import for one league/season with optional dry-run preview."""
    admin_id = str(admin["_id"])
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid}, {"_id": 1, "football_data_last_import_at": 1})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    await _check_football_data_import_rate_limit(admin_id, league_id)

    t0 = _time.monotonic()
    result = await import_football_data_stats(
        league_id=league_oid,
        season=body.season,
        dry_run=body.dry_run,
    )
    duration_ms = int((_time.monotonic() - t0) * 1000)

    if not body.dry_run:
        now = utcnow()
        await _db.db.leagues.update_one(
            {"_id": league_oid},
            {
                "$set": {
                    "football_data_last_import_at": now,
                    "football_data_last_import_season": result.get("season"),
                    "football_data_last_import_by": admin_id,
                    "football_data_last_import_summary": {
                        "matched": int(result.get("matched", 0)),
                        "updated": int(result.get("updated", 0)),
                        "odds_snapshots_total": int(result.get("odds_snapshots_total", 0)),
                    },
                    "updated_at": now,
                }
            },
        )

    response = {
        "success": True,
        "league_id": league_id,
        "season": result.get("season"),
        "dry_run": body.dry_run,
        "rate_limit_window_seconds": FOOTBALL_DATA_IMPORT_RATE_LIMIT_SECONDS,
        "last_import_at": (
            ensure_utc(league.get("football_data_last_import_at")).isoformat()
            if league.get("football_data_last_import_at")
            else None
        ),
        "results": result,
    }

    await log_audit(
        actor_id=admin_id,
        target_id=league_id,
        action="LEAGUE_FOOTBALL_DATA_IMPORT",
        metadata={
            "season": body.season,
            "dry_run": body.dry_run,
            "duration_ms": duration_ms,
            "result": {
                "processed": result.get("processed", 0),
                "matched": result.get("matched", 0),
                "updated": result.get("updated", 0),
                "odds_snapshots_total": result.get("odds_snapshots_total", 0),
            },
        },
        request=request,
    )
    return response


@router.post("/leagues/{league_id}/import-football-data/async")
async def trigger_league_football_data_import_async_admin(
    league_id: str,
    body: FootballDataImportRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Queue football-data import as async admin job and return a job id."""
    admin_id = str(admin["_id"])
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid}, {"_id": 1})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    await _check_football_data_import_rate_limit(admin_id, league_id)
    now = utcnow()
    job_doc = {
        "type": "football_data_import",
        "league_id": league_oid,
        "admin_id": admin_id,
        "season": body.season,
        "dry_run": body.dry_run,
        "status": "queued",
        "phase": "queued",
        "progress": {"processed": 0, "total": 0, "percent": 0.0},
        "counters": {
            "matched": 0,
            "existing_matches": 0,
            "new_matches": 0,
            "updated": 0,
            "odds_snapshots_total": 0,
            "odds_ingest_inserted": 0,
            "odds_ingest_deduplicated": 0,
            "odds_ingest_markets_updated": 0,
            "odds_ingest_errors": 0,
        },
        "results": None,
        "error": None,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "finished_at": None,
    }
    insert_result = await _db.db.admin_import_jobs.insert_one(job_doc)
    job_id = insert_result.inserted_id
    background_tasks.add_task(
        _run_football_data_import_job,
        job_id,
        league_oid,
        body.season,
        body.dry_run,
        admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=league_id,
        action="LEAGUE_FOOTBALL_DATA_IMPORT_ASYNC",
        metadata={"job_id": str(job_id), "season": body.season, "dry_run": body.dry_run},
        request=request,
    )
    return {
        "accepted": True,
        "job_id": str(job_id),
        "league_id": league_id,
        "season": body.season,
        "dry_run": body.dry_run,
        "status": "queued",
    }


@router.post("/leagues/{league_id}/match-ingest/async")
async def trigger_unified_match_ingest_async_admin(
    league_id: str,
    body: UnifiedMatchIngestRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Queue unified match ingest from one provider source."""
    admin_id = str(admin["_id"])
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid}, {"_id": 1})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    source = str(body.source or "").strip().lower()
    if source not in {"football_data_uk", "football_data", "openligadb", "theoddsapi"}:
        raise HTTPException(status_code=400, detail="Unsupported source.")
    if source == "theoddsapi":
        raise HTTPException(status_code=400, detail="theoddsapi unified match ingest is not available yet.")
    if source in {"football_data", "openligadb"} and body.season is None:
        raise HTTPException(status_code=400, detail="season is required for this source.")

    await _check_admin_import_rate_limit(f"match_ingest_{source}", admin_id, league_id)
    now = utcnow()
    job_doc = {
        "type": "match_ingest_unified",
        "source": source,
        "league_id": league_oid,
        "admin_id": admin_id,
        "season": body.season,
        "dry_run": bool(body.dry_run),
        "status": "queued",
        "phase": "queued",
        "progress": {"processed": 0, "total": 0, "percent": 0.0},
        "counters": {
            "processed": 0,
            "created": 0,
            "updated": 0,
            "matched": 0,
            "skipped": 0,
            "conflicts": 0,
            "matched_by_external_id": 0,
            "matched_by_identity_window": 0,
            "unresolved_league": 0,
            "unresolved_team": 0,
            "team_name_conflict": 0,
            "other_conflicts": 0,
        },
        "results": None,
        "error": None,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "finished_at": None,
    }
    insert_result = await _db.db.admin_import_jobs.insert_one(job_doc)
    job_id = insert_result.inserted_id
    background_tasks.add_task(
        _run_unified_match_ingest_job,
        job_id,
        league_oid,
        source,
        body.season,
        bool(body.dry_run),
        admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id=league_id,
        action="LEAGUE_MATCH_INGEST_UNIFIED_ASYNC",
        metadata={
            "job_id": str(job_id),
            "source": source,
            "season": body.season,
            "dry_run": bool(body.dry_run),
        },
        request=request,
    )
    return {
        "accepted": True,
        "job_id": str(job_id),
        "league_id": league_id,
        "source": source,
        "season": body.season,
        "dry_run": bool(body.dry_run),
        "status": "queued",
    }


@router.post("/enrich-xg/async")
async def trigger_xg_enrichment_async_admin(
    body: XGEnrichmentRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Queue xG enrichment (Understat) as async admin job."""
    admin_id = str(admin["_id"])
    rate_scope = str(body.sport_key or "__all__")
    await _check_admin_import_rate_limit("xg_enrichment", admin_id, rate_scope)

    now = utcnow()
    job_doc = {
        "type": "xg_enrichment",
        "source": "understat",
        "admin_id": admin_id,
        "sport_key": body.sport_key,
        "season": body.season,
        "dry_run": bool(body.dry_run),
        "force": bool(body.force),
        "status": "queued",
        "phase": "queued",
        "progress": {"processed": 0, "total": 0, "percent": 0.0},
        "counters": {
            "total": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped": 0,
            "already_enriched": 0,
            "leagues_processed": 0,
            "seasons_processed": 0,
            "runs_total": 0,
            "runs_completed": 0,
        },
        "results": None,
        "error": None,
        "created_at": now,
        "started_at": None,
        "updated_at": now,
        "finished_at": None,
    }
    insert_result = await _db.db.admin_import_jobs.insert_one(job_doc)
    job_id = insert_result.inserted_id
    background_tasks.add_task(
        _run_xg_enrichment_job,
        job_id,
        body.sport_key,
        body.season,
        bool(body.dry_run),
        bool(body.force),
        admin_id,
    )
    await log_audit(
        actor_id=admin_id,
        target_id="xg_enrichment",
        action="XG_ENRICHMENT_ASYNC",
        metadata={
            "job_id": str(job_id),
            "sport_key": body.sport_key,
            "season": body.season,
            "dry_run": bool(body.dry_run),
            "force": bool(body.force),
        },
        request=request,
    )
    return {
        "accepted": True,
        "job_id": str(job_id),
        "source": "understat",
        "sport_key": body.sport_key,
        "season": body.season,
        "dry_run": bool(body.dry_run),
        "force": bool(body.force),
        "status": "queued",
    }


@router.get("/leagues/import-jobs/{job_id}")
async def get_league_import_job_status_admin(job_id: str, admin=Depends(get_admin_user)):
    oid = _parse_object_id(job_id, "job_id")
    doc = await _db.db.admin_import_jobs.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Import job not found.")
    return _serialize_import_job(doc)


@router.post("/leagues/{league_id}/import-stats")
async def trigger_league_stats_import_admin(
    league_id: str,
    request: Request,
    body: LeagueStatsImportBody = LeagueStatsImportBody(),
    admin=Depends(get_admin_user),
):
    """Deprecated alias for football-data import without dry-run."""
    data = FootballDataImportRequest(season=body.season, dry_run=False)
    return await trigger_league_football_data_import_admin(
        league_id=league_id,
        body=data,
        request=request,
        admin=admin,
    )


def _team_to_dict(doc: dict) -> dict:
    aliases = []
    for alias in doc.get("aliases", []):
        aliases.append(
            {
                "name": alias.get("name", ""),
                "normalized": alias.get("normalized", ""),
                "sport_key": alias.get("sport_key"),
                "source": alias.get("source"),
            }
        )
    return {
        "id": str(doc["_id"]),
        "display_name": doc.get("display_name", ""),
        "normalized_name": doc.get("normalized_name", ""),
        "sport_key": doc.get("sport_key"),
        "needs_review": bool(doc.get("needs_review", False)),
        "source": doc.get("source"),
        "aliases": aliases,
        "created_at": ensure_utc(doc.get("created_at")).isoformat() if doc.get("created_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
    }


async def _refresh_team_registry() -> None:
    await TeamRegistry.get().initialize()


def _parse_object_id(value: str, field_name: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.") from exc


class TeamAliasCreateBody(BaseModel):
    name: str
    sport_key: Optional[str] = None
    source: str = "admin"


class TeamAliasDeleteBody(BaseModel):
    name: str
    sport_key: Optional[str] = None


class TeamUpdateBody(BaseModel):
    display_name: Optional[str] = None
    needs_review: Optional[bool] = None


class TeamMergeBody(BaseModel):
    target_id: str


class AliasSuggestionApplyInput(BaseModel):
    id: str
    team_id: Optional[str] = None


class ApplyAliasSuggestionsBody(BaseModel):
    items: list[AliasSuggestionApplyInput]


class RejectAliasSuggestionBody(BaseModel):
    reason: Optional[str] = None


@router.get("/teams/alias-suggestions")
async def list_alias_suggestions_admin(
    status_filter: str = Query("pending", alias="status"),
    source: Optional[str] = Query(None),
    sport_key: Optional[str] = Query(None),
    league_id: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    admin=Depends(get_admin_user),
):
    """List alias suggestions persisted by TeamRegistry across all providers."""
    if isinstance(status_filter, str):
        status_value = status_filter.strip().lower() or "pending"
    else:
        status_value = "pending"
    if status_value not in {"pending", "applied", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid status.")

    query: dict[str, Any] = {"status": status_value}
    source_value = source.strip().lower() if isinstance(source, str) else ""
    sport_key_value = sport_key.strip() if isinstance(sport_key, str) else ""
    league_id_value = league_id.strip() if isinstance(league_id, str) else ""
    query_text = q.strip() if isinstance(q, str) else ""
    if source_value:
        query["source"] = source_value
    if sport_key_value:
        query["sport_key"] = sport_key_value
    if league_id_value:
        query["league_id"] = _parse_object_id(league_id_value, "league_id")
    if query_text:
        escaped = re.escape(query_text)
        query["$or"] = [
            {"raw_team_name": {"$regex": escaped, "$options": "i"}},
            {"normalized_name": {"$regex": escaped, "$options": "i"}},
        ]

    docs = await _db.db.team_alias_suggestions.find(query).sort("last_seen_at", -1).limit(limit).to_list(length=limit)
    team_ids: set[ObjectId] = set()
    league_ids: set[ObjectId] = set()
    for doc in docs:
        suggested_id = doc.get("suggested_team_id")
        applied_id = doc.get("applied_to_team_id")
        if isinstance(suggested_id, ObjectId):
            team_ids.add(suggested_id)
        if isinstance(applied_id, ObjectId):
            team_ids.add(applied_id)
        if isinstance(doc.get("league_id"), ObjectId):
            league_ids.add(doc["league_id"])

    teams_by_id: dict[ObjectId, dict[str, Any]] = {}
    if team_ids:
        team_docs = await _db.db.teams.find({"_id": {"$in": list(team_ids)}}, {"display_name": 1, "aliases": 1}).to_list(length=len(team_ids))
        teams_by_id = {row["_id"]: row for row in team_docs}

    leagues_by_id: dict[ObjectId, dict[str, Any]] = {}
    if league_ids:
        league_docs = await _db.db.leagues.find({"_id": {"$in": list(league_ids)}}, {"name": 1, "sport_key": 1}).to_list(length=len(league_ids))
        leagues_by_id = {row["_id"]: row for row in league_docs}

    items: list[dict[str, Any]] = []
    for doc in docs:
        normalized_name = str(doc.get("normalized_name") or "")
        sport_value = str(doc.get("sport_key") or "")
        suggested_team = teams_by_id.get(doc.get("suggested_team_id"))
        applied_team = teams_by_id.get(doc.get("applied_to_team_id"))
        if status_value == "pending" and suggested_team:
            alias_exists = False
            for alias in (suggested_team.get("aliases") or []):
                if not isinstance(alias, dict):
                    continue
                if str(alias.get("normalized") or "") != normalized_name:
                    continue
                alias_sport = str(alias.get("sport_key") or "")
                if not sport_value or not alias_sport or alias_sport == sport_value:
                    alias_exists = True
                    break
            if alias_exists:
                continue

        league_doc = leagues_by_id.get(doc.get("league_id"))
        items.append(
            {
                "id": str(doc.get("_id")),
                "status": str(doc.get("status") or "pending"),
                "source": str(doc.get("source") or ""),
                "sport_key": sport_value or None,
                "league_id": str(doc.get("league_id")) if doc.get("league_id") else None,
                "league_name": str((league_doc or {}).get("name") or ""),
                "league_external_id": str(doc.get("league_external_id") or "") or None,
                "raw_team_name": str(doc.get("raw_team_name") or ""),
                "normalized_name": normalized_name,
                "reason": str(doc.get("reason") or "unresolved_team"),
                "confidence": doc.get("confidence"),
                "seen_count": int(doc.get("seen_count") or 0),
                "first_seen_at": ensure_utc(doc.get("first_seen_at")).isoformat() if doc.get("first_seen_at") else None,
                "last_seen_at": ensure_utc(doc.get("last_seen_at")).isoformat() if doc.get("last_seen_at") else None,
                "suggested_team_id": str(doc.get("suggested_team_id")) if doc.get("suggested_team_id") else None,
                "suggested_team_name": str(doc.get("suggested_team_name") or (suggested_team or {}).get("display_name") or ""),
                "applied_to_team_id": str(doc.get("applied_to_team_id")) if doc.get("applied_to_team_id") else None,
                "applied_to_team_name": str((applied_team or {}).get("display_name") or ""),
                "sample_refs": doc.get("sample_refs") or [],
            }
        )

    return {"total": len(items), "items": items}


@router.post("/teams/alias-suggestions/apply")
async def apply_alias_suggestions_admin(
    body: ApplyAliasSuggestionsBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    if not body.items:
        raise HTTPException(status_code=400, detail="Provide at least one suggestion id.")
    registry = TeamRegistry.get()
    applied = 0
    failed: list[dict[str, Any]] = []
    for item in body.items:
        try:
            suggestion_oid = _parse_object_id(item.id, "id")
            suggestion_doc = await _db.db.team_alias_suggestions.find_one({"_id": suggestion_oid})
            if not suggestion_doc:
                failed.append({"id": item.id, "code": "not_found", "message": "Suggestion not found"})
                continue
            if str(suggestion_doc.get("status") or "pending") != "pending":
                failed.append({"id": item.id, "code": "invalid_status", "message": "Suggestion is not pending"})
                continue

            target_team_id = item.team_id or (str(suggestion_doc.get("suggested_team_id")) if suggestion_doc.get("suggested_team_id") else "")
            if not target_team_id:
                failed.append({"id": item.id, "code": "missing_target", "message": "No target team_id provided"})
                continue
            team_oid = _parse_object_id(target_team_id, "team_id")
            alias_name = str(suggestion_doc.get("raw_team_name") or "").strip()
            if not alias_name:
                failed.append({"id": item.id, "code": "invalid_alias", "message": "Suggestion has empty team name"})
                continue

            added = await registry.add_alias(
                team_oid,
                alias_name,
                sport_key=suggestion_doc.get("sport_key"),
                source="admin_alias_suggestion",
                refresh_cache=False,
            )
            await _db.db.team_alias_suggestions.delete_one({"_id": suggestion_oid})
            applied += 1
            if not added:
                failed.append({"id": item.id, "code": "duplicate_alias", "message": "Alias already exists"})
        except HTTPException:
            failed.append({"id": item.id, "code": "invalid_team_id", "message": "Invalid team_id"})
        except ValueError as exc:
            msg = str(exc).lower()
            code = "alias_error"
            if "team not found" in msg:
                code = "team_not_found"
            elif "normalization is empty" in msg or "empty" in msg:
                code = "invalid_alias"
            failed.append({"id": item.id, "code": code, "message": str(exc)})
        except Exception as exc:
            failed.append({"id": item.id, "code": "unexpected_error", "message": str(exc)})

    await registry.initialize()
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id="team_alias_suggestions",
        action="TEAM_ALIAS_SUGGESTIONS_APPLY",
        metadata={
            "requested": len(body.items),
            "applied": applied,
            "failed": len(failed),
        },
        request=request,
    )
    return {"applied": applied, "failed": failed}


@router.post("/teams/alias-suggestions/{suggestion_id}/reject")
async def reject_alias_suggestion_admin(
    suggestion_id: str,
    body: RejectAliasSuggestionBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    suggestion_oid = _parse_object_id(suggestion_id, "suggestion_id")
    suggestion_doc = await _db.db.team_alias_suggestions.find_one({"_id": suggestion_oid}, {"status": 1})
    if not suggestion_doc:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    if str(suggestion_doc.get("status") or "pending") != "pending":
        raise HTTPException(status_code=400, detail="Suggestion is not pending.")

    now = utcnow()
    await _db.db.team_alias_suggestions.update_one(
        {"_id": suggestion_oid},
        {
            "$set": {
                "status": "rejected",
                "rejected_at": now,
                "rejected_reason": str(body.reason or "").strip() or None,
                "updated_at": now,
            }
        },
    )
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=str(suggestion_oid),
        action="TEAM_ALIAS_SUGGESTION_REJECT",
        metadata={"reason": str(body.reason or "").strip() or None},
        request=request,
    )
    return {"ok": True, "id": str(suggestion_oid)}


@router.get("/teams")
async def list_teams_admin(
    needs_review: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    query: dict = {}
    if needs_review is not None:
        query["needs_review"] = needs_review
    if search:
        escaped = re.escape(search.strip())
        query["$or"] = [
            {"display_name": {"$regex": escaped, "$options": "i"}},
            {"aliases.name": {"$regex": escaped, "$options": "i"}},
        ]

    total = await _db.db.teams.count_documents(query)
    docs = await _db.db.teams.find(query).sort("updated_at", -1).skip(offset).limit(limit).to_list(length=limit)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_team_to_dict(d) for d in docs],
    }


@router.post("/teams/{team_id}/aliases")
async def add_team_alias(
    team_id: str,
    body: TeamAliasCreateBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    team_oid = _parse_object_id(team_id, "team_id")
    team = await _db.db.teams.find_one({"_id": team_oid})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")

    normalized = normalize_team_name(body.name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Alias normalization is empty.")

    alias_doc = {
        "name": body.name.strip(),
        "normalized": normalized,
        "sport_key": body.sport_key or team.get("sport_key"),
        "source": body.source or "admin",
    }
    now = utcnow()
    await _db.db.teams.update_one(
        {"_id": team_oid},
        {"$addToSet": {"aliases": alias_doc}, "$set": {"updated_at": now}},
    )
    cleanup_result = await _db.db.team_alias_suggestions.delete_many(
        {
            "status": "pending",
            "normalized_name": normalized,
            "$or": [
                {"sport_key": alias_doc.get("sport_key")},
                {"sport_key": None},
                {"sport_key": ""},
            ],
        },
    )
    await _refresh_team_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=team_id,
        action="TEAM_ALIAS_ADD",
        metadata={"alias": alias_doc, "alias_suggestions_deleted": int(cleanup_result.deleted_count or 0)},
        request=request,
    )
    return {"message": "Alias added.", "alias": alias_doc}


@router.delete("/teams/{team_id}/aliases")
async def remove_team_alias(
    team_id: str,
    body: TeamAliasDeleteBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    team_oid = _parse_object_id(team_id, "team_id")
    team = await _db.db.teams.find_one({"_id": team_oid})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")

    normalized = normalize_team_name(body.name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Alias normalization is empty.")

    pull_filter: dict = {"normalized": normalized}
    if body.sport_key:
        pull_filter["sport_key"] = body.sport_key
    now = utcnow()
    await _db.db.teams.update_one(
        {"_id": team_oid},
        {"$pull": {"aliases": pull_filter}, "$set": {"updated_at": now}},
    )
    await _refresh_team_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=team_id,
        action="TEAM_ALIAS_REMOVE",
        metadata={"normalized": normalized, "sport_key": body.sport_key},
        request=request,
    )
    return {"message": "Alias removed.", "normalized": normalized}


@router.patch("/teams/{team_id}")
async def update_team_admin(
    team_id: str,
    body: TeamUpdateBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    if body.display_name is None and body.needs_review is None:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    team_oid = _parse_object_id(team_id, "team_id")
    team = await _db.db.teams.find_one({"_id": team_oid})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")

    updates: dict = {"updated_at": utcnow()}
    if body.display_name is not None:
        updates["display_name"] = body.display_name.strip()
    if body.needs_review is not None:
        updates["needs_review"] = bool(body.needs_review)

    await _db.db.teams.update_one({"_id": team_oid}, {"$set": updates})
    await _refresh_team_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=team_id,
        action="TEAM_UPDATE",
        metadata={"updates": updates},
        request=request,
    )
    return {"message": "Team updated."}


@router.post("/teams/{team_id}/merge")
async def merge_team_admin(
    team_id: str,
    body: TeamMergeBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    source_id = _parse_object_id(team_id, "team_id")
    target_id = _parse_object_id(body.target_id, "target_id")
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ.")

    stats = await merge_teams(source_id, target_id)
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=str(target_id),
        action="TEAM_MERGE",
        metadata={"source_id": str(source_id), "target_id": str(target_id), "stats": stats},
        request=request,
    )
    return {"message": "Teams merged.", "stats": stats}


# ---------------------------------------------------------------------------
# Qbot Lab — Strategy Dashboard
# ---------------------------------------------------------------------------

@router.get("/qbot/strategies")
async def qbot_strategies(admin=Depends(get_admin_user)):
    """Qbot strategies grouped by league with active/shadow/archived categories."""
    strategies = await _db.db.qbot_strategies.find({}).sort("created_at", -1).to_list(2000)

    now = utcnow()
    gene_ranges = {
        "min_edge": [3.0, 15.0],
        "min_confidence": [0.30, 0.80],
        "sharp_weight": [0.5, 2.0],
        "momentum_weight": [0.5, 2.0],
        "rest_weight": [0.0, 1.5],
        "kelly_fraction": [0.05, 0.50],
        "max_stake": [10.0, 100.0],
        "home_bias": [0.80, 1.20],
        "away_bias": [0.80, 1.20],
        "h2h_weight": [0.0, 2.0],
        "draw_threshold": [0.0, 1.0],
        "volatility_buffer": [0.0, 0.20],
        "bayes_trust_factor": [0.0, 1.5],
    }

    by_sport_docs: dict[str, list[dict]] = {}
    for doc in strategies:
        sport_key = doc.get("sport_key", "all")
        by_sport_docs.setdefault(sport_key, []).append(doc)

    def classify_strategy(doc: dict, val_f: dict, stress: dict, stage_used: int | None) -> str:
        val_roi = float(val_f.get("roi", 0.0))
        ruin_prob = float(stress.get("monte_carlo_ruin_prob", 1.0))
        stress_passed = bool(stress.get("stress_passed", False))
        is_active = bool(doc.get("is_active", False))
        is_shadow = bool(doc.get("is_shadow", False))

        if val_roi < 0 or ruin_prob > 0.20:
            return "failed"
        if is_active and stress_passed and val_roi > 0:
            return "active"
        if is_shadow or stage_used == 2:
            return "shadow"
        return "shadow"

    def strategy_archetype(doc: dict) -> str:
        raw = doc.get("archetype")
        if isinstance(raw, str) and raw:
            return raw
        if bool(doc.get("is_ensemble", False)):
            return "consensus"
        return "standard"

    def summarize_identity(doc: dict) -> dict:
        val_f = doc.get("validation_fitness", {}) or {}
        stress = doc.get("stress_test", {}) or {}
        notes = doc.get("optimization_notes", {}) or {}
        stage_info = notes.get("stage_info", {}) or {}
        category = classify_strategy(doc, val_f, stress, stage_info.get("stage_used"))
        return {
            "id": str(doc["_id"]),
            "archetype": strategy_archetype(doc),
            "version": doc.get("version", "v1"),
            "generation": doc.get("generation", 0),
            "is_active": bool(doc.get("is_active", False)),
            "is_shadow": bool(doc.get("is_shadow", False)),
            "category": category,
            "roi": float(val_f.get("roi", 0.0)),
            "total_bets": int(val_f.get("total_bets", 0)),
            "created_at": ensure_utc(doc.get("created_at", now)).isoformat(),
        }

    def active_comparison(doc: dict, active_doc: dict | None) -> dict | None:
        if not active_doc:
            return None
        if str(doc.get("_id")) == str(active_doc.get("_id")):
            return None
        val_doc = doc.get("validation_fitness", {}) or {}
        val_active = active_doc.get("validation_fitness", {}) or {}
        return {
            "active_id": str(active_doc["_id"]),
            "roi_diff": round(float(val_doc.get("roi", 0.0)) - float(val_active.get("roi", 0.0)), 4),
            "bets_diff": int(val_doc.get("total_bets", 0)) - int(val_active.get("total_bets", 0)),
            "sharpe_diff": round(float(val_doc.get("sharpe", 0.0)) - float(val_active.get("sharpe", 0.0)), 4),
        }

    def build_item(
        doc: dict,
        *,
        selection_source: str,
        active_doc: dict | None = None,
        identities: dict[str, dict] | None = None,
    ) -> dict:
        created = ensure_utc(doc.get("created_at", now))
        age_days = (now - created).days
        train_f = doc.get("training_fitness", {}) or {}
        val_f = doc.get("validation_fitness", {}) or {}
        stress = doc.get("stress_test", {}) or {}
        notes = doc.get("optimization_notes", {}) or {}
        stage_info = notes.get("stage_info", {}) or {}
        rescue_log = notes.get("rescue_log", {}) or {}
        stage_used = stage_info.get("stage_used")
        train_roi = float(train_f.get("roi", 0.0))
        val_roi = float(val_f.get("roi", 0.0))
        overfit_warning = (train_roi - val_roi) > 0.15
        category = classify_strategy(doc, val_f, stress, stage_used)

        if stage_used == 2:
            stage_label = "Stage: 2 (Relaxed)"
        elif stage_used == 1:
            stage_label = "Stage: 1 (Ideal)"
        else:
            stage_label = f"Stage: {stage_used}" if stage_used is not None else "Stage: n/a"

        rescue_applied = bool(rescue_log.get("applied", False))
        rescue_scale = rescue_log.get("final_risk_scaling")
        if rescue_applied and rescue_scale is not None:
            rescue_label = f"Rescue: Applied (Scale {rescue_scale})"
        elif rescue_applied:
            rescue_label = "Rescue: Applied"
        else:
            rescue_label = "Rescue: Not Applied"

        return {
            "id": str(doc["_id"]),
            "sport_key": doc.get("sport_key", "all"),
            "version": doc.get("version", "v1"),
            "generation": doc.get("generation", 0),
            "dna": doc.get("dna", {}),
            "training_fitness": train_f,
            "validation_fitness": val_f,
            "stress_test": stress if stress else None,
            "is_active": bool(doc.get("is_active", False)),
            "is_shadow": bool(doc.get("is_shadow", False)),
            "created_at": created.isoformat(),
            "age_days": age_days,
            "overfit_warning": overfit_warning,
            "category": category,
            "optimization_notes": notes,
            "stage_used": stage_used,
            "stage_label": stage_label,
            "rescue_applied": rescue_applied,
            "rescue_scale": rescue_scale,
            "rescue_label": rescue_label,
            "selection_source": selection_source,
            "archetype": strategy_archetype(doc),
            "identities": identities,
            "active_comparison": active_comparison(doc, active_doc),
        }

    representatives: list[dict] = []
    shadow_extras: list[dict] = []
    by_sport: dict[str, dict] = {}

    for sport_key, docs in by_sport_docs.items():
        active_doc = next((d for d in docs if d.get("is_active", False)), None)
        selected = active_doc or docs[0]
        identities: dict[str, dict] = {}
        for d in docs:
            key = strategy_archetype(d)
            if key not in {"consensus", "profit_hunter", "volume_grinder"}:
                continue
            if key not in identities:
                identities[key] = summarize_identity(d)
        if "consensus" not in identities:
            identities["consensus"] = summarize_identity(selected)

        item = build_item(
            selected,
            selection_source="active" if active_doc is not None else "latest",
            active_doc=active_doc,
            identities=identities,
        )
        representatives.append(item)
        by_sport[sport_key] = {
            "strategy": item,
            "category": item["category"],
            "identities": identities,
        }

        # Bonus: expose shadow identities even when league representative is active.
        seen_shadow_keys: set[str] = set()
        for d in docs:
            if str(d.get("_id")) == str(selected.get("_id")):
                continue
            shadow_cat = classify_strategy(
                d,
                d.get("validation_fitness", {}) or {},
                d.get("stress_test", {}) or {},
                ((d.get("optimization_notes", {}) or {}).get("stage_info", {}) or {}).get("stage_used"),
            )
            if shadow_cat != "shadow":
                continue
            key = strategy_archetype(d)
            normalized = key if key in {"consensus", "profit_hunter", "volume_grinder"} else "standard"
            if normalized in seen_shadow_keys:
                continue
            seen_shadow_keys.add(normalized)
            shadow_extras.append(
                build_item(
                    d,
                    selection_source="shadow_identity",
                    active_doc=active_doc,
                    identities=identities,
                )
            )

    active = [r for r in representatives if r["category"] == "active"]
    shadow = [r for r in representatives if r["category"] == "shadow"]
    failed = [r for r in representatives if r["category"] == "failed"]

    existing_shadow_ids = {s["id"] for s in shadow}
    for extra in shadow_extras:
        if extra["id"] not in existing_shadow_ids:
            shadow.append(extra)
            existing_shadow_ids.add(extra["id"])

    active.sort(key=lambda r: float(r.get("validation_fitness", {}).get("roi", -999)), reverse=True)
    shadow.sort(key=lambda r: float(r.get("validation_fitness", {}).get("roi", -999)), reverse=True)
    failed.sort(
        key=lambda r: (
            float(r.get("stress_test", {}).get("monte_carlo_ruin_prob", 0.0)),
            -float(r.get("validation_fitness", {}).get("roi", 0.0)),
        ),
        reverse=True,
    )

    results = active + shadow + failed
    count_active = len(active)
    count_shadow = len(shadow)
    count_failed = len(failed)
    portfolio_avg_roi = (
        sum(float(r.get("validation_fitness", {}).get("roi", 0.0)) for r in active) / count_active
        if count_active
        else 0.0
    )
    worst_league = None
    worst_roi = 999.0
    oldest_days = 0
    all_stress_passed = True
    for r in results:
        val_roi = float(r.get("validation_fitness", {}).get("roi", 0.0))
        if val_roi < worst_roi:
            worst_roi = val_roi
            worst_league = r.get("sport_key", "all")
        oldest_days = max(oldest_days, int(r.get("age_days", 0)))
        stress = r.get("stress_test") or {}
        if r["category"] == "active" and not bool(stress.get("stress_passed", False)):
            all_stress_passed = False

    return {
        "strategies": results,
        "categories": {
            "active": active,
            "shadow": shadow,
            "failed": failed,
            "archived": failed,
        },
        "by_sport": by_sport,
        "gene_ranges": gene_ranges,
        "summary": {
            "portfolio_avg_roi": round(portfolio_avg_roi, 4),
            "count_active": count_active,
            "count_shadow": count_shadow,
            "count_failed": count_failed,
            "total_active": count_active,
            "avg_val_roi": round(portfolio_avg_roi, 4),
            "worst_league": worst_league,
            "worst_roi": round(worst_roi, 4) if worst_league else 0.0,
            "oldest_strategy_days": oldest_days,
            "all_stress_passed": all_stress_passed,
        },
    }


@router.get("/qbot/strategies/{strategy_id}/backtest")
async def qbot_strategy_backtest(
    strategy_id: str,
    since_date: str | None = Query(None, description="ISO date filter start"),
    admin=Depends(get_admin_user),
):
    """Run an admin backtest equity-curve simulation for one strategy."""
    if not ObjectId.is_valid(strategy_id):
        raise HTTPException(status_code=400, detail="Invalid strategy id.")

    strategy = await _db.db.qbot_strategies.find_one({"_id": ObjectId(strategy_id)})
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    return await simulate_strategy_backtest(
        strategy,
        starting_bankroll=1000.0,
        since_date=since_date,
    )


@router.get("/qbot/strategies/{strategy_id}/backtest/ledger")
async def qbot_strategy_backtest_ledger(
    strategy_id: str,
    limit: int = Query(24, ge=0, description="0 = all ledger rows"),
    since_date: str | None = Query(None, description="ISO date filter start"),
    admin=Depends(get_admin_user),
):
    """Return detailed backtest bet ledger for one strategy."""
    if not ObjectId.is_valid(strategy_id):
        raise HTTPException(status_code=400, detail="Invalid strategy id.")

    strategy = await _db.db.qbot_strategies.find_one({"_id": ObjectId(strategy_id)})
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    result = await simulate_strategy_backtest(
        strategy,
        starting_bankroll=1000.0,
        limit_ledger=(None if limit == 0 else limit),
        since_date=since_date,
    )
    return {
        "strategy_id": result["strategy_id"],
        "sport_key": result["sport_key"],
        "starting_bankroll": result["starting_bankroll"],
        "ending_bankroll": result["ending_bankroll"],
        "total_bets": result["total_bets"],
        "wins": result["wins"],
        "win_rate": result["win_rate"],
        "weighted_roi": result.get("weighted_roi", 0.0),
        "weighted_profit": result.get("weighted_profit", 0.0),
        "weighted_staked": result.get("weighted_staked", 0.0),
        "ledger": result["ledger"],
        "window": result.get("window", {}),
    }


@router.get("/qbot/strategies/{strategy_id}")
async def qbot_strategy_detail(strategy_id: str, admin=Depends(get_admin_user)):
    """Return one strategy plus available league identities."""
    if not ObjectId.is_valid(strategy_id):
        raise HTTPException(status_code=400, detail="Invalid strategy id.")

    strategy = await _db.db.qbot_strategies.find_one({"_id": ObjectId(strategy_id)})
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    now = utcnow()

    def classify(doc: dict) -> str:
        val_f = doc.get("validation_fitness", {}) or {}
        stress = doc.get("stress_test", {}) or {}
        stage_used = ((doc.get("optimization_notes", {}) or {}).get("stage_info", {}) or {}).get("stage_used")
        val_roi = float(val_f.get("roi", 0.0))
        ruin_prob = float(stress.get("monte_carlo_ruin_prob", 1.0))
        stress_passed = bool(stress.get("stress_passed", False))
        is_active = bool(doc.get("is_active", False))
        is_shadow = bool(doc.get("is_shadow", False))
        if val_roi < 0 or ruin_prob > 0.20:
            return "failed"
        if is_active and stress_passed and val_roi > 0:
            return "active"
        if is_shadow or stage_used == 2:
            return "shadow"
        return "shadow"

    def archetype_of(doc: dict) -> str:
        raw = doc.get("archetype")
        if isinstance(raw, str) and raw:
            return raw
        if bool(doc.get("is_ensemble", False)):
            return "consensus"
        return "standard"

    def identity_row(doc: dict) -> dict:
        val_f = doc.get("validation_fitness", {}) or {}
        created = ensure_utc(doc.get("created_at", now))
        return {
            "id": str(doc["_id"]),
            "archetype": archetype_of(doc),
            "version": doc.get("version", "v1"),
            "generation": doc.get("generation", 0),
            "is_active": bool(doc.get("is_active", False)),
            "is_shadow": bool(doc.get("is_shadow", False)),
            "category": classify(doc),
            "roi": float(val_f.get("roi", 0.0)),
            "total_bets": int(val_f.get("total_bets", 0)),
            "created_at": created.isoformat(),
        }

    sport_key = strategy.get("sport_key", "all")
    docs = await _db.db.qbot_strategies.find({"sport_key": sport_key}).sort("created_at", -1).to_list(200)
    identities: dict[str, dict] = {}
    for doc in docs:
        archetype = archetype_of(doc)
        if archetype in {"consensus", "profit_hunter", "volume_grinder"} and archetype not in identities:
            identities[archetype] = identity_row(doc)
    own_archetype = archetype_of(strategy)
    if own_archetype in {"consensus", "profit_hunter", "volume_grinder"}:
        identities[own_archetype] = identity_row(strategy)
    if "consensus" not in identities:
        identities["consensus"] = identity_row(strategy)

    created = ensure_utc(strategy.get("created_at", now))
    train_f = strategy.get("training_fitness", {}) or {}
    val_f = strategy.get("validation_fitness", {}) or {}
    stress = strategy.get("stress_test", {}) or {}
    return {
        "id": str(strategy["_id"]),
        "sport_key": sport_key,
        "version": strategy.get("version", "v1"),
        "generation": strategy.get("generation", 0),
        "dna": strategy.get("dna", {}),
        "training_fitness": train_f,
        "validation_fitness": val_f,
        "stress_test": stress,
        "is_active": bool(strategy.get("is_active", False)),
        "is_shadow": bool(strategy.get("is_shadow", False)),
        "is_ensemble": bool(strategy.get("is_ensemble", False)),
        "archetype": archetype_of(strategy),
        "created_at": created.isoformat(),
        "age_days": (now - created).days,
        "category": classify(strategy),
        "optimization_notes": strategy.get("optimization_notes", {}) or {},
        "identities": identities,
    }


@router.post("/qbot/strategies/{strategy_id}/activate")
async def qbot_strategy_activate(strategy_id: str, admin=Depends(get_admin_user)):
    """Activate one strategy for its league and deactivate previous active strategy."""
    if not ObjectId.is_valid(strategy_id):
        raise HTTPException(status_code=400, detail="Invalid strategy id.")

    strategy = await _db.db.qbot_strategies.find_one({"_id": ObjectId(strategy_id)})
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    sport_key = strategy.get("sport_key", "all")
    await _db.db.qbot_strategies.update_many(
        {"sport_key": sport_key, "is_active": True},
        {"$set": {"is_active": False}},
    )
    await _db.db.qbot_strategies.update_one(
        {"_id": ObjectId(strategy_id)},
        {"$set": {"is_active": True, "is_shadow": False}},
    )
    return {"status": "activated", "strategy_id": strategy_id, "sport_key": sport_key}


@router.get("/odds/{match_id}")
async def admin_odds_debug(match_id: str, limit: int = Query(500, ge=10, le=5000), admin=Depends(get_admin_user)):
    """Admin debug: current odds_meta plus raw odds_events timeline for one match."""
    if not ObjectId.is_valid(match_id):
        raise HTTPException(status_code=400, detail="Invalid match id.")
    oid = ObjectId(match_id)

    match = await _db.db.matches.find_one(
        {"_id": oid},
        {"_id": 1, "sport_key": 1, "home_team": 1, "away_team": 1, "odds_meta": 1},
    )
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")

    events = await _db.db.odds_events.find(
        {"match_id": oid},
        {
            "_id": 0,
            "provider": 1,
            "market": 1,
            "selection_key": 1,
            "price": 1,
            "line": 1,
            "snapshot_at": 1,
            "ingested_at": 1,
        },
    ).sort("snapshot_at", -1).limit(limit).to_list(length=limit)

    provider_count = {}
    dropped_by_line = {}
    stale_excluded = {}
    markets = ((match.get("odds_meta") or {}).get("markets") or {})
    for market, node in markets.items():
        provider_count[market] = int(node.get("provider_count", 0) or 0)
        dropped_by_line[market] = int(node.get("dropped_by_line", 0) or 0)
        stale_excluded[market] = int(node.get("stale_excluded", 0) or 0)

    return {
        "match": {
            "id": str(match["_id"]),
            "sport_key": match.get("sport_key"),
            "home_team": match.get("home_team"),
            "away_team": match.get("away_team"),
            "odds_meta": match.get("odds_meta", {}),
        },
        "diagnostics": {
            "provider_count": provider_count,
            "dropped_by_line": dropped_by_line,
            "stale_excluded": stale_excluded,
        },
        "events": events,
        "event_count": len(events),
    }
