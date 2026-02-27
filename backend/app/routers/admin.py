"""
backend/app/routers/admin.py

Purpose:
    Admin HTTP router for operational controls across users, matches, workers,
    Team Tower, League Tower, and Qbot tooling.

Dependencies:
    - app.services.auth_service
    - app.services.audit_service
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
from app.services.admin_view_catalog_service import list_view_catalog
from app.services.auth_service import get_admin_user, invalidate_user_tokens
from app.services.audit_service import log_audit
from app.services.league_service import (
    LeagueRegistry,
    default_current_season_for_league,
    default_season_start_month_for_league,
    invalidate_navigation_cache,
    update_league_order,
)
from app.services.qbot_backtest_service import simulate_strategy_backtest
from app.services.event_bus import event_bus
from app.services.event_bus_monitor import event_bus_monitor
from app.services.provider_settings_service import provider_settings_service
from app.services.persona_policy_service import get_persona_policy_service
from app.services.referee_dna_service import build_referee_profiles
from app.services.sportmonks_connector import sportmonks_connector
from app.providers.sportmonks import sportmonks_provider
from app.config import settings
from app.utils import ensure_utc, utcnow
from app.workers._state import get_synced_at, get_worker_state

logger = logging.getLogger("quotico.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])
SPORTMONKS_IMPORT_RATE_LIMIT_SECONDS = 10
ADMIN_MATCHES_CACHE_TTL_SECONDS = 30
_ADMIN_MATCHES_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}


def _admin_matches_cache_invalidate() -> None:
    _ADMIN_MATCHES_CACHE.clear()


def _admin_matches_cache_key(
    *,
    page: int,
    page_size: int,
    league_id: int | None,
    status_filter: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    manual_check: bool | None,
    odds_available: bool | None,
    search: str | None,
) -> str:
    return "|".join(
        [
            f"p={page}",
            f"ps={page_size}",
            f"l={league_id if league_id is not None else ''}",
            f"s={status_filter or ''}",
            f"df={ensure_utc(date_from).isoformat() if date_from else ''}",
            f"dt={ensure_utc(date_to).isoformat() if date_to else ''}",
            f"mc={manual_check if manual_check is not None else ''}",
            f"oa={odds_available if odds_available is not None else ''}",
            f"q={(search or '').strip().lower()}",
        ]
    )


# --- V3 league name cache (for admin match views) ---

_V3_LEAGUE_MAP: dict[int, str] = {}
_V3_LEAGUE_MAP_TS: datetime | None = None


async def _get_v3_league_map() -> dict[int, str]:
    """Return cached league_id → display name map from league_registry_v3, refreshing every 10 min."""
    global _V3_LEAGUE_MAP, _V3_LEAGUE_MAP_TS
    now = utcnow()
    if _V3_LEAGUE_MAP_TS and (now - ensure_utc(_V3_LEAGUE_MAP_TS)).total_seconds() < 600:
        return _V3_LEAGUE_MAP
    rows = await _db.db.league_registry_v3.find({}, {"_id": 1, "name": 1}).to_list(length=500)
    _V3_LEAGUE_MAP = {
        int(r["_id"]): str(r.get("name") or "")
        for r in rows
        if isinstance(r.get("_id"), int)
    }
    _V3_LEAGUE_MAP_TS = now
    return _V3_LEAGUE_MAP


def _referee_payload(
    *,
    profile: dict[str, Any] | None,
    referee_id: int | None,
    referee_name: str | None,
    include_detail: bool = False,
) -> dict[str, Any] | None:
    if referee_id is None and not profile:
        return None
    if profile:
        payload: dict[str, Any] = {
            "id": int(profile.get("id") or (referee_id or 0)),
            "name": str(profile.get("name") or referee_name or ""),
            "strictness_index": float(profile.get("strictness_index") or 100.0),
            "strictness_band": str(profile.get("strictness_band") or "normal"),
            "avg_yellow": float(profile.get("avg_yellow") or 0.0),
            "avg_red": float(profile.get("avg_red") or 0.0),
            "penalty_pct": float(profile.get("penalty_pct") or 0.0),
        }
        if include_detail:
            payload["season_avg"] = profile.get("season_avg") or None
            payload["career_avg"] = profile.get("career_avg") or None
            payload["trend"] = str(profile.get("trend") or "flat")
        return payload
    return {
        "id": int(referee_id or 0),
        "name": str(referee_name or ""),
        "strictness_index": 100.0,
        "strictness_band": "normal",
        "avg_yellow": 0.0,
        "avg_red": 0.0,
        "penalty_pct": 0.0,
    }


# --- Request models ---

class PointsAdjust(BaseModel):
    delta: float
    reason: str


class AdminTipPersonaBody(BaseModel):
    tip_persona: str


class AdminTipOverrideBody(BaseModel):
    tip_override_persona: str | None = None


class TipPolicyRuleBody(BaseModel):
    when: dict[str, Any] = {}
    set_output_level: str


class TipPolicyPatchBody(BaseModel):
    rules: list[TipPolicyRuleBody]
    note: str | None = None


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
    total_matches = await _db.db.matches_v3.count_documents({})
    pending_matches = await _db.db.matches_v3.count_documents({"status": {"$in": ["SCHEDULED", "LIVE"]}})
    completed_matches = await _db.db.matches_v3.count_documents({"status": "FINISHED"})
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
    }


# --- Provider Status ---

# Worker definitions: id -> (label, provider, import path)
_WORKER_REGISTRY: dict[str, dict] = {
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
    "match_resolver", "matchday_sync",
    "leaderboard", "matchday_resolver", "matchday_leaderboard",
    "quotico_tip_worker",
    "calibration_eval", "calibration_refine", "calibration_explore", "reliability_check"
}

_SUPPORTED_PROVIDER_SETTINGS = {
    "sportmonks",
    "understat",
}

_TIP_PERSONA_VALUES = {"casual", "pro", "silent", "experimental"}
_OUTPUT_LEVEL_VALUES = {"none", "summary", "full", "experimental"}


@router.get("/provider-status")
async def provider_status(admin=Depends(get_admin_user)):
    """Aggregated status of all providers and background workers."""
    from app.main import scheduler, automation_enabled, automated_job_count

    # Provider health
    providers = {
        "sportmonks": {"label": "Sportmonks", "status": "ok"},
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

    odds_status = await _db.db.meta.find_one(
        {"_id": "odds_scheduler_status"},
        {"_id": 0, "last_tick_at": 1, "rounds_synced": 1, "fixtures_synced": 1, "matches_in_window": 1, "tier_breakdown": 1},
    ) or {}
    return {
        "providers": providers,
        "workers": workers,
        "heartbeat": {
            "enabled": bool(settings.METRICS_HEARTBEAT_ENABLED),
            "last_tick_at": (
                ensure_utc(odds_status.get("last_tick_at")).isoformat()
                if odds_status.get("last_tick_at")
                else None
            ),
            "rounds_synced": int(odds_status.get("rounds_synced") or 0),
            "fixtures_synced": int(odds_status.get("fixtures_synced") or 0),
            "matches_in_window": int(odds_status.get("matches_in_window") or 0),
            "tier_breakdown": odds_status.get("tier_breakdown") if isinstance(odds_status.get("tier_breakdown"), dict) else {},
        },
        "automated_workers_enabled": automation_enabled(),
        "automated_workers_scheduled_jobs": automated_job_count(),
        "scheduler_running": bool(scheduler.running),
    }


@router.get("/views/catalog")
async def admin_views_catalog(admin=Depends(get_admin_user)):
    _ = admin
    return list_view_catalog()


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
    league_id: Optional[int] = None
    api_key: str


class ProviderSecretClearBody(BaseModel):
    scope: str = "global"
    league_id: Optional[int] = None


class ProviderProbeBody(BaseModel):
    league_id: Optional[int] = None


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
    league_id: Optional[int] = None,
    admin=Depends(get_admin_user),
):
    """List effective runtime provider settings."""
    items: list[dict[str, Any]] = []
    for provider in sorted(_SUPPORTED_PROVIDER_SETTINGS):
        resolved = await provider_settings_service.get_effective(
            provider,
            league_id=league_id,
            include_secret=True,
        )
        items.append(_mask_effective(resolved))
    return {
        "items": items,
        "league_id": int(league_id) if isinstance(league_id, int) else None,
    }


@router.get("/provider-settings/{provider}")
async def get_provider_settings(
    provider: str,
    league_id: Optional[int] = None,
    admin=Depends(get_admin_user),
):
    """Get effective runtime settings for one provider."""
    normalized = _ensure_supported_provider(provider)
    resolved = await provider_settings_service.get_effective(
        normalized,
        league_id=league_id,
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
    league_id: int,
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
            "league_id": int(league_id) if isinstance(league_id, int) else None,
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
    league_id: Optional[int] = None,
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
    elif normalized in {"sportmonks", "sportmonks"} and not api_key:
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
    league_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    days: int = Query(default=0, ge=0, le=3650),
    admin=Depends(get_admin_user),
):
    """List Engine Time Machine justice snapshots for admin analytics UI."""
    query: dict[str, Any] = {}
    if league_id is not None:
        query["league_id"] = league_id
    if days > 0:
        query["snapshot_date"] = {"$gte": utcnow() - timedelta(days=days)}

    projection = {
        "league_id": 1,
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
                "league_id": row.get("league_id") if isinstance(row.get("league_id"), int) else None,
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

    available_sports = await _db.db.engine_time_machine_justice.distinct("league_id")
    return {
        "items": items,
        "count": len(items),
        "available_sports": sorted(int(x) for x in available_sports if isinstance(x, int)),
        "filters": {
            "league_id": int(league_id) if isinstance(league_id, int) else None,
            "limit": int(limit),
            "days": int(days),
        },
    }


class AutomationToggleRequest(BaseModel):
    enabled: bool
    run_initial_sync: bool = False


@router.get("/workers/automation")
async def get_automation_state(admin=Depends(get_admin_user)):
    _ = admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/workers/automation")
async def set_automation_state(
    body: AutomationToggleRequest,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = body, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


# ---------------------------------------------------------------------------
# Heartbeat runtime config (stored in meta, overrides config.py defaults)
# ---------------------------------------------------------------------------

_HEARTBEAT_CONFIG_KEY = "heartbeat_config"

_HEARTBEAT_DEFAULTS = {
    "xg_crawler_tick_seconds": settings.XG_CRAWLER_TICK_SECONDS,
}

_HEARTBEAT_FIELD_LIMITS = {
    "xg_crawler_tick_seconds": (1, 300),  # floor 1s, ceiling 5min
}


class HeartbeatConfigPatch(BaseModel):
    xg_crawler_tick_seconds: int | None = None


class HeartbeatOddsTickRequest(BaseModel):
    reason: str | None = "manual_provider_overview"


class OddsModelExcludeBody(BaseModel):
    reason: str | None = None


@router.get("/heartbeat/config")
async def get_heartbeat_config(admin=Depends(get_admin_user)):
    """Current heartbeat runtime config (meta override + defaults)."""
    doc = await _db.db.meta.find_one({"_id": _HEARTBEAT_CONFIG_KEY}) or {}
    effective = {}
    for key, default in _HEARTBEAT_DEFAULTS.items():
        effective[key] = doc.get(key, default)
    effective["_source"] = {
        k: ("runtime" if k in doc else "default") for k in _HEARTBEAT_DEFAULTS
    }
    return effective


@router.patch("/heartbeat/config")
async def patch_heartbeat_config(
    body: HeartbeatConfigPatch,
    admin=Depends(get_admin_user),
):
    """Update heartbeat runtime config. Changes take effect on next tick."""
    updates: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_none=True).items():
        lo, hi = _HEARTBEAT_FIELD_LIMITS.get(field, (None, None))
        if lo is not None and value < lo:
            raise HTTPException(400, f"{field} must be >= {lo}")
        if hi is not None and value > hi:
            raise HTTPException(400, f"{field} must be <= {hi}")
        updates[field] = value

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates["updated_at"] = utcnow()
    updates["updated_by"] = str(admin["_id"])
    await _db.db.meta.update_one(
        {"_id": _HEARTBEAT_CONFIG_KEY},
        {"$set": updates},
        upsert=True,
    )

    from app.services.audit_service import log_audit
    await log_audit(
        actor_id=str(admin["_id"]),
        action="heartbeat_config.updated",
        target_id=_HEARTBEAT_CONFIG_KEY,
        details={"changed_fields": list(updates.keys())},
    )

    return {"ok": True, "applied": updates}


@router.post("/heartbeat/odds/tick")
async def trigger_heartbeat_odds_tick(
    body: HeartbeatOddsTickRequest,
    request: Request,
    admin=Depends(get_admin_user),
):
    from app.services.metrics_heartbeat import (
        OddsTickAlreadyRunningError,
        metrics_heartbeat,
    )

    reason = str(body.reason or "manual_provider_overview").strip() or "manual_provider_overview"
    try:
        result = await metrics_heartbeat.run_odds_tick_now(triggered_by=reason)
    except OddsTickAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await log_audit(
        actor_id=str(admin["_id"]),
        action="heartbeat.odds_tick.manual",
        target_id="heartbeat:odds",
        details={"reason": reason, "result": result.get("tick")},
        request=request,
    )
    return {
        "ok": True,
        "message": "Heartbeat odds tick completed.",
        "duration_ms": int(result.get("duration_ms") or 0),
        "result": result.get("tick") or {},
        "generated_at_utc": ensure_utc(result.get("finished_at")).isoformat(),
    }


# --- User Management ---


async def _log_admin_persona_change(
    *,
    admin_id: str,
    target_user_id: str,
    field: str,
    old_value: str | None,
    new_value: str | None,
    request: Request,
) -> None:
    now = utcnow()
    await _db.db.admin_audit_logs.insert_one(
        {
            "admin_id": str(admin_id),
            "target_user_id": str(target_user_id),
            "field": str(field),
            "old_value": old_value,
            "new_value": new_value,
            "timestamp": now,
            "created_at": now,
        }
    )
    await log_audit(
        actor_id=str(admin_id),
        target_id=str(target_user_id),
        action="ADMIN_PERSONA_UPDATED",
        metadata={"field": field, "old_value": old_value, "new_value": new_value},
        request=request,
    )

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

    policy = get_persona_policy_service()
    items: list[dict[str, Any]] = []
    for u in users:
        effective, source = await policy.resolve_effective_persona(u)
        items.append(
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
                "tip_persona": str(u.get("tip_persona") or "casual"),
                "tip_override_persona": u.get("tip_override_persona"),
                "tip_persona_effective": effective,
                "tip_persona_source": source,
                "tip_persona_updated_at": (
                    ensure_utc(u.get("tip_persona_updated_at")).isoformat()
                    if u.get("tip_persona_updated_at")
                    else None
                ),
            }
        )
    return items


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


@router.patch("/users/{user_id}/tip-persona")
async def admin_update_user_tip_persona(
    user_id: str,
    body: AdminTipPersonaBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    persona = str(body.tip_persona or "").strip().lower()
    if persona not in _TIP_PERSONA_VALUES:
        raise HTTPException(status_code=422, detail="Invalid tip_persona.")
    user = await _db.db.users.find_one({"_id": ObjectId(user_id), "is_deleted": False})
    if not isinstance(user, dict):
        raise HTTPException(status_code=404, detail="User not found.")
    now = utcnow()
    old_value = str(user.get("tip_persona") or "casual")
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "tip_persona": persona,
                "tip_persona_updated_at": now,
                "updated_at": now,
            }
        },
    )
    await _log_admin_persona_change(
        admin_id=str(admin["_id"]),
        target_user_id=user_id,
        field="tip_persona",
        old_value=old_value,
        new_value=persona,
        request=request,
    )
    return {"ok": True, "user_id": user_id, "tip_persona": persona}


@router.patch("/users/{user_id}/tip-override")
async def admin_update_user_tip_override(
    user_id: str,
    body: AdminTipOverrideBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    override = body.tip_override_persona
    if override is not None:
        override = str(override).strip().lower()
        if override not in _TIP_PERSONA_VALUES:
            raise HTTPException(status_code=422, detail="Invalid tip_override_persona.")
    user = await _db.db.users.find_one({"_id": ObjectId(user_id), "is_deleted": False})
    if not isinstance(user, dict):
        raise HTTPException(status_code=404, detail="User not found.")
    now = utcnow()
    old_value = user.get("tip_override_persona")
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "tip_override_persona": override,
                "tip_override_updated_at": now,
                "updated_at": now,
            }
        },
    )
    await _log_admin_persona_change(
        admin_id=str(admin["_id"]),
        target_user_id=user_id,
        field="tip_override_persona",
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(override) if override is not None else None,
        request=request,
    )
    return {"ok": True, "user_id": user_id, "tip_override_persona": override}


def _validate_tip_policy_rules(rules: list[TipPolicyRuleBody]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, rule in enumerate(rules):
        level = str(rule.set_output_level or "").strip().lower()
        if level not in _OUTPUT_LEVEL_VALUES:
            raise HTTPException(status_code=422, detail=f"Invalid set_output_level at rules[{idx}].")
        when_raw = rule.when if isinstance(rule.when, dict) else {}
        when: dict[str, Any] = {}
        if "persona" in when_raw and when_raw["persona"] is not None:
            persona = str(when_raw["persona"]).strip().lower()
            if persona not in _TIP_PERSONA_VALUES:
                raise HTTPException(status_code=422, detail=f"Invalid persona in rules[{idx}].when.")
            when["persona"] = persona
        for key in ("is_authenticated", "is_admin", "league_tipping_enabled"):
            if key in when_raw and when_raw[key] is not None:
                when[key] = bool(when_raw[key])
        normalized.append({"when": when, "set_output_level": level})
    return normalized


@router.get("/tip-policy")
async def get_tip_policy(admin=Depends(get_admin_user)):
    _ = admin
    doc = await _db.db.tip_persona_policy.find_one({"is_active": True}, sort=[("version", -1)])
    if not isinstance(doc, dict):
        return {
            "version": 1,
            "is_active": True,
            "rules": [],
            "note": None,
            "updated_by": "system",
            "updated_at": ensure_utc(utcnow()).isoformat(),
        }
    return {
        "version": int(doc.get("version") or 1),
        "is_active": bool(doc.get("is_active", True)),
        "rules": doc.get("rules") if isinstance(doc.get("rules"), list) else [],
        "note": doc.get("note"),
        "updated_by": str(doc.get("updated_by") or ""),
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
    }


@router.patch("/tip-policy")
async def patch_tip_policy(
    body: TipPolicyPatchBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    rules = _validate_tip_policy_rules(body.rules)
    previous = await _db.db.tip_persona_policy.find_one({"is_active": True}, sort=[("version", -1)])
    old_version = int(previous.get("version") or 1) if isinstance(previous, dict) else 1
    now = utcnow()
    if isinstance(previous, dict):
        await _db.db.tip_persona_policy.update_one({"_id": previous["_id"]}, {"$set": {"is_active": False}})
    next_doc = {
        "version": old_version + 1,
        "is_active": True,
        "rules": rules,
        "note": str(body.note or "").strip() or None,
        "updated_by": str(admin["_id"]),
        "updated_at": now,
        "created_at": now,
    }
    await _db.db.tip_persona_policy.insert_one(next_doc)
    await get_persona_policy_service().invalidate()
    await _db.db.admin_audit_logs.insert_one(
        {
            "admin_id": str(admin["_id"]),
            "target_user_id": None,
            "field": "tip_persona_policy",
            "old_value": f"v{old_version}",
            "new_value": f"v{old_version + 1}",
            "timestamp": now,
            "created_at": now,
        }
    )
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id="tip_persona_policy",
        action="TIP_POLICY_UPDATED",
        metadata={"old_version": old_version, "new_version": old_version + 1},
        request=request,
    )
    return {"ok": True, "old_version": old_version, "new_version": old_version + 1}


@router.post("/tip-policy/simulate")
async def simulate_tip_policy(
    body: TipPolicyPatchBody,
    admin=Depends(get_admin_user),
):
    _ = admin
    proposed_rules = _validate_tip_policy_rules(body.rules)
    users = await _db.db.users.find({"is_deleted": False}, {"tip_persona": 1, "tip_override_persona": 1, "is_admin": 1}).to_list(length=100_000)
    active = await _db.db.tip_persona_policy.find_one({"is_active": True}, sort=[("version", -1)])
    current_rules = active.get("rules") if isinstance(active, dict) and isinstance(active.get("rules"), list) else []

    def _resolve_base_persona(user_doc: dict[str, Any]) -> str:
        if user_doc.get("tip_override_persona") in _TIP_PERSONA_VALUES:
            return str(user_doc.get("tip_override_persona"))
        if user_doc.get("tip_persona") in _TIP_PERSONA_VALUES:
            return str(user_doc.get("tip_persona"))
        return "casual"

    def _apply_rules(base_level: str, persona: str, is_admin: bool, ruleset: list[dict[str, Any]]) -> str:
        current = str(base_level)
        for rule in ruleset:
            when = rule.get("when") if isinstance(rule.get("when"), dict) else {}
            if "persona" in when and str(when["persona"]) != persona:
                continue
            if "is_admin" in when and bool(when["is_admin"]) != bool(is_admin):
                continue
            if "is_authenticated" in when and bool(when["is_authenticated"]) is not True:
                continue
            if "league_tipping_enabled" in when and bool(when["league_tipping_enabled"]) is not True:
                continue
            target = str(rule.get("set_output_level") or "none")
            rank = {"none": 0, "summary": 1, "full": 2, "experimental": 3}
            if rank.get(target, 0) <= rank.get(current, 0):
                current = target
        return current

    affected = 0
    delta_counts: dict[str, int] = {}
    base_map = {"casual": "summary", "pro": "full", "silent": "none", "experimental": "experimental"}
    for user_doc in users:
        persona = _resolve_base_persona(user_doc)
        is_admin = bool(user_doc.get("is_admin", False))
        base = base_map.get(persona, "summary")
        old_level = _apply_rules(base, persona, is_admin, current_rules)
        new_level = _apply_rules(base, persona, is_admin, proposed_rules)
        if old_level != new_level:
            affected += 1
            key = f"{old_level}->{new_level}"
            delta_counts[key] = delta_counts.get(key, 0) + 1

    return {
        "ok": True,
        "affected_users": int(affected),
        "delta": delta_counts,
        "proposed_rules": proposed_rules,
        "current_version": int(active.get("version") or 1) if isinstance(active, dict) else 1,
    }


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


# --- Match Management (v3 — queries matches_v3 collection) ---

class MatchSyncBody(BaseModel):
    league_id: int


@router.get("/matches")
async def list_all_matches(
    response: Response,
    league_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    manual_check: Optional[bool] = Query(None),
    odds_available: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin=Depends(get_admin_user),
):
    """List matches from matches_v3 with filters (admin view)."""
    if search and len(search.strip()) > 64:
        raise HTTPException(status_code=400, detail="search must be at most 64 chars.")

    cache_key = _admin_matches_cache_key(
        page=page,
        page_size=page_size,
        league_id=league_id,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        manual_check=manual_check,
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
    if league_id is not None:
        and_filters.append({"league_id": league_id})
    if status_filter:
        and_filters.append({"status": status_filter.upper()})
    if date_from or date_to:
        date_q: dict[str, datetime] = {}
        if date_from:
            date_q["$gte"] = ensure_utc(date_from)
        if date_to:
            date_q["$lte"] = ensure_utc(date_to)
        and_filters.append({"start_at": date_q})
    # FIXME: ODDS_V3_BREAK — odds_available filter reads odds_meta.summary_1x2 which is no longer written by connector
    if odds_available is True:
        and_filters.append({"odds_meta.summary_1x2.home.avg": {"$exists": True}})
    elif odds_available is False:
        and_filters.append(
            {
                "$or": [
                    {"odds_meta.summary_1x2.home.avg": {"$exists": False}},
                    {"odds_meta.summary_1x2": {"$exists": False}},
                ]
            }
        )
    if search and search.strip():
        escaped = re.escape(search.strip())
        and_filters.append(
            {
                "$or": [
                    {"teams.home.name": {"$regex": escaped, "$options": "i"}},
                    {"teams.away.name": {"$regex": escaped, "$options": "i"}},
                ]
            }
        )
    if manual_check is True:
        and_filters.append({"manual_check_required": True})
    elif manual_check is False:
        and_filters.append(
            {"$or": [{"manual_check_required": False}, {"manual_check_required": {"$exists": False}}]}
        )

    query: dict[str, Any] = {"$and": and_filters} if and_filters else {}

    projection = {
        "_id": 1, "league_id": 1, "start_at": 1, "status": 1,
        "teams.home.name": 1, "teams.home.image_path": 1,
        "teams.away.name": 1, "teams.away.image_path": 1,
        "scores": 1, "round_id": 1,
        "season_id": 1, "referee_id": 1, "referee_name": 1,
        # FIXME: ODDS_V3_BREAK — projection reads odds_meta.summary_1x2 no longer produced by connector
        "odds_meta.summary_1x2.home.avg": 1, "odds_meta.updated_at": 1,
        "has_advanced_stats": 1, "manual_check_required": 1,
    }

    total = await _db.db.matches_v3.count_documents(query)
    skip = (page - 1) * page_size
    matches = (
        await _db.db.matches_v3.find(query, projection)
        .sort("start_at", -1)
        .skip(skip)
        .limit(page_size)
        .to_list(length=page_size)
    )

    league_map = await _get_v3_league_map()
    referee_profiles_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    referee_ids_by_league: dict[int, set[int]] = {}
    for row in matches:
        lid = row.get("league_id")
        rid = row.get("referee_id")
        if isinstance(lid, int) and isinstance(rid, int):
            referee_ids_by_league.setdefault(int(lid), set()).add(int(rid))
    for lid, ids in referee_ids_by_league.items():
        profiles = await build_referee_profiles(sorted(ids), league_id=int(lid))
        for rid, profile in profiles.items():
            referee_profiles_by_key[(int(lid), int(rid))] = profile

    items = []
    for m in matches:
        teams = m.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        odds_meta = m.get("odds_meta") if isinstance(m.get("odds_meta"), dict) else {}
        om_updated = odds_meta.get("updated_at") if isinstance(odds_meta, dict) else None
        has_odds = bool(((odds_meta.get("summary_1x2") or {}).get("home") or {}).get("avg"))
        league_id_value = int(m.get("league_id") or 0)
        referee_id_value = m.get("referee_id") if isinstance(m.get("referee_id"), int) else None
        referee_profile = (
            referee_profiles_by_key.get((league_id_value, int(referee_id_value)))
            if referee_id_value is not None
            else None
        )
        items.append(
            {
                "id": int(m["_id"]),
                "league_id": league_id_value,
                "league_name": league_map.get(league_id_value, ""),
                "home_team": home.get("name", ""),
                "away_team": away.get("name", ""),
                "home_image": home.get("image_path"),
                "away_image": away.get("image_path"),
                "start_at": ensure_utc(m["start_at"]).isoformat(),
                "status": m["status"],
                "scores": m.get("scores", {}),
                "round_id": m.get("round_id"),
                "has_odds": has_odds,
                "odds_updated_at": (ensure_utc(om_updated).isoformat() if om_updated else None),
                "has_advanced_stats": bool(m.get("has_advanced_stats")),
                "manual_check_required": bool(m.get("manual_check_required")),
                "referee": _referee_payload(
                    profile=referee_profile,
                    referee_id=referee_id_value,
                    referee_name=str(m.get("referee_name") or "") or None,
                ),
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
async def get_admin_match_detail(match_id: int, admin=Depends(get_admin_user)):
    """Return full v3 match detail including odds_timeline (admin view)."""
    doc = await _db.db.matches_v3.find_one({"_id": match_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Match not found.")

    league_map = await _get_v3_league_map()
    teams = doc.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    referee_id_value = doc.get("referee_id") if isinstance(doc.get("referee_id"), int) else None
    season_id_value = doc.get("season_id") if isinstance(doc.get("season_id"), int) else None
    league_id_value = doc.get("league_id") if isinstance(doc.get("league_id"), int) else None
    referee_profile: dict[str, Any] | None = None
    if referee_id_value is not None:
        profiles = await build_referee_profiles(
            [int(referee_id_value)],
            season_id=season_id_value,
            league_id=league_id_value,
        )
        referee_profile = profiles.get(int(referee_id_value))

    def _team_out(t: dict) -> dict:
        return {
            "sm_id": t.get("sm_id"),
            "name": t.get("name", ""),
            "short_code": t.get("short_code"),
            "image_path": t.get("image_path"),
            "score": t.get("score"),
            "xg": t.get("xg"),
        }

    return {
        "id": int(doc["_id"]),
        "league_id": int(doc["league_id"]),
        "league_name": league_map.get(doc.get("league_id", 0), ""),
        "season_id": doc.get("season_id"),
        "round_id": doc.get("round_id"),
        "referee_id": referee_id_value,
        "referee_name": doc.get("referee_name"),
        "referee": _referee_payload(
            profile=referee_profile,
            referee_id=referee_id_value,
            referee_name=str(doc.get("referee_name") or "") or None,
            include_detail=True,
        ),
        "start_at": ensure_utc(doc["start_at"]).isoformat(),
        "status": doc["status"],
        "finish_type": doc.get("finish_type"),
        "has_advanced_stats": bool(doc.get("has_advanced_stats")),
        "teams": {"home": _team_out(home), "away": _team_out(away)},
        "scores": doc.get("scores", {}),
        "events": doc.get("events", []),
        # FIXME: ODDS_V3_BREAK — returns odds_meta and odds_timeline no longer produced by connector
        "odds_meta": doc.get("odds_meta", {}),
        "odds_timeline": doc.get("odds_timeline", []),
        "manual_check_required": bool(doc.get("manual_check_required")),
        "manual_check_reasons": doc.get("manual_check_reasons", []),
    }


@router.get("/match-duplicates")
async def list_match_duplicates_admin(admin=Depends(get_admin_user)):
    raise HTTPException(status_code=410, detail="Legacy endpoint removed. V3 deduplicates at ingest time.")


@router.post("/match-duplicates/cleanup")
async def cleanup_match_duplicates_admin(admin=Depends(get_admin_user)):
    raise HTTPException(status_code=410, detail="Legacy endpoint removed. V3 deduplicates at ingest time.")


async def _run_matches_sync_for_league(league_id: int) -> None:
    from app.services.match_service import sync_matches_for_sport

    try:
        await sync_matches_for_sport(league_id)
    except Exception:
        logger.exception("Manual match sync failed for %s", league_id)


@router.post("/matches/sync")
async def trigger_matches_sync(
    body: MatchSyncBody,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = body, background_tasks, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/matches/{match_id}/override")
async def override_result(
    match_id: str, body: ResultOverride, request: Request, admin=Depends(get_admin_user)
):
    """Override a match result (force settle)."""
    match = await _db.db.matches_v3.find_one({"_id": int(match_id)})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")

    now = utcnow()
    before = {
        "status": match.get("status"),
        "result": match.get("result", {}),
        "start_at": ensure_utc(match.get("start_at")).isoformat() if match.get("start_at") else None,
    }
    old_result = before.get("result", {}).get("outcome")

    # Update match
    await _db.db.matches_v3.update_one(
        {"_id": int(match_id)},
        {
            "$set": {
                "status": "FINISHED",
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
                "status": "FINISHED",
                "result": {
                    "outcome": body.result,
                    "home_score": body.home_score,
                    "away_score": body.away_score,
                },
                "start_at": before.get("start_at"),
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

    match = await _db.db.matches_v3.find_one({"_id": int(match_id)})
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
    league_id = doc.get("league_id") if isinstance(doc.get("league_id"), int) else None
    season_start_month = int(
        doc.get("season_start_month")
        or default_season_start_month_for_league(league_id)
    )

    return {
        "id": str(doc["_id"]),
        "league_id": int(league_id) if isinstance(league_id, int) else None,
        "display_name": doc.get("display_name") or doc.get("name") or "",
        "structure_type": str(doc.get("structure_type") or "league"),
        "country_code": doc.get("country_code"),
        "tier": doc.get("tier"),
        "season_start_month": season_start_month,
        "current_season": int(
            doc.get("current_season")
            or default_current_season_for_league(
                league_id,
                season_start_month=season_start_month,
            )
        ),
        "ui_order": int(doc.get("ui_order", 999)),
        "is_active": bool(doc.get("is_active", False)),
        "features": {
            "tipping": bool(features.get("tipping")),
            "match_load": bool(features.get("match_load")),
            "xg_sync": bool(features.get("xg_sync")),
            "odds_sync": bool(features.get("odds_sync")),
        },
        "external_ids": {
            str(provider).strip().lower(): str(external_id).strip()
            for provider, external_id in external_ids.items()
            if str(provider).strip() and str(external_id).strip()
        },
        "sportmonks_last_import_at": (
            ensure_utc(doc.get("sportmonks_last_import_at")).isoformat()
            if doc.get("sportmonks_last_import_at")
            else None
        ),
        "sportmonks_last_import_season": doc.get("sportmonks_last_import_season"),
        "sportmonks_last_import_by": doc.get("sportmonks_last_import_by"),
        "created_at": ensure_utc(doc.get("created_at")).isoformat() if doc.get("created_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
    }


async def _refresh_league_registry() -> None:
    await invalidate_navigation_cache()


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
    league_ids: list[int]


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
    league_id: int | None = None
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
        "league_id": doc.get("league_id") if isinstance(doc.get("league_id"), int) else None,
        "season": doc.get("season"),
        "dry_run": bool(doc.get("dry_run", False)),
        "started_at": ensure_utc(doc.get("started_at")).isoformat() if doc.get("started_at") else None,
        "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
        "finished_at": ensure_utc(doc.get("finished_at")).isoformat() if doc.get("finished_at") else None,
        "error": doc.get("error"),
        "results": doc.get("results"),
    }


async def _run_unified_match_ingest_job(
    job_id: ObjectId,
    league_id: int,
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
        raise ValueError(
            f"Legacy ingest source '{source_key}' removed. Use Sportmonks ingest endpoints."
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
    league_id: int | None,
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
    _ = league_id, season_spec, dry_run, force, _admin_id
    now = utcnow()
    await _db.db.admin_import_jobs.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "failed",
                "phase": "disabled",
                "updated_at": now,
                "finished_at": now,
                "error": {
                    "message": "xg_enrichment_service removed in v3 hard-cut.",
                    "type": "ServiceRemovedError",
                },
            }
        },
    )


@router.get("/leagues")
async def list_leagues_admin(admin=Depends(get_admin_user)):
    docs = await _db.db.league_registry_v3.find({}).sort([("ui_order", 1), ("name", 1)]).to_list(length=10_000)
    return {"items": [_league_to_dict(doc) for doc in docs]}


@router.post("/leagues/seed")
async def seed_leagues_admin(request: Request, admin=Depends(get_admin_user)):
    _ = request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.put("/leagues/order")
async def update_leagues_order_admin(
    body: LeagueOrderBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    if not body.league_ids:
        raise HTTPException(status_code=400, detail="league_ids must not be empty.")

    ordered_ids: list[int] = []
    seen: set[int] = set()
    for league_id in body.league_ids:
        if league_id in seen:
            continue
        seen.add(league_id)
        ordered_ids.append(int(league_id))

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
    league_id: int,
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

    league_id_int = int(league_id)
    league = await _db.db.league_registry_v3.find_one({"_id": league_id_int})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    updates: dict = {"updated_at": utcnow()}
    if body.display_name is not None:
        cleaned_name = body.display_name.strip()
        if not cleaned_name:
            raise HTTPException(status_code=400, detail="display_name must not be empty.")
        updates["name"] = cleaned_name
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
            raise HTTPException(
                status_code=409,
                detail="League missing required features object. Fix source data first.",
            )
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

    await _db.db.league_registry_v3.update_one({"_id": league_id_int}, {"$set": updates})
    await _refresh_league_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=league_id,
        action="LEAGUE_UPDATE",
        metadata={"updates": updates},
        request=request,
    )
    updated = await _db.db.league_registry_v3.find_one({"_id": league_id_int})
    return {"message": "League updated.", "item": _league_to_dict(updated or league)}


async def _run_single_league_sync(
    league_id: int,
    season: int | None = None,
    full_season: bool = False,
) -> None:
    from app.workers.matchday_sync import sync_matchdays_for_sport

    try:
        await sync_matchdays_for_sport(
            int(league_id),
            season=season,
            full_season=full_season,
        )
    except Exception:
        logger.exception("Manual league sync failed for %s", league_id)


async def _check_sportmonks_import_rate_limit(
    admin_id: str,
    league_id: int,
) -> None:
    await _check_admin_import_rate_limit("sportmonks_import", admin_id, league_id)


async def _check_admin_import_rate_limit(
    import_key: str,
    admin_id: str,
    league_id: int,
) -> None:
    now = utcnow()
    limit_key = f"{import_key}:{admin_id}:{league_id}"
    existing = await _db.db.meta.find_one({"_id": limit_key}, {"last_requested_at": 1})
    if existing and existing.get("last_requested_at"):
        last_requested_at = ensure_utc(existing["last_requested_at"])
        retry_after = int(
            SPORTMONKS_IMPORT_RATE_LIMIT_SECONDS
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
                SPORTMONKS_IMPORT_RATE_LIMIT_SECONDS
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
    league_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    body: LeagueSyncRequest | None = None,
    admin=Depends(get_admin_user),
):
    _ = league_id, background_tasks, request, body, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/leagues/{league_id}/import-football-data")
async def trigger_league_sportmonks_import_admin(
    league_id: int,
    body: FootballDataImportRequest,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = league_id, body, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/leagues/{league_id}/import-football-data/async")
async def trigger_league_sportmonks_import_async_admin(
    league_id: int,
    body: FootballDataImportRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = league_id, body, background_tasks, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/leagues/{league_id}/match-ingest/async")
async def trigger_unified_match_ingest_async_admin(
    league_id: int,
    body: UnifiedMatchIngestRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = league_id, body, background_tasks, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/enrich-xg/async")
async def trigger_xg_enrichment_async_admin(
    body: XGEnrichmentRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    _ = body, background_tasks, request, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.get("/leagues/import-jobs/{job_id}")
async def get_league_import_job_status_admin(job_id: str, admin=Depends(get_admin_user)):
    _ = job_id, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


@router.post("/leagues/{league_id}/import-stats")
async def trigger_league_stats_import_admin(
    league_id: int,
    request: Request,
    body: LeagueStatsImportBody = LeagueStatsImportBody(),
    admin=Depends(get_admin_user),
):
    _ = league_id, request, body, admin
    raise HTTPException(status_code=410, detail="Legacy endpoint disabled in v3.1")


def _parse_object_id(value: str, field_name: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.") from exc


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
        league_id = doc.get("league_id", "all")
        by_sport_docs.setdefault(league_id, []).append(doc)

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
            "league_id": doc.get("league_id", "all"),
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

    for league_id, docs in by_sport_docs.items():
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
        by_sport[league_id] = {
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
            worst_league = r.get("league_id", "all")
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
        "league_id": result["league_id"],
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

    league_id = strategy.get("league_id", "all")
    docs = await _db.db.qbot_strategies.find({"league_id": league_id}).sort("created_at", -1).to_list(200)
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
        "league_id": int(league_id) if isinstance(league_id, int) else None,
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

    league_id = strategy.get("league_id", "all")
    await _db.db.qbot_strategies.update_many(
        {"league_id": int(league_id) if isinstance(league_id, int) else None, "is_active": True},
        {"$set": {"is_active": False}},
    )
    await _db.db.qbot_strategies.update_one(
        {"_id": ObjectId(strategy_id)},
        {"$set": {"is_active": True, "is_shadow": False}},
    )
    return {"status": "activated", "strategy_id": strategy_id, "league_id": league_id}


def _odds_summary_overround(summary_1x2: dict[str, Any]) -> float | None:
    home = ((summary_1x2.get("home") or {}).get("avg")) if isinstance(summary_1x2, dict) else None
    draw = ((summary_1x2.get("draw") or {}).get("avg")) if isinstance(summary_1x2, dict) else None
    away = ((summary_1x2.get("away") or {}).get("avg")) if isinstance(summary_1x2, dict) else None
    if not all(isinstance(v, (int, float)) and float(v) > 0 for v in (home, draw, away)):
        return None
    return float((1.0 / float(home)) + (1.0 / float(draw)) + (1.0 / float(away)))


# FIXME: ODDS_V3_BREAK — reads odds_timeline, market_entropy, and summary_1x2 no longer produced by connector
def _odds_quality_anomalies_for_match(
    doc: dict[str, Any],
    *,
    now_dt: datetime,
    min_snapshots: int,
    entropy_threshold: float,
    overround_floor: float,
    kickoff_window_hours: int,
) -> tuple[list[dict[str, Any]], float]:
    anomalies: list[dict[str, Any]] = []
    odds_timeline = doc.get("odds_timeline") if isinstance(doc.get("odds_timeline"), list) else []
    snapshot_count = len(odds_timeline)

    start_at_raw = doc.get("start_at")
    start_at = ensure_utc(start_at_raw) if start_at_raw else None
    hours_to_kickoff = ((start_at - now_dt).total_seconds() / 3600.0) if start_at else None

    status = str(doc.get("status") or "").upper()
    near_kickoff = bool(hours_to_kickoff is not None and 0 <= hours_to_kickoff <= float(kickoff_window_hours))
    upcoming_or_live = status in {"SCHEDULED", "LIVE"}

    if upcoming_or_live and near_kickoff and snapshot_count < int(min_snapshots):
        anomalies.append(
            {
                "code": "low_snapshot_count",
                "severity": 30,
                "detail": {
                    "snapshot_count": int(snapshot_count),
                    "min_snapshots": int(min_snapshots),
                    "hours_to_kickoff": round(float(hours_to_kickoff or 0.0), 2),
                },
            }
        )

    odds_meta = doc.get("odds_meta") if isinstance(doc.get("odds_meta"), dict) else {}
    market_entropy = odds_meta.get("market_entropy") if isinstance(odds_meta.get("market_entropy"), dict) else {}
    spread = market_entropy.get("current_spread_pct")
    drift = market_entropy.get("drift_velocity_3h")
    spread_value = float(spread) if isinstance(spread, (int, float)) else None
    drift_value = float(drift) if isinstance(drift, (int, float)) else None
    if (spread_value is not None and spread_value >= float(entropy_threshold)) or (
        drift_value is not None and drift_value >= float(entropy_threshold)
    ):
        anomalies.append(
            {
                "code": "high_entropy",
                "severity": 35,
                "detail": {
                    "current_spread_pct": spread_value,
                    "drift_velocity_3h": drift_value,
                    "threshold": float(entropy_threshold),
                },
            }
        )

    summary_1x2 = odds_meta.get("summary_1x2") if isinstance(odds_meta.get("summary_1x2"), dict) else {}
    overround = _odds_summary_overround(summary_1x2)
    if overround is not None and overround < float(overround_floor):
        anomalies.append(
            {
                "code": "negative_overround",
                "severity": 45,
                "detail": {
                    "overround": round(float(overround), 6),
                    "threshold": float(overround_floor),
                },
            }
        )

    lineups = doc.get("lineups") if isinstance(doc.get("lineups"), list) else []
    if upcoming_or_live and near_kickoff and len(lineups) == 0:
        anomalies.append(
            {
                "code": "missing_lineups",
                "severity": 25,
                "detail": {
                    "lineup_count": 0,
                    "hours_to_kickoff": round(float(hours_to_kickoff or 0.0), 2),
                },
            }
        )

    severity_score = float(sum(int(row.get("severity") or 0) for row in anomalies))
    quality_score = max(0.0, min(100.0, 100.0 - severity_score))
    return anomalies, quality_score


@router.get("/odds/anomalies")
async def list_odds_anomalies(
    limit: int = Query(200, ge=1, le=1000),
    kickoff_window_hours: int = Query(8, ge=1, le=72),
    min_snapshots: int = Query(3, ge=1, le=50),
    entropy_threshold: float = Query(0.12, ge=0.0, le=5.0),
    overround_floor: float = Query(0.99, ge=0.5, le=2.0),
    league_id: int | None = Query(None),
    season_id: int | None = Query(None),
    status: str | None = Query(None),
    admin=Depends(get_admin_user),
):
    _ = admin
    now_dt = utcnow()
    query: dict[str, Any] = {
        "start_at": {"$lte": now_dt + timedelta(hours=int(kickoff_window_hours))},
    }
    if league_id is not None:
        query["league_id"] = int(league_id)
    if season_id is not None:
        query["season_id"] = int(season_id)
    if status and str(status).strip():
        query["status"] = str(status).strip().upper()
    else:
        query["status"] = {"$in": ["SCHEDULED", "LIVE"]}

    rows = await _db.db.matches_v3.find(
        query,
        {
            "_id": 1,
            "league_id": 1,
            "season_id": 1,
            "round_id": 1,
            "start_at": 1,
            "status": 1,
            "referee_id": 1,
            "referee_name": 1,
            "teams.home.name": 1,
            "teams.away.name": 1,
            # FIXME: ODDS_V3_BREAK — anomaly query projects odds_meta and odds_timeline no longer produced by connector
            "odds_meta.summary_1x2": 1,
            "odds_meta.market_entropy": 1,
            "odds_meta.updated_at": 1,
            "odds_timeline": 1,
            "lineups": 1,
            "manual_check_required": 1,
            "manual_check_reasons": 1,
            "model_excluded": 1,
            "quality_override": 1,
        },
    ).sort("start_at", 1).limit(int(limit)).to_list(length=int(limit))

    league_map = await _get_v3_league_map()
    referee_profiles_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    referee_ids_by_league: dict[int, set[int]] = {}
    for row in rows:
        lid = row.get("league_id")
        rid = row.get("referee_id")
        if isinstance(lid, int) and isinstance(rid, int):
            referee_ids_by_league.setdefault(int(lid), set()).add(int(rid))
    for lid, ids in referee_ids_by_league.items():
        profiles = await build_referee_profiles(sorted(ids), league_id=int(lid))
        for rid, profile in profiles.items():
            referee_profiles_by_key[(int(lid), int(rid))] = profile

    items: list[dict[str, Any]] = []
    for row in rows:
        anomalies, quality_score = _odds_quality_anomalies_for_match(
            row,
            now_dt=now_dt,
            min_snapshots=int(min_snapshots),
            entropy_threshold=float(entropy_threshold),
            overround_floor=float(overround_floor),
            kickoff_window_hours=int(kickoff_window_hours),
        )
        if not anomalies:
            continue
        match_id = int(row.get("_id"))
        start_at = ensure_utc(row.get("start_at")) if row.get("start_at") else None
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        home = (teams.get("home") or {}) if isinstance(teams, dict) else {}
        away = (teams.get("away") or {}) if isinstance(teams, dict) else {}
        summary_1x2 = ((row.get("odds_meta") or {}).get("summary_1x2") or {}) if isinstance(row.get("odds_meta"), dict) else {}
        overround = _odds_summary_overround(summary_1x2)
        league_id_value = int(row.get("league_id") or 0)
        referee_id_value = row.get("referee_id") if isinstance(row.get("referee_id"), int) else None
        referee_profile = (
            referee_profiles_by_key.get((league_id_value, int(referee_id_value)))
            if referee_id_value is not None
            else None
        )
        items.append(
            {
                "match_id": match_id,
                "league_id": league_id_value,
                "league_name": league_map.get(league_id_value, ""),
                "season_id": int(row.get("season_id") or 0),
                "round_id": row.get("round_id"),
                "start_at": start_at.isoformat() if start_at else None,
                "status": str(row.get("status") or ""),
                "home_team": str(home.get("name") or ""),
                "away_team": str(away.get("name") or ""),
                "referee": _referee_payload(
                    profile=referee_profile,
                    referee_id=referee_id_value,
                    referee_name=str(row.get("referee_name") or "") or None,
                ),
                "snapshot_count": len(row.get("odds_timeline") or []),
                "overround": round(float(overround), 6) if isinstance(overround, float) else None,
                "manual_check_required": bool(row.get("manual_check_required")),
                "manual_check_reasons": list(row.get("manual_check_reasons") or []),
                "model_excluded": bool(row.get("model_excluded", False)),
                "quality_override": row.get("quality_override") if isinstance(row.get("quality_override"), dict) else None,
                "quality_score": round(float(quality_score), 2),
                "severity_score": int(sum(int(a.get("severity") or 0) for a in anomalies)),
                "anomalies": anomalies,
            }
        )

    items.sort(key=lambda r: (int(r.get("severity_score") or 0), str(r.get("start_at") or "")), reverse=True)
    return {
        "items": items,
        "count": len(items),
        "thresholds": {
            "kickoff_window_hours": int(kickoff_window_hours),
            "min_snapshots": int(min_snapshots),
            "entropy_threshold": float(entropy_threshold),
            "overround_floor": float(overround_floor),
        },
        "generated_at": now_dt.isoformat(),
    }


@router.post("/odds/fix/{match_id}")
async def fix_odds_anomaly_match(
    match_id: int,
    request: Request,
    admin=Depends(get_admin_user),
):
    doc = await _db.db.matches_v3.find_one({"_id": int(match_id)}, {"_id": 1, "round_id": 1, "season_id": 1})
    if not isinstance(doc, dict):
        raise HTTPException(status_code=404, detail="Match not found.")

    odds_synced = await sportmonks_connector.sync_fixture_odds_summary(
        int(match_id),
        source="admin_odds_force_sync",
        phase="admin_force_sync",
    )

    lineups_refreshed = False
    round_id = doc.get("round_id")
    season_id = doc.get("season_id")
    if isinstance(round_id, int) and isinstance(season_id, int):
        round_payload = await sportmonks_provider.get_round_fixtures(int(round_id))
        fixtures = ((round_payload.get("payload") or {}).get("data") or []) if isinstance(round_payload, dict) else []
        fixture = next(
            (
                row for row in fixtures
                if isinstance(row, dict) and isinstance(row.get("id"), int) and int(row.get("id")) == int(match_id)
            ),
            None,
        )
        if isinstance(fixture, dict):
            await sportmonks_connector._sync_people_from_fixture(fixture)
            await sportmonks_connector.upsert_match_v3(int(match_id), sportmonks_connector._map_fixture_to_match(fixture, int(season_id)))
            lineups_refreshed = True

    now = utcnow()
    await _db.db.matches_v3.update_one(
        {"_id": int(match_id)},
        {
            "$set": {
                "quality_override.last_fix_at": now,
                "quality_override.last_fix_by": str(admin["_id"]),
                "quality_override.last_fix_source": "admin_odds_force_sync",
                "updated_at": now,
                "updated_at_utc": now,
            }
        },
    )
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=str(int(match_id)),
        action="ODDS_MONITOR_FORCE_SYNC",
        metadata={"odds_synced": bool(odds_synced), "lineups_refreshed": bool(lineups_refreshed)},
        request=request,
    )
    return {
        "ok": True,
        "match_id": int(match_id),
        "odds_synced": bool(odds_synced),
        "lineups_refreshed": bool(lineups_refreshed),
    }


@router.post("/odds/mark-valid/{match_id}")
async def mark_odds_match_valid(
    match_id: int,
    request: Request,
    admin=Depends(get_admin_user),
):
    now = utcnow()
    result = await _db.db.matches_v3.update_one(
        {"_id": int(match_id)},
        {
            "$set": {
                "manual_check_required": False,
                "manual_check_reasons": [],
                "quality_override.state": "valid",
                "quality_override.updated_at": now,
                "quality_override.updated_by": str(admin["_id"]),
                "updated_at": now,
                "updated_at_utc": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Match not found.")
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=str(int(match_id)),
        action="ODDS_MONITOR_MARK_VALID",
        metadata={"match_id": int(match_id)},
        request=request,
    )
    return {"ok": True, "match_id": int(match_id), "state": "valid"}


@router.post("/odds/exclude/{match_id}")
async def exclude_odds_match_from_model(
    match_id: int,
    body: OddsModelExcludeBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    now = utcnow()
    result = await _db.db.matches_v3.update_one(
        {"_id": int(match_id)},
        {
            "$set": {
                "model_excluded": True,
                "model_excluded_reason": str(body.reason or "").strip() or "admin_odds_monitor_exclude",
                "model_excluded_at": now,
                "model_excluded_by": str(admin["_id"]),
                "quality_override.state": "excluded",
                "quality_override.updated_at": now,
                "quality_override.updated_by": str(admin["_id"]),
                "updated_at": now,
                "updated_at_utc": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Match not found.")
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=str(int(match_id)),
        action="ODDS_MONITOR_EXCLUDE",
        metadata={"match_id": int(match_id), "reason": str(body.reason or "").strip() or None},
        request=request,
    )
    return {"ok": True, "match_id": int(match_id), "state": "excluded"}


@router.get("/odds/{match_id}")
async def admin_odds_debug(match_id: str, admin=Depends(get_admin_user)):
    """Deprecated — odds_timeline is now included in GET /admin/matches/{match_id}."""
    raise HTTPException(
        status_code=410,
        detail="Use GET /admin/matches/{match_id} which includes odds_timeline.",
    )
