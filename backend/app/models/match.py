from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel


class MatchStatus(str, Enum):
    upcoming = "upcoming"
    live = "live"
    completed = "completed"
    cancelled = "cancelled"


class MatchInDB(BaseModel):
    """Full match document as stored in MongoDB."""
    external_id: str  # TheOddsAPI event ID
    sport_key: str
    teams: Dict[str, str]  # {"home": "FCB", "away": "BVB"}
    commence_time: datetime
    status: MatchStatus = MatchStatus.upcoming
    current_odds: Dict[str, float]  # {"1": 1.8, "X": 3.4, "2": 4.1} or {"1": 1.5, "2": 2.3}
    totals_odds: Dict[str, float] = {}  # {"over": 1.85, "under": 2.05, "line": 2.5}
    spreads_odds: Dict[str, float] = {}  # {"home_line": -5.5, "home_odds": 1.91, "away_line": 5.5, "away_odds": 1.91}
    odds_updated_at: datetime
    result: Optional[str] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    # Spieltag-Modus fields
    matchday_number: Optional[int] = None
    matchday_season: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class MatchResponse(BaseModel):
    """Match data returned to the client."""
    id: str
    sport_key: str
    teams: Dict[str, str]
    commence_time: datetime
    status: str
    current_odds: Dict[str, float]
    totals_odds: Dict[str, float] = {}
    spreads_odds: Dict[str, float] = {}
    odds_updated_at: datetime
    result: Optional[str] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None


class LiveScoreResponse(BaseModel):
    """Live score for an in-progress match."""
    match_id: str
    home_score: int
    away_score: int
    minute: Optional[int] = None
    half_time_home: Optional[int] = None
    half_time_away: Optional[int] = None
