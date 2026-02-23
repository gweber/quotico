import logging

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.config import settings
import app.database as _db
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.tip_service")


async def create_tip(
    user_id: str, match_id: str, prediction: str, displayed_odds: float
) -> dict:
    """Create a new tip with server-side validation.

    Validates:
    - Match exists and is upcoming
    - No duplicate tip for this user+match
    - Prediction is valid for the match type
    - Displayed odds match current odds (within staleness threshold)
    """
    # Get match
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spiel nicht gefunden.",
        )

    # Check match hasn't started
    now = utcnow()
    commence = ensure_utc(match["commence_time"])
    if commence <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dieses Spiel hat bereits begonnen. Tipps sind nicht mehr möglich.",
        )

    if match["status"] != "upcoming":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tipps sind nur für kommende Spiele möglich.",
        )

    # Validate prediction value
    current_odds = match["current_odds"]
    if prediction not in current_odds:
        valid = ", ".join(current_odds.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ungültige Vorhersage. Erlaubt: {valid}",
        )

    # Check odds staleness
    odds_updated = ensure_utc(match["odds_updated_at"])
    odds_age = (now - odds_updated).total_seconds()
    if odds_age > settings.ODDS_STALENESS_MAX_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Die Quoten sind veraltet. Bitte Seite neu laden.",
        )

    # Lock odds server-side (use current odds, not displayed)
    locked_odds = current_odds[prediction]

    # Validate displayed odds aren't too far off (>20% difference = suspicious)
    if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Die Quoten haben sich geändert. Bitte erneut versuchen.",
        )

    tip_doc = {
        "user_id": user_id,
        "match_id": match_id,
        "selection": {"type": "moneyline", "value": prediction},
        "locked_odds": locked_odds,
        "locked_odds_age_seconds": int(odds_age),
        "points_earned": None,
        "status": "pending",
        "void_reason": None,
        "resolved_at": None,
        "created_at": now,
    }

    try:
        result = await _db.db.tips.insert_one(tip_doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Du hast bereits einen Tipp für dieses Spiel abgegeben.",
        )

    tip_doc["_id"] = result.inserted_id
    logger.info(
        "Tip created: user=%s match=%s prediction=%s odds=%.2f",
        user_id, match_id, prediction, locked_odds,
    )
    return tip_doc


async def create_tip_internal(
    user_id: str, match_id: str, prediction: str
) -> dict:
    """Create a tip for internal/bot use — no HTTP exceptions, no odds drift check.

    Raises ValueError for invalid state.
    Raises DuplicateKeyError for duplicate tips (caller handles idempotency).
    """
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise ValueError(f"Match {match_id} not found")

    now = utcnow()
    commence = ensure_utc(match["commence_time"])
    if commence <= now:
        raise ValueError(f"Match {match_id} already started")

    if match["status"] != "upcoming":
        raise ValueError(f"Match {match_id} status is {match['status']}")

    current_odds = match.get("current_odds", {})
    if prediction not in current_odds:
        raise ValueError(f"Invalid prediction '{prediction}' for match {match_id}")

    locked_odds = current_odds[prediction]
    odds_updated = ensure_utc(match["odds_updated_at"])
    odds_age = int((now - odds_updated).total_seconds())

    tip_doc = {
        "user_id": user_id,
        "match_id": match_id,
        "selection": {"type": "moneyline", "value": prediction},
        "locked_odds": locked_odds,
        "locked_odds_age_seconds": odds_age,
        "points_earned": None,
        "status": "pending",
        "void_reason": None,
        "resolved_at": None,
        "created_at": now,
    }

    result = await _db.db.tips.insert_one(tip_doc)
    tip_doc["_id"] = result.inserted_id
    logger.info(
        "Internal tip created: user=%s match=%s prediction=%s odds=%.2f",
        user_id, match_id, prediction, locked_odds,
    )
    return tip_doc


async def get_user_tips(
    user_id: str, limit: int = 50, match_ids: list[str] | None = None
) -> list[dict]:
    """Get tips for a user, optionally filtered by match IDs."""
    match_filter: dict = {"user_id": user_id}
    if match_ids:
        match_filter["match_id"] = {"$in": match_ids}
    pipeline = [
        {"$match": match_filter},
        {"$sort": {"created_at": -1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "matches",
                "let": {"mid": {"$toObjectId": "$match_id"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$mid"]}}},
                    {"$project": {"teams": 1, "sport_key": 1}},
                ],
                "as": "match_info",
            }
        },
        {"$unwind": {"path": "$match_info", "preserveNullAndEmptyArrays": True}},
    ]
    return await _db.db.tips.aggregate(pipeline).to_list(length=limit)
