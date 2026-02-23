"""Game Mode configuration for Squad-based competition modes."""

from enum import Enum


class GameMode(str, Enum):
    classic = "classic"
    bankroll = "bankroll"
    survivor = "survivor"
    over_under = "over_under"
    fantasy = "fantasy"
    moneyline = "moneyline"


GAME_MODE_DEFAULTS: dict[str, dict] = {
    "classic": {},
    "bankroll": {
        "initial_balance": 1000,
        "min_bet": 10,
        "max_bet_pct": 50,
    },
    "survivor": {
        "draw_eliminates": True,
    },
    "over_under": {
        "default_line": 2.5,
    },
    "fantasy": {
        "scoring": "goals",
        "pure_stats_only": True,
    },
    "moneyline": {},
}

GAME_MODE_LABELS: dict[str, str] = {
    "classic": "Tippspiel",
    "bankroll": "Bankroll",
    "survivor": "Survivor",
    "over_under": "Über/Unter",
    "fantasy": "Fantasy",
    "moneyline": "Quotentipp",
}
