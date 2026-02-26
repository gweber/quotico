"""
backend/app/models/v3_query_models.py

Purpose:
    Strict request/response schemas for v3 public query transport endpoints.
    Supports compact GET reads and POST payload-based queries.

Dependencies:
    - pydantic
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class BatchIdsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list, min_length=1)


class JusticeMetrics(BaseModel):
    xg_share_home: float | None = None
    implied_prob_home: float | None = None
    justice_diff: float | None = None


class MatchTeamSideOut(BaseModel):
    sm_id: int
    name: str | None = None
    short_code: str | None = None
    image_path: str | None = None
    xg: float | None = None
    score: int | None = None


class MatchEventOut(BaseModel):
    type: str
    minute: int | None = None
    extra_minute: int | None = None
    player_name: str = ""
    player_id: int | None = None
    team_id: int | None = None
    detail: str = ""
    sort_order: int | None = None


class PeriodScoreOut(BaseModel):
    home: int | None = None
    away: int | None = None


class PeriodScoresOut(BaseModel):
    half_time: PeriodScoreOut = Field(default_factory=PeriodScoreOut)
    full_time: PeriodScoreOut = Field(default_factory=PeriodScoreOut)


class MatchV3Out(BaseModel):
    id: int = Field(alias="_id")
    league_id: int
    season_id: int
    round_id: int | None = None
    referee_id: int | None = None
    start_at: datetime
    status: str
    finish_type: str | None = None
    has_advanced_stats: bool = False
    teams: dict[str, MatchTeamSideOut]
    events: list[MatchEventOut] = Field(default_factory=list)
    scores: PeriodScoresOut = Field(default_factory=PeriodScoresOut)
    odds_meta: dict[str, Any] = Field(default_factory=dict)
    odds_timeline: list[dict[str, Any]] = Field(default_factory=list)
    manual_check_required: bool = False
    justice: JusticeMetrics | None = None


class V3ListMeta(BaseModel):
    total: int
    limit: int
    offset: int
    query_hash: str | None = None
    source: Literal["fresh", "cache"] = "fresh"


class MatchesListResponse(BaseModel):
    items: list[MatchV3Out]
    meta: V3ListMeta


class MatchesQueryRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    season_id: int | None = None
    league_id: int | None = None
    team_id: int | None = None
    statuses: list[str] = Field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    sort_by: Literal["start_at", "season_id"] = "start_at"
    sort_dir: Literal["asc", "desc"] = "desc"
    min_justice_diff: float | None = None


class StatsQueryRequest(BaseModel):
    season_id: int | None = None
    league_id: int | None = None
    status: str | None = None


class StatsQueryResponse(BaseModel):
    total_matches: int
    advanced_stats_matches: int
    odds_covered_matches: int
    xg_coverage_percent: float
    odds_coverage_percent: float


class QbotTipsQueryRequest(BaseModel):
    season_id: int | None = None
    league_id: int | None = None
    limit: int = Field(default=50, ge=1, le=200)


class QbotTipsQueryResponse(BaseModel):
    items: list[dict[str, Any]]
    meta: V3ListMeta
