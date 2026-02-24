"""
football-data.co.uk Historical Data Scraper for Quotico

Walks football-data.co.uk for all supported leagues, fetches CSVs across
all available seasons, parses match results + odds + stats, and pushes
them to the Quotico backend API via POST /api/historical/import.

Also builds a team_aliases payload that maps historical team names
to normalized keys for matching with live providers — ready for
match card enrichment in phase 2.

Usage:
    python tools/scrapper.py --api-url https://quotico.de --api-key YOUR_KEY
    python tools/scrapper.py --api-url http://localhost:4201 --api-key dev123
    python tools/scrapper.py --league D1 --from-season 2020
    python tools/scrapper.py --dry-run        # parse only, don't push
"""

import argparse
import logging
import sys
import time
import unicodedata
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scrapper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CSV_BASE_URL = "https://www.football-data.co.uk/mmz4281"
BATCH_SIZE = 400  # matches per API call (server limit is 500)

# football-data.co.uk CSV league code -> Quotico sport_key
LEAGUES: dict[str, dict] = {
    "D1": {
        "sport_key": "soccer_germany_bundesliga",
        "country": "Germany",
        "name": "Bundesliga",
        "first_season": 1993,
    },
    "D2": {
        "sport_key": "soccer_germany_bundesliga2",
        "country": "Germany",
        "name": "2. Bundesliga",
        "first_season": 1993,
    },
    "E0": {
        "sport_key": "soccer_epl",
        "country": "England",
        "name": "Premier League",
        "first_season": 1993,
    },
    "SP1": {
        "sport_key": "soccer_spain_la_liga",
        "country": "Spain",
        "name": "La Liga",
        "first_season": 1993,
    },
    "I1": {
        "sport_key": "soccer_italy_serie_a",
        "country": "Italy",
        "name": "Serie A",
        "first_season": 1993,
    },
    "F1": {
        "sport_key": "soccer_france_ligue_one",
        "country": "France",
        "name": "Ligue 1",
        "first_season": 1993,
    },
    "N1": {
        "sport_key": "soccer_netherlands_eredivisie",
        "country": "Netherlands",
        "name": "Eredivisie",
        "first_season": 1993,
    },
    "P1": {
        "sport_key": "soccer_portugal_primeira_liga",
        "country": "Portugal",
        "name": "Primeira Liga",
        "first_season": 1993,
    },
}

# Current season: 2025/26
CURRENT_SEASON_START = 2025


def _season_code(full_year: int) -> str:
    """Convert a full year (e.g. 2024) to football-data.co.uk season code (e.g. '2425')."""
    short_start = full_year % 100
    short_end = (full_year + 1) % 100
    return f"{short_start:02d}{short_end:02d}"


def _season_label(full_year: int) -> str:
    """Human-readable season label: '2024/25'."""
    return f"{full_year}/{(full_year + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# CSV Column Mapping
# ---------------------------------------------------------------------------

# Match statistics
STATS_COLS = {
    "HS": "shots_home",
    "AS": "shots_away",
    "HST": "shots_on_target_home",
    "AST": "shots_on_target_away",
    "HC": "corners_home",
    "AC": "corners_away",
    "HF": "fouls_home",
    "AF": "fouls_away",
    "HY": "yellow_cards_home",
    "AY": "yellow_cards_away",
    "HR": "red_cards_home",
    "AR": "red_cards_away",
}

# Bookmaker odds — organized by bookmaker for structured storage
ODDS_BOOKMAKERS = {
    "bet365":       {"home": "B365H", "draw": "B365D", "away": "B365A"},
    "bwin":         {"home": "BWH",   "draw": "BWD",   "away": "BWA"},
    "pinnacle":     {"home": "PSH",   "draw": "PSD",   "away": "PSA"},
    "william_hill": {"home": "WHH",   "draw": "WHD",   "away": "WHA"},
    "market_max":   {"home": "MaxH",  "draw": "MaxD",  "away": "MaxA"},
    "market_avg":   {"home": "AvgH",  "draw": "AvgD",  "away": "AvgA"},
    "betbrain_avg": {"home": "BbAvH", "draw": "BbAvD", "away": "BbAvA"},
    "betbrain_max": {"home": "BbMxH", "draw": "BbMxD", "away": "BbMxA"},
}

