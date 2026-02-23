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
        raise HTTPException(status_code=404, detail="Eine oder beide Gruppen nicht gefunden.")

    if squad_a["admin_id"] != admin_id and squad_b["admin_id"] != admin_id:
        raise HTTPException(
            status_code=403,
            detail="Nur ein Admin einer der beteiligten Gruppen kann ein Battle erstellen.",
        )

    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="Startzeit muss vor Endzeit liegen.")

    now = utcnow()
    battle_doc = {
        "squad_a_id": squad_a_id,
        "squad_b_id": squad_b_id,
        "start_time": start_time,
        "end_time": end_time,
        "status": "upcoming",
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
        raise HTTPException(status_code=404, detail="Battle nicht gefunden.")

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
                detail="Das Battle hat bereits begonnen. Seitenwechsel nicht mehr möglich.",
            )

    # Verify user is member of the chosen squad
    if squad_id not in [battle["squad_a_id"], battle["squad_b_id"]]:
        raise HTTPException(status_code=400, detail="Diese Gruppe nimmt nicht an diesem Battle teil.")

    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad or user_id not in squad["members"]:
        raise HTTPException(status_code=403, detail="Du bist kein Mitglied dieser Gruppe.")

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
    """Calculate battle results based on committed participants' tips during the battle window."""
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Battle nicht gefunden.")

    async def squad_score(squad_id: str) -> dict:
        # Get committed participants for this side
        participants = await _db.db.battle_participations.find({
            "battle_id": battle_id,
            "squad_id": squad_id,
        }).to_list(length=100)

        if not participants:
            return {"squad_id": squad_id, "participants": 0, "total_points": 0, "avg_points": 0}

        user_ids = [p["user_id"] for p in participants]

        # Sum won tips during the battle window
        pipeline = [
            {
                "$match": {
                    "user_id": {"$in": user_ids},
                    "status": "won",
                    "created_at": {
                        "$gte": battle["start_time"],
                        "$lte": battle["end_time"],
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$locked_odds"},
                    "unique_users": {"$addToSet": "$user_id"},
                }
            },
        ]
        result = await _db.db.tips.aggregate(pipeline).to_list(length=1)

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
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Nur der Squad-Admin kann Herausforderungen erstellen.")

    now = utcnow()
    if start_time <= now:
        raise HTTPException(status_code=400, detail="Startzeit muss in der Zukunft liegen.")
    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="Startzeit muss vor Endzeit liegen.")

    # Anti-spam: max 1 open + 3 pending outgoing challenges per squad
    open_count = await _db.db.battles.count_documents({
        "squad_a_id": squad_id, "status": "open",
    })
    if target_squad_id is None and open_count >= 1:
        raise HTTPException(
            status_code=400,
            detail="Du hast bereits eine offene Herausforderung. Warte bis sie angenommen oder abgelaufen ist.",
        )

    pending_count = await _db.db.battles.count_documents({
        "squad_a_id": squad_id, "status": "pending",
    })
    if target_squad_id and pending_count >= 3:
        raise HTTPException(
            status_code=400,
            detail="Maximal 3 ausstehende direkte Herausforderungen erlaubt.",
        )

    if target_squad_id:
        if target_squad_id == squad_id:
            raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst herausfordern.")
        target = await _db.db.squads.find_one({"_id": ObjectId(target_squad_id)})
        if not target:
            raise HTTPException(status_code=404, detail="Ziel-Squad nicht gefunden.")
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
        raise HTTPException(status_code=404, detail="Herausforderung nicht gefunden.")

    if battle["status"] not in ("open", "pending"):
        raise HTTPException(status_code=400, detail="Diese Herausforderung kann nicht mehr angenommen werden.")

    # Cannot accept own challenge
    if accepting_squad_id == battle["squad_a_id"]:
        raise HTTPException(status_code=400, detail="Du kannst deine eigene Herausforderung nicht annehmen.")

    # Verify accepting admin
    squad = await _db.db.squads.find_one({"_id": ObjectId(accepting_squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Nur der Squad-Admin kann Herausforderungen annehmen.")

    # For direct challenges, only the target squad can accept
    if battle["status"] == "pending" and battle.get("squad_b_id") != accepting_squad_id:
        raise HTTPException(status_code=403, detail="Diese Herausforderung ist nicht an deinen Squad gerichtet.")

    # Check start_time hasn't passed
    now = utcnow()
    if ensure_utc(battle["start_time"]) <= now:
        raise HTTPException(status_code=400, detail="Die Startzeit ist bereits vorbei.")

    await _db.db.battles.update_one(
        {"_id": ObjectId(battle_id)},
        {"$set": {
            "squad_b_id": accepting_squad_id,
            "status": "upcoming",
            "updated_at": now,
        }},
    )
    logger.info("Challenge %s accepted by %s", battle_id, squad["name"])
    return {"message": "Herausforderung angenommen!"}


async def decline_challenge(admin_id: str, battle_id: str) -> dict:
    """Decline a direct challenge."""
    battle = await _db.db.battles.find_one({"_id": ObjectId(battle_id)})
    if not battle:
        raise HTTPException(status_code=404, detail="Herausforderung nicht gefunden.")

    if battle["status"] != "pending":
        raise HTTPException(status_code=400, detail="Nur ausstehende Herausforderungen können abgelehnt werden.")

    target_squad_id = battle.get("squad_b_id")
    if not target_squad_id:
        raise HTTPException(status_code=400, detail="Ungültige Herausforderung.")

    squad = await _db.db.squads.find_one({"_id": ObjectId(target_squad_id)})
    if not squad or squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Nur der Admin des Ziel-Squads kann ablehnen.")

    now = utcnow()
    await _db.db.battles.update_one(
        {"_id": ObjectId(battle_id)},
        {"$set": {"status": "declined", "updated_at": now}},
    )
    logger.info("Challenge %s declined", battle_id)
    return {"message": "Herausforderung abgelehnt."}


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
