from datetime import datetime
from typing import Dict, List, Optional, Any

from pydantic import BaseModel


class SquadInDB(BaseModel):
    """Full squad document as stored in MongoDB."""
    name: str
    description: Optional[str] = None
    invite_code: str  # Unique, e.g. QUO-789-XY
    admin_id: str  # User._id reference
    members: List[str] = []  # List of User._ids (includes admin)
    created_at: datetime
    updated_at: datetime


class SquadCreate(BaseModel):
    """Request body for creating a squad."""
    name: str
    description: Optional[str] = None


class SquadJoin(BaseModel):
    """Request body for joining a squad."""
    invite_code: str


class SquadResponse(BaseModel):
    """Squad data returned to the client."""
    id: str
    name: str
    description: Optional[str] = None
    invite_code: str
    admin_id: str
    member_count: int
    is_admin: bool = False
    created_at: datetime


class SquadLeaderboardEntry(BaseModel):
    """Single entry in a squad leaderboard."""
    user_id: str
    email: str  # Masked for privacy
    points: float
    tip_count: int
    avg_odds: float


class SquadBattleResponse(BaseModel):
    """Result of a squad-vs-squad comparison."""
    squad_a: Dict[str, Any]
    squad_b: Dict[str, Any]
    winner: Optional[str] = None
