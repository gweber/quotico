"""
backend/app/services/football_data_service.py

Purpose:
    Core import service for football-data.co.uk match statistics. Resolves teams
    via Team Tower, matches existing fixtures, upserts stats into matches, and
    imports bookmaker odds snapshots into the Greenfield odds pipeline.

Dependencies:
    - app.providers.football_data_uk
    - app.services.league_service
    - app.services.team_registry_service
    - app.services.odds_service
    - app.database
    - app.utils.utcnow
"""

from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Awaitable, Callable

import httpx
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.providers.football_data_uk import football_data_uk_provider
from app.services.league_service import LeagueRegistry
from app.services.match_ingest_adapters.football_data_uk_adapter import build_football_data_uk_match_data
from app.services.match_ingest_service import match_ingest_service
from app.services.odds_service import odds_service
from app.services.team_registry_service import TeamRegistry  # kept for module-level patching in tests
from app.utils import ensure_utc, utcnow

logger = logging.getLogger(__name__)

STATS_COLUMN_MAP = {
    "HC": "corners_home",
    "AC": "corners_away",
    "HS": "shots_home",
    "AS": "shots_away",
    "HF": "fouls_home",
    "AF": "fouls_away",
    "HY": "cards_yellow_home",
    "AY": "cards_yellow_away",
    "HR": "cards_red_home",
    "AR": "cards_red_away",
}

BOOKMAKER_PREFIXES = [
    "INTERWETTEN",
    "PINNACLE",
    "BETFAIR",
    "SBET",
    "SBO",
    "SYN",
    "SJK",
    "CSI",
    "PSC",
    "B365",
    "WH",
    "VC",
    "PS",
    "SB",
    "BW",
    "IW",
    "LB",
    "SM",
    "YSB",
    "BY",
    "CHI",
]

BOOKMAKER_PROVIDER_MAP = {
    "B365": "bet365",
    "WH": "william_hill",
    "VC": "betvictor",
    "PS": "pinnacle",
    "PSC": "pinnacle",
    "PINNACLE": "pinnacle",
    "SB": "sportsbet",
    "SBO": "sportsbet",
    "BW": "betway",
    "IW": "interwetten",
    "INTERWETTEN": "interwetten",
    "LB": "ladbrokes",
    "SM": "smarkets",
    "YSB": "youwin",
    "BY": "bet365",
    "CHI": "chilipari",
    "BETFAIR": "betfair",
    "CSI": "csi",
    "SJK": "sjk",
    "SYN": "synot",
    "SBET": "sbet",
}

AGGREGATE_BLACKLIST_PREFIXES = ("BbAv", "BbMx", "Avg", "Max")
TOTALS_COL_RE = re.compile(r"^([<>])(\d+(?:\.\d+)?)$")
ODDS_INGEST_CHUNK_SIZE = 1500


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_float(value: str | None, *, positive_only: bool = True) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
        if positive_only and parsed <= 0:
            return None
        return parsed
    except ValueError:
        return None


def _is_aggregate_column(col: str) -> bool:
    return col.startswith(AGGREGATE_BLACKLIST_PREFIXES)


def _match_bookmaker_prefix(col: str) -> str | None:
    for prefix in BOOKMAKER_PREFIXES:
        if col.startswith(prefix):
            return prefix
    return None


def _provider_name(prefix: str) -> str:
    return BOOKMAKER_PROVIDER_MAP.get(prefix, prefix.lower())


def _snapshot_at_noon(match_date: datetime) -> datetime:
    dt = ensure_utc(match_date)
    return dt.replace(hour=12, minute=0, second=0, microsecond=0)


def _extract_h2h_snapshot(row: dict[str, str], prefix: str, base: dict) -> dict | None:
    home = _to_float(row.get(f"{prefix}H"))
    draw = _to_float(row.get(f"{prefix}D"))
    away = _to_float(row.get(f"{prefix}A"))
    if home is None or draw is None or away is None:
        logger.debug("Skipping incomplete h2h market for prefix=%s", prefix)
        return None
    return {**base, "odds": {"1": home, "X": draw, "2": away}}


