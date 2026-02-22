from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# Badge definitions — each badge has a key, name, description, icon, and criteria
BADGE_DEFINITIONS = {
    "first_tip": {
        "name": "Erster Tipp",
        "description": "Deinen ersten Tipp abgegeben",
        "icon": "\uD83C\uDF1F",
    },
    "ten_tips": {
        "name": "Stammtipper",
        "description": "10 Tipps abgegeben",
        "icon": "\uD83C\uDFAF",
    },
    "fifty_tips": {
        "name": "Tipp-Maschine",
        "description": "50 Tipps abgegeben",
        "icon": "\uD83D\uDD25",
    },
    "first_win": {
        "name": "Erster Treffer",
        "description": "Deinen ersten Tipp gewonnen",
        "icon": "\u2705",
    },
    "underdog_king": {
        "name": "Underdog King",
        "description": "Einen Tipp mit Quote > 4.0 gewonnen",
        "icon": "\uD83D\uDC51",
    },
    "hot_streak_3": {
        "name": "Hot Streak",
        "description": "3 Tipps in Folge gewonnen",
        "icon": "\uD83D\uDD25",
    },
    "squad_leader": {
        "name": "Anführer",
        "description": "Einen Squad erstellt",
        "icon": "\uD83D\uDEE1\uFE0F",
    },
    "battle_victor": {
        "name": "Schlachtenbummler",
        "description": "An einem Battle teilgenommen",
        "icon": "\u2694\uFE0F",
    },
    "century_points": {
        "name": "Triple Digits",
        "description": "100 Punkte erreicht",
        "icon": "\uD83D\uDCAF",
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
