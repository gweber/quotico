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
from app.services.historical_service import (
    team_name_key, _strip_accents_lower,
    clear_context_cache, get_canonical_cache, reload_canonical_cache,
    seed_canonical_map,
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
    active_today = await _db.db.tips.count_documents({
        "created_at": {"$gte": now.replace(hour=0, minute=0, second=0)},
    })
    total_tips = await _db.db.tips.count_documents({})
    total_matches = await _db.db.matches.count_documents({})
    pending_matches = await _db.db.matches.count_documents({"status": {"$in": ["upcoming", "live"]}})
    completed_matches = await _db.db.matches.count_documents({"status": "completed"})
    squad_count = await _db.db.squads.count_documents({})
    battle_count = await _db.db.battles.count_documents({})
    banned_count = await _db.db.users.count_documents({"is_banned": True})

    return {
        "users": {
            "total": user_count,
            "banned": banned_count,
        },
        "tips": {
            "total": total_tips,
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

# Worker definitions: id → (label, provider, import path)
_WORKER_REGISTRY: dict[str, dict] = {
    "odds_poller": {"label": "Odds Poller", "provider": "odds_api"},
    "match_resolver": {"label": "Match Resolver", "provider": "multiple"},
    "matchday_sync": {"label": "Matchday Sync", "provider": "multiple"},
    "leaderboard": {"label": "Leaderboard", "provider": None},
    "badge_engine": {"label": "Badge Engine", "provider": None},
    "spieltag_resolver": {"label": "Spieltag Resolver", "provider": "multiple"},
    "spieltag_leaderboard": {"label": "Spieltag Leaderboard", "provider": None},
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
    "leaderboard", "spieltag_resolver", "spieltag_leaderboard",
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
            "last_synced": last.isoformat() if last else None,
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
        logger.error("Manual sync %s failed: %s", body.worker_id, e)
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")
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
    if worker_id == "spieltag_resolver":
        from app.workers.spieltag_resolver import resolve_spieltag_predictions
        return resolve_spieltag_predictions
    if worker_id == "spieltag_leaderboard":
        from app.workers.spieltag_leaderboard import materialize_spieltag_leaderboard
        return materialize_spieltag_leaderboard
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
            "created_at": u["created_at"].isoformat(),
            "tip_count": await _db.db.tips.count_documents({"user_id": str(u["_id"])}),
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
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    now = utcnow()
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$inc": {"points": body.delta}, "$set": {"updated_at": now}},
    )
    await _db.db.points_transactions.insert_one({
        "user_id": user_id,
        "tip_id": "admin_adjustment",
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
    return {"message": f"Punkte angepasst: {body.delta:+.1f}", "new_total": user.get("points", 0) + body.delta}


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: str, request: Request, admin=Depends(get_admin_user)):
    """Ban a user."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")
    if user.get("is_admin"):
        raise HTTPException(status_code=400, detail="Admin kann nicht gesperrt werden.")

    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_banned": True, "updated_at": utcnow()}},
    )
    await invalidate_user_tokens(user_id)
    admin_id = str(admin["_id"])
    logger.info("Admin %s banned user %s", admin_id, user_id)
    await log_audit(actor_id=admin_id, target_id=user_id, action="USER_BAN", request=request)
    return {"message": f"{user['email']} wurde gesperrt."}


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
    return {"message": "Sperre aufgehoben."}


@router.post("/users/{user_id}/reset-alias")
async def reset_alias(user_id: str, request: Request, admin=Depends(get_admin_user)):
    """Reset a user's alias back to a default User#XXXXXX tag."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

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
    logger.info("Admin %s reset alias for %s: %s → %s", admin_id, user_id, old_alias, alias)
    await log_audit(
        actor_id=admin_id, target_id=user_id, action="ALIAS_RESET",
        metadata={"old_alias": old_alias, "new_alias": alias}, request=request,
    )
    return {"message": f"Alias zurückgesetzt: {old_alias} → {alias}"}


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

    matches = await _db.db.matches.find(query).sort("commence_time", -1).limit(limit).to_list(length=limit)
    return [
        {
            "id": str(m["_id"]),
            "external_id": m.get("external_id"),
            "sport_key": m["sport_key"],
            "teams": m["teams"],
            "commence_time": m["commence_time"].isoformat(),
            "status": m["status"],
            "result": m.get("result"),
            "home_score": m.get("home_score"),
            "away_score": m.get("away_score"),
            "current_odds": m.get("current_odds", {}),
            "tip_count": await _db.db.tips.count_documents({"match_id": str(m["_id"])}),
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
        raise HTTPException(status_code=404, detail="Spiel nicht gefunden.")

    now = utcnow()
    old_result = match.get("result")

    # Update match
    await _db.db.matches.update_one(
        {"_id": ObjectId(match_id)},
        {
            "$set": {
                "status": "completed",
                "result": body.result,
                "home_score": body.home_score,
                "away_score": body.away_score,
                "updated_at": now,
            }
        },
    )

    # Re-resolve tips if needed
    if old_result != body.result:
        await _re_resolve_tips(match_id, body.result, now, admin)

    # Archive to historical_matches for H2H / form data consistency
    try:
        from app.services.historical_service import archive_resolved_match
        await archive_resolved_match(match, body.result, body.home_score, body.away_score)
    except Exception:
        logger.warning("archive_resolved_match failed for %s", match_id, exc_info=True)

    admin_id = str(admin["_id"])
    logger.info(
        "Admin %s overrode match %s: %s → %s (%d-%d)",
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
    return {"message": f"Ergebnis überschrieben: {body.result} ({body.home_score}-{body.away_score})"}


@router.post("/matches/{match_id}/force-settle")
async def force_settle(
    match_id: str, body: ResultOverride, request: Request, admin=Depends(get_admin_user)
):
    """Force settle a match that hasn't been resolved yet."""
    return await override_result(match_id, body, request, admin)


async def _re_resolve_tips(match_id: str, new_result: str, now: datetime, admin: dict) -> None:
    """Re-resolve all tips for a match after a result override."""
    tips = await _db.db.tips.find({"match_id": match_id}).to_list(length=10000)

    for tip in tips:
        old_status = tip["status"]
        old_points = tip.get("points_earned", 0) or 0
        prediction = tip["selection"]["value"]
        is_won = prediction == new_result
        new_status = "won" if is_won else "lost"
        new_points = tip["locked_odds"] if is_won else 0.0
        points_delta = new_points - old_points

        # Update tip
        await _db.db.tips.update_one(
            {"_id": tip["_id"]},
            {"$set": {"status": new_status, "points_earned": new_points, "resolved_at": now}},
        )

        # Adjust user points
        if points_delta != 0:
            await _db.db.users.update_one(
                {"_id": ObjectId(tip["user_id"])},
                {"$inc": {"points": points_delta}},
            )
            await _db.db.points_transactions.insert_one({
                "user_id": tip["user_id"],
                "tip_id": str(tip["_id"]),
                "delta": points_delta,
                "scoring_version": 0,
                "reason": f"Admin override: {old_status} → {new_status}",
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
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if body.squad_a_id == body.squad_b_id:
        raise HTTPException(status_code=400, detail="Squad kann nicht gegen sich selbst kämpfen.")

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
        "message": f"Battle erstellt: {squad_a['name']} vs {squad_b['name']}",
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
                "timestamp": entry["timestamp"].isoformat(),
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
            entry["timestamp"].isoformat(),
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


# --- Team Alias Management ---

@router.get("/team-aliases")
async def list_team_aliases(
    sport_key: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    """List team aliases with optional filtering."""
    query: dict = {}
    if sport_key:
        query["sport_key"] = sport_key
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"team_name": {"$regex": escaped, "$options": "i"}},
            {"team_key": {"$regex": escaped, "$options": "i"}},
        ]

    total = await _db.db.team_aliases.count_documents(query)
    aliases = await _db.db.team_aliases.find(query).sort(
        "team_key", 1,
    ).skip(offset).limit(limit).to_list(length=limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(a["_id"]),
                "sport_key": a["sport_key"],
                "team_name": a["team_name"],
                "team_key": a["team_key"],
                "canonical_name": a.get("canonical_name"),
            }
            for a in aliases
        ],
    }


@router.get("/team-aliases/canonical-map")
async def list_canonical_map(
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    """List the canonical team name map (DB-backed, editable)."""
    query: dict = {}
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"provider_name": {"$regex": escaped, "$options": "i"}},
            {"canonical_name": {"$regex": escaped, "$options": "i"}},
        ]
    if source:
        query["source"] = source

    total = await _db.db.canonical_map.count_documents(query)
    docs = await _db.db.canonical_map.find(query).sort(
        "provider_name", 1,
    ).skip(offset).limit(limit).to_list(length=limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(d["_id"]),
                "provider_name": d["provider_name"],
                "canonical_name": d["canonical_name"],
                "team_key": team_name_key(d["canonical_name"]),
                "source": d.get("source", "seed"),
            }
            for d in docs
        ],
    }


class CanonicalMapUpdate(BaseModel):
    canonical_name: str


@router.put("/team-aliases/canonical-map/{entry_id}")
async def update_canonical_entry(
    entry_id: str, body: CanonicalMapUpdate, request: Request,
    admin=Depends(get_admin_user),
):
    """Update a canonical map entry's canonical name."""
    entry = await _db.db.canonical_map.find_one({"_id": ObjectId(entry_id)})
    if not entry:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden.")

    old_name = entry["canonical_name"]
    await _db.db.canonical_map.update_one(
        {"_id": ObjectId(entry_id)},
        {"$set": {"canonical_name": body.canonical_name, "source": "manual", "updated_at": utcnow()}},
    )

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=entry_id, action="CANONICAL_MAP_UPDATE",
        metadata={"provider_name": entry["provider_name"], "old": old_name, "new": body.canonical_name},
        request=request,
    )
    return {"message": f"Aktualisiert: {entry['provider_name']} → {body.canonical_name}"}


class CanonicalMapCreate(BaseModel):
    provider_name: str
    canonical_name: str


@router.post("/team-aliases/canonical-map")
async def create_canonical_entry(
    body: CanonicalMapCreate, request: Request, admin=Depends(get_admin_user),
):
    """Add a new canonical map entry."""
    provider_key = _strip_accents_lower(body.provider_name)
    now = utcnow()
    await _db.db.canonical_map.update_one(
        {"provider_name": provider_key},
        {
            "$set": {"canonical_name": body.canonical_name, "source": "manual", "updated_at": now},
            "$setOnInsert": {"provider_name": provider_key, "imported_at": now},
        },
        upsert=True,
    )

    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=provider_key, action="CANONICAL_MAP_CREATE",
        metadata={"provider_name": provider_key, "canonical_name": body.canonical_name},
        request=request,
    )
    return {"message": f"Erstellt: {provider_key} → {body.canonical_name}"}


@router.delete("/team-aliases/canonical-map/{entry_id}")
async def delete_canonical_entry(
    entry_id: str, request: Request, admin=Depends(get_admin_user),
):
    """Delete a canonical map entry."""
    entry = await _db.db.canonical_map.find_one({"_id": ObjectId(entry_id)})
    if not entry:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden.")

    await _db.db.canonical_map.delete_one({"_id": ObjectId(entry_id)})
    await reload_canonical_cache()
    clear_context_cache()

    admin_id = str(admin["_id"])
    await log_audit(
        actor_id=admin_id, target_id=entry_id, action="CANONICAL_MAP_DELETE",
        metadata={"provider_name": entry["provider_name"], "canonical_name": entry["canonical_name"]},
        request=request,
    )
    return {"message": f"Gelöscht: {entry['provider_name']}"}


@router.post("/team-aliases/canonical-map/reseed")
async def reseed_canonical_map(
    request: Request, admin=Depends(get_admin_user),
):
    """Re-run the canonical seed. Restores missing entries, never overwrites manual edits."""
    upserted = await seed_canonical_map()
    await reload_canonical_cache()
    clear_context_cache()

    await log_audit(
        actor_id=str(admin["_id"]), target_id="canonical_map", action="CANONICAL_MAP_RESEED",
        metadata={"upserted": upserted},
        request=request,
    )
    return {"message": f"Seed abgeschlossen. {upserted} neue Einträge eingefügt."}


class TeamAliasUpdate(BaseModel):
    team_key: str


@router.put("/team-aliases/{alias_id}")
async def update_team_alias(
    alias_id: str, body: TeamAliasUpdate, request: Request,
    admin=Depends(get_admin_user),
):
    """Update a team alias's resolved key."""
    alias = await _db.db.team_aliases.find_one({"_id": ObjectId(alias_id)})
    if not alias:
        raise HTTPException(status_code=404, detail="Alias nicht gefunden.")

    old_key = alias["team_key"]
    new_key = body.team_key.strip().lower()

    await _db.db.team_aliases.update_one(
        {"_id": ObjectId(alias_id)},
        {"$set": {"team_key": new_key, "updated_at": utcnow()}},
    )

    clear_context_cache()

    admin_id = str(admin["_id"])
    logger.info("Admin %s updated alias %s: %s → %s", admin_id, alias["team_name"], old_key, new_key)
    await log_audit(
        actor_id=admin_id, target_id=alias_id, action="ALIAS_UPDATE",
        metadata={"team_name": alias["team_name"], "old_key": old_key, "new_key": new_key},
        request=request,
    )
    return {"message": f"Alias aktualisiert: {alias['team_name']} → {new_key}"}


class TeamAliasCreate(BaseModel):
    sport_key: str
    team_name: str
    team_key: str


@router.post("/team-aliases")
async def create_team_alias(
    body: TeamAliasCreate, request: Request, admin=Depends(get_admin_user),
):
    """Manually create a team alias."""
    now = utcnow()
    await _db.db.team_aliases.update_one(
        {"sport_key": body.sport_key, "team_name": body.team_name},
        {
            "$set": {"team_key": body.team_key.strip().lower(), "updated_at": now},
            "$setOnInsert": {
                "sport_key": body.sport_key,
                "team_name": body.team_name,
                "canonical_name": None,
                "imported_at": now,
            },
        },
        upsert=True,
    )

    clear_context_cache()

    admin_id = str(admin["_id"])
    logger.info("Admin %s created alias: %s → %s (%s)", admin_id, body.team_name, body.team_key, body.sport_key)
    await log_audit(
        actor_id=admin_id, target_id=body.team_name, action="ALIAS_CREATE",
        metadata={"sport_key": body.sport_key, "team_name": body.team_name, "team_key": body.team_key},
        request=request,
    )
    return {"message": f"Alias erstellt: {body.team_name} → {body.team_key}"}


@router.delete("/team-aliases/{alias_id}")
async def delete_team_alias(
    alias_id: str, request: Request, admin=Depends(get_admin_user),
):
    """Delete a team alias."""
    alias = await _db.db.team_aliases.find_one({"_id": ObjectId(alias_id)})
    if not alias:
        raise HTTPException(status_code=404, detail="Alias nicht gefunden.")

    await _db.db.team_aliases.delete_one({"_id": ObjectId(alias_id)})
    clear_context_cache()

    admin_id = str(admin["_id"])
    logger.info("Admin %s deleted alias: %s → %s", admin_id, alias["team_name"], alias["team_key"])
    await log_audit(
        actor_id=admin_id, target_id=alias_id, action="ALIAS_DELETE",
        metadata={"team_name": alias["team_name"], "team_key": alias["team_key"]},
        request=request,
    )
    return {"message": f"Alias gelöscht: {alias['team_name']}"}