def _extract_totals_snapshots(row: dict[str, str], prefix: str, base: dict) -> list[dict]:
    totals_by_line: dict[float, dict[str, float]] = {}
    for col, raw_value in row.items():
        if not col.startswith(prefix) or _is_aggregate_column(col):
            continue
        suffix = col[len(prefix):]
        match = TOTALS_COL_RE.fullmatch(suffix)
        if not match:
            continue

        op, line_text = match.group(1), match.group(2)
        line = float(line_text)
        price = _to_float(raw_value)
        if price is None:
            continue

        bucket = totals_by_line.setdefault(line, {})
        if op == ">":
            bucket["over"] = price
        elif op == "<":
            bucket["under"] = price

    snapshots: list[dict] = []
    for line, market in totals_by_line.items():
        over = market.get("over")
        under = market.get("under")
        if over is None or under is None:
            logger.debug("Skipping incomplete totals market for prefix=%s line=%s", prefix, line)
            continue
        snapshots.append({**base, "totals": {"over": over, "under": under, "line": line}})

    return snapshots


def _resolve_spread_line(row: dict[str, str], prefix: str) -> float | None:
    candidates = (
        f"{prefix}AHh",
        f"AHh_{prefix}",
        "AHh",
    )
    for col in candidates:
        parsed = _to_float(row.get(col), positive_only=False)
        if parsed is not None:
            return parsed
    return None


def _extract_spreads_snapshot(
    row: dict[str, str],
    prefix: str,
    base: dict,
    match_id: ObjectId | None,
    match_date: datetime,
) -> dict | None:
    home = _to_float(row.get(f"{prefix}AHH"))
    away = _to_float(row.get(f"{prefix}AHA"))
    if home is None or away is None:
        return None

    line = _resolve_spread_line(row, prefix)
    if line is None:
        logger.warning(
            "Skipping spreads due to missing line: match_id=%s prefix=%s date=%s",
            str(match_id),
            prefix,
            ensure_utc(match_date).date().isoformat(),
        )
        return None

    return {**base, "spreads": {"home": home, "away": away, "line": line}}


def _extract_odds_snapshots_from_row(
    row: dict[str, str],
    match_doc: dict,
    league_id: ObjectId,
    sport_key: str,
    match_date: datetime,
) -> dict[str, list[dict]]:
    snapshot_at = _snapshot_at_noon(match_date)
    match_id = match_doc["_id"]
    prefixes = _bookmaker_prefixes_from_row(row)

    by_provider: dict[str, list[dict]] = defaultdict(list)
    base_common = {
        "match_id": str(match_id),
        "league_id": league_id,
        "sport_key": sport_key,
        "snapshot_at": snapshot_at,
    }
    for prefix in sorted(prefixes):
        provider = _provider_name(prefix)
        base = {**base_common}

        h2h_snapshot = _extract_h2h_snapshot(row, prefix, base)
        if h2h_snapshot:
            by_provider[provider].append(h2h_snapshot)

        totals_snapshots = _extract_totals_snapshots(row, prefix, base)
        if totals_snapshots:
            by_provider[provider].extend(totals_snapshots)

        spreads_snapshot = _extract_spreads_snapshot(row, prefix, base, match_id, match_date)
        if spreads_snapshot:
            by_provider[provider].append(spreads_snapshot)

    return by_provider


def _bookmaker_prefixes_from_row(row: dict[str, str]) -> set[str]:
    prefixes: set[str] = set()
    for col in row.keys():
        if _is_aggregate_column(col):
            logger.debug("Ignoring aggregate column: %s", col)
            continue
        prefix = _match_bookmaker_prefix(col)
        if prefix is not None:
            prefixes.add(prefix)
    return prefixes


