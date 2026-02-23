from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.models.game_mode import GAME_MODE_DEFAULTS, GameMode
from app.models.squad import (
    JoinRequestResponse,
    LeagueConfigResponse,
    SquadCreate,
    SquadJoin,
    SquadResponse,
    WarRoomResponse,
)
from app.services.auth_service import get_current_user
from app.services.squad_service import (
    create_squad,
    delete_squad,
    join_squad,
    leave_squad,
    remove_member,
    update_squad,
    get_user_squads,
    get_squad_leaderboard,
    get_squad_battle,
)
from app.services.war_room_service import get_war_room
import app.database as _db
from app.utils import utcnow

router = APIRouter(prefix="/api/squads", tags=["squads"])


@router.get("/preview/{invite_code}")
async def preview(invite_code: str):
    """Public: get minimal squad info for the invite landing page. No auth required."""
    squad = await _db.db.squads.find_one(
        {"invite_code": invite_code.upper()},
        {"name": 1, "description": 1, "members": 1},
    )
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")

    member_count = len(squad.get("members", []))
    return {
        "name": squad["name"],
        "description": squad.get("description"),
        "member_count": member_count,
        "is_full": member_count >= 50,
    }


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=SquadResponse)
async def create(body: SquadCreate, user=Depends(get_current_user)):
    """Create a new squad. The creator becomes admin."""
    user_id = str(user["_id"])
    squad = await create_squad(user_id, body.name, body.description)
    return _squad_response(squad, is_admin=True)


@router.post("/join", response_model=SquadResponse)
async def join(body: SquadJoin, user=Depends(get_current_user)):
    """Join a squad using an invite code."""
    user_id = str(user["_id"])
    squad = await join_squad(user_id, body.invite_code)
    return _squad_response(squad, is_admin=squad["admin_id"] == user_id)


@router.get("/public")
async def public_squads(
    q: str = Query("", description="Search by squad name"),
    user=Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Browse public squads: search by name or get top 10 by member count."""
    user_id = str(user["_id"])

    # Base filter: public squads the user is NOT already in
    filt: dict[str, Any] = {
        "is_public": {"$ne": False},
        "members": {"$nin": [user_id]},
    }
    if q.strip():
        filt["name"] = {"$regex": q.strip(), "$options": "i"}

    pipeline = [
        {"$match": filt},
        {"$addFields": {"_member_count": {"$size": {"$ifNull": ["$members", []]}}}},
        {"$sort": {"_member_count": -1}},
        {"$limit": 10},
        {"$project": {"name": 1, "description": 1, "is_open": 1, "_member_count": 1}},
    ]
    results = await _db.db.squads.aggregate(pipeline).to_list(length=10)

    return [
        {
            "id": str(s["_id"]),
            "name": s["name"],
            "description": s.get("description"),
            "member_count": s["_member_count"],
            "is_open": s.get("is_open", True),
        }
        for s in results
    ]


@router.get("/mine", response_model=list[SquadResponse])
async def my_squads(user=Depends(get_current_user)):
    """Get all squads the current user is a member of."""
    user_id = str(user["_id"])
    squads = await get_user_squads(user_id)

    # Enrich admin squads with pending join-request count
    admin_squad_ids = [s["_id"] for s in squads if s["admin_id"] == user_id]
    pending_counts: dict[str, int] = {}
    if admin_squad_ids:
        pipeline = [
            {"$match": {"squad_id": {"$in": [str(sid) for sid in admin_squad_ids]}, "status": "pending"}},
            {"$group": {"_id": "$squad_id", "count": {"$sum": 1}}},
        ]
        counts = await _db.db.join_requests.aggregate(pipeline).to_list(length=100)
        pending_counts = {c["_id"]: c["count"] for c in counts}

    results = []
    for s in squads:
        is_admin = s["admin_id"] == user_id
        s["_pending_requests"] = pending_counts.get(str(s["_id"]), 0) if is_admin else 0
        results.append(_squad_response(s, is_admin=is_admin))
    return results


@router.get("/{squad_id}/leaderboard")
async def squad_leaderboard(
    squad_id: str, user=Depends(get_current_user)
) -> list[dict[str, Any]]:
    """Get the leaderboard for a specific squad.

    Privacy: only shows tips for matches that have started.
    """
    user_id = str(user["_id"])
    entries = await get_squad_leaderboard(squad_id, user_id)
    return [
        {"rank": i + 1, **entry}
        for i, entry in enumerate(entries)
    ]


@router.get("/{squad_id}/war-room/{match_id}", response_model=WarRoomResponse)
async def war_room(
    squad_id: str,
    match_id: str,
    user=Depends(get_current_user),
) -> WarRoomResponse:
    """Squad War Room: see how your squad picked a specific match.

    Pre-kickoff (Shadow Logic): only your own selection is visible.
    Post-kickoff: all selections revealed + consensus breakdown.
    """
    user_id = str(user["_id"])
    payload = await get_war_room(squad_id, match_id, user_id)
    return WarRoomResponse(**payload)


@router.post("/{squad_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave(squad_id: str, user=Depends(get_current_user)):
    """Leave a squad."""
    user_id = str(user["_id"])
    await leave_squad(user_id, squad_id)


@router.delete("/{squad_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kick_member(squad_id: str, member_id: str, user=Depends(get_current_user)):
    """Admin removes a member from the squad."""
    admin_id = str(user["_id"])
    await remove_member(admin_id, squad_id, member_id)


class SquadUpdate(BaseModel):
    description: str | None = None


@router.patch("/{squad_id}", response_model=SquadResponse)
async def update(squad_id: str, body: SquadUpdate, user=Depends(get_current_user)):
    """Update squad details (admin only)."""
    user_id = str(user["_id"])
    squad = await update_squad(user_id, squad_id, body.description)
    return _squad_response(squad, is_admin=True)


@router.delete("/{squad_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(squad_id: str, user=Depends(get_current_user)):
    """Delete a squad (admin only)."""
    user_id = str(user["_id"])
    await delete_squad(user_id, squad_id)


@router.get("/battle")
async def battle(
    squad_a: str = Query(..., description="Squad A ID"),
    squad_b: str = Query(..., description="Squad B ID"),
    user=Depends(get_current_user),
) -> dict[str, Any]:
    """Compare two squads by average member points (Squad Battle)."""
    return await get_squad_battle(squad_a, squad_b)


# ---------- Auto-Tipp ----------

class AutoTippUpdate(BaseModel):
    blocked: bool


@router.patch("/{squad_id}/auto-tipp")
async def toggle_auto_tipp(
    squad_id: str,
    body: AutoTippUpdate,
    user=Depends(get_current_user),
):
    """Toggle auto-tipp blocking for a squad (admin only)."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Auto-Tipp-Einstellungen ändern.")

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"auto_tipp_blocked": body.blocked, "updated_at": now}},
    )

    return {"auto_tipp_blocked": body.blocked}


