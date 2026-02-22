from typing import Any

from fastapi import APIRouter, Depends, Query, status

from app.models.squad import SquadCreate, SquadJoin, SquadResponse
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

router = APIRouter(prefix="/api/squads", tags=["squads"])


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=SquadResponse)
async def create(body: SquadCreate, user=Depends(get_current_user)):
    """Create a new squad. The creator becomes admin."""
    user_id = str(user["_id"])
    squad = await create_squad(user_id, body.name, body.description)
    return SquadResponse(
        id=str(squad["_id"]),
        name=squad["name"],
        description=squad.get("description"),
        invite_code=squad["invite_code"],
        admin_id=squad["admin_id"],
        member_count=len(squad["members"]),
        is_admin=True,
        created_at=squad["created_at"],
    )


@router.post("/join", response_model=SquadResponse)
async def join(body: SquadJoin, user=Depends(get_current_user)):
    """Join a squad using an invite code."""
    user_id = str(user["_id"])
    squad = await join_squad(user_id, body.invite_code)
    return SquadResponse(
        id=str(squad["_id"]),
        name=squad["name"],
        description=squad.get("description"),
        invite_code=squad["invite_code"],
        admin_id=squad["admin_id"],
        member_count=len(squad["members"]) + 1,
        is_admin=squad["admin_id"] == user_id,
        created_at=squad["created_at"],
    )


@router.get("/mine", response_model=list[SquadResponse])
async def my_squads(user=Depends(get_current_user)):
    """Get all squads the current user is a member of."""
    user_id = str(user["_id"])
    squads = await get_user_squads(user_id)
    return [
        SquadResponse(
            id=str(s["_id"]),
            name=s["name"],
            description=s.get("description"),
            invite_code=s["invite_code"],
            admin_id=s["admin_id"],
            member_count=len(s["members"]),
            is_admin=s["admin_id"] == user_id,
            created_at=s["created_at"],
        )
        for s in squads
    ]


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