def _count_odds_snapshots_from_row(
    row: dict[str, str],
    match_date: datetime,
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    base = {"snapshot_at": _snapshot_at_noon(match_date)}
    for prefix in sorted(_bookmaker_prefixes_from_row(row)):
        provider = _provider_name(prefix)
        if _extract_h2h_snapshot(row, prefix, base):
            counts[provider] += 1
        totals_snapshots = _extract_totals_snapshots(row, prefix, base)
        if totals_snapshots:
            counts[provider] += len(totals_snapshots)
        if _extract_spreads_snapshot(row, prefix, base, None, match_date):
            counts[provider] += 1
    return counts


def _season_start_year_from_code(season_code: str) -> int:
    yy = int(season_code[:2])
    return 2000 + yy


def _derive_outcome(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "1"
    if home_score < away_score:
        return "2"
    return "X"


def _derive_match_status(
    match_date: datetime,
    home_score: int | None,
    away_score: int | None,
    now: datetime,
) -> str:
    if home_score is not None and away_score is not None:
        return "final"
    if ensure_utc(match_date) <= ensure_utc(now):
        return "final"
    return "scheduled"


def _season_code_from_start_year(start_year: int) -> str:
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def _normalize_football_data_uk_season_code(
    season: str | None,
    *,
    default_start_year: int,
) -> str:
    """Normalize admin season input to football-data.co.uk code.

    Accepted inputs:
    - season code: "2526"
    - season start year: "2025" (converted to "2526")
    """
    raw = str(season or "").strip()
    if not raw:
        return _season_code_from_start_year(int(default_start_year))
    if len(raw) == 4 and raw.isdigit():
        year = int(raw)
        if 1900 <= year <= 2100:
            return _season_code_from_start_year(year)
        return raw
    raise HTTPException(
        status_code=400,
        detail="season must be football-data.co.uk code ('2526') or season start year ('2025').",
    )


def _chunked(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _group_snapshots_by_reference(snapshots: list[dict]) -> dict[datetime, list[dict]]:
    grouped: dict[datetime, list[dict]] = defaultdict(list)
    for snap in snapshots:
        snapshot_at = snap.get("snapshot_at")
        if not snapshot_at:
            continue
        grouped[ensure_utc(snapshot_at)].append(snap)
    return grouped


async def import_football_data_stats(
    league_id: ObjectId,
    season: str | None = None,
    dry_run: bool = False,
    progress_cb: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """Import football-data.co.uk stats for one league+season."""
    league = await _db.db.leagues.find_one({"_id": league_id})
    if not league:
        raise HTTPException(status_code=404, detail="League not found.")

    sport_key = str(league.get("sport_key") or "").strip()
    if not sport_key:
        raise HTTPException(status_code=400, detail="League has no sport_key.")

    # Ensure registry is warmed and league is known to the tower.
    registry = LeagueRegistry.get()
    await registry.ensure_for_import(sport_key, auto_create_inactive=True)

    external_ids = league.get("external_ids") or {}
    if not isinstance(external_ids, dict):
        external_ids = {}
    division_code = str(external_ids.get("football_data_uk") or "").strip()
    if not division_code:
        raise HTTPException(status_code=400, detail="League has no football_data_uk external_id.")

    season_code = _normalize_football_data_uk_season_code(
        season,
        default_start_year=int(league.get("current_season") or utcnow().year),
    )

    if dry_run:
        logger.info(
            "Running football-data import in DRY RUN mode for league=%s season=%s",
            str(league_id),
            season_code,
        )

    try:
        csv_text = await football_data_uk_provider.fetch_season_csv(season_code, division_code)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise HTTPException(
            status_code=400,
            detail=(
                f"CSV not available for season={season_code} division={division_code} "
                f"(upstream_status={status})"
            ),
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"CSV not available for season={season_code} division={division_code}",
        ) from exc

    rows = list(csv.DictReader(StringIO(csv_text)))
    total_rows = len(rows)
    async def _emit_progress(phase: str, processed_rows: int) -> None:
        if not progress_cb:
            return
        percent = round((processed_rows / total_rows) * 100, 2) if total_rows else 0.0
        await progress_cb(
            {
                "phase": phase,
                "progress": {
                    "processed": processed_rows,
                    "total": total_rows,
                    "percent": percent,
                },
            }
        )

    await _emit_progress("fetching_csv", 0)
    if not rows:
        result = {
            "processed": 0,
            "matched": 0,
            "existing_matches": 0,
            "new_matches": 0,
            "updated": 0,
            "season": season_code,
            "division": division_code,
            "odds_snapshots_total": 0,
            "odds_providers_seen": 0,
            "odds_ingest_inserted": 0,
            "odds_ingest_deduplicated": 0,
            "odds_ingest_markets_updated": 0,
            "odds_ingest_errors": 0,
        }
        if dry_run:
            result["dry_run_preview"] = {
                "matches_found": 0,
                "existing_matches": 0,
                "new_matches": 0,
                "odds_snapshots_by_provider": {},
                "would_update_stats": 0,
                "would_ingest_snapshots": 0,
            }
        return result

    now = utcnow()
    processed = 0
    matched = 0
    existing_matches = 0
    new_matches = 0
    updated = 0
    would_update_stats = 0
    would_ingest_snapshots = 0
    odds_snapshots_total = 0
    odds_providers_seen: set[str] = set()
    odds_ingest_inserted = 0
    odds_ingest_deduplicated = 0
    odds_ingest_markets_updated = 0
    odds_ingest_errors = 0
    odds_snapshots_by_provider: dict[str, list[dict]] = defaultdict(list)
    odds_snapshots_by_provider_preview: dict[str, int] = defaultdict(int)
    prepared_rows: list[tuple[dict[str, str], datetime, str]] = []
    prepared_matches = []
    season_start_year = _season_start_year_from_code(season_code)

    for row in rows:
        home_team = (row.get("HomeTeam") or "").strip()
        away_team = (row.get("AwayTeam") or "").strip()
        date_raw = row.get("Date")
        if not home_team or not away_team or not date_raw:
            continue
        match_date = _parse_date(date_raw)
        if not match_date:
            continue

        full_time_home = _to_int(row.get("FTHG"))
        full_time_away = _to_int(row.get("FTAG"))
        score = {"full_time": {"home": full_time_home, "away": full_time_away}}
        status_raw = "FINISHED" if (full_time_home is not None and full_time_away is not None) else "SCHEDULED"
        match_payload = build_football_data_uk_match_data(
            sport_key=sport_key,
            league_external_id=division_code,
            season_start_year=season_start_year,
            match_date=match_date,
            home_team=home_team,
            away_team=away_team,
            status_raw=status_raw,
            score=score,
            metadata={"csv_date": date_raw, "division": division_code},
        )
        prepared_matches.append(match_payload)
        prepared_rows.append((row, match_date, match_payload["external_id"]))
        processed += 1
        if processed == 1 or processed % 10 == 0:
            await _emit_progress("matching", processed)

    ingest_result = await match_ingest_service.process_matches(
        prepared_matches,
        league_id=league_id,
        dry_run=dry_run,
    )
    new_matches = int(ingest_result.get("created", 0))
    existing_matches = int(ingest_result.get("updated", 0))
    matched = new_matches + existing_matches

    for row, match_date, external_id in prepared_rows:
        match_doc = None
        if not dry_run:
            match_doc = await _db.db.matches.find_one(
                {"external_ids.football_data_uk": external_id},
                {"_id": 1},
            )

        if dry_run:
            counts = _count_odds_snapshots_from_row(row, match_date)
            for provider, count in counts.items():
                odds_snapshots_total += count
                odds_providers_seen.add(provider)
                odds_snapshots_by_provider_preview[provider] += count
                would_ingest_snapshots += count
        elif match_doc:
            row_snapshots = _extract_odds_snapshots_from_row(
                row=row,
                match_doc=match_doc,
                league_id=league_id,
                sport_key=sport_key,
                match_date=match_date,
            )
            for provider, snapshots in row_snapshots.items():
                if not snapshots:
                    continue
                odds_snapshots_by_provider[provider].extend(snapshots)
                odds_snapshots_total += len(snapshots)
                odds_providers_seen.add(provider)

        stats: dict[str, int | str | datetime] = {
            "source": "football_data_co_uk",
            "updated_at": now,
        }
        for csv_key, target_key in STATS_COLUMN_MAP.items():
            parsed_value = _to_int(row.get(csv_key))
            if parsed_value is not None:
                stats[target_key] = parsed_value

        if len(stats) <= 2:
            continue

        if dry_run:
            would_update_stats += 1
            logger.debug(
                "DRY RUN: skipping write operation matches.update_one for external_id=%s",
                external_id,
            )
        elif match_doc:
            result = await _db.db.matches.update_one(
                {"_id": match_doc["_id"]},
                {"$set": {"stats": stats, "updated_at": now, "last_updated": now}},
            )
            if result.modified_count:
                updated += 1

    if dry_run:
        logger.debug("DRY RUN: skipping write operation odds_service.ingest_snapshot_batch")
        logger.info(
            "Dry-run summary for league=%s season=%s: existing_matches=%d new_matches=%d would_update_stats=%d would_ingest_snapshots=%d",
            str(league_id),
            season_code,
            existing_matches,
            new_matches,
            would_update_stats,
            would_ingest_snapshots,
        )
    else:
        await _emit_progress("ingesting_odds", processed)
        for provider, snapshots in odds_snapshots_by_provider.items():
            for reference_ts, dated_snapshots in _group_snapshots_by_reference(snapshots).items():
                for chunk in _chunked(dated_snapshots, ODDS_INGEST_CHUNK_SIZE):
                    try:
                        # Anchor each ingest call to the actual snapshot date in that group.
                        ingest_result = await odds_service.ingest_snapshot_batch(
                            provider,
                            chunk,
                            reference_ts=reference_ts,
                        )
                        odds_ingest_inserted += int(ingest_result.get("inserted", 0))
                        odds_ingest_deduplicated += int(ingest_result.get("deduplicated", 0))
                        odds_ingest_markets_updated += int(ingest_result.get("markets_updated", 0))
                    except Exception:
                        odds_ingest_errors += 1
                        logger.error(
                            "Odds ingest failed: provider=%s chunk_size=%d reference_ts=%s season=%s division=%s",
                            provider,
                            len(chunk),
                            reference_ts.isoformat(),
                            season_code,
                            division_code,
                            exc_info=True,
                        )

    result = {
        "processed": processed,
        "matched": matched,
        "existing_matches": existing_matches,
        "new_matches": new_matches,
        "updated": updated,
        "season": season_code,
        "division": division_code,
        "odds_snapshots_total": odds_snapshots_total,
        "odds_providers_seen": len(odds_providers_seen),
        "odds_ingest_inserted": odds_ingest_inserted,
        "odds_ingest_deduplicated": odds_ingest_deduplicated,
        "odds_ingest_markets_updated": odds_ingest_markets_updated,
        "odds_ingest_errors": odds_ingest_errors,
    }
    if dry_run:
        result["dry_run_preview"] = {
            "matches_found": matched,
            "existing_matches": existing_matches,
            "new_matches": new_matches,
            "odds_snapshots_by_provider": {
                provider: int(count)
                for provider, count in sorted(odds_snapshots_by_provider_preview.items())
            },
            "would_update_stats": would_update_stats,
            "would_ingest_snapshots": would_ingest_snapshots,
        }
    await _emit_progress("finalizing", processed)
    return result
