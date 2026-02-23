from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

import app.database as _db
from app.models.battle import (
    BattleCreate,
    BattleCommit,
    BattleResponse,
    ChallengeAccept,
    ChallengeCreate,
)
from app.services.auth_service import get_current_user
from app.services.battle_service import (
    accept_challenge,
    commit_to_battle,
    create_battle,
    create_challenge,
    decline_challenge,
    get_battle_results,
    get_lobby_challenges,
    get_user_commitment,
)

router = APIRouter(prefix="/api/battles", tags=["battles"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create(body: BattleCreate, user=Depends(get_current_user)):
    """Create a new Squad Battle (classic — both squads known)."""
    admin_id = str(user["_id"])
    battle = await create_battle(
        admin_id, body.squad_a_id, body.squad_b_id, body.start_time, body.end_time
    )
    return {"id": str(battle["_id"]), "message": "Battle erstellt."}


@router.post("/challenge", status_code=status.HTTP_201_CREATED)
async def create_challenge_endpoint(
    body: ChallengeCreate, user=Depends(get_current_user)
):
    """Create an open or direct challenge."""
    admin_id = str(user["_id"])
    battle = await create_challenge(
        admin_id, body.squad_id, body.start_time, body.end_time, body.target_squad_id,
    )
    return {
        "id": str(battle["_id"]),
        "status": battle["status"],
        "message": "Herausforderung erstellt.",
    }


@router.post("/{battle_id}/accept")
async def accept_challenge_endpoint(
    battle_id: str, body: ChallengeAccept, user=Depends(get_current_user)
):
    """Accept an open or direct challenge."""
    admin_id = str(user["_id"])
    return await accept_challenge(admin_id, battle_id, body.squad_id)


@router.post("/{battle_id}/decline")
async def decline_challenge_endpoint(
    battle_id: str, user=Depends(get_current_user)
):
    """Decline a direct challenge."""
    admin_id = str(user["_id"])
    return await decline_challenge(admin_id, battle_id)


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


@router.get("/squads/search")
async def search_squads(
    q: str = Query(..., min_length=1, description="Search by squad name"),
    user=Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Search squads by name for direct challenge targeting."""
    user_id = str(user["_id"])
    # Get user's own squad IDs to exclude them from results
    my_squads = await _db.db.squads.find({"members": user_id}, {"_id": 1}).to_list(length=20)
    my_squad_ids = [s["_id"] for s in my_squads]

    squads = await _db.db.squads.find(
        {
            "name": {"$regex": q, "$options": "i"},
            "_id": {"$nin": my_squad_ids},
            "is_public": {"$ne": False},  # Public by default (missing field = public)
        },
        {"name": 1, "members": 1},
    ).to_list(length=10)

    return [
        {"id": str(s["_id"]), "name": s["name"], "member_count": len(s.get("members", []))}
        for s in squads
    ]


@router.get("/lobby")
async def get_lobby(user=Depends(get_current_user)) -> list[dict[str, Any]]:
    """Get available challenges: open from other squads + incoming direct."""
    user_id = str(user["_id"])
    squads = await _db.db.squads.find({"members": user_id}).to_list(length=20)
    squad_ids = [str(s["_id"]) for s in squads]
    admin_squad_ids = {str(s["_id"]) for s in squads if s["admin_id"] == user_id}

    if not squad_ids:
        return []

    battles = await get_lobby_challenges(squad_ids)

    result = []
    for b in battles:
        squad_a = await _db.db.squads.find_one({"_id": ObjectId(b["squad_a_id"])})
        squad_b_id = b.get("squad_b_id")
        squad_b = await _db.db.squads.find_one({"_id": ObjectId(squad_b_id)}) if squad_b_id else None

        result.append({
            "id": str(b["_id"]),
            "squad_a": {"id": b["squad_a_id"], "name": squad_a["name"] if squad_a else "?"},
            "squad_b": {"id": squad_b_id, "name": squad_b["name"]} if squad_b else None,
            "challenge_type": b.get("challenge_type", "classic"),
            "start_time": b["start_time"].isoformat(),
            "end_time": b["end_time"].isoformat(),
            "status": b["status"],
            "can_accept": bool(admin_squad_ids),  # user is admin of at least one squad
        })

    return result


@router.get("/mine/active")
async def my_battles(user=Depends(get_current_user)) -> list[dict[str, Any]]:
    """Get all active, upcoming, and outgoing challenge battles for the user's squads."""
    user_id = str(user["_id"])

    # Get user's squads
    squads = await _db.db.squads.find({"members": user_id}).to_list(length=20)
    squad_ids = [str(s["_id"]) for s in squads]

    if not squad_ids:
        return []

    battles = await _db.db.battles.find({
        "$or": [
            # Active/upcoming battles the user's squads are in
            {
                "status": {"$in": ["upcoming", "active"]},
                "$or": [
                    {"squad_a_id": {"$in": squad_ids}},
                    {"squad_b_id": {"$in": squad_ids}},
                ],
            },
            # Outgoing open/pending challenges from user's squads
            {
                "status": {"$in": ["open", "pending"]},
                "squad_a_id": {"$in": squad_ids},
            },
        ],
    }).sort("start_time", 1).to_list(length=30)

    result = []
    for b in battles:
        commitment = await get_user_commitment(user_id, str(b["_id"]))
        squad_a = await _db.db.squads.find_one({"_id": ObjectId(b["squad_a_id"])})
        squad_b_id = b.get("squad_b_id")
        squad_b = await _db.db.squads.find_one({"_id": ObjectId(squad_b_id)}) if squad_b_id else None

        result.append({
            "id": str(b["_id"]),
            "squad_a": {"id": b["squad_a_id"], "name": squad_a["name"] if squad_a else "?"},
            "squad_b": {"id": squad_b_id, "name": squad_b["name"]} if squad_b else None,
            "challenge_type": b.get("challenge_type", "classic"),
            "start_time": b["start_time"].isoformat(),
            "end_time": b["end_time"].isoformat(),
            "status": b["status"],
            "my_commitment": commitment,
            "needs_commitment": commitment is None and b["status"] in ("upcoming", "active"),
        })

    return result


@router.get("/{battle_id}", response_model=BattleResponse)
async def get_battle(battle_id: str, user=Depends(get_current_user)):
    """Get battle details including current scores and user's commitment."""
    user_id = str(user["_id"])
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Battle nicht gefunden.")

    # For open/pending challenges, don't compute scores yet
    if battle["status"] in ("open", "pending"):
        squad_a = await _db.db.squads.find_one({"_id": ObjectId(battle["squad_a_id"])})
        squad_b_id = battle.get("squad_b_id")
        squad_b = await _db.db.squads.find_one({"_id": ObjectId(squad_b_id)}) if squad_b_id else None

        return BattleResponse(
            id=str(battle["_id"]),
            squad_a={"id": battle["squad_a_id"], "name": squad_a["name"] if squad_a else "?"},
            squad_b={"id": squad_b_id, "name": squad_b["name"]} if squad_b else None,
            challenge_type=battle.get("challenge_type", "classic"),
            start_time=battle["start_time"],
            end_time=battle["end_time"],
            status=battle["status"],
        )

    results = await get_battle_results(battle_id)
    commitment = await get_user_commitment(user_id, battle_id)

    return BattleResponse(
        id=str(battle["_id"]),
        squad_a=results["squad_a"],
        squad_b=results["squad_b"],
        challenge_type=battle.get("challenge_type", "classic"),
        start_time=battle["start_time"],
        end_time=battle["end_time"],
        status=battle["status"],
        my_commitment=commitment,
        result=results if battle["status"] == "finished" else None,
    )