# Over/Under 2.5 goals odds
OVER_UNDER_COLS = {
    "bet365":       {"over": "B365>2.5", "under": "B365<2.5"},
    "pinnacle":     {"over": "P>2.5",    "under": "P<2.5"},
    "market_max":   {"over": "Max>2.5",  "under": "Max<2.5"},
    "market_avg":   {"over": "Avg>2.5",  "under": "Avg<2.5"},
    "betbrain_avg": {"over": "BbAv>2.5", "under": "BbAv<2.5"},
    "betbrain_max": {"over": "BbMx>2.5", "under": "BbMx<2.5"},
}


# ---------------------------------------------------------------------------
# Team Name Normalization (for matching with live providers)
# ---------------------------------------------------------------------------

def normalize_team_name(name: str) -> str:
    """Normalize team name for matching: lowercase, remove accents & noise."""
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
# CSV Parsing
# ---------------------------------------------------------------------------

def parse_csv(text: str, league_code: str, sport_key: str,
              season_code: str, season_label: str) -> list[dict]:
    """Parse a football-data.co.uk CSV into normalized match records."""
    try:
        df = pd.read_csv(StringIO(text), encoding="utf-8", on_bad_lines="skip")
    except Exception:
        try:
            df = pd.read_csv(StringIO(text), encoding="latin-1", on_bad_lines="skip")
        except Exception as e:
            log.error("Failed to parse CSV for %s %s: %s", league_code, season_code, e)
            return []

    if df.empty:
        return []

    df = df.dropna(how="all")

    records = []
    for _, row in df.iterrows():
        record = _parse_row(row, df.columns, league_code, sport_key,
                            season_code, season_label)
        if record:
            records.append(record)

    return records


def _parse_row(row, columns, league_code: str, sport_key: str,
               season_code: str, season_label: str) -> dict | None:
    """Parse a single CSV row into a match document."""
    home_team = _get_val(row, columns, ["HomeTeam", "Home"])
    away_team = _get_val(row, columns, ["AwayTeam", "Away"])
    if not home_team or not away_team:
        return None

    home_team = str(home_team).strip()
    away_team = str(away_team).strip()

    date_str = _get_val(row, columns, ["Date"])
    if not date_str or pd.isna(date_str):
        return None

    match_date = _parse_date(str(date_str))
    if not match_date:
        return None

    home_goals = _safe_int(_get_val(row, columns, ["FTHG", "HG"]))
    away_goals = _safe_int(_get_val(row, columns, ["FTAG", "AG"]))
    result = _get_val(row, columns, ["FTR", "Res"])

    if home_goals is None or away_goals is None:
        return None

    doc: dict = {
        "sport_key": sport_key,
        "league_code": league_code,
        "season": season_code,
        "season_label": season_label,
        "match_date": match_date.isoformat(),
        "home_team": home_team,
        "away_team": away_team,
        "home_team_key": team_name_key(home_team),
        "away_team_key": team_name_key(away_team),
        "home_goals": home_goals,
        "away_goals": away_goals,
        "result": str(result).strip() if result and not pd.isna(result) else None,
    }

    # Half-time
    ht_home = _safe_int(_get_val(row, columns, ["HTHG"]))
    ht_away = _safe_int(_get_val(row, columns, ["HTAG"]))
    ht_result = _get_val(row, columns, ["HTR"])
    if ht_home is not None and ht_away is not None:
        doc["ht_home_goals"] = ht_home
        doc["ht_away_goals"] = ht_away
        if ht_result and not pd.isna(ht_result):
            doc["ht_result"] = str(ht_result).strip()

    # Match statistics
    stats = {}
    for csv_col, field_name in STATS_COLS.items():
        val = _safe_int(_get_val(row, columns, [csv_col]))
        if val is not None:
            stats[field_name] = val
    if stats:
        doc["stats"] = stats

    # Bookmaker odds
    odds = {}
    for bookmaker, col_map in ODDS_BOOKMAKERS.items():
        h = _safe_float(_get_val(row, columns, [col_map["home"]]))
        d = _safe_float(_get_val(row, columns, [col_map["draw"]]))
        a = _safe_float(_get_val(row, columns, [col_map["away"]]))
        if h and d and a:
            odds[bookmaker] = {"home": h, "draw": d, "away": a}
    if odds:
        doc["odds"] = odds

    # Over/Under 2.5 odds
    ou_odds = {}
    for bookmaker, col_map in OVER_UNDER_COLS.items():
        over = _safe_float(_get_val(row, columns, [col_map["over"]]))
        under = _safe_float(_get_val(row, columns, [col_map["under"]]))
        if over and under:
            ou_odds[bookmaker] = {"over": over, "under": under, "line": 2.5}
    if ou_odds:
        doc["over_under_odds"] = ou_odds

    # Referee
    referee = _get_val(row, columns, ["Referee"])
    if referee and not pd.isna(referee):
        doc["referee"] = str(referee).strip()

    return doc


