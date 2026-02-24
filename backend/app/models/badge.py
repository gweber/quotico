from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# Badge definitions -- each badge has a key, name, description, icon, and criteria
BADGE_DEFINITIONS = {
    "first_bet": {
        "name": "First Bet",
        "description": "Placed your first bet",
        "icon": "\uD83C\uDF1F",
    },
    "ten_bets": {
        "name": "Regular",
        "description": "Placed 10 bets",
        "icon": "\uD83C\uDFAF",
    },
    "fifty_bets": {
        "name": "Bet Machine",
        "description": "Placed 50 bets",
        "icon": "\uD83D\uDD25",
    },
    "first_win": {
        "name": "First Win",
        "description": "Won your first bet",
        "icon": "\u2705",
    },
    "underdog_king": {
        "name": "Underdog King",
        "description": "Won a bet with odds > 4.0",
        "icon": "\uD83D\uDC51",
    },
    "hot_streak_3": {
        "name": "Hot Streak",
        "description": "Won 3 bets in a row",
        "icon": "\uD83D\uDD25",
    },
    "squad_leader": {
        "name": "Squad Leader",
        "description": "Created a squad",
        "icon": "\uD83D\uDEE1\uFE0F",
    },
    "battle_victor": {
        "name": "Battle Victor",
        "description": "Participated in a battle",
        "icon": "\u2694\uFE0F",
    },
    "century_points": {
        "name": "Triple Digits",
        "description": "Reached 100 points",
        "icon": "\uD83D\uDCAF",
    },
    "matchday_debut": {
        "name": "Matchday Debut",
        "description": "Completed your first matchday",
        "icon": "\uD83D\uDCCB",
    },
    "oracle": {
        "name": "Oracle",
        "description": "Predicted an exact score (3 points)",
        "icon": "\uD83D\uDD2E",
    },
    "perfect_matchday": {
        "name": "Perfect Matchday",
        "description": "Predicted all matches of a matchday correctly",
        "icon": "\u2B50",
    },
}


class BadgeInDB(BaseModel):
    """Badge award document in MongoDB."""
    user_id: str
    badge_key: str
    awarded_at: datetime


class BadgeResponse(BaseModel):
    """Badge data returned to the client."""
    key: str
    name: str
    description: str
    icon: str
    awarded_at: Optional[datetime] = None
