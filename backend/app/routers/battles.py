from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, status

import app.database as _db
from app.models.battle import BattleCreate, BattleCommit, BattleResponse
from app.services.auth_service import get_current_user
from app.services.battle_service import (
    create_battle,
    commit_to_battle,
    get_battle_results,
    get_user_commitment,
)

router = APIRouter(prefix="/api/battles", tags=["battles"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create(body: BattleCreate, user=Depends(get_current_user)):
    """Create a new Squad Battle."""
    admin_id = str(user["_id"])
    battle = await create_battle(
        admin_id, body.squad_a_id, body.squad_b_id, body.start_time, body.end_time
    )
    return {"id": str(battle["_id"]), "message": "Battle erstellt."}


@router.post("/{battle_id}/commit")
async def commit(
    battle_id: str, body: BattleCommit, user=Depends(get_current_user)
):
    """Commit to fighting for a specific squad in a battle.

    Lock-in: Cannot change side after battle starts.
    """
    user_id = str(user["_id"])
    await commit_to_battle(user_id, battle_id, body.squad_id)
    return {"message": "Commitment bestätigt."}


@router.get("/{battle_id}", response_model=BattleResponse)
async def get_battle(battle_id: str, user=Depends(get_current_user)):
    """Get battle details including current scores and user's commitment."""
    user_id = str(user["_id"])
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Battle nicht gefunden.")

    results = await get_battle_results(battle_id)
    commitment = await get_user_commitment(user_id, battle_id)

    return BattleResponse(
        id=str(battle["_id"]),
        squad_a=results["squad_a"],
        squad_b=results["squad_b"],
        start_time=battle["start_time"],
        end_time=battle["end_time"],
        status=battle["status"],
        my_commitment=commitment,
        result=results if battle["status"] == "finished" else None,
    )


@router.get("/mine/active")
async def my_battles(user=Depends(get_current_user)) -> list[dict[str, Any]]:
    """Get all active and upcoming battles for squads the user is in."""
    user_id = str(user["_id"])

    # Get user's squads
    squads = await _db.db.squads.find({"members": user_id}).to_list(length=20)
    squad_ids = [str(s["_id"]) for s in squads]

    if not squad_ids:
        return []

    battles = await _db.db.battles.find({
        "status": {"$in": ["upcoming", "active"]},
        "$or": [
            {"squad_a_id": {"$in": squad_ids}},
            {"squad_b_id": {"$in": squad_ids}},
        ],
    }).sort("start_time", 1).to_list(length=20)

    result = []
    for b in battles:
        commitment = await get_user_commitment(user_id, str(b["_id"]))
        squad_a = await _db.db.squads.find_one({"_id": ObjectId(b["squad_a_id"])})
        squad_b = await _db.db.squads.find_one({"_id": ObjectId(b["squad_b_id"])})

        result.append({
            "id": str(b["_id"]),
            "squad_a": {"id": b["squad_a_id"], "name": squad_a["name"] if squad_a else "?"},
            "squad_b": {"id": b["squad_b_id"], "name": squad_b["name"] if squad_b else "?"},
            "start_time": b["start_time"].isoformat(),
            "end_time": b["end_time"].isoformat(),
            "status": b["status"],
            "my_commitment": commitment,
            "needs_commitment": commitment is None,
        })

    return result
