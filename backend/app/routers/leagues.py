"""
backend/app/routers/leagues.py

Purpose:
    Public League Tower endpoints used by the frontend navigation layer.
    Exposes cached active+tippable leagues ordered for UI rendering.

Dependencies:
    - app.services.league_service
"""

from fastapi import APIRouter

from app.services.league_service import get_active_navigation

router = APIRouter(prefix="/api/leagues", tags=["leagues"])


@router.get("/navigation")
async def league_navigation():
    return {"items": await get_active_navigation()}
