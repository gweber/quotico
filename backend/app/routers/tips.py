from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.models.tip import TipCreate, TipResponse
from app.services.auth_service import get_current_user
from app.services.tip_service import create_tip, get_user_tips

router = APIRouter(prefix="/api/tips", tags=["tips"])


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=TipResponse)
async def submit_tip(
    body: TipCreate,
    user=Depends(get_current_user),
):
    """Submit a prediction for a match. Odds are locked server-side."""
    user_id = str(user["_id"])
    tip = await create_tip(
        user_id=user_id,
        match_id=body.match_id,
        prediction=body.prediction,
        displayed_odds=body.displayed_odds,
    )
    return TipResponse(
        id=str(tip["_id"]),
        match_id=tip["match_id"],
        selection=tip["selection"],
        locked_odds=tip["locked_odds"],
        points_earned=tip.get("points_earned"),
        status=tip["status"],
        created_at=tip["created_at"],
    )


@router.get("/mine", response_model=list[TipResponse])
async def my_tips(user=Depends(get_current_user)):
    """Get all tips for the current user."""
    user_id = str(user["_id"])
    tips = await get_user_tips(user_id)
    return [
        TipResponse(
            id=str(t["_id"]),
            match_id=t["match_id"],
            selection=t["selection"],
            locked_odds=t["locked_odds"],
            points_earned=t.get("points_earned"),
            status=t["status"],
            created_at=t["created_at"],
            match_teams=t.get("match_info", {}).get("teams"),
            match_sport_key=t.get("match_info", {}).get("sport_key"),
        )
        for t in tips
    ]
