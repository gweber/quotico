"""
backend/app/services/xg_enrichment_service.py

Purpose:
    Fetch match-level expected goals data from Understat and enrich finalized
    matches, guarded by League Tower feature flags and Team Tower resolution.
    Provides a single-season enrichment entrypoint for admin async jobs.

Dependencies:
    - app.services.league_service
    - app.services.team_registry_service
    - app.utils.team_matching
    - app.database
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.services.league_service import LeagueRegistry, league_feature_enabled
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.utils import parse_utc, utcnow
from app.utils.team_matching import teams_match

try:
    import soccerdata as sd
except ImportError:
    sd = None  # type: ignore[assignment]

logger = logging.getLogger("quotico.xg_enrichment")

MATCH_DATE_WINDOW_HOURS = 24
UNDERSTAT_PROVIDER = "understat"
UNMATCHED_TEAMS_LIMIT = 500
RAW_ROWS_PREVIEW_LIMIT = 400


def _season_str(year: int) -> str:
    return f"{year}/{year + 1}"


def current_season_year() -> int:
    now = utcnow()
    return now.year if now.month >= 7 else now.year - 1


def parse_season_spec(spec: str | None) -> list[int]:
    """Parse season spec: None, single year, or inclusive year range."""
    if spec is None or not str(spec).strip():
        return [current_season_year()]

    value = str(spec).strip()
    if "-" not in value:
        return [int(value)]

    start_raw, end_raw = value.split("-", 1)
    start = int(start_raw.strip())
    end = int(end_raw.strip())
    if start > end:
        raise ValueError("Invalid season range: start must be <= end.")
    return list(range(start, end + 1))


def fetch_season_xg(sport_key: str, season_year: int, understat_league_id: str):
    if sd is None:
        raise RuntimeError("soccerdata is not installed. Run: pip install soccerdata")
    if not understat_league_id:
        raise ValueError(
            f"Sport key {sport_key!r} has no Understat mapping in leagues.external_ids.understat"
        )

    logger.info("Fetching xG from Understat: %s season %s", understat_league_id, _season_str(season_year))
    understat = sd.Understat(leagues=understat_league_id, seasons=season_year, no_cache=False)
    return understat.read_schedule().reset_index()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fuzzy_resolve_team(
    incoming_name: str,
    *,
    team_registry: TeamRegistry,
    sport_key: str,
    league_id: ObjectId | None,
    league_external_id: str | None,
    teams_index: list[dict[str, Any]],
    run_seen_suggestions: set[tuple[str, str, str, str]],
    sample_ref: dict[str, Any],
) -> Any | None:
    incoming = str(incoming_name or "").strip()
    if not incoming:
        return None

    normalized_incoming = normalize_team_name(incoming)
    candidates: list[dict[str, Any]] = []
    for team in teams_index:
        for candidate_name in team.get("names", []):
            if not candidate_name:
                continue
            if teams_match(incoming, candidate_name):
                candidates.append(team)
                break

    if len(candidates) != 1:
        return None

    winner = candidates[0]
    team_id = winner.get("_id")
    if not team_id:
        return None

    # Record alias suggestion when this incoming name is new for the resolved team.
    known_normalized = winner.get("normalized", set())
    if normalized_incoming and normalized_incoming not in known_normalized:
        dedupe_key = (
            sport_key,
            str(league_id) if league_id else "",
            UNDERSTAT_PROVIDER,
            normalized_incoming,
        )
        if dedupe_key not in run_seen_suggestions:
            run_seen_suggestions.add(dedupe_key)
            await team_registry.record_alias_suggestion(
                source=UNDERSTAT_PROVIDER,
                raw_team_name=incoming,
                sport_key=sport_key,
                league_id=league_id,
                league_external_id=league_external_id,
                reason="name_mismatch",
                sample_ref=sample_ref,
                suggested_team_id=team_id,
                suggested_team_name=str(winner.get("display_name") or incoming),
                confidence=0.9,
            )
    return team_id


async def _build_teams_index(sport_key: str) -> list[dict[str, Any]]:
    docs = await _db.db.teams.find(
        {"sport_key": sport_key},
        {"_id": 1, "display_name": 1, "aliases": 1},
    ).to_list(length=20_000)
    out: list[dict[str, Any]] = []
    for doc in docs:
        names: list[str] = []
        normalized: set[str] = set()
        display_name = str(doc.get("display_name") or "").strip()
        if display_name:
            names.append(display_name)
            norm = normalize_team_name(display_name)
            if norm:
                normalized.add(norm)
        for alias in doc.get("aliases", []):
            if not isinstance(alias, dict):
                continue
            alias_name = str(alias.get("name") or "").strip()
            if alias_name:
                names.append(alias_name)
            alias_norm = str(alias.get("normalized") or "").strip()
            if alias_norm:
                normalized.add(alias_norm)
        out.append(
            {
                "_id": doc.get("_id"),
                "display_name": display_name,
                "names": names,
                "normalized": normalized,
            }
        )
    return out


async def enrich_matches(
    sport_key: str,
    season_year: int,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    league_registry = LeagueRegistry.get()
    league = await league_registry.ensure_for_import(
        sport_key,
        provider_name=UNDERSTAT_PROVIDER,
        provider_id=sport_key,
        auto_create_inactive=True,
    )
    if not league.get("is_active", False):
        raise ValueError(f"xG import blocked for inactive league: {sport_key}")
    if not league_feature_enabled(league, "xg_sync", False):
        raise ValueError(f"xG import blocked for disabled xg_sync feature: {sport_key}")

    league_id = league.get("_id")
    if not league_id:
        raise ValueError(f"xG import blocked: {sport_key} has no league_id")

    understat_league_id = (league.get("external_ids") or {}).get(UNDERSTAT_PROVIDER)
    if not understat_league_id:
        understat_league_id = (league.get("provider_mappings") or {}).get(UNDERSTAT_PROVIDER)
    if not understat_league_id:
        raise ValueError(
            f"xG import blocked: {sport_key} has no understat provider mapping in leagues collection."
        )

    df = fetch_season_xg(sport_key, season_year, understat_league_id)
    team_registry = TeamRegistry.get()
    teams_index = await _build_teams_index(sport_key)
    cached_team_resolution: dict[str, Any | None] = {}

    matched = 0
    unmatched = 0
    skipped = 0
    already_enriched = 0
    total = 0
    unmatched_teams: set[str] = set()
    recorded_suggestions = 0
    run_seen_suggestions: set[tuple[str, str, str, str]] = set()
    raw_rows_preview: list[dict[str, Any]] = []

    def _add_raw_row(
        *,
        action: str,
        reason: str,
        home_team: str,
        away_team: str,
        date_raw: Any = None,
        home_xg: float | None = None,
        away_xg: float | None = None,
        match_id: Any = None,
    ) -> None:
        if not dry_run or len(raw_rows_preview) >= RAW_ROWS_PREVIEW_LIMIT:
            return
        raw_rows_preview.append(
            {
                "action": action,
                "reason": reason,
                "home_team": home_team,
                "away_team": away_team,
                "date": str(date_raw) if date_raw is not None else None,
                "home_xg": home_xg,
                "away_xg": away_xg,
                "match_id": str(match_id) if match_id is not None else None,
            }
        )

    for _, row in df.iterrows():
        total += 1
        home_xg = _safe_float(row.get("home_xg"))
        away_xg = _safe_float(row.get("away_xg"))
        if home_xg is None or away_xg is None:
            skipped += 1
            _add_raw_row(
                action="skipped",
                reason="missing_xg",
                home_team=str(row.get("home_team", "")).strip(),
                away_team=str(row.get("away_team", "")).strip(),
                date_raw=row.get("date"),
            )
            continue

        home_name = str(row.get("home_team", "")).strip()
        away_name = str(row.get("away_team", "")).strip()
        if not home_name or not away_name:
            skipped += 1
            _add_raw_row(
                action="skipped",
                reason="missing_team_name",
                home_team=home_name,
                away_team=away_name,
                date_raw=row.get("date"),
                home_xg=home_xg,
                away_xg=away_xg,
            )
            continue

        async def _resolve_name(name: str) -> Any | None:
            nonlocal recorded_suggestions
            cached = cached_team_resolution.get(name)
            if cached is not None or name in cached_team_resolution:
                return cached

            resolved = await team_registry.resolve_by_external_id_or_name(
                source=UNDERSTAT_PROVIDER,
                external_id="",
                name=name,
                sport_key=sport_key,
                create_if_missing=False,
            )
            if not resolved:
                resolved = await _fuzzy_resolve_team(
                    name,
                    team_registry=team_registry,
                    sport_key=sport_key,
                    league_id=league_id,
                    league_external_id=str(understat_league_id),
                    teams_index=teams_index,
                    run_seen_suggestions=run_seen_suggestions,
                    sample_ref={"season_year": int(season_year)},
                )
                if resolved:
                    recorded_suggestions += 1
            cached_team_resolution[name] = resolved
            return resolved

        home_team_id = await _resolve_name(home_name)
        away_team_id = await _resolve_name(away_name)
        if not home_team_id or not away_team_id:
            if not home_team_id:
                await team_registry.record_alias_suggestion(
                    source=UNDERSTAT_PROVIDER,
                    raw_team_name=home_name,
                    sport_key=sport_key,
                    league_id=league_id,
                    league_external_id=str(understat_league_id),
                    reason="unresolved_team",
                    sample_ref={"season_year": int(season_year), "side": "home"},
                )
                recorded_suggestions += 1
            if not away_team_id:
                await team_registry.record_alias_suggestion(
                    source=UNDERSTAT_PROVIDER,
                    raw_team_name=away_name,
                    sport_key=sport_key,
                    league_id=league_id,
                    league_external_id=str(understat_league_id),
                    reason="unresolved_team",
                    sample_ref={"season_year": int(season_year), "side": "away"},
                )
                recorded_suggestions += 1
            unmatched += 1
            if len(unmatched_teams) < UNMATCHED_TEAMS_LIMIT:
                unmatched_teams.add(f"{home_name} vs {away_name}")
            _add_raw_row(
                action="unmatched",
                reason="team_not_resolved",
                home_team=home_name,
                away_team=away_name,
                date_raw=row.get("date"),
                home_xg=home_xg,
                away_xg=away_xg,
            )
            continue

        match_date_raw = row.get("date")
        if match_date_raw is None:
            skipped += 1
            _add_raw_row(
                action="skipped",
                reason="missing_match_date",
                home_team=home_name,
                away_team=away_name,
                home_xg=home_xg,
                away_xg=away_xg,
            )
            continue
        match_date = parse_utc(match_date_raw)

        query = {
            "league_id": league_id,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "status": "final",
            "match_date": {
                "$gte": match_date - timedelta(hours=MATCH_DATE_WINDOW_HOURS),
                "$lte": match_date + timedelta(hours=MATCH_DATE_WINDOW_HOURS),
            },
        }
        db_match = await _db.db.matches.find_one(
            query,
            {"_id": 1, "updated_at": 1, "result.home_xg": 1, "result.away_xg": 1},
            sort=[("updated_at", -1), ("_id", -1)],
        )
        if not db_match:
            unmatched += 1
            if len(unmatched_teams) < UNMATCHED_TEAMS_LIMIT:
                unmatched_teams.add(f"{home_name} vs {away_name}")
            _add_raw_row(
                action="unmatched",
                reason="match_not_found",
                home_team=home_name,
                away_team=away_name,
                date_raw=match_date_raw,
                home_xg=home_xg,
                away_xg=away_xg,
            )
            continue

        has_xg = (
            (db_match.get("result") or {}).get("home_xg") is not None
            and (db_match.get("result") or {}).get("away_xg") is not None
        )
        if has_xg and not force:
            already_enriched += 1
            _add_raw_row(
                action="already_enriched",
                reason="xg_already_present",
                home_team=home_name,
                away_team=away_name,
                date_raw=match_date_raw,
                home_xg=home_xg,
                away_xg=away_xg,
                match_id=db_match.get("_id"),
            )
            continue

        if not dry_run:
            await _db.db.matches.update_one(
                {"_id": db_match["_id"]},
                {
                    "$set": {
                        "result.home_xg": round(home_xg, 2),
                        "result.away_xg": round(away_xg, 2),
                        "result.xg_provider": UNDERSTAT_PROVIDER,
                    }
                },
            )
        else:
            _add_raw_row(
                action="would_update",
                reason="xg_writable",
                home_team=home_name,
                away_team=away_name,
                date_raw=match_date_raw,
                home_xg=home_xg,
                away_xg=away_xg,
                match_id=db_match.get("_id"),
            )

        matched += 1

    return {
        "sport_key": sport_key,
        "season_year": int(season_year),
        "provider": UNDERSTAT_PROVIDER,
        "matched": matched,
        "unmatched": unmatched,
        "skipped": skipped,
        "already_enriched": already_enriched,
        "total": total,
        "unmatched_teams": sorted(unmatched_teams),
        "alias_suggestions_recorded": recorded_suggestions,
        "raw_rows_preview": raw_rows_preview,
    }


async def list_xg_target_sport_keys(requested_sport_key: str | None = None) -> list[str]:
    """Return sport_keys eligible for xG enrichment."""
    query: dict[str, Any] = {
        "is_active": True,
        "features.xg_sync": True,
        "external_ids.understat": {"$exists": True, "$nin": [None, ""]},
    }
    if requested_sport_key:
        query["sport_key"] = str(requested_sport_key).strip()

    rows = await _db.db.leagues.find(query, {"sport_key": 1}).to_list(length=500)
    sport_keys = sorted({str(row.get("sport_key") or "").strip() for row in rows if row.get("sport_key")})
    return sport_keys


# Backward-compatible alias for older imports/tools.
match_and_enrich = enrich_matches
