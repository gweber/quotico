"""
ESPN NFL Historical Data Backfiller for Quotico

Fetches historical NFL game results from ESPN's public scoreboard API
and pushes them to the Quotico backend via POST /api/historical/import.

Uses ESPN's calendar to discover week boundaries, then fetches each week's
games with a date range query. Follows the same patterns as tools/scrapper.py.

Usage:
    python tools/nfl_history_backfiller.py --api-url http://localhost:4201 --api-key dev123 --start-year 2024
    python tools/nfl_history_backfiller.py --start-year 2020 --end-year 2024 --dry-run
    python tools/nfl_history_backfiller.py --start-year 2002 --end-year 2024 --api-key YOUR_KEY
"""

import argparse
import logging
import sys
import time
import unicodedata
from datetime import datetime

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nfl_backfiller")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SPORT_KEY = "americanfootball_nfl"
LEAGUE_CODE = "NFL"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
BATCH_SIZE = 400  # matches per API call (server limit is 500)
DEFAULT_START_YEAR = 2002  # ESPN data goes back to at least 2002
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubles each attempt
SLEEP_BETWEEN_WEEKS = 1  # seconds between ESPN requests


# ---------------------------------------------------------------------------
# Season helpers (same as scrapper.py)
# ---------------------------------------------------------------------------

def _season_code(full_year: int) -> str:
    """Convert a full year (e.g. 2024) to season code (e.g. '2425')."""
    short_start = full_year % 100
    short_end = (full_year + 1) % 100
    return f"{short_start:02d}{short_end:02d}"


def _season_label(full_year: int) -> str:
    """Human-readable season label: '2024/25'."""
    return f"{full_year}/{(full_year + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# Team name normalization (same as scrapper.py)
# ---------------------------------------------------------------------------

def normalize_team_name(name: str) -> str:
    """Normalize team name: remove accents & combining chars."""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return name.strip()


def team_name_key(name: str) -> str:
    """Create a fuzzy-match key from a team name."""
    name = normalize_team_name(name).lower()
    noise = {"fc", "cf", "sc", "ac", "as", "ss", "us", "afc", "rcd", "1.",
             "club", "de", "sv", "vfb", "vfl", "tsg", "fsv", "bsc", "fk"}
    tokens = sorted(t for t in name.split() if t not in noise and len(t) >= 3)
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# ESPN fetching
# ---------------------------------------------------------------------------