def _get_val(row, columns, candidates: list[str]):
    """Get the first available column value from a list of candidates."""
    for col in candidates:
        if col in columns:
            val = row.get(col)
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                return val
    return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return int(f)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return round(f, 3)
    except (ValueError, TypeError):
        return None


def _parse_date(date_str: str) -> datetime | None:
    """Parse dates from football-data.co.uk CSVs (dd/mm/yy or dd/mm/yyyy)."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Download + API Push
# ---------------------------------------------------------------------------

def download_csv(league_code: str, season_code: str) -> str | None:
    """Download a CSV from football-data.co.uk. Returns text or None."""
    url = f"{CSV_BASE_URL}/{season_code}/{league_code}.csv"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning("Download failed: %s — %s", url, e)
        return None


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
        description="Import historical match data from football-data.co.uk into Quotico"
    )
    parser.add_argument("--api-url", type=str, default="http://localhost:4201",
                        help="Quotico backend URL (default: http://localhost:4201)")
    parser.add_argument("--api-key", type=str, default="",
                        help="Import API key (IMPORT_API_KEY from .env)")
    parser.add_argument("--league", type=str,
                        help="Single league code (e.g. D1, D2, E0, SP1, I1)")
    parser.add_argument("--from-season", type=int, default=None,
                        help="Start year (e.g. 2020 or 20 for 2020/21)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse CSVs but don't push to API")
    args = parser.parse_args()

    if not args.dry_run and not args.api_key:
        log.error("--api-key is required (or use --dry-run)")
        sys.exit(1)

    # Filter leagues
    leagues = LEAGUES
    if args.league:
        if args.league not in LEAGUES:
            log.error("Unknown league code: %s. Available: %s",
                      args.league, list(LEAGUES.keys()))
            sys.exit(1)
        leagues = {args.league: LEAGUES[args.league]}

    # Verify API connectivity
    if not args.dry_run:
        try:
            resp = requests.get(f"{args.api_url}/health", timeout=10)
            resp.raise_for_status()
            log.info("Connected to %s", args.api_url)
        except requests.RequestException as e:
            log.error("Cannot reach backend at %s: %s", args.api_url, e)
            sys.exit(1)

    total_imported = 0
    total_seasons = 0
    total_errors = 0
    all_teams: set[tuple[str, str, str]] = set()

    for league_code, league_info in leagues.items():
        sport_key = league_info["sport_key"]
        league_name = league_info["name"]
        first_season = league_info["first_season"]

        if args.from_season is not None:
            fs = args.from_season
            if fs < 100:
                fs = fs + 1900 if fs >= 90 else fs + 2000
            first_season = fs

        log.info("=" * 60)
        log.info("League: %s (%s) — %s", league_name, league_code, sport_key)
        log.info("=" * 60)

        season_start = first_season
        while season_start <= CURRENT_SEASON_START:
            season_code = _season_code(season_start)
            season_label = _season_label(season_start)

            text = download_csv(league_code, season_code)
            if text is None:
                log.debug("  %s: no data available", season_label)
                season_start += 1
                continue

            records = parse_csv(text, league_code, sport_key,
                                season_code, season_label)
            if not records:
                log.warning("  %s: CSV downloaded but 0 records parsed", season_label)
                season_start += 1
                total_errors += 1
                continue

            # Collect team names for alias building
            for r in records:
                all_teams.add((r["sport_key"], r["home_team"], r["home_team_key"]))
                all_teams.add((r["sport_key"], r["away_team"], r["away_team_key"]))

            if args.dry_run:
                log.info("  %s: %d matches parsed (odds: %d)",
                         season_label, len(records),
                         sum(1 for r in records if r.get("odds")))
            else:
                result = push_matches(records, args.api_url, args.api_key)
                log.info("  %s: %d matches -> %d new, %d updated",
                         season_label, len(records),
                         result["upserted"], result["modified"])

            total_imported += len(records)
            total_seasons += 1
            season_start += 1

            # Be polite to the CSV server
            time.sleep(0.3)

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
    log.info("  Leagues:  %d", len(leagues))
    log.info("  Seasons:  %d", total_seasons)
    log.info("  Matches:  %d", total_imported)
    log.info("  Teams:    %d unique names", len(all_teams))
    if total_errors:
        log.info("  Errors:   %d", total_errors)
    if args.dry_run:
        log.info("  (dry run — nothing pushed)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