# ---------- Lock Deadline ----------

class LockMinutesUpdate(BaseModel):
    minutes: int


@router.patch("/{squad_id}/lock-minutes")
async def set_lock_minutes(
    squad_id: str,
    body: LockMinutesUpdate,
    user=Depends(get_current_user),
):
    """Set the prediction lock deadline for a squad (admin only).

    Minutes before kickoff when predictions lock. Range: 0–120.
    """
    user_id = str(user["_id"])
    if not 0 <= body.minutes <= 120:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wert muss zwischen 0 und 120 Minuten liegen.")

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann die Tippfrist ändern.")

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"lock_minutes": body.minutes, "updated_at": now}},
    )

    return {"lock_minutes": body.minutes}


class VisibilityUpdate(BaseModel):
    is_public: bool


@router.patch("/{squad_id}/visibility")
async def set_visibility(
    squad_id: str,
    body: VisibilityUpdate,
    user=Depends(get_current_user),
):
    """Set squad visibility: public (searchable) or private (invite/ID only)."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann die Sichtbarkeit ändern.")

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"is_public": body.is_public, "updated_at": now}},
    )

    return {"is_public": body.is_public}


class InviteVisibleUpdate(BaseModel):
    visible: bool


@router.patch("/{squad_id}/invite-visible")
async def set_invite_visible(
    squad_id: str,
    body: InviteVisibleUpdate,
    user=Depends(get_current_user),
):
    """Toggle whether squad members can see the invite code (admin only)."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann das ändern.")

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"invite_visible": body.visible, "updated_at": now}},
    )

    return {"invite_visible": body.visible}


# ---------- Open / Locked ----------

class OpenUpdate(BaseModel):
    is_open: bool


