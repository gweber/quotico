from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel


class SlipType(str, Enum):
    single = "single"
    parlay = "parlay"
    system = "system"
    matchday_round = "matchday_round"    # Matchday pool: one slip = all picks for a matchday
    survivor = "survivor"                # Season-long elimination game
    fantasy = "fantasy"                  # Per-matchday team performance pick


class SelectionStatus(str, Enum):
    draft = "draft"        # Not yet committed, odds not locked
    locked = "locked"      # Per-leg auto-freeze at kickoff minus buffer, odds frozen
    pending = "pending"    # Committed, awaiting match result
    won = "won"
    lost = "lost"
    scored = "scored"      # exact_score / fantasy leg — points_earned is set
    void = "void"


class SlipStatus(str, Enum):
    draft = "draft"        # Server-persisted, editable, no odds locked yet
    pending = "pending"    # Submitted/locked — odds frozen, awaiting resolution
    won = "won"
    lost = "lost"
    partial = "partial"    # Some resolved, some pending
    resolved = "resolved"  # Matchday round / fantasy: all scored
    void = "void"


class SelectionInDB(BaseModel):
    """One pick inside a betting slip.

    The `market` field determines resolution logic:

    h2h:
        pick = "1" | "X" | "2"
        locked_odds set at submit/lock time

    totals:
        pick = "over" | "under"
        locked_odds set at submit/lock time, line = totals line (e.g. 2.5)

    exact_score:
        pick = {"home": 2, "away": 1}
        points_earned calculated (configurable weights: exact/diff/tendency/miss)

    survivor_pick:
        pick = "Team Name" (team the user backs to win)
        match_result = "won" | "lost" | "draw"

    fantasy_pick:
        pick = "Team Name" (team whose stats contribute points)
        fantasy_points calculated from goals scored/conceded
    """
    match_id: str
    market: str = "h2h"                           # h2h | totals | exact_score | survivor_pick | fantasy_pick
    pick: Any                                     # str for h2h/totals/survivor/fantasy, dict for exact_score
    team_id: Optional[str] = None                 # Survivor/fantasy: team identity reference
    team_name: Optional[str] = None               # Survivor/fantasy: display only
    displayed_odds: Optional[float] = None        # Odds user saw when adding leg (draft state)
    locked_odds: Optional[float] = None           # Frozen server-side at submit/lock time
    line: Optional[float] = None                  # Totals line (e.g. 2.5 goals)
    points_earned: Optional[int] = None           # Set after resolution (exact_score, fantasy)
    is_auto: bool = False                         # Matchday auto-bet flag
    is_admin_entry: bool = False                  # Admin-entered prediction
    status: SelectionStatus = SelectionStatus.draft
    locked_at: Optional[datetime] = None          # Timestamp when this leg was frozen

    # Resolution audit fields
    actual_score: Optional[Dict[str, int]] = None   # {"home": int, "away": int}
    match_result: Optional[str] = None              # "won" | "lost" | "draw" (survivor/fantasy)
    goals_scored: Optional[int] = None              # Fantasy: from picked team's perspective
    goals_conceded: Optional[int] = None            # Fantasy: from picked team's perspective
    fantasy_points: Optional[int] = None            # Fantasy scoring result
    matchday_number: Optional[int] = None           # Survivor/fantasy: which matchday this pick is for


