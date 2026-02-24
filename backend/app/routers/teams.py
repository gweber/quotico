"""Team detail API — public team profile, stats, and schedule."""

from fastapi import APIRouter, HTTPException, Query

from app.services.team_service import get_team_profile, search_teams

router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.get("/search")
async def search(
    q: str = Query(..., min_length=2, max_length=100),
    sport_key: str | None = Query(None, description="Filter by sport key"),
    limit: int = Query(20, ge=1, le=50),
):
    """Search teams by name across all leagues."""
    return await search_teams(q, sport_key, limit)


@router.get("/{team_slug}")
async def get_team(
    team_slug: str,
    sport_key: str | None = Query(None, description="Filter by sport key"),
):
    """Team detail page data: profile, form, season stats, upcoming schedule."""
    profile = await get_team_profile(team_slug, sport_key)
    if not profile:
        raise HTTPException(status_code=404, detail="Team not found.")
    return profile
