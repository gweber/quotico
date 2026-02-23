from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BattleInDB(BaseModel):
    """Squad Battle document as stored in MongoDB."""
    squad_a_id: str
    squad_b_id: Optional[str] = None  # None for open challenges
    challenge_type: str = "classic"  # "classic" | "open" | "direct"
    start_time: datetime  # When the first match of the battle starts
    end_time: datetime  # When the last match of the battle ends
    status: str = "upcoming"  # open, pending, upcoming, active, finished, declined, expired
    result: Optional[dict] = None  # Set when finished: {winner, squad_a_avg, squad_b_avg}
    created_at: datetime
    updated_at: datetime


class BattleParticipation(BaseModel):
    """User's commitment to a squad for a specific battle."""
    battle_id: str
    user_id: str
    squad_id: str  # Which squad they're fighting for
    joined_at: datetime


class BattleCreate(BaseModel):
    """Request body for creating a battle (classic — both squads known)."""
    squad_a_id: str
    squad_b_id: str
    start_time: datetime
    end_time: datetime


class ChallengeCreate(BaseModel):
    """Request body for creating a challenge (open or direct)."""
    squad_id: str  # Challenger's squad
    start_time: datetime
    end_time: datetime
    target_squad_id: Optional[str] = None  # None = open challenge, set = direct


class ChallengeAccept(BaseModel):
    """Request body for accepting a challenge."""
    squad_id: str  # Accepting squad's ID


class BattleCommit(BaseModel):
    """Request body for committing to a side in a battle."""
    squad_id: str  # Which squad to fight for


class BattleResponse(BaseModel):
    """Battle data returned to the client."""
    id: str
    squad_a: dict  # {id, name, member_count, avg_points}
    squad_b: Optional[dict] = None  # None for unaccepted open challenges
    challenge_type: str = "classic"
    start_time: datetime
    end_time: datetime
    status: str
    my_commitment: Optional[str] = None  # squad_id the user committed to
    result: Optional[dict] = None
