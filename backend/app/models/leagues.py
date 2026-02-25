"""
backend/app/models/leagues.py

Purpose:
    Pydantic models for league persistence and admin-level feature flag control.
    Defines the League Tower schema used by admin APIs and worker gating logic.

Dependencies:
    - enum.Enum
    - pydantic.BaseModel
    - pydantic.Field
"""

from enum import Enum

from pydantic import BaseModel, Field


class LeagueType(str, Enum):
    LEAGUE = "league"
    CUP = "cup"
    TOURNAMENT = "tournament"


class LeagueFeatures(BaseModel):
    tipping: bool = False
    match_load: bool = True
    xg_sync: bool = False
    odds_sync: bool = False


class League(BaseModel):
    sport_key: str
    display_name: str
    structure_type: LeagueType = LeagueType.LEAGUE
    country_code: str | None = None
    tier: str | None = None
    ui_order: int = 999
    current_season: int
    is_active: bool = True
    needs_review: bool = False
    features: LeagueFeatures = Field(default_factory=LeagueFeatures)
    external_ids: dict[str, str] = Field(default_factory=dict)
