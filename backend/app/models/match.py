from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel

from app.utils import as_utc


class MatchStatus(str, Enum):
    scheduled = "scheduled"
    live = "live"
    final = "final"
    cancelled = "cancelled"


class OddsData(BaseModel):
    """Nested odds structure stored in a match document."""
    h2h: Dict[str, float] = {}           # {"1": 1.85, "X": 3.4, "2": 4.1}
    totals: Dict[str, float] = {}        # {"over": 1.85, "under": 2.05, "line": 2.5}
    spreads: Dict[str, float] = {}       # {"home_line": -5.5, "home_odds": 1.91, ...}
    updated_at: Optional[datetime] = None
    bookmakers: Dict[str, Dict[str, float]] = {}  # multi-bookmaker historical


class ResultData(BaseModel):
    """Nested result structure stored in a match document."""
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    outcome: Optional[str] = None        # "1", "X", "2"
    half_time: Optional[Dict[str, Optional[int]]] = None


class MatchInDB(BaseModel):
    """Unified match document as stored in MongoDB.

    A single document matures through: scheduled -> live -> final.
    Replaces both the old ``matches`` and ``historical_matches`` collections.
    """
    sport_key: str
    match_date: datetime                  # kickoff time (was commence_time)
    status: MatchStatus = MatchStatus.scheduled
    season: str                           # "2526"
    season_label: str                     # "2025/26"
    league_code: Optional[str] = None     # "D1", "E0", etc.

    # Teams — canonical at insert time
    home_team: str                        # display name
    away_team: str
    home_team_key: str                    # normalized key (dedup + H2H)
    away_team_key: str

    # Provider metadata
    metadata: Dict = {}                   # {theoddsapi_id, source, ...}

    # Matchday mode
    matchday_number: Optional[int] = None
    matchday_season: Optional[int] = None

    # Odds (nested)
    odds: OddsData = OddsData()

    # Result (nested, null fields until final)
    result: ResultData = ResultData()

    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# API response models — native field names (no compatibility shim)
# ---------------------------------------------------------------------------


class OddsResponse(BaseModel):
    """Odds data returned to the client."""
    h2h: Dict[str, float] = {}
    totals: Dict[str, float] = {}
    spreads: Dict[str, float] = {}
    updated_at: Optional[datetime] = None


class ResultResponse(BaseModel):
    """Result data returned to the client."""
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    outcome: Optional[str] = None


class MatchResponse(BaseModel):
    """Match data returned to the client."""
    id: str
    sport_key: str
    home_team: str
    away_team: str
    match_date: datetime
    status: str  # scheduled, live, final, cancelled
    odds: OddsResponse
    result: ResultResponse


def db_to_response(doc: dict) -> MatchResponse:
    """Convert a MongoDB match document to an API response."""
    odds = doc.get("odds", {})
    result = doc.get("result", {})

    return MatchResponse(
        id=str(doc["_id"]),
        sport_key=doc["sport_key"],
        home_team=doc.get("home_team", ""),
        away_team=doc.get("away_team", ""),
        match_date=as_utc(doc["match_date"]),
        status=doc.get("status", "scheduled"),
        odds=OddsResponse(
            h2h=odds.get("h2h", {}),
            totals=odds.get("totals", {}),
            spreads=odds.get("spreads", {}),
            updated_at=as_utc(odds.get("updated_at")),
        ),
        result=ResultResponse(
            home_score=result.get("home_score"),
            away_score=result.get("away_score"),
            outcome=result.get("outcome"),
        ),
    )


class LiveScoreResponse(BaseModel):
    """Live score for an in-progress match."""
    match_id: str
    home_score: int
    away_score: int
    minute: Optional[int] = None
    half_time_home: Optional[int] = None
    half_time_away: Optional[int] = None
