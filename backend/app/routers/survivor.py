"""Survivor mode endpoints — pick teams, check status, view standings."""

from fastapi import APIRouter, Depends, Query, status

from app.models.survivor import SurvivorEntryResponse, SurvivorPickCreate
from app.services import survivor_service
from app.services.auth_service import get_current_user
from app.utils import utcnow

router = APIRouter(prefix="/api/survivor", tags=["survivor"])


@router.post("/{squad_id}/pick", status_code=status.HTTP_201_CREATED)
async def make_pick(
    squad_id: str,
    body: SurvivorPickCreate,
    user=Depends(get_current_user),
):
    """Make a survivor pick for the current matchday."""
    user_id = str(user["_id"])
    entry = await survivor_service.make_pick(
        user_id=user_id,
        squad_id=squad_id,
        match_id=body.match_id,
        team=body.team,
    )
    return _entry_response(entry)


@router.get("/{squad_id}/status")
async def get_status(
    squad_id: str,
    league_id: int = Query(...),
    season: int = Query(None),
    user=Depends(get_current_user),
):
    """Get user's survivor status and picks."""
    user_id = str(user["_id"])
    if not season:
        season = utcnow().year

    entry = await survivor_service.get_entry(user_id, squad_id, int(league_id), season)
    if not entry:
        return {"status": "not_started", "picks": [], "used_teams": [], "streak": 0}
    return _entry_response(entry)


@router.get("/{squad_id}/standings")
async def get_standings(
    squad_id: str,
    league_id: int = Query(...),
    season: int = Query(None),
    user=Depends(get_current_user),
):
    """Get survivor standings for a squad."""
    if not season:
        season = utcnow().year
    return await survivor_service.get_standings(squad_id, int(league_id), season)


def _entry_response(entry: dict) -> dict:
    picks = []
    for p in entry.get("picks", []):
        row = p if isinstance(p, dict) else p.model_dump()
        row = dict(row)
        if row.get("team_id") is not None:
            row["team_id"] = str(row["team_id"])
        picks.append(row)

    used_team_ids = [str(tid) for tid in entry.get("used_team_ids", [])]
    return SurvivorEntryResponse(
        id=str(entry["_id"]),
        status=entry["status"],
        picks=picks,
        used_teams=entry.get("used_teams", []),
        used_team_ids=used_team_ids,
        streak=entry.get("streak", 0),
        eliminated_at=entry.get("eliminated_at"),
    ).model_dump()
