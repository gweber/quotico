"""
backend/app/config_matchday.py

Purpose:
    Matchday mode configuration for v3.1 greenfield flows.
    Defines sport keys and their deterministic league mapping for matches_v3.
"""

MATCHDAY_SPORTS: dict[str, dict] = {
    "soccer_germany_bundesliga": {
        "provider": "openligadb",
        "competition_code": "bl1",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Matchday {n}",
    },
    "soccer_germany_bundesliga2": {
        "provider": "openligadb",
        "competition_code": "bl2",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Matchday {n}",
    },
    "soccer_epl": {
        "provider": "football_data",
        "competition_code": "PL",
        "matches_per_matchday": 10,
        "matchdays_per_season": 38,
        "label_template": "Matchweek {n}",
    },
    "soccer_spain_la_liga": {
        "provider": "football_data",
        "competition_code": "PD",
        "matches_per_matchday": 10,
        "matchdays_per_season": 38,
        "label_template": "Jornada {n}",
    },
    "soccer_italy_serie_a": {
        "provider": "football_data",
        "competition_code": "SA",
        "matches_per_matchday": 10,
        "matchdays_per_season": 38,
        "label_template": "Giornata {n}",
    },
    "soccer_france_ligue_one": {
        "provider": "football_data",
        "competition_code": "FL1",
        "matches_per_matchday": 10,
        "matchdays_per_season": 38,
        "label_template": "Journée {n}",
    },
    "soccer_netherlands_eredivisie": {
        "provider": "football_data",
        "competition_code": "DED",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Speelronde {n}",
    },
    "soccer_portugal_primeira_liga": {
        "provider": "football_data",
        "competition_code": "PPL",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Jornada {n}",
    },
}

# v3.1 hard-cut mapping: deterministic mapping for matches_v3 aggregation.
# Operators can tune these IDs as league_registry_v3 evolves per environment.
MATCHDAY_V3_SPORTS: dict[str, dict] = {
    "soccer_germany_bundesliga": {
        "league_ids": [82],
        "label_template": "Matchday {n}",
        "matchdays_per_season": 34,
        "enabled": True,
    },
    "soccer_germany_bundesliga2": {
        "league_ids": [390],
        "label_template": "Matchday {n}",
        "matchdays_per_season": 34,
        "enabled": True,
    },
    "soccer_epl": {
        "league_ids": [8],
        "label_template": "Matchweek {n}",
        "matchdays_per_season": 38,
        "enabled": True,
    },
    "soccer_spain_la_liga": {
        "league_ids": [564],
        "label_template": "Jornada {n}",
        "matchdays_per_season": 38,
        "enabled": True,
    },
    "soccer_italy_serie_a": {
        "league_ids": [384],
        "label_template": "Giornata {n}",
        "matchdays_per_season": 38,
        "enabled": True,
    },
    "soccer_france_ligue_one": {
        "league_ids": [301],
        "label_template": "Journee {n}",
        "matchdays_per_season": 34,
        "enabled": True,
    },
}
