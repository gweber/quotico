"""
backend/app/models/match.py

Purpose:
    Compatibility import layer for the migrated Match domain model now located
    in `backend/app/models/matches.py`.

Dependencies:
    - app.models.matches
"""

from app.models.matches import (
    LiveScoreResponse,
    MatchInDB,
    MatchResponse,
    MatchScore,
    MatchStatus,
    OddsResponse,
    ResultResponse,
    ScoreDetail,
    db_to_response,
)

__all__ = [
    "LiveScoreResponse",
    "MatchInDB",
    "MatchResponse",
    "MatchScore",
    "MatchStatus",
    "OddsResponse",
    "ResultResponse",
    "ScoreDetail",
    "db_to_response",
]
