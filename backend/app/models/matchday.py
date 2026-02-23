"""Spieltag-Modus data models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MatchdayStatus(str, Enum):
    upcoming = "upcoming"
    in_progress = "in_progress"
    completed = "completed"


class AutoTippStrategy(str, Enum):
    q_bot = "q_bot"       # Follow QuoticoTip → fallback odds favorite → fallback 1:1
    draw = "draw"         # Predict 1:1 for all untipped matches
    favorite = "favorite" # Predict based on odds favorite
    none = "none"         # No auto-tipp


# ---------- MongoDB documents ----------

class MatchdayInDB(BaseModel):
    """A single matchday (Spieltag) in a league season."""
    sport_key: str
    season: int
    matchday_number: int
    label: str                          # "Spieltag 17"
    match_ids: list[str] = Field(default_factory=list)  # References to matches collection
    match_count: int = 0
    first_kickoff: Optional[datetime] = None
    last_kickoff: Optional[datetime] = None
    status: MatchdayStatus = MatchdayStatus.upcoming
    all_resolved: bool = False
    created_at: datetime
    updated_at: datetime


class MatchPrediction(BaseModel):
    """A single match prediction within a Spieltag prediction."""
    match_id: str
    home_score: int
    away_score: int
    is_auto: bool = False
    is_admin_entry: bool = False
    points_earned: Optional[int] = None  # 0/1/2/3 after resolution


class SpieltagPredictionInDB(BaseModel):
    """A user's predictions for an entire matchday."""
    user_id: str
    matchday_id: str
    squad_id: Optional[str] = None  # None = global (no squad context)
    sport_key: str
    season: int
    matchday_number: int
    auto_tipp_strategy: AutoTippStrategy = AutoTippStrategy.none
    predictions: list[MatchPrediction] = Field(default_factory=list)
    admin_unlocked_matches: list[str] = Field(default_factory=list)
    total_points: Optional[int] = None
    status: str = "open"  # "open" | "partial" | "resolved"
    created_at: datetime
    updated_at: datetime


# ---------- API request/response models ----------

class PredictionInput(BaseModel):
    """Single match prediction from the client."""
    match_id: str
    home_score: int = Field(ge=0, le=99)
    away_score: int = Field(ge=0, le=99)


class SavePredictionsRequest(BaseModel):
    """Request to save/update predictions for a matchday."""
    predictions: list[PredictionInput]
    auto_tipp_strategy: AutoTippStrategy = AutoTippStrategy.none
    squad_id: Optional[str] = None  # Optional squad context


class AdminUnlockRequest(BaseModel):
    """Request for squad admin to unlock a match for a user."""
    squad_id: str
    matchday_id: str
    user_id: str
    match_id: str


class AdminPredictionRequest(BaseModel):
    """Request for squad admin to enter a prediction on behalf of a user."""
    squad_id: str
    matchday_id: str
    user_id: str
    match_id: str
    home_score: int = Field(ge=0, le=99)
    away_score: int = Field(ge=0, le=99)


class MatchdayResponse(BaseModel):
    """Matchday data returned to the client."""
    id: str
    sport_key: str
    season: int
    matchday_number: int
    label: str
    match_count: int
    first_kickoff: Optional[datetime] = None
    last_kickoff: Optional[datetime] = None
    status: str
    all_resolved: bool = False


class MatchdayDetailMatch(BaseModel):
    """Match data within a matchday detail response."""
    id: str
    teams: dict[str, str]
    commence_time: datetime
    status: str
    current_odds: dict[str, float] = Field(default_factory=dict)
    totals_odds: dict[str, float] = Field(default_factory=dict)
    spreads_odds: dict[str, float] = Field(default_factory=dict)
    result: Optional[str] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    is_locked: bool = False  # True if < 15 min to kickoff or started
    h2h_context: Optional[dict] = None  # Embedded historical H2H + form data
    quotico_tip: Optional[dict] = None  # Embedded QuoticoTip recommendation


class PredictionResponse(BaseModel):
    """A single match prediction in API response."""
    match_id: str
    home_score: int
    away_score: int
    is_auto: bool = False
    is_admin_entry: bool = False
    points_earned: Optional[int] = None


class SpieltagPredictionResponse(BaseModel):
    """User's prediction set for a matchday."""
    matchday_id: str
    squad_id: Optional[str] = None
    auto_tipp_strategy: str
    predictions: list[PredictionResponse]
    admin_unlocked_matches: list[str] = Field(default_factory=list)
    total_points: Optional[int] = None
    status: str


class SpieltagLeaderboardEntry(BaseModel):
    """Single entry in the Spieltag leaderboard."""
    rank: int
    user_id: str
    alias: str
    total_points: int
    matchdays_played: int
    exact_count: int = 0       # 3-point predictions
    diff_count: int = 0        # 2-point predictions
    tendency_count: int = 0    # 1-point predictions
