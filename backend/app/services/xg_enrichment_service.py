"""
backend/app/services/xg_enrichment_service.py

Purpose:
    Fetch match-level expected goals data from Understat and enrich finalized
    matches, guarded by League Tower feature flags.

Dependencies:
    - app.services.league_service
    - app.services.team_mapping_service
    - app.database
"""

import logging
from datetime import timedelta

import app.database as _db
from app.services.league_service import LeagueRegistry, league_feature_enabled
from app.services.team_mapping_service import resolve_team
from app.utils import parse_utc

try:
    import soccerdata as sd
except ImportError:
    sd = None  # type: ignore[assignment]

logger = logging.getLogger("quotico.xg_enrichment")

# Match date tolerance for linking Understat → Quotico matches
MATCH_DATE_WINDOW_HOURS = 24


def _season_str(year: int) -> str:
    """Convert season start year to soccerdata format: '2024/2025'."""
    return f"{year}/{year + 1}"


def fetch_season_xg(sport_key: str, season_year: int, understat_league_id: str):
    """Fetch match-level xG from Understat for a league+season.

    Returns a pandas DataFrame with columns including:
    home_team, away_team, date, home_xg, away_xg, home_goals, away_goals.

    Raises RuntimeError if soccerdata is not installed.
    """
    if sd is None:
        raise RuntimeError(
            "soccerdata is not installed. Run: pip install soccerdata"
        )

    if not understat_league_id:
        raise ValueError(
            f"Sport key {sport_key!r} has no Understat mapping in leagues.external_ids.understat"
        )

    logger.info("Fetching xG from Understat: %s season %s", understat_league_id, _season_str(season_year))
    understat = sd.Understat(leagues=understat_league_id, seasons=season_year, no_cache=False)
    schedule = understat.read_schedule()

    # Reset the multi-index (league, season, game) to flat columns
    df = schedule.reset_index()
    logger.info("Fetched %d matches from Understat", len(df))
    return df


async def match_and_enrich(
    sport_key: str,
    season_year: int,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Fetch xG data and update matching MongoDB match documents.

    Args:
        sport_key: Quotico sport key (e.g. 'soccer_epl').
        season_year: Season start year (e.g. 2024 for 2024/25).
        dry_run: If True, only count matches without writing.
        force: If True, overwrite existing xG data.

    Returns:
        Summary dict with matched/unmatched/skipped/total counts.
    """
    league_registry = LeagueRegistry.get()
    league = await league_registry.ensure_for_import(
        sport_key,
        provider_name="understat",
        provider_id=sport_key,
        auto_create_inactive=True,
    )
    if not league.get("is_active", False):
        raise ValueError(f"xG import blocked for inactive league: {sport_key}")
    if not league_feature_enabled(league, "xg_sync", False):
        raise ValueError(f"xG import blocked for disabled xg_sync feature: {sport_key}")
    understat_league_id = (league.get("external_ids") or {}).get("understat")
    if not understat_league_id:
        understat_league_id = (league.get("provider_mappings") or {}).get("understat")
    if not understat_league_id:
        raise ValueError(
            f"xG import blocked: {sport_key} has no understat provider mapping in leagues collection."
        )

    df = fetch_season_xg(sport_key, season_year, understat_league_id)

    matched = 0
    unmatched = 0
    skipped = 0
    already_enriched = 0
    unmatched_teams: set[str] = set()

    for _, row in df.iterrows():
        # Only process completed matches (with actual xG data)
        home_xg = row.get("home_xg")
        away_xg = row.get("away_xg")
        if home_xg is None or away_xg is None:
            skipped += 1
            continue

        # Skip if xG values are NaN (pandas)
        try:
            home_xg = float(home_xg)
            away_xg = float(away_xg)
        except (TypeError, ValueError):
            skipped += 1
            continue

        # Resolve team names to our canonical team_keys
        home_name = str(row.get("home_team", ""))
        away_name = str(row.get("away_team", ""))

        home_resolved = await resolve_team(home_name, sport_key)
        if not home_resolved:
            unmatched += 1
            unmatched_teams.add(home_name)
            continue
        _, _, home_key = home_resolved

        away_resolved = await resolve_team(away_name, sport_key)
        if not away_resolved:
            unmatched += 1
            unmatched_teams.add(away_name)
            continue
        _, _, away_key = away_resolved

        # Parse match date from Understat
        match_date_raw = row.get("date")
        if match_date_raw is None:
            skipped += 1
            continue
        match_date = parse_utc(match_date_raw)

        # Find corresponding match in our database (±24h window)
        query = {
            "sport_key": sport_key,
            "home_team_key": home_key,
            "away_team_key": away_key,
            "status": "final",
            "match_date": {
                "$gte": match_date - timedelta(hours=MATCH_DATE_WINDOW_HOURS),
                "$lte": match_date + timedelta(hours=MATCH_DATE_WINDOW_HOURS),
            },
        }

        if not force:
            query["result.home_xg"] = {"$exists": False}

        db_match = await _db.db.matches.find_one(query, {"_id": 1, "result.home_xg": 1})

        if not db_match:
            # Check if already enriched (when not forcing)
            if not force:
                exists_query = {
                    "sport_key": sport_key,
                    "home_team_key": home_key,
                    "away_team_key": away_key,
                    "status": "final",
                    "match_date": {
                        "$gte": match_date - timedelta(hours=MATCH_DATE_WINDOW_HOURS),
                        "$lte": match_date + timedelta(hours=MATCH_DATE_WINDOW_HOURS),
                    },
                    "result.home_xg": {"$exists": True},
                }
                if await _db.db.matches.find_one(exists_query, {"_id": 1}):
                    already_enriched += 1
                    continue

            unmatched += 1
            continue

        if not dry_run:
            await _db.db.matches.update_one(
                {"_id": db_match["_id"]},
                {"$set": {
                    "result.home_xg": round(home_xg, 2),
                    "result.away_xg": round(away_xg, 2),
                    "result.xg_provider": "understat",
                }},
            )

        matched += 1

    total = matched + unmatched + skipped + already_enriched

    return {
        "matched": matched,
        "unmatched": unmatched,
        "skipped": skipped,
        "already_enriched": already_enriched,
        "total": total,
        "unmatched_teams": sorted(unmatched_teams),
    }
