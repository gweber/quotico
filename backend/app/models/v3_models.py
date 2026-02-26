"""
backend/app/models/v3_models.py

Purpose:
    Greenfield v3 MongoDB models for Sportmonks-based ingest domains.
    Defines canonical schemas for persons, matches_v3, and league_registry_v3.

Dependencies:
    - pydantic
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonStatsCache(BaseModel):
    matches_officiated: int = 0
    avg_yellow_cards: float = 0.0
    goals_total: int = 0


class PersonInDB(BaseModel):
    id: int = Field(alias="_id")
    type: Literal["referee", "player"]
    name: str
    common_name: str | None = None
    image_path: str | None = None
    stats_cache: PersonStatsCache = Field(default_factory=PersonStatsCache)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True)


class TeamV3(BaseModel):
    sm_id: int
    xg: float | None = None


class LineupEntryV3(BaseModel):
    player_id: int
    formation_field: str | None = None
    team_id: int


class PenaltyDetailV3(BaseModel):
    minute: int
    converted: bool


class PenaltyInfoV3(BaseModel):
    occurred: bool = False
    details: list[PenaltyDetailV3] = Field(default_factory=list)


class MatchTeamsV3(BaseModel):
    home: TeamV3
    away: TeamV3


class MatchV3InDB(BaseModel):
    id: int = Field(alias="_id")
    league_id: int
    season_id: int
    round_id: int | None = None
    referee_id: int | None = None
    start_at: datetime
    has_advanced_stats: bool = False
    status: Literal["FINISHED", "LIVE", "SCHEDULED", "POSTPONED", "WALKOVER"] = "SCHEDULED"
    teams: MatchTeamsV3
    lineups: list[LineupEntryV3] = Field(default_factory=list)
    penalty_info: PenaltyInfoV3 = Field(default_factory=PenaltyInfoV3)
    odds_meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True)


class LeagueRegistryV3(BaseModel):
    id: int = Field(alias="_id")
    name: str
    country: str | None = None
    is_cup: bool = False
    available_seasons: list[dict] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True)
