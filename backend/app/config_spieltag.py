"""Spieltag-Modus configuration per league."""

SPIELTAG_SPORTS: dict[str, dict] = {
    "soccer_germany_bundesliga": {
        "provider": "openligadb",
        "competition_code": "bl1",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Spieltag {n}",
    },
    "soccer_germany_bundesliga2": {
        "provider": "openligadb",
        "competition_code": "bl2",
        "matches_per_matchday": 9,
        "matchdays_per_season": 34,
        "label_template": "Spieltag {n}",
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
    "soccer_uefa_champs_league": {
        "provider": "football_data",
        "competition_code": "CL",
        "matches_per_matchday": 8,
        "matchdays_per_season": 8,
        "label_template": "Matchday {n}",
    },
}