def _espn_get(params: dict, retries: int = MAX_RETRIES) -> dict:
    """GET ESPN scoreboard with retry + backoff."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(ESPN_BASE, params=params, timeout=15)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("ESPN %d, retrying in %ds...", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt < retries:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("ESPN error: %s, retrying in %ds...", e, wait)
                time.sleep(wait)
            else:
                log.error("ESPN request failed after %d retries: %s", retries, e)
                raise
    return {}


def discover_season_weeks(year: int, include_preseason: bool = False) -> list[dict]:
    """Discover all weeks for an NFL season from ESPN's calendar.

    Returns list of {"label": str, "season_type": int, "start": str, "end": str}
    where start/end are YYYYMMDD strings.
    """
    # Hit ESPN with a September date to get the full season calendar
    data = _espn_get({"dates": f"{year}0901"})

    calendar = []
    for league in data.get("leagues", []):
        for section in league.get("calendar", []):
            if not isinstance(section, dict):
                continue

            label = section.get("label", "")
            # season_type: Preseason=1, Regular Season=2, Postseason=3
            if "preseason" in label.lower():
                season_type = 1
            elif "postseason" in label.lower():
                season_type = 3
            else:
                season_type = 2

            for entry in section.get("entries", []):
                week_label = entry.get("label", "")

                # Skip preseason unless requested
                if season_type == 1 and not include_preseason:
                    continue

                # Skip Pro Bowl and Off Season
                if "pro bowl" in week_label.lower() or "off season" in label.lower():
                    continue

                start_date = entry.get("startDate", "")
                end_date = entry.get("endDate", "")
                if not start_date or not end_date:
                    continue

                # Parse ISO dates to YYYYMMDD
                start_yyyymmdd = start_date[:10].replace("-", "")
                end_yyyymmdd = end_date[:10].replace("-", "")

                calendar.append({
                    "label": week_label,
                    "season_type": season_type,
                    "start": start_yyyymmdd,
                    "end": end_yyyymmdd,
                })

    return calendar


def fetch_week_events(start_date: str, end_date: str) -> list[dict]:
    """Fetch all events for a week using date range query."""
    data = _espn_get({"dates": f"{start_date}-{end_date}", "limit": "1000"})
    return data.get("events", [])


# ---------------------------------------------------------------------------
# Event -> HistoricalMatch transformation
# ---------------------------------------------------------------------------

def event_to_record(event: dict, season_year: int) -> dict | None:
    """Convert an ESPN event to a HistoricalMatch record."""
    competitions = event.get("competitions", [])
    if not competitions:
        return None

    comp = competitions[0]

    # Only completed games
    status_type = comp.get("status", {}).get("type", {})
    if not status_type.get("completed"):
        return None

    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    # Identify home and away
    home = None
    away = None
    for c in competitors:
        if c.get("homeAway") == "home":
            home = c
        else:
            away = c

    if not home or not away:
        home, away = competitors[0], competitors[1]

    home_name = home.get("team", {}).get("displayName", "")
    away_name = away.get("team", {}).get("displayName", "")
    if not home_name or not away_name:
        return None

    home_score = int(home.get("score", "0"))
    away_score = int(away.get("score", "0"))

    # Result: 1=home win, 2=away win, X=tie
    if home_score > away_score:
        result = "1"
    elif away_score > home_score:
        result = "2"
    else:
        result = "X"

    # Parse match date
    match_date = event.get("date", "")
    if not match_date:
        return None
    # Ensure ISO format
    try:
        dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        match_date_iso = dt.isoformat()
    except ValueError:
        match_date_iso = match_date

    return {
        "sport_key": SPORT_KEY,
        "league_code": LEAGUE_CODE,
        "season": _season_code(season_year),
        "season_label": _season_label(season_year),
        "match_date": match_date_iso,
        "home_team": home_name,
        "away_team": away_name,
        "home_team_key": team_name_key(home_name),
        "away_team_key": team_name_key(away_name),
        "home_goals": home_score,
        "away_goals": away_score,
        "result": result,
    }


# ---------------------------------------------------------------------------
# API push (same as scrapper.py)
# ---------------------------------------------------------------------------

def push_matches(records: list[dict], api_url: str, api_key: str) -> dict:
    """Push a batch of matches to the backend API. Returns result summary."""
    totals = {"received": 0, "upserted": 0, "modified": 0}

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        try:
            resp = requests.post(
                f"{api_url}/api/historical/import",
                json={"matches": batch},
                headers={"X-Import-Key": api_key},
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            totals["received"] += result.get("received", 0)
            totals["upserted"] += result.get("upserted", 0)
            totals["modified"] += result.get("modified", 0)
        except requests.RequestException as e:
            log.error("API push failed for batch %d-%d: %s", i, i + len(batch), e)
            if hasattr(e, "response") and e.response is not None:
                log.error("  Response: %s", e.response.text[:500])

    return totals


def push_aliases(all_teams: set[tuple[str, str, str]], api_url: str, api_key: str) -> int:
    """Push team aliases to the backend API. Returns count."""
    aliases = [
        {"sport_key": sk, "team_name": tn, "team_key": tk}
        for sk, tn, tk in all_teams
        if tn and tk
    ]

    total = 0
    for i in range(0, len(aliases), 2000):
        batch = aliases[i:i + 2000]
        try:
            resp = requests.post(
                f"{api_url}/api/historical/aliases",
                json={"aliases": batch},
                headers={"X-Import-Key": api_key},
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            total += result.get("upserted", 0) + result.get("modified", 0)
        except requests.RequestException as e:
            log.error("Alias push failed: %s", e)

    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Import historical NFL data from ESPN into Quotico"
    )
    parser.add_argument("--api-url", type=str, default="http://localhost:4201",
                        help="Quotico backend URL (default: http://localhost:4201)")
    parser.add_argument("--api-key", type=str, default="",
                        help="Import API key (IMPORT_API_KEY from .env)")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR,
                        help=f"First NFL season year (default: {DEFAULT_START_YEAR})")
    parser.add_argument("--end-year", type=int, default=None,
                        help="Last NFL season year (default: current year - 1)")
    parser.add_argument("--include-preseason", action="store_true",
                        help="Include preseason games (skipped by default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse only, don't push to API")
    args = parser.parse_args()

    if args.end_year is None:
        args.end_year = datetime.now().year - 1

    if not args.dry_run and not args.api_key:
        log.error("--api-key is required (or use --dry-run)")
        sys.exit(1)

    # Verify API connectivity
    if not args.dry_run:
        try:
            resp = requests.get(f"{args.api_url}/health", timeout=10)
            resp.raise_for_status()
            log.info("Connected to %s", args.api_url)
        except requests.RequestException as e:
            log.error("Cannot reach backend at %s: %s", args.api_url, e)
            sys.exit(1)

    total_matches = 0
    total_weeks = 0
    total_errors = 0
    all_teams: set[tuple[str, str, str]] = set()

    log.info("=" * 60)
    log.info("NFL Historical Backfill — %s", SPORT_KEY)
    log.info("Seasons: %d to %d", args.start_year, args.end_year)
    log.info("=" * 60)

    for year in range(args.start_year, args.end_year + 1):
        label = _season_label(year)

        # Discover week boundaries
        try:
            weeks = discover_season_weeks(year, args.include_preseason)
        except Exception as e:
            log.error("%s: Failed to discover schedule: %s", label, e)
            total_errors += 1
            continue

        if not weeks:
            log.warning("%s: No weeks found in calendar", label)
            total_errors += 1
            continue

        log.info("%s: %d weeks to fetch", label, len(weeks))

        season_records = []
        for week in weeks:
            try:
                events = fetch_week_events(week["start"], week["end"])
            except Exception as e:
                log.error("  %s: fetch failed: %s", week["label"], e)
                total_errors += 1
                continue

            week_records = []
            for event in events:
                record = event_to_record(event, year)
                if record:
                    week_records.append(record)
                    all_teams.add((SPORT_KEY, record["home_team"], record["home_team_key"]))
                    all_teams.add((SPORT_KEY, record["away_team"], record["away_team_key"]))

            if week_records:
                log.info("  %s: %d games", week["label"], len(week_records))
            season_records.extend(week_records)
            total_weeks += 1

            time.sleep(SLEEP_BETWEEN_WEEKS)

        if not season_records:
            log.warning("%s: 0 completed games found", label)
            continue

        if args.dry_run:
            log.info("%s: %d matches parsed (dry run)", label, len(season_records))
        else:
            result = push_matches(season_records, args.api_url, args.api_key)
            log.info("%s: %d matches -> %d new, %d updated",
                     label, len(season_records),
                     result["upserted"], result["modified"])

        total_matches += len(season_records)

    # Push team aliases
    if not args.dry_run and all_teams:
        log.info("")
        log.info("Pushing team aliases...")
        alias_count = push_aliases(all_teams, args.api_url, args.api_key)
        log.info("Team aliases: %d entries synced", alias_count)

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("DONE")
    log.info("  Seasons:  %d", args.end_year - args.start_year + 1)
    log.info("  Weeks:    %d", total_weeks)
    log.info("  Matches:  %d", total_matches)
    log.info("  Teams:    %d unique names", len(all_teams))
    if total_errors:
        log.info("  Errors:   %d", total_errors)
    if args.dry_run:
        log.info("  (dry run — nothing pushed)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
