import logging

from fastapi import APIRouter, Depends

import app.database as _db
from app.models.badge import BADGE_DEFINITIONS, BadgeResponse
from app.services.auth_service import get_current_user

logger = logging.getLogger("quotico.badges")
router = APIRouter(prefix="/api/badges", tags=["badges"])


@router.get("/mine", response_model=list[BadgeResponse])
async def get_my_badges(user=Depends(get_current_user)):
    """Get all badges for the current user."""
    user_id = str(user["_id"])
    awarded = await _db.db.badges.find(
        {"user_id": user_id}
    ).to_list(length=100)

    badges: list[BadgeResponse] = []
    for badge_key, definition in BADGE_DEFINITIONS.items():
        award = next((a for a in awarded if a["badge_key"] == badge_key), None)
        badges.append(BadgeResponse(
            key=badge_key,
            name=definition["name"],
            description=definition["description"],
            icon=definition["icon"],
            awarded_at=award["awarded_at"] if award else None,
        ))

    return badges
