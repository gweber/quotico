from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel


class TipInDB(BaseModel):
    """Full tip document as stored in MongoDB. Immutable after creation (only status changes)."""
    user_id: str
    match_id: str
    selection: Dict[str, str]  # {"type": "moneyline", "value": "1"} or {"type": "moneyline", "value": "X"}
    locked_odds: float
    locked_odds_age_seconds: int  # How old the odds were when locked
    points_earned: Optional[float] = None
    status: str = "pending"  # pending, won, lost, void
    void_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime


class TipCreate(BaseModel):
    """Request body for creating a tip."""
    match_id: str
    prediction: str  # "1", "X", or "2"
    displayed_odds: float  # The odds the user saw when clicking


class TipResponse(BaseModel):
    """Tip data returned to the client."""
    id: str
    match_id: str
    selection: Dict[str, str]
    locked_odds: float
    points_earned: Optional[float] = None
    status: str
    created_at: datetime
    # Match context (populated on read)
    match_teams: Optional[Dict[str, str]] = None
    match_sport_key: Optional[str] = None
