"""
backend/app/services/match_ingest_adapters/openligadb_adapter.py

Purpose:
    Adapter that transforms OpenLigaDB season fixtures into unified MatchData
    records for centralized ingest.

Dependencies:
    - app.providers.openligadb
    - app.services.match_ingest_types
    - app.utils
"""

from __future__ import annotations

from app.providers.openligadb import openligadb_provider
from app.services.match_ingest_types import MatchData
from app.utils import ensure_utc, parse_utc


def _season_start_year_from_kickoff(kickoff) -> int:
    dt = ensure_utc(kickoff)
    return dt.year if dt.month >= 7 else dt.year - 1


async def build_openligadb_matches(
    *,
    league_shortcut: str,
    season_year: int,
    sport_key: str,
) -> tuple[list[MatchData], int]:
    rows = await openligadb_provider.get_season_matches(league_shortcut, int(season_year))
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

        home_score = row.get("home_score")
        away_score = row.get("away_score")
        status_raw = "finished" if bool(row.get("is_finished")) else "scheduled"

        payload: MatchData = {
            "external_id": str(row.get("match_id") or ""),
            "source": "openligadb",
            "league_external_id": str(league_shortcut),
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
            "status": status_raw,
            "matchday": int(row["matchday"]) if row.get("matchday") is not None else None,
            "score": {
                "full_time": {"home": home_score, "away": away_score},
                "half_time": {"home": row.get("half_time_home"), "away": row.get("half_time_away")},
            },
            "metadata": {
                "is_finished": bool(row.get("is_finished")),
            },
        }
        if payload["external_id"]:
            out.append(payload)

    return out, skipped_season_mismatch