@router.patch("/{squad_id}/open")
async def set_open(
    squad_id: str,
    body: OpenUpdate,
    user=Depends(get_current_user),
):
    """Toggle whether the squad accepts join requests (admin only)."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann das ändern.")

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"is_open": body.is_open, "updated_at": now}},
    )

    return {"is_open": body.is_open}


# ---------- Join Requests ----------


@router.post("/{squad_id}/request-join", status_code=status.HTTP_201_CREATED)
async def request_join(squad_id: str, user=Depends(get_current_user)):
    """Request to join a public, open squad."""
    user_id = str(user["_id"])

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if not squad.get("is_public", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dieser Squad ist privat.")
    if not squad.get("is_open", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dieser Squad nimmt gerade keine Anfragen an.")
    if user_id in squad.get("members", []):
        raise HTTPException(status.HTTP_409_CONFLICT, "Du bist bereits Mitglied.")
    if len(squad.get("members", [])) >= 50:
        raise HTTPException(status.HTTP_409_CONFLICT, "Squad ist voll (max. 50).")

    # Check for existing pending request
    existing = await _db.db.join_requests.find_one({
        "squad_id": squad_id, "user_id": user_id, "status": "pending",
    })
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Du hast bereits eine offene Anfrage.")

    now = utcnow()
    doc = {
        "squad_id": squad_id,
        "user_id": user_id,
        "status": "pending",
        "created_at": now,
        "resolved_at": None,
        "resolved_by": None,
    }
    result = await _db.db.join_requests.insert_one(doc)
    return {"id": str(result.inserted_id), "status": "pending"}


@router.get("/{squad_id}/join-requests", response_model=list[JoinRequestResponse])
async def get_join_requests(squad_id: str, user=Depends(get_current_user)):
    """Get pending join requests for a squad (admin only)."""
    user_id = str(user["_id"])

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Anfragen einsehen.")

    requests = await _db.db.join_requests.find(
        {"squad_id": squad_id, "status": "pending"}
    ).sort("created_at", 1).to_list(length=50)

    # Enrich with user aliases
    user_ids = [r["user_id"] for r in requests]
    users = {}
    if user_ids:
        user_docs = await _db.db.users.find(
            {"_id": {"$in": [ObjectId(uid) for uid in user_ids]}},
            {"alias": 1},
        ).to_list(length=50)
        users = {str(u["_id"]): u.get("alias", "?") for u in user_docs}

    return [
        JoinRequestResponse(
            id=str(r["_id"]),
            squad_id=r["squad_id"],
            user_id=r["user_id"],
            alias=users.get(r["user_id"], "?"),
            status=r["status"],
            created_at=r["created_at"],
        )
        for r in requests
    ]


@router.post("/{squad_id}/join-requests/{request_id}/approve")
async def approve_join_request(
    squad_id: str, request_id: str, user=Depends(get_current_user),
):
    """Approve a join request (admin only). Adds the user to the squad."""
    admin_id = str(user["_id"])

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Anfragen genehmigen.")
    if len(squad.get("members", [])) >= 50:
        raise HTTPException(status.HTTP_409_CONFLICT, "Squad ist voll (max. 50).")

    jr = await _db.db.join_requests.find_one({"_id": ObjectId(request_id), "squad_id": squad_id})
    if not jr or jr["status"] != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anfrage nicht gefunden oder bereits bearbeitet.")

    now = utcnow()
    # Add user to squad
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$addToSet": {"members": jr["user_id"]}, "$set": {"updated_at": now}},
    )
    # Mark request approved
    await _db.db.join_requests.update_one(
        {"_id": jr["_id"]},
        {"$set": {"status": "approved", "resolved_at": now, "resolved_by": admin_id}},
    )

    return {"status": "approved"}


@router.post("/{squad_id}/join-requests/{request_id}/decline")
async def decline_join_request(
    squad_id: str, request_id: str, user=Depends(get_current_user),
):
    """Decline a join request (admin only)."""
    admin_id = str(user["_id"])

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Anfragen ablehnen.")

    jr = await _db.db.join_requests.find_one({"_id": ObjectId(request_id), "squad_id": squad_id})
    if not jr or jr["status"] != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anfrage nicht gefunden oder bereits bearbeitet.")

    now = utcnow()
    await _db.db.join_requests.update_one(
        {"_id": jr["_id"]},
        {"$set": {"status": "declined", "resolved_at": now, "resolved_by": admin_id}},
    )

    return {"status": "declined"}


# ---------- Game Mode ----------

class GameModeUpdate(BaseModel):
    game_mode: GameMode
    config: dict = {}


@router.put("/{squad_id}/game-mode")
async def update_game_mode(
    squad_id: str,
    body: GameModeUpdate,
    user=Depends(get_current_user),
):
    """Set the game mode for a squad (admin only)."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann den Spielmodus ändern.")

    mode = body.game_mode.value
    defaults = GAME_MODE_DEFAULTS.get(mode, {})
    config = {**defaults, **body.config}

    now = utcnow()
    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {
            "game_mode": mode,
            "game_mode_config": config,
            "game_mode_changed_at": now,
            "updated_at": now,
        }},
    )

    return {"game_mode": mode, "game_mode_config": config}


