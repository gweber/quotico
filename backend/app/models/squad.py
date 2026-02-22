from datetime import datetime
from typing import Dict, List, Optional, Any

from pydantic import BaseModel


class LeagueConfig(BaseModel):
    """A single league configuration within a squad."""
    sport_key: str
    game_mode: str  # classic | bankroll | survivor | over_under | fantasy | spieltag
    config: Dict[str, Any] = {}
    activated_at: datetime
    deactivated_at: Optional[datetime] = None


class SquadInDB(BaseModel):
    """Full squad document as stored in MongoDB."""
    name: str
    description: Optional[str] = None
    invite_code: str  # Unique, e.g. QUO-789-XY
    admin_id: str  # User._id reference
    members: List[str] = []  # List of User._ids (includes admin)
    # Per-league game mode configuration
    league_configs: List[LeagueConfig] = []
    # Legacy game mode settings (deprecated, kept for backward compat)
    game_mode: str = "classic"
    game_mode_config: Dict[str, Any] = {}
    game_mode_changed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SquadCreate(BaseModel):
    """Request body for creating a squad."""
    name: str
    description: Optional[str] = None


class SquadJoin(BaseModel):
    """Request body for joining a squad."""
    invite_code: str


class LeagueConfigResponse(BaseModel):
    """League configuration returned to the client."""
    sport_key: str
    game_mode: str
    config: Dict[str, Any] = {}
    activated_at: datetime
    deactivated_at: Optional[datetime] = None


class SquadResponse(BaseModel):
    """Squad data returned to the client."""
    id: str
    name: str
    description: Optional[str] = None
    invite_code: str
    admin_id: str
    member_count: int
    is_admin: bool = False
    league_configs: List[LeagueConfigResponse] = []
    # Legacy (deprecated, kept for backward compat)
    game_mode: str = "classic"
    game_mode_config: Dict[str, Any] = {}
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
