"""
backend/app/services/match_ingest_adapters/football_data_uk_adapter.py

Purpose:
    Adapter helpers to transform football-data.co.uk CSV rows into unified
    MatchData payloads for centralized ingest.

Dependencies:
    - hashlib
    - app.services.match_ingest_types
    - app.utils
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services.match_ingest_types import MatchData
from app.utils import ensure_utc


def build_football_data_uk_external_id(
    *,
    sport_key: str,
    league_external_id: str,
    home_team: str,
    away_team: str,
    match_date: datetime,
) -> str:
    payload = "|".join(
        [
            str(sport_key).strip().lower(),
            str(league_external_id).strip().lower(),
            str(home_team).strip().lower(),
            str(away_team).strip().lower(),
            ensure_utc(match_date).isoformat(),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


def build_football_data_uk_match_data(
    *,
    sport_key: str,
    league_external_id: str,
    season_start_year: int,
    match_date: datetime,
    home_team: str,
    away_team: str,
    status_raw: str,
    score: dict[str, Any],
    matchday: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> MatchData:
    return {
        "external_id": build_football_data_uk_external_id(
            sport_key=sport_key,
            league_external_id=league_external_id,
            home_team=home_team,
            away_team=away_team,
            match_date=match_date,
        ),
        "source": "football_data_uk",
        "league_external_id": str(league_external_id),
        "season": int(season_start_year),
        "sport_key": str(sport_key),
        "match_date": ensure_utc(match_date),
        "home_team": {"external_id": None, "name": str(home_team).strip()},
        "away_team": {"external_id": None, "name": str(away_team).strip()},
        "status": str(status_raw).strip() or None,
        "matchday": matchday,
        "score": score,
        "metadata": metadata or {},
    }