# ---------- League Configuration (Multi-Liga, Multi-Modus) ----------

class LeagueConfigUpdate(BaseModel):
    sport_key: str
    game_mode: GameMode
    config: dict = {}


@router.put("/{squad_id}/league-config")
async def upsert_league_config(
    squad_id: str,
    body: LeagueConfigUpdate,
    user=Depends(get_current_user),
):
    """Add or update a league config for a squad (admin only).

    Each sport_key can only appear once (active) per squad.
    """
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Liga-Konfigurationen ändern.")

    now = utcnow()
    mode = body.game_mode.value
    defaults = GAME_MODE_DEFAULTS.get(mode, {})
    config = {**defaults, **body.config}

    league_configs = squad.get("league_configs", [])

    # Check if sport_key already exists (active or deactivated)
    existing_idx = None
    for i, lc in enumerate(league_configs):
        if lc["sport_key"] == body.sport_key:
            existing_idx = i
            break

    if existing_idx is not None:
        # Update existing config (reactivate if deactivated)
        league_configs[existing_idx] = {
            "sport_key": body.sport_key,
            "game_mode": mode,
            "config": config,
            "activated_at": league_configs[existing_idx].get("activated_at", now),
            "deactivated_at": None,
        }
    else:
        # Add new config
        league_configs.append({
            "sport_key": body.sport_key,
            "game_mode": mode,
            "config": config,
            "activated_at": now,
            "deactivated_at": None,
        })

    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {"$set": {"league_configs": league_configs, "updated_at": now}},
    )

    return {"league_configs": league_configs}


@router.delete("/{squad_id}/league-config/{sport_key}")
async def deactivate_league_config(
    squad_id: str,
    sport_key: str,
    user=Depends(get_current_user),
):
    """Soft-deactivate a league from a squad (admin only).

    Sets deactivated_at. Existing predictions are preserved and will still be resolved.
    """
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur der Admin kann Liga-Konfigurationen ändern.")

    now = utcnow()
    result = await _db.db.squads.update_one(
        {"_id": ObjectId(squad_id), "league_configs.sport_key": sport_key},
        {"$set": {
            "league_configs.$.deactivated_at": now,
            "updated_at": now,
        }},
    )

    if result.modified_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Liga-Konfiguration nicht gefunden.")

    return {"status": "deactivated", "sport_key": sport_key}


@router.get("/{squad_id}/league-configs")
async def get_league_configs(
    squad_id: str,
    user=Depends(get_current_user),
):
    """Get all league configs for a squad."""
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")

    user_id = str(user["_id"])
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Du bist kein Mitglied dieses Squads.")

    raw_configs = squad.get("league_configs", [])
    return [
        LeagueConfigResponse(
            sport_key=lc["sport_key"],
            game_mode=lc["game_mode"],
            config=lc.get("config", {}),
            activated_at=lc["activated_at"],
            deactivated_at=lc.get("deactivated_at"),
        )
        for lc in raw_configs
    ]


# ---------- Helpers ----------

def _squad_response(squad: dict, is_admin: bool = False) -> SquadResponse:
    # Build league_configs from raw dicts
    raw_configs = squad.get("league_configs", [])
    league_configs = [
        LeagueConfigResponse(
            sport_key=lc["sport_key"],
            game_mode=lc["game_mode"],
            config=lc.get("config", {}),
            activated_at=lc["activated_at"],
            deactivated_at=lc.get("deactivated_at"),
        )
        for lc in raw_configs
    ]

    invite_visible = squad.get("invite_visible", False)
    show_invite = is_admin or invite_visible

    return SquadResponse(
        id=str(squad["_id"]),
        name=squad["name"],
        description=squad.get("description"),
        invite_code=squad["invite_code"] if show_invite else None,
        admin_id=squad["admin_id"],
        member_count=len(squad.get("members", [])),
        is_admin=is_admin,
        league_configs=league_configs,
        auto_tipp_blocked=squad.get("auto_tipp_blocked", False),
        lock_minutes=squad.get("lock_minutes", 15),
        is_public=squad.get("is_public", True),
        is_open=squad.get("is_open", True),
        invite_visible=invite_visible,
        pending_requests=squad.get("_pending_requests", 0),
        game_mode=squad.get("game_mode", "classic"),
        game_mode_config=squad.get("game_mode_config", {}),
        created_at=squad["created_at"],
    )
