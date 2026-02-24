import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.battle_service")


async def create_battle(
    admin_id: str,
    squad_a_id: str,
    squad_b_id: str,
    start_time: datetime,
    end_time: datetime,
) -> dict:
    """Create a new Squad Battle. Only squad admins can create battles."""
    # Verify admin is admin of at least one squad
    squad_a = await _db.db.squads.find_one({"_id": ObjectId(squad_a_id)})
    squad_b = await _db.db.squads.find_one({"_id": ObjectId(squad_b_id)})

    if not squad_a or not squad_b:
        raise HTTPException(status_code=404, detail="One or both squads not found.")

    if squad_a["admin_id"] != admin_id and squad_b["admin_id"] != admin_id:
        raise HTTPException(
            status_code=403,
            detail="Only an admin of one of the participating squads can create a battle.",
        )

    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="Start time must be before end time.")

    now = utcnow()
    battle_doc = {
        "squad_a_id": squad_a_id,
        "squad_b_id": squad_b_id,
        "start_time": start_time,
        "end_time": end_time,
        "status": "scheduled",
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.battles.insert_one(battle_doc)
    battle_doc["_id"] = result.inserted_id
    logger.info("Battle created: %s vs %s", squad_a["name"], squad_b["name"])
    return battle_doc


async def commit_to_battle(user_id: str, battle_id: str, squad_id: str) -> dict:
    """Commit a user to fight for a specific squad in a battle.

    Rules:
    - User must be a member of the chosen squad
    - Cannot change commitment after first match starts (lock-in)
    - Cannot commit to both sides (double-dipping prevention)
    """
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Battle not found.")

    # Lock-in check: cannot change after battle starts
    now = utcnow()
    start_time = ensure_utc(battle["start_time"])
    if battle["status"] == "active" or now >= start_time:
        existing = await _db.db.battle_participations.find_one({
            "battle_id": battle_id,
            "user_id": user_id,
        })
        if existing:
            raise HTTPException(
                status_code=400,
                detail="The battle has already started. Switching sides is no longer possible.",
            )

    # Verify user is member of the chosen squad
    if squad_id not in [battle["squad_a_id"], battle["squad_b_id"]]:
        raise HTTPException(status_code=400, detail="This squad is not participating in this battle.")

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad or user_id not in squad["members"]:
        raise HTTPException(status_code=403, detail="You are not a member of this squad.")

    # Double-dipping prevention: remove any existing commitment
    await _db.db.battle_participations.delete_many({
        "battle_id": battle_id,
        "user_id": user_id,
    })

    participation = {
        "battle_id": battle_id,
        "user_id": user_id,
        "squad_id": squad_id,
        "joined_at": now,
    }
    await _db.db.battle_participations.insert_one(participation)
    logger.info("User %s committed to squad %s in battle %s", user_id, squad_id, battle_id)
    return participation


async def get_battle_results(battle_id: str) -> dict:
    """Calculate battle results based on committed participants' bets during the battle window."""
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Battle not found.")

    async def squad_score(squad_id: str) -> dict:
        # Get committed participants for this side
        participants = await _db.db.battle_participations.find({
            "battle_id": battle_id,
            "squad_id": squad_id,
        }).to_list(length=100)

        if not participants:
            return {"squad_id": squad_id, "participants": 0, "total_points": 0, "avg_points": 0}

        user_ids = [p["user_id"] for p in participants]

        # Sum won slips during the battle window
        pipeline = [
            {
                "$match": {
                    "user_id": {"$in": user_ids},
                    "type": {"$in": ["single", "parlay"]},
                    "status": "won",
                    "submitted_at": {
                        "$gte": battle["start_time"],
                        "$lte": battle["end_time"],
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$total_odds"},
                    "unique_users": {"$addToSet": "$user_id"},
                }
            },
        ]
        result = await _db.db.betting_slips.aggregate(pipeline).to_list(length=1)

        if result:
            total = result[0]["total"]
            active_users = len(result[0]["unique_users"])
            avg = round(total / active_users, 2) if active_users > 0 else 0
        else:
            total = 0
            active_users = 0
            avg = 0

        squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
        return {
            "squad_id": squad_id,
            "name": squad["name"] if squad else "?",
            "participants": len(user_ids),
            "active_participants": active_users,
            "total_points": round(total, 2),
            "avg_points": avg,
        }

    a = await squad_score(battle["squad_a_id"])
    b = await squad_score(battle["squad_b_id"])

    winner = None
    if a["avg_points"] > b["avg_points"]:
        winner = a["name"]
    elif b["avg_points"] > a["avg_points"]:
        winner = b["name"]

    return {"squad_a": a, "squad_b": b, "winner": winner}


async def get_user_commitment(user_id: str, battle_id: str) -> Optional[str]:
    """Get which squad a user is committed to for a specific battle."""
    participation = await _db.db.battle_participations.find_one({
        "battle_id": battle_id,
        "user_id": user_id,
    })
    return participation["squad_id"] if participation else None


# ---------- Challenge system ----------


async def create_challenge(
    admin_id: str,
    squad_id: str,
    start_time: datetime,
    end_time: datetime,
    target_squad_id: Optional[str] = None,
) -> dict:
    """Create an open or direct challenge.

    Open (target_squad_id=None): visible in lobby for any squad to accept.
    Direct (target_squad_id set): only the target squad admin can accept.
    """
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Squad not found.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Only the squad admin can create challenges.")

    now = utcnow()
    if start_time <= now:
        raise HTTPException(status_code=400, detail="Start time must be in the future.")
    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="Start time must be before end time.")

    # Anti-spam: max 1 open + 3 pending outgoing challenges per squad
    open_count = await _db.db.battles.count_documents({
        "squad_a_id": squad_id, "status": "open",
    })
    if target_squad_id is None and open_count >= 1:
        raise HTTPException(
            status_code=400,
            detail="You already have an open challenge. Wait until it is accepted or expired.",
        )

    pending_count = await _db.db.battles.count_documents({
        "squad_a_id": squad_id, "status": "pending",
    })
    if target_squad_id and pending_count >= 3:
        raise HTTPException(
            status_code=400,
            detail="Maximum 3 pending direct challenges allowed.",
        )

    if target_squad_id:
        if target_squad_id == squad_id:
            raise HTTPException(status_code=400, detail="You cannot challenge yourself.")
        target = await _db.db.squads.find_one({"_id": ObjectId(target_squad_id)})
        if not target:
            raise HTTPException(status_code=404, detail="Target squad not found.")
        challenge_type = "direct"
        battle_status = "pending"
    else:
        challenge_type = "open"
        battle_status = "open"

    battle_doc = {
        "squad_a_id": squad_id,
        "squad_b_id": target_squad_id,
        "challenge_type": challenge_type,
        "start_time": start_time,
        "end_time": end_time,
        "status": battle_status,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.battles.insert_one(battle_doc)
    battle_doc["_id"] = result.inserted_id
    logger.info(
        "Challenge created: %s (%s) → %s",
        squad["name"], challenge_type, target_squad_id or "open lobby",
    )
    return battle_doc


async def accept_challenge(
    admin_id: str,
    battle_id: str,
    accepting_squad_id: str,
) -> dict:
    """Accept an open or direct challenge. Transitions to 'upcoming'."""
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Challenge not found.")

    if battle["status"] not in ("open", "pending"):
        raise HTTPException(status_code=400, detail="This challenge can no longer be accepted.")

    # Cannot accept own challenge
    if accepting_squad_id == battle["squad_a_id"]:
        raise HTTPException(status_code=400, detail="You cannot accept your own challenge.")

    # Verify accepting admin
    squad = await _db.db.squads.find_one({"_id": ObjectId(accepting_squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Squad not found.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Only the squad admin can accept challenges.")

    # For direct challenges, only the target squad can accept
    if battle["status"] == "pending" and battle.get("squad_b_id") != accepting_squad_id:
        raise HTTPException(status_code=403, detail="This challenge is not directed at your squad.")

    # Check start_time hasn't passed
    now = utcnow()
    if ensure_utc(battle["start_time"]) <= now:
        raise HTTPException(status_code=400, detail="The start time has already passed.")

    await _db.db.battles.update_one(
        {"_id": ObjectId(battle_id)},
        {"$set": {
            "squad_b_id": accepting_squad_id,
            "status": "scheduled",
            "updated_at": now,
        }},
    )
    logger.info("Challenge %s accepted by %s", battle_id, squad["name"])
    return {"message": "Challenge accepted!"}


async def decline_challenge(admin_id: str, battle_id: str) -> dict:
    """Decline a direct challenge."""
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Challenge not found.")

    if battle["status"] != "pending":
        raise HTTPException(status_code=400, detail="Only pending challenges can be declined.")

    target_squad_id = battle.get("squad_b_id")
    if not target_squad_id:
        raise HTTPException(status_code=400, detail="Invalid challenge.")

    squad = await _db.db.squads.find_one({"_id": ObjectId(target_squad_id)})
    if not squad or squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Only the admin of the target squad can decline.")

    now = utcnow()
    await _db.db.battles.update_one(
        {"_id": ObjectId(battle_id)},
        {"$set": {"status": "declined", "updated_at": now}},
    )
    logger.info("Challenge %s declined", battle_id)
    return {"message": "Challenge declined."}


async def get_lobby_challenges(user_squad_ids: list[str]) -> list[dict]:
    """Get challenges visible to the user:
    - Open challenges from other squads
    - Incoming direct challenges to user's squads
    """
    now = utcnow()
    battles = await _db.db.battles.find({
        "$or": [
            # Open challenges from other squads (not ours)
            {"status": "open", "squad_a_id": {"$nin": user_squad_ids}, "start_time": {"$gt": now}},
            # Incoming direct challenges to our squads
            {"status": "pending", "squad_b_id": {"$in": user_squad_ids}, "start_time": {"$gt": now}},
        ],
    }).sort("start_time", 1).to_list(length=50)

    return battles


async def expire_stale_challenges() -> int:
    """Expire open/pending challenges whose start_time has passed."""
    now = utcnow()
    result = await _db.db.battles.update_many(
        {"status": {"$in": ["open", "pending"]}, "start_time": {"$lte": now}},
        {"$set": {"status": "expired", "updated_at": now}},
    )
    if result.modified_count:
        logger.info("Expired %d stale challenges", result.modified_count)
    return result.modified_count
