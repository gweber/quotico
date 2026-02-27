"""
backend/app/models/leagues.py

Purpose:
    Pydantic models for league persistence and admin-level feature flag control.
    Defines the League Tower schema used by admin APIs and worker gating logic.

Dependencies:
    - enum.Enum
    - pydantic.BaseModel
    - pydantic.Field
    - app.models.common.PyObjectId
"""

from enum import Enum

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.models.common import PyObjectId


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
    id: PyObjectId | None = Field(alias="_id", default=None)
    league_id: int
    display_name: str
    structure_type: LeagueType = LeagueType.LEAGUE
    country_code: str | None = None
    tier: str | None = None
    ui_order: int = 999
    current_season: int
    is_active: bool = True
    features: LeagueFeatures = Field(default_factory=LeagueFeatures)
    external_ids: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )
