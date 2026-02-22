import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db

logger = logging.getLogger("quotico.squad_service")

MAX_SQUAD_MEMBERS = 50


def generate_invite_code() -> str:
    """Generate a unique invite code like QUO-789-XY."""
    digits = "".join(secrets.choice(string.digits) for _ in range(3))
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(2))
    return f"QUO-{digits}-{letters}"


async def create_squad(admin_id: str, name: str, description: Optional[str] = None) -> dict:
    """Create a new squad with the creator as admin."""
    # Generate unique invite code
    for _ in range(10):
        code = generate_invite_code()
        existing = await _db.db.squads.find_one({"invite_code": code})
        if not existing:
            break
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Konnte keinen Einladungscode generieren.",
        )

    now = datetime.now(timezone.utc)
    squad_doc = {
        "name": name,
        "description": description,
        "invite_code": code,
        "admin_id": admin_id,
        "members": [admin_id],  # Admin is automatically a member
        "created_at": now,
        "updated_at": now,
    }

    result = await _db.db.squads.insert_one(squad_doc)
    squad_doc["_id"] = result.inserted_id
    logger.info("Squad created: %s (code: %s) by user %s", name, code, admin_id)
    return squad_doc


async def join_squad(user_id: str, invite_code: str) -> dict:
    """Join a squad using an invite code."""
    squad = await _db.db.squads.find_one({"invite_code": invite_code.upper()})
    if not squad:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ungültiger Einladungscode.",
        )

    if user_id in squad["members"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Du bist bereits Mitglied dieser Gruppe.",
        )

    if len(squad["members"]) >= MAX_SQUAD_MEMBERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Diese Gruppe hat die maximale Mitgliederzahl ({MAX_SQUAD_MEMBERS}) erreicht.",
        )

    await _db.db.squads.update_one(
        {"_id": squad["_id"]},
        {
            "$addToSet": {"members": user_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    logger.info("User %s joined squad %s", user_id, squad["name"])
    return squad


async def leave_squad(user_id: str, squad_id: str) -> None:
    """Leave a squad. Admin cannot leave (must transfer or delete)."""
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Gruppe nicht gefunden.")

    if squad["admin_id"] == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Als Admin kannst du die Gruppe nicht verlassen. Lösche sie oder übertrage die Admin-Rolle.",
        )

    await _db.db.squads.update_one(
        {"_id": ObjectId(squad_id)},
        {
            "$pull": {"members": user_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )


async def remove_member(admin_id: str, squad_id: str, member_id: str) -> None:
    """Admin removes a member from the squad."""
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Gruppe nicht gefunden.")

    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Nur der Admin kann Mitglieder entfernen.")

    if member_id == admin_id:
        raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst entfernen.")

    await _db.db.squads.update_one(
        {"_id": ObjectId(squad_id)},
        {
            "$pull": {"members": member_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )


async def get_user_squads(user_id: str) -> list[dict]:
    """Get all squads a user is a member of."""
    return await _db.db.squads.find({"members": user_id}).to_list(length=20)


async def get_squad_leaderboard(squad_id: str, requesting_user_id: str) -> list[dict]:
    """Calculate leaderboard for a squad using aggregation pipeline.

    Privacy rule: tips are only included for matches that have started.
    """
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Gruppe nicht gefunden.")

    if requesting_user_id not in squad["members"]:
        raise HTTPException(status_code=403, detail="Du bist kein Mitglied dieser Gruppe.")

    member_ids = squad["members"]

    pipeline = [
        # Only tips from squad members
        {"$match": {"user_id": {"$in": member_ids}}},
        # Only tips for matches that have started (privacy rule)
        {
            "$lookup": {
                "from": "matches",
                "let": {"mid": {"$toObjectId": "$match_id"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$mid"]}}},
                    {"$match": {"status": {"$in": ["live", "completed"]}}},
                ],
                "as": "match",
            }
        },
        {"$match": {"match": {"$ne": []}}},
        # Group by user
        {
            "$group": {
                "_id": "$user_id",
                "points": {
                    "$sum": {"$cond": [{"$eq": ["$status", "won"]}, "$locked_odds", 0]}
                },
                "tip_count": {"$sum": 1},
                "avg_odds": {"$avg": "$locked_odds"},
            }
        },
        {"$sort": {"points": -1}},
        # Join user alias
        {
            "$lookup": {
                "from": "users",
                "let": {"uid": {"$toObjectId": "$_id"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$uid"]}}},
                    {"$project": {"alias": 1}},
                ],
                "as": "user",
            }
        },
        {"$unwind": "$user"},
    ]

    results = await _db.db.tips.aggregate(pipeline).to_list(length=MAX_SQUAD_MEMBERS)

    return [
        {
            "user_id": r["_id"],
            "alias": r["user"].get("alias", "Anonymous"),
            "points": round(r["points"], 2),
            "tip_count": r["tip_count"],
            "avg_odds": round(r["avg_odds"], 2) if r["avg_odds"] else 0,
        }
        for r in results
    ]


async def get_squad_battle(squad_a_id: str, squad_b_id: str) -> dict:
    """Compare two squads by average member points."""
    async def squad_avg(sid: str) -> dict:
        squad = await _db.db.squads.find_one({"_id": ObjectId(sid)})
        if not squad:
            raise HTTPException(status_code=404, detail=f"Gruppe {sid} nicht gefunden.")

        pipeline = [
            {"$match": {"user_id": {"$in": squad["members"]}, "status": "won"}},
            {
                "$group": {
                    "_id": None,
                    "total_points": {"$sum": "$locked_odds"},
                    "member_count": {"$addToSet": "$user_id"},
                }
            },
        ]
        result = await _db.db.tips.aggregate(pipeline).to_list(length=1)
        if result:
            total = result[0]["total_points"]
            members = len(result[0]["member_count"])
            avg = round(total / members, 2) if members > 0 else 0
        else:
            total = 0
            members = 0
            avg = 0

        return {
            "squad_id": sid,
            "name": squad["name"],
            "total_points": round(total, 2),
            "member_count": members,
            "avg_points": avg,
        }

    a = await squad_avg(squad_a_id)
    b = await squad_avg(squad_b_id)

    winner = None
    if a["avg_points"] > b["avg_points"]:
        winner = a["name"]
    elif b["avg_points"] > a["avg_points"]:
        winner = b["name"]

    return {"squad_a": a, "squad_b": b, "winner": winner}
