from datetime import datetime
from typing import Dict, List, Optional, Any

from pydantic import BaseModel


class LeagueConfig(BaseModel):
    """A single league configuration within a squad."""
    league_id: int
    game_mode: str  # classic | bankroll | survivor | over_under | fantasy | matchday
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
    auto_bet_blocked: bool = False
    lock_minutes: int = 15
    is_public: bool = True  # Public squads appear in search; private only via invite/ID
    is_open: bool = True  # If True, users can request to join; if False, squad is locked
    invite_visible: bool = False  # If True, all members can see the invite code; otherwise admin only
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
    league_id: int
    game_mode: str
    config: Dict[str, Any] = {}
    activated_at: datetime
    deactivated_at: Optional[datetime] = None


class SquadResponse(BaseModel):
    """Squad data returned to the client."""
    id: str
    name: str
    description: Optional[str] = None
    invite_code: Optional[str] = None  # None when hidden from non-admin members
    admin_id: str
    member_count: int
    is_admin: bool = False
    league_configs: List[LeagueConfigResponse] = []
    auto_bet_blocked: bool = False
    lock_minutes: int = 15
    is_public: bool = True
    is_open: bool = True
    invite_visible: bool = False
    pending_requests: int = 0
    # Legacy (deprecated, kept for backward compat)
    game_mode: str = "classic"
    game_mode_config: Dict[str, Any] = {}
    created_at: datetime


class SquadLeaderboardEntry(BaseModel):
    """Single entry in a squad leaderboard."""
    user_id: str
    email: str  # Masked for privacy
    points: float
    bet_count: int
    avg_odds: float


class SquadBattleResponse(BaseModel):
    """Result of a squad-vs-squad comparison."""
    squad_a: Dict[str, Any]
    squad_b: Dict[str, Any]
    winner: Optional[str] = None


# ---------- War Room ----------


class WarRoomSelection(BaseModel):
    type: str  # "moneyline"
    value: str  # "1", "X", "2"


class WarRoomMember(BaseModel):
    user_id: str
    alias: str
    has_bet: bool
    is_self: bool = False
    selection: Optional[WarRoomSelection] = None
    locked_odds: Optional[float] = None
    bet_status: Optional[str] = None  # pending / won / lost / void
    points_earned: Optional[float] = None
    is_currently_winning: Optional[bool] = None


class WarRoomMatch(BaseModel):
    id: str
    league_id: int
    home_team: str
    away_team: str
    match_date: datetime
    status: str
    odds: Dict = {}
    result: Dict = {}


class WarRoomConsensus(BaseModel):
    percentages: Dict[str, float]  # e.g. {"1": 70.0, "X": 20.0, "2": 10.0}
    total_bettors: int


class WarRoomResponse(BaseModel):
    match: WarRoomMatch
    members: List[WarRoomMember]
    consensus: Optional[WarRoomConsensus] = None
    mavericks: Optional[List[str]] = None  # user_ids who went against majority
    is_post_kickoff: bool


# ---------- Join Requests ----------


class JoinRequestResponse(BaseModel):
    id: str
    squad_id: str
    user_id: str
    alias: str
    status: str  # pending | approved | declined
    created_at: datetime
