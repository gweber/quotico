"""Survivor mode models — pick one team per matchday, eliminated on loss."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SurvivorPick(BaseModel):
    matchday_number: int
    team: str
    team_name: Optional[str] = None
    team_id: Optional[str] = None
    match_id: str
    result: str = "pending"  # pending | won | lost | draw


class SurvivorEntryInDB(BaseModel):
    """One entry per user per squad per sport per season."""
    user_id: str
    squad_id: str
    league_id: int
    season: int
    status: str = "alive"  # alive | eliminated
    picks: list[SurvivorPick] = []
    used_teams: list[str] = []
    used_team_ids: list[str] = []
    streak: int = 0
    eliminated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SurvivorPickCreate(BaseModel):
    """Request body for making a survivor pick."""
    match_id: str
    team: str  # team name to pick


class SurvivorEntryResponse(BaseModel):
    """Survivor status returned to the client."""
    id: str
    status: str
    picks: list[dict]
    used_teams: list[str]
    used_team_ids: list[str] = []
    streak: int
    eliminated_at: Optional[datetime] = None


class SurvivorStandingEntry(BaseModel):
    """Single entry in survivor standings."""
    user_id: str
    alias: str
    status: str
    streak: int
    last_pick: Optional[str] = None
    eliminated_at: Optional[datetime] = None
