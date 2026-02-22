import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

import app.database as _db
from app.services.alias_service import generate_default_alias
from app.services.auth_service import get_admin_user
from app.providers.odds_api import odds_provider

logger = logging.getLogger("quotico.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _audit(admin_id: str, action: str, target_id: str, details: dict | None = None) -> None:
    """Write an immutable audit record to the admin_audit_log collection."""
    await _db.db.admin_audit_log.insert_one({
        "admin_id": admin_id,
        "action": action,
        "target_id": target_id,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc),
    })


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
    now = datetime.now(timezone.utc)

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
    user_id: str, body: PointsAdjust, admin=Depends(get_admin_user)
):
    """Manually adjust a user's points."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    now = datetime.now(timezone.utc)
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
    await _audit(admin_id, "points_adjust", user_id, {"delta": body.delta, "reason": body.reason})
    return {"message": f"Punkte angepasst: {body.delta:+.1f}", "new_total": user.get("points", 0) + body.delta}


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: str, admin=Depends(get_admin_user)):
    """Ban a user."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")
    if user.get("is_admin"):
        raise HTTPException(status_code=400, detail="Admin kann nicht gesperrt werden.")

    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_banned": True, "updated_at": datetime.now(timezone.utc)}},
    )
    admin_id = str(admin["_id"])
    logger.info("Admin %s banned user %s", admin_id, user_id)
    await _audit(admin_id, "ban_user", user_id)
    return {"message": f"{user['email']} wurde gesperrt."}


@router.post("/users/{user_id}/unban")
async def unban_user(user_id: str, admin=Depends(get_admin_user)):
    """Unban a user."""
    await _db.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_banned": False, "updated_at": datetime.now(timezone.utc)}},
    )
    await _audit(str(admin["_id"]), "unban_user", user_id)
    return {"message": "Sperre aufgehoben."}


@router.post("/users/{user_id}/reset-alias")
async def reset_alias(user_id: str, admin=Depends(get_admin_user)):
    """Reset a user's alias back to a default User#XXXXXX tag."""
    user = await _db.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden.")

    old_alias = user.get("alias", "")
    alias, alias_slug = await generate_default_alias(_db.db)
    now = datetime.now(timezone.utc)

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
    await _audit(admin_id, "reset_alias", user_id, {"old_alias": old_alias, "new_alias": alias})
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
    match_id: str, body: ResultOverride, admin=Depends(get_admin_user)
):
    """Override a match result (force settle)."""
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status_code=404, detail="Spiel nicht gefunden.")

    now = datetime.now(timezone.utc)
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
    await _audit(admin_id, "match_override", match_id, {
        "old_result": old_result,
        "new_result": body.result,
        "home_score": body.home_score,
        "away_score": body.away_score,
    })
    return {"message": f"Ergebnis überschrieben: {body.result} ({body.home_score}-{body.away_score})"}


@router.post("/matches/{match_id}/force-settle")
async def force_settle(
    match_id: str, body: ResultOverride, admin=Depends(get_admin_user)
):
    """Force settle a match that hasn't been resolved yet."""
    return await override_result(match_id, body, admin)


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
    body: BattleCreateAdmin, admin=Depends(get_admin_user)
):
    """Admin creates a battle between any two squads."""
    squad_a = await _db.db.squads.find_one({"_id": ObjectId(body.squad_a_id)})
    squad_b = await _db.db.squads.find_one({"_id": ObjectId(body.squad_b_id)})

    if not squad_a or not squad_b:
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if body.squad_a_id == body.squad_b_id:
        raise HTTPException(status_code=400, detail="Squad kann nicht gegen sich selbst kämpfen.")

    now = datetime.now(timezone.utc)
    battle_doc = {
        "squad_a_id": body.squad_a_id,
        "squad_b_id": body.squad_b_id,
        "start_time": body.start_time,
        "end_time": body.end_time,
        "status": "upcoming" if body.start_time > now else "active",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.battles.insert_one(battle_doc)

    battle_id = str(result.inserted_id)
    admin_id = str(admin["_id"])
    logger.info("Admin %s created battle %s: %s vs %s", admin_id, battle_id, squad_a["name"], squad_b["name"])
    await _audit(admin_id, "create_battle", battle_id, {
        "squad_a_id": body.squad_a_id,
        "squad_b_id": body.squad_b_id,
    })
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
