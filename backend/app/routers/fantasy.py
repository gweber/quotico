"""Fantasy Matchups endpoints — pick teams, view standings."""

from fastapi import APIRouter, Depends, Query, status

from app.models.wallet import FantasyPickCreate, FantasyPickResponse
from app.services import fantasy_service
from app.services.auth_service import get_current_user
from app.utils import utcnow

router = APIRouter(prefix="/api/fantasy", tags=["fantasy"])


@router.post("/{squad_id}/pick", status_code=status.HTTP_201_CREATED)
async def make_pick(
    squad_id: str,
    body: FantasyPickCreate,
    user=Depends(get_current_user),
):
    """Make a fantasy team pick for the current matchday."""
    user_id = str(user["_id"])
    pick = await fantasy_service.make_pick(
        user_id=user_id,
        squad_id=squad_id,
        match_id=body.match_id,
        team=body.team,
    )
    return FantasyPickResponse(
        id=str(pick["_id"]),
        team=pick["team"],
        match_id=pick["match_id"],
        goals_scored=pick.get("goals_scored"),
        goals_conceded=pick.get("goals_conceded"),
        fantasy_points=pick.get("fantasy_points"),
        status=pick["status"],
        created_at=pick["created_at"],
    )


@router.get("/{squad_id}/pick")
async def get_pick(
    squad_id: str,
    sport: str = Query(...),
    season: int = Query(None),
    matchday: int = Query(...),
    user=Depends(get_current_user),
):
    """Get user's fantasy pick for a specific matchday."""
    user_id = str(user["_id"])
    if not season:
        season = utcnow().year

    pick = await fantasy_service.get_user_pick(user_id, squad_id, sport, season, matchday)
    if not pick:
        return None
    return FantasyPickResponse(
        id=str(pick["_id"]),
        team=pick["team"],
        match_id=pick["match_id"],
        goals_scored=pick.get("goals_scored"),
        goals_conceded=pick.get("goals_conceded"),
        fantasy_points=pick.get("fantasy_points"),
        status=pick["status"],
        created_at=pick["created_at"],
    )


@router.get("/{squad_id}/standings")
async def get_standings(
    squad_id: str,
    sport: str = Query(...),
    season: int = Query(None),
    user=Depends(get_current_user),
):
    """Get fantasy standings for a squad."""
    if not season:
        season = utcnow().year
    return await fantasy_service.get_standings(squad_id, sport, season)
