import csv
import io
import logging
import re
import time as _time
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import app.database as _db
from app.services.alias_service import generate_default_alias
from app.services.auth_service import get_admin_user, invalidate_user_tokens
from app.services.audit_service import log_audit
from app.services.historical_service import clear_context_cache
from app.services.team_mapping_service import (
    team_name_key, _strip_accents_lower, make_canonical_id,
    load_cache as reload_canonical_cache,
    seed_team_mappings as seed_canonical_map,
)
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
}


@router.get("/provider-status")
async def provider_status(admin=Depends(get_admin_user)):
    """Aggregated status of all providers and background workers."""
    from app.main import scheduler

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

    return {"providers": providers, "workers": workers}


class TriggerSyncRequest(BaseModel):
    worker_id: str


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
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    admin=Depends(get_admin_user),
):
    """List all matches (admin view)."""
    query: dict = {}
    if status_filter:
        query["status"] = status_filter

    matches = await _db.db.matches.find(query).sort("match_date", -1).limit(limit).to_list(length=limit)
    return [
        {
            "id": str(m["_id"]),
            "sport_key": m["sport_key"],
            "home_team": m.get("home_team", ""),
            "away_team": m.get("away_team", ""),
            "match_date": ensure_utc(m["match_date"]).isoformat(),
            "status": m["status"],
            "odds": m.get("odds", {}),
            "result": m.get("result", {}),
            "bet_count": await _db.db.betting_slips.count_documents({"selections.match_id": str(m["_id"])}),
        }
        for m in matches
    ]


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


# --- Team Mapping Management ---


def _mapping_to_dict(doc: dict) -> dict:
    """Convert a team_mappings MongoDB document to an API-friendly dict."""
    return {
        "id": str(doc["_id"]),
        "canonical_id": doc["canonical_id"],
        "display_name": doc["display_name"],
        "names": doc.get("names", []),
        "sport_keys": doc.get("sport_keys", []),
        "external_ids": doc.get("external_ids", {}),
    }


class TeamMappingUpdate(BaseModel):
    display_name: str


class TeamMappingCreate(BaseModel):
    display_name: str
    names: list[str] = []
    sport_keys: list[str] = []


class TeamMappingNamesBody(BaseModel):
    names: list[str]


@router.get("/team-mappings")
async def list_team_mappings(
    search: Optional[str] = Query(None),
    sport_key: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    """List team mappings with optional search and sport_key filter."""
    query: dict = {}
    if sport_key:
        query["sport_keys"] = sport_key
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"display_name": {"$regex": escaped, "$options": "i"}},
            {"names": {"$regex": escaped, "$options": "i"}},
        ]

    total = await _db.db.team_mappings.count_documents(query)
    docs = await _db.db.team_mappings.find(query).sort(
        "display_name", 1,
    ).skip(offset).limit(limit).to_list(length=limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_mapping_to_dict(d) for d in docs],
    }


@router.get("/team-mappings/{mapping_id}")
async def get_team_mapping(
    mapping_id: str, admin=Depends(get_admin_user),
):
    """Get a single team mapping by ID."""
    doc = await _db.db.team_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team mapping not found.")
    return _mapping_to_dict(doc)


@router.put("/team-mappings/{mapping_id}")
async def update_team_mapping(
    mapping_id: str, body: TeamMappingUpdate, request: Request,
    admin=Depends(get_admin_user),
):
    """Update a team mapping's display_name."""
    doc = await _db.db.team_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team mapping not found.")

    old_name = doc["display_name"]
    await _db.db.team_mappings.update_one(
        {"_id": ObjectId(mapping_id)},
        {"$set": {"display_name": body.display_name, "updated_at": utcnow()}},
    )

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=mapping_id, action="TEAM_MAPPING_UPDATE",
        metadata={"canonical_id": doc["canonical_id"], "old": old_name, "new": body.display_name},
        request=request,
    )
    return {"message": f"Updated display_name: {old_name} -> {body.display_name}"}


@router.post("/team-mappings")
async def create_team_mapping(
    body: TeamMappingCreate, request: Request, admin=Depends(get_admin_user),
):
    """Create a new team mapping."""
    canonical_id = make_canonical_id(body.display_name)
    now = utcnow()

    # Ensure the display_name itself is always in the names array
    names = list(dict.fromkeys([body.display_name] + body.names))

    doc = {
        "canonical_id": canonical_id,
        "display_name": body.display_name,
        "names": names,
        "external_ids": {},
        "sport_keys": body.sport_keys,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.team_mappings.insert_one(doc)

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=str(result.inserted_id), action="TEAM_MAPPING_CREATE",
        metadata={"canonical_id": canonical_id, "display_name": body.display_name},
        request=request,
    )
    return {"message": f"Created team mapping: {body.display_name} ({canonical_id})", "id": str(result.inserted_id)}


@router.delete("/team-mappings/{mapping_id}")
async def delete_team_mapping(
    mapping_id: str, request: Request, admin=Depends(get_admin_user),
):
    """Delete a team mapping."""
    doc = await _db.db.team_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team mapping not found.")

    await _db.db.team_mappings.delete_one({"_id": ObjectId(mapping_id)})
    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=mapping_id, action="TEAM_MAPPING_DELETE",
        metadata={"canonical_id": doc["canonical_id"], "display_name": doc["display_name"]},
        request=request,
    )
    return {"message": f"Deleted team mapping: {doc['display_name']}"}


@router.post("/team-mappings/{mapping_id}/names")
async def add_mapping_names(
    mapping_id: str, body: TeamMappingNamesBody, request: Request,
    admin=Depends(get_admin_user),
):
    """Add name variant(s) to a team mapping."""
    doc = await _db.db.team_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team mapping not found.")

    await _db.db.team_mappings.update_one(
        {"_id": ObjectId(mapping_id)},
        {"$addToSet": {"names": {"$each": body.names}}, "$set": {"updated_at": utcnow()}},
    )

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=mapping_id, action="TEAM_MAPPING_ADD_NAMES",
        metadata={"canonical_id": doc["canonical_id"], "added": body.names},
        request=request,
    )
    return {"message": f"Added {len(body.names)} name(s) to {doc['display_name']}"}


@router.delete("/team-mappings/{mapping_id}/names")
async def remove_mapping_names(
    mapping_id: str, body: TeamMappingNamesBody, request: Request,
    admin=Depends(get_admin_user),
):
    """Remove name variant(s) from a team mapping."""
    doc = await _db.db.team_mappings.find_one({"_id": ObjectId(mapping_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Team mapping not found.")

    await _db.db.team_mappings.update_one(
        {"_id": ObjectId(mapping_id)},
        {"$pull": {"names": {"$in": body.names}}, "$set": {"updated_at": utcnow()}},
    )

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=mapping_id, action="TEAM_MAPPING_REMOVE_NAMES",
        metadata={"canonical_id": doc["canonical_id"], "removed": body.names},
        request=request,
    )
    return {"message": f"Removed {len(body.names)} name(s) from {doc['display_name']}"}


@router.post("/team-mappings/reseed")
async def reseed_team_mappings(
    request: Request, admin=Depends(get_admin_user),
):
    """Re-run the team mapping seed. Restores missing entries, never overwrites manual edits."""
    upserted = await seed_canonical_map()
    await reload_canonical_cache()
    clear_context_cache()

    await log_audit(
        actor_id=str(admin["_id"]), target_id="team_mappings", action="TEAM_MAPPING_RESEED",
        metadata={"upserted": upserted},
        request=request,
    )
    return {"message": f"Seed completed. {upserted} new entries inserted."}
