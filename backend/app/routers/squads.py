from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.models.game_mode import GAME_MODE_DEFAULTS, GameMode
from app.models.squad import LeagueConfigResponse, SquadCreate, SquadJoin, SquadResponse
from app.services.auth_service import get_current_user
from app.services.squad_service import (
    create_squad,
    join_squad,
    leave_squad,
    remove_member,
    get_user_squads,
    get_squad_leaderboard,
    get_squad_battle,
)
import app.database as _db
from app.utils import utcnow

router = APIRouter(prefix="/api/squads", tags=["squads"])


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


@router.get("/mine", response_model=list[SquadResponse])
async def my_squads(user=Depends(get_current_user)):
    """Get all squads the current user is a member of."""
    user_id = str(user["_id"])
    squads = await get_user_squads(user_id)
    return [_squad_response(s, is_admin=s["admin_id"] == user_id) for s in squads]


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


@router.get("/battle")
async def battle(
    squad_a: str = Query(..., description="Squad A ID"),
    squad_b: str = Query(..., description="Squad B ID"),
    user=Depends(get_current_user),
) -> dict[str, Any]:
    """Compare two squads by average member points (Squad Battle)."""
    return await get_squad_battle(squad_a, squad_b)


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

    return SquadResponse(
        id=str(squad["_id"]),
        name=squad["name"],
        description=squad.get("description"),
        invite_code=squad["invite_code"],
        admin_id=squad["admin_id"],
        member_count=len(squad.get("members", [])),
        is_admin=is_admin,
        league_configs=league_configs,
        game_mode=squad.get("game_mode", "classic"),
        game_mode_config=squad.get("game_mode_config", {}),
        created_at=squad["created_at"],
    )
