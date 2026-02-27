"""Virtual Wallet models — balance tracking, transactions, bets."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


# ---------- Wallet ----------

class WalletStatus(str, Enum):
    active = "active"
    bankrupt = "bankrupt"
    frozen = "frozen"


class WalletInDB(BaseModel):
    """Per-user per-squad per-season wallet."""
    user_id: str
    squad_id: str
    league_id: int
    season: int
    balance: float = 1000.0
    initial_balance: float = 1000.0
    total_wagered: float = 0.0
    total_won: float = 0.0
    status: WalletStatus = WalletStatus.active
    bankrupt_since: Optional[datetime] = None
    consecutive_bonus_days: int = 0
    last_daily_bonus_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class WalletResponse(BaseModel):
    """Wallet data returned to the client."""
    id: str
    squad_id: str
    league_id: int
    season: int
    balance: float
    initial_balance: float
    total_wagered: float
    total_won: float
    status: str
    bankrupt_since: Optional[datetime] = None
    consecutive_bonus_days: int = 0


# ---------- Wallet Transactions ----------

class TransactionType(str, Enum):
    INITIAL_CREDIT = "INITIAL_CREDIT"
    BET_PLACED = "BET_PLACED"
    BET_WON = "BET_WON"
    BET_LOST = "BET_LOST"
    BET_VOID = "BET_VOID"
    DAILY_BONUS = "DAILY_BONUS"
    PARLAY_PLACED = "PARLAY_PLACED"
    PARLAY_WON = "PARLAY_WON"
    PARLAY_LOST = "PARLAY_LOST"


class WalletTransactionInDB(BaseModel):
    """Immutable audit trail for every coin movement."""
    wallet_id: str
    user_id: str
    squad_id: str
    type: TransactionType
    amount: float  # positive = credit, negative = debit
    balance_after: float
    reference_type: Optional[str] = None  # "bankroll_bet" | "parlay" | None
    reference_id: Optional[str] = None
    description: str
    created_at: datetime


class TransactionResponse(BaseModel):
    """Transaction data returned to the client."""
    id: str
    type: str
    amount: float
    balance_after: float
    description: str
    created_at: datetime


# ---------- Bankroll Bets ----------

class BetStatus(str, Enum):
    pending = "pending"
    won = "won"
    lost = "lost"
    void = "void"


class BankrollBetInDB(BaseModel):
    """A single bankroll bet on a match outcome."""
    user_id: str
    squad_id: str
    wallet_id: str
    match_id: str
    matchday_id: str
    prediction: str  # "1", "X", "2"
    stake: float
    locked_odds: float
    potential_win: float  # stake * locked_odds
    points_earned: Optional[float] = None
    status: BetStatus = BetStatus.pending
    resolved_at: Optional[datetime] = None
    created_at: datetime


class BankrollBetCreate(BaseModel):
    """Request body for placing a bankroll bet."""
    match_id: str
    prediction: str  # "1", "X", "2"
    stake: float
    displayed_odds: float  # for anti-manipulation check


class BankrollBetResponse(BaseModel):
    """Bet data returned to the client."""
    id: str
    match_id: str
    prediction: str
    stake: float
    locked_odds: float
    potential_win: float
    status: str
    points_earned: Optional[float] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime


# ---------- Over/Under Bets ----------

class OverUnderBetInDB(BaseModel):
    """A bet on total goals over/under a line."""
    user_id: str
    squad_id: str
    wallet_id: Optional[str] = None  # only in bankroll combo
    match_id: str
    matchday_id: str
    prediction: str  # "over" | "under"
    line: float  # e.g. 2.5
    locked_odds: float
    stake: Optional[float] = None  # None in classic mode
    status: BetStatus = BetStatus.pending
    points_earned: Optional[float] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime


class OverUnderBetCreate(BaseModel):
    """Request body for placing an over/under bet."""
    match_id: str
    prediction: str  # "over" | "under"
    stake: Optional[float] = None
    displayed_odds: float


class OverUnderBetResponse(BaseModel):
    id: str
    match_id: str
    prediction: str
    line: float
    locked_odds: float
    stake: Optional[float] = None
    status: str
    points_earned: Optional[float] = None
    created_at: datetime


# ---------- Fantasy Picks ----------

class FantasyPickInDB(BaseModel):
    """A fantasy team pick for a matchday."""
    user_id: str
    squad_id: str
    league_id: int
    season: int
    matchday_number: int
    team: str
    match_id: str
    goals_scored: Optional[int] = None
    goals_conceded: Optional[int] = None
    match_result: Optional[str] = None  # "won" | "draw" | "lost"
    fantasy_points: Optional[int] = None
    status: str = "pending"  # pending | resolved
    created_at: datetime
    updated_at: datetime


class FantasyPickCreate(BaseModel):
    """Request body for making a fantasy pick."""
    match_id: str
    team: str  # team name (home or away)


class FantasyPickResponse(BaseModel):
    id: str
    team: str
    match_id: str
    goals_scored: Optional[int] = None
    goals_conceded: Optional[int] = None
    fantasy_points: Optional[int] = None
    status: str
    created_at: datetime


# ---------- Parlays ----------

class ParlayLeg(BaseModel):
    match_id: str
    prediction: str  # "1", "X", "2", "over", "under"
    locked_odds: float
    result: str = "pending"  # pending | won | lost | void


class ParlayInDB(BaseModel):
    """A combination bet (3 legs, all must win)."""
    user_id: str
    squad_id: str
    matchday_id: str
    league_id: int
    season: int
    matchday_number: int
    legs: list[ParlayLeg]
    combined_odds: float
    stake: Optional[float] = None  # None in classic mode
    potential_win: float
    status: BetStatus = BetStatus.pending
    points_earned: Optional[float] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime


class ParlayCreate(BaseModel):
    """Request body for creating a parlay."""
    legs: list[dict]  # [{match_id, prediction, displayed_odds}]
    stake: Optional[float] = None


class ParlayResponse(BaseModel):
    id: str
    legs: list[dict]
    combined_odds: float
    stake: Optional[float] = None
    potential_win: float
    status: str
    points_earned: Optional[float] = None
    created_at: datetime


# ---------- Device Fingerprints ----------

class DeviceFingerprintInDB(BaseModel):
    """DSGVO-compliant: only hash stored, no raw components.  IP truncated."""
    user_id: str
    fingerprint_hash: str
    ip_truncated: str  # last octet replaced with "xxx" (DSGVO)
    created_at: datetime
    last_seen_at: datetime
