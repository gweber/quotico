"""
backend/app/services/match_ingest_adapters/football_data_org_adapter.py

Purpose:
    Adapter that transforms football-data.org season fixtures into unified
    MatchData records for centralized ingest.

Dependencies:
    - app.providers.football_data
    - app.services.match_ingest_types
    - app.utils
"""

from __future__ import annotations

from typing import Any

from app.providers.football_data import football_data_provider
from app.services.match_ingest_types import MatchData
from app.utils import ensure_utc, parse_utc


def _season_start_year_from_kickoff(kickoff) -> int:
    dt = ensure_utc(kickoff)
    return dt.year if dt.month >= 7 else dt.year - 1


async def build_football_data_org_matches(
    *,
    competition: str,
    season_year: int,
    sport_key: str,
) -> tuple[list[MatchData], int]:
    rows = await football_data_provider.get_season_matches(competition, int(season_year))
    out: list[MatchData] = []
    skipped_season_mismatch = 0

    for row in rows:
        kickoff_raw = row.get("utc_date")
        if not kickoff_raw:
            continue
        try:
            kickoff = parse_utc(kickoff_raw)
        except Exception:
            continue

        if _season_start_year_from_kickoff(kickoff) != int(season_year):
            skipped_season_mismatch += 1
            continue

        score = row.get("score") if isinstance(row.get("score"), dict) else {}
        payload: MatchData = {
            "external_id": str(row.get("match_id") or ""),
            "source": "football_data",
            "league_external_id": str(competition),
            "season": int(season_year),
            "sport_key": sport_key,
            "match_date": ensure_utc(kickoff),
            "home_team": {
                "external_id": str(row.get("home_team_id") or "").strip() or None,
                "name": str(row.get("home_team_name") or "").strip(),
            },
            "away_team": {
                "external_id": str(row.get("away_team_id") or "").strip() or None,
                "name": str(row.get("away_team_name") or "").strip(),
            },
            "status": str(row.get("status_raw") or "").strip() or None,
            "matchday": int(row["matchday"]) if row.get("matchday") is not None else None,
            "score": {
                "full_time": (score.get("full_time") if isinstance(score.get("full_time"), dict) else {"home": None, "away": None}),
                "half_time": score.get("half_time"),
                "extra_time": score.get("extra_time"),
                "penalties": score.get("penalties"),
            },
            "metadata": {
                "group": row.get("group"),
                "stage": row.get("stage"),
                "status_raw": row.get("status_raw"),
            },
        }
        if payload["external_id"]:
            out.append(payload)

    return out, skipped_season_mismatch
