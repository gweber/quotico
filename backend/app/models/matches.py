"""
backend/app/models/matches.py

Purpose:
    Unified Match domain model for league and cup fixtures in Team-Tower /
    League-Tower architecture. Stores identity links via ObjectId references,
    UTC scheduling, provider external IDs, and structured score states.

Dependencies:
    - pydantic
    - bson.ObjectId
    - app.models.common.PyObjectId
    - app.services.odds_meta_service
    - app.utils.ensure_utc
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.models.common import PyObjectId
from app.services.odds_meta_service import build_legacy_like_odds
from app.utils import ensure_utc


class MatchStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"


class ScoreDetail(BaseModel):
    home: int | None = None
    away: int | None = None


class MatchScore(BaseModel):
    full_time: ScoreDetail = Field(default_factory=ScoreDetail)
    half_time: ScoreDetail | None = None
    extra_time: ScoreDetail | None = None
    penalties: ScoreDetail | None = None


class MatchInDB(BaseModel):
    id: PyObjectId | None = Field(alias="_id", default=None)
    league_id: PyObjectId
    sport_key: str
    home_team_id: PyObjectId
    away_team_id: PyObjectId
    match_date: datetime
    last_updated: datetime
    matchday: int | None = None
    round_name: str | None = None
    group_name: str | None = None
    season: int
    status: MatchStatus = MatchStatus.SCHEDULED
    score: MatchScore = Field(default_factory=MatchScore)
    score_extra_time: ScoreDetail | None = None
    score_penalties: ScoreDetail | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )


class Match(MatchInDB):
    pass


class OddsResponse(BaseModel):
    h2h: dict[str, float] = Field(default_factory=dict)
    totals: dict[str, float] = Field(default_factory=dict)
    spreads: dict[str, float] = Field(default_factory=dict)
    updated_at: datetime | None = None


class ResultResponse(BaseModel):
    home_score: int | None = None
    away_score: int | None = None
    outcome: str | None = None


class MatchResponse(BaseModel):
    id: str
    sport_key: str
    home_team: str
    away_team: str
    match_date: datetime
    status: str
    odds_meta: dict[str, Any] = Field(default_factory=dict)
    odds: OddsResponse
    result: ResultResponse


class LiveScoreResponse(BaseModel):
    match_id: str
    home_score: int
    away_score: int
    minute: int | None = None
    half_time_home: int | None = None
    half_time_away: int | None = None


def _coerce_score(score: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not isinstance(score, dict):
        return None, None
    return score.get("home"), score.get("away")


def _legacy_outcome(home: int | None, away: int | None) -> str | None:
    if home is None or away is None:
        return None
    if home > away:
        return "1"
    if home < away:
        return "2"
    return "X"


def db_to_response(doc: dict) -> MatchResponse:
    score = doc.get("score", {})
    full_time = score.get("full_time", {}) if isinstance(score, dict) else {}
    home_score, away_score = _coerce_score(full_time)

    odds = build_legacy_like_odds(doc)
    return MatchResponse(
        id=str(doc["_id"]),
        sport_key=str(doc.get("sport_key") or ""),
        home_team=str(doc.get("home_team") or ""),
        away_team=str(doc.get("away_team") or ""),
        match_date=ensure_utc(doc.get("match_date")),
        status=str(doc.get("status") or MatchStatus.SCHEDULED.value),
        odds_meta=doc.get("odds_meta", {}) if isinstance(doc.get("odds_meta"), dict) else {},
        odds=OddsResponse(
            h2h=odds.get("h2h", {}) if isinstance(odds, dict) else {},
            totals=odds.get("totals", {}) if isinstance(odds, dict) else {},
            spreads=odds.get("spreads", {}) if isinstance(odds, dict) else {},
            updated_at=ensure_utc(odds.get("updated_at")) if isinstance(odds, dict) and odds.get("updated_at") else None,
        ),
        result=ResultResponse(
            home_score=home_score,
            away_score=away_score,
            outcome=_legacy_outcome(home_score, away_score),
        ),
    )
