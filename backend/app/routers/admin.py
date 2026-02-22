import csv
import io
import logging
import re
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
from app.providers.odds_api import odds_provider
from app.utils import ensure_utc, utcnow

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
        "api_usage": odds_provider.api_usage,
        "circuit_open": odds_provider.circuit_open,
    }


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
