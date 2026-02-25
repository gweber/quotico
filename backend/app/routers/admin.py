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
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import app.database as _db
from app.services.alias_service import generate_default_alias
from app.services.admin_service import merge_teams
from app.services.auth_service import get_admin_user, invalidate_user_tokens
from app.services.audit_service import log_audit
from app.services.league_service import (
    LeagueRegistry,
    invalidate_navigation_cache,
    seed_core_leagues,
    update_league_order,
)
from app.services.qbot_backtest_service import simulate_strategy_backtest
from app.services.football_data_service import import_football_data_stats
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.providers.odds_api import odds_provider
from app.utils import ensure_utc, utcnow
from app.workers._state import get_synced_at, get_worker_state

logger = logging.getLogger("quotico.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])


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

@router.get("/matches")
async def list_all_matches(
    league_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    needs_review: Optional[bool] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    admin=Depends(get_admin_user),
):
    """List matches with league/status/date/review filters (admin view)."""
    query: dict = {}
    if league_id:
        query["league_id"] = _parse_object_id(league_id, "league_id")
    if status_filter:
        query["status"] = status_filter
    if date_from or date_to:
        query["match_date"] = {}
        if date_from:
            query["match_date"]["$gte"] = ensure_utc(date_from)
        if date_to:
            query["match_date"]["$lte"] = ensure_utc(date_to)
    if needs_review is True:
        review_team_ids = await _db.db.teams.distinct("_id", {"needs_review": True})
        if not review_team_ids:
            return []
        query["$or"] = [
            {"home_team_id": {"$in": review_team_ids}},
            {"away_team_id": {"$in": review_team_ids}},
        ]

    matches = await _db.db.matches.find(query).sort("match_date", -1).limit(limit).to_list(length=limit)
    return [
        {
            "id": str(m["_id"]),
            "league_id": str(m["league_id"]) if m.get("league_id") else None,
            "sport_key": m["sport_key"],
            "home_team": m.get("home_team", ""),
            "away_team": m.get("away_team", ""),
            "home_team_id": str(m.get("home_team_id")) if m.get("home_team_id") else None,
            "away_team_id": str(m.get("away_team_id")) if m.get("away_team_id") else None,
            "match_date": ensure_utc(m["match_date"]).isoformat(),
            "status": m["status"],
            "score": m.get("score", {}),
            "external_ids": m.get("external_ids", {}),
            "odds": m.get("odds", {}),
            "bet_count": await _db.db.betting_slips.count_documents({"selections.match_id": str(m["_id"])}),
        }
        for m in matches
    ]


class MatchSyncBody(BaseModel):
    league_id: str


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
    old_result = match.get("result", {}).get("outcome")

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

    return {
        "id": str(doc["_id"]),
        "sport_key": doc.get("sport_key", ""),
        "display_name": doc.get("display_name", ""),
        "structure_type": str(doc.get("structure_type") or "league"),
        "country_code": doc.get("country_code"),
        "tier": doc.get("tier"),
        "current_season": int(doc.get("current_season") or utcnow().year),
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
    ui_order: Optional[int] = None
    is_active: Optional[bool] = None
    external_ids: Optional[dict[str, str]] = None
    features: Optional[LeagueFeaturesUpdateBody] = None


class LeagueOrderBody(BaseModel):
    league_ids: list[str]


class LeagueStatsImportBody(BaseModel):
    season: str | None = None


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


async def _run_single_league_sync(sport_key: str) -> None:
    from app.workers.matchday_sync import sync_matchdays_for_sport

    try:
        await sync_matchdays_for_sport(sport_key)
    except Exception:
        logger.exception("Manual league sync failed for %s", sport_key)


async def _run_league_stats_import(league_oid: ObjectId, season: str | None = None) -> None:
    try:
        result = await import_football_data_stats(league_oid, season=season)
        logger.info("League stats import completed for %s: %s", str(league_oid), result)
    except Exception:
        logger.exception("League stats import failed for %s", str(league_oid))


@router.post("/leagues/{league_id}/sync")
async def trigger_league_sync_admin(
    league_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key.")

    background_tasks.add_task(_run_single_league_sync, sport_key)
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=league_id,
        action="LEAGUE_SYNC_TRIGGER",
        metadata={"sport_key": sport_key},
        request=request,
    )
    return {"message": "League sync queued.", "sport_key": sport_key}


@router.post("/leagues/{league_id}/import-stats")
async def trigger_league_stats_import_admin(
    league_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    body: LeagueStatsImportBody = LeagueStatsImportBody(),
    admin=Depends(get_admin_user),
):
    league_oid = _parse_object_id(league_id, "league_id")
    league = await _db.db.leagues.find_one({"_id": league_oid}, {"_id": 1})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    background_tasks.add_task(_run_league_stats_import, league_oid, body.season)
    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=league_id,
        action="LEAGUE_STATS_IMPORT_TRIGGER",
        metadata={"season": body.season},
        request=request,
    )
    return {"message": "League stats import queued."}


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
        "canonical_id": doc.get("canonical_id"),
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
    await _refresh_team_registry()

    await log_audit(
        actor_id=str(admin["_id"]),
        target_id=team_id,
        action="TEAM_ALIAS_ADD",
        metadata={"alias": alias_doc},
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
