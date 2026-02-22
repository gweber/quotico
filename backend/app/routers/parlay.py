"""Parlay (Kombi-Joker) endpoints — create and view combo bets."""

from fastapi import APIRouter, Depends, Query, status

from app.models.wallet import ParlayCreate, ParlayResponse
from app.services import parlay_service
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/parlay", tags=["parlay"])


@router.post("/{squad_id}", status_code=status.HTTP_201_CREATED)
async def create_parlay(
    squad_id: str,
    body: ParlayCreate,
    matchday_id: str = Query(...),
    user=Depends(get_current_user),
):
    """Create a parlay (combo bet) with exactly 3 legs."""
    user_id = str(user["_id"])
    parlay = await parlay_service.create_parlay(
        user_id=user_id,
        squad_id=squad_id,
        matchday_id=matchday_id,
        legs=body.legs,
        stake=body.stake,
    )
    return ParlayResponse(
        id=str(parlay["_id"]),
        legs=parlay["legs"],
        combined_odds=parlay["combined_odds"],
        stake=parlay.get("stake"),
        potential_win=parlay["potential_win"],
        status=parlay["status"],
        points_earned=parlay.get("points_earned"),
        created_at=parlay["created_at"],
    )


@router.get("/{squad_id}")
async def get_parlay(
    squad_id: str,
    matchday_id: str = Query(...),
    user=Depends(get_current_user),
):
    """Get user's parlay for a specific matchday."""
    user_id = str(user["_id"])
    parlay = await parlay_service.get_user_parlay(user_id, squad_id, matchday_id)
    if not parlay:
        return None
    return ParlayResponse(
        id=str(parlay["_id"]),
        legs=parlay["legs"],
        combined_odds=parlay["combined_odds"],
        stake=parlay.get("stake"),
        potential_win=parlay["potential_win"],
        status=parlay["status"],
        points_earned=parlay.get("points_earned"),
        created_at=parlay["created_at"],
    )