class BettingSlipInDB(BaseModel):
    """Full betting slip document as stored in MongoDB."""
    user_id: str
    squad_id: Optional[str] = None
    type: SlipType = SlipType.single
    selections: List[SelectionInDB]

    # Market bet fields (single/parlay)
    total_odds: Optional[float] = None            # Product of all selection odds
    stake: float = 10.0                           # Virtual currency stake
    potential_payout: Optional[float] = None      # stake * total_odds
    funding: str = "virtual"                      # "virtual" | "wallet"
    wallet_id: Optional[str] = None               # Bankroll/O-U wallet link

    # Matchday round fields
    matchday_id: Optional[str] = None             # Links to matchdays collection
    matchday_number: Optional[int] = None
    sport_key: Optional[str] = None
    season: Optional[int] = None
    auto_bet_strategy: Optional[str] = None       # "none" | "draw" | "favorite" | "q_bot"
    total_points: Optional[int] = None            # Sum of selection points_earned
    admin_unlocked_matches: List[str] = []        # match_ids unlocked by squad admin
    point_weights: Optional[Dict[str, int]] = None  # {"exact":4,"diff":3,"tendency":2,"miss":0}

    # Survivor fields
    used_teams: List[str] = []                    # Teams already picked this season
    used_team_ids: List[str] = []                 # Team ObjectIds already picked this season
    streak: int = 0                               # Consecutive survived matchdays
    eliminated_at: Optional[datetime] = None      # When eliminated

    # Common
    status: SlipStatus = SlipStatus.draft
    submitted_at: Optional[datetime] = None       # None until user submits (locks)
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ---------- Request / Response models ----------

class SlipSelectionCreate(BaseModel):
    """One selection in a slip submission."""
    match_id: str
    market: str = "h2h"
    pick: str                                     # "1", "X", "2", "over", "under"
    displayed_odds: float                         # Odds the user saw


class CreateSlipRequest(BaseModel):
    """Request body for creating a classic betting slip."""
    selections: List[SlipSelectionCreate]


class CreateDraftRequest(BaseModel):
    """Request body for creating a new draft slip."""
    type: SlipType = SlipType.single
    squad_id: Optional[str] = None
    matchday_id: Optional[str] = None
    sport_key: Optional[str] = None
    funding: str = "virtual"


class PatchSelectionRequest(BaseModel):
    """Add, update, or remove a leg on a draft slip."""
    action: str                                   # "add" | "update" | "remove"
    match_id: str
    market: str = "h2h"
    pick: Optional[Any] = None                    # Required for add/update
    displayed_odds: Optional[float] = None        # Required for h2h/totals add/update


class BankrollBetRequest(BaseModel):
    """Request body for a bankroll-funded single bet."""
    squad_id: str
    match_id: str
    prediction: str                               # "1", "X", "2"
    stake: float
    displayed_odds: float


class OverUnderBetRequest(BaseModel):
    """Request body for an over/under bet."""
    squad_id: str
    match_id: str
    prediction: str                               # "over" | "under"
    stake: Optional[float] = None
    displayed_odds: float


class SurvivorPickRequest(BaseModel):
    """Request body for a survivor pick."""
    squad_id: str
    match_id: str
    team: str


class FantasyPickRequest(BaseModel):
    """Request body for a fantasy team pick."""
    squad_id: str
    match_id: str
    team: str


class ParlayLeg(BaseModel):
    """One leg in a parlay request."""
    match_id: str
    prediction: str
    displayed_odds: float


class ParlayRequest(BaseModel):
    """Request body for a parlay (combo bet)."""
    squad_id: str
    matchday_id: str
    legs: List[ParlayLeg]
    stake: Optional[float] = None


class BettingSlipResponse(BaseModel):
    """Betting slip data returned to the client."""
    id: str
    user_id: str
    squad_id: Optional[str] = None
    type: str
    selections: List[Dict]
    total_odds: Optional[float] = None
    stake: float
    potential_payout: Optional[float] = None
    funding: str = "virtual"
    status: str
    submitted_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    # Matchday round specific
    matchday_number: Optional[int] = None
    total_points: Optional[int] = None
    point_weights: Optional[Dict[str, int]] = None

    # Survivor specific
    streak: Optional[int] = None
    eliminated_at: Optional[datetime] = None
    used_teams: Optional[List[str]] = None
    used_team_ids: Optional[List[str]] = None

    # Fantasy specific (points come via total_points)
