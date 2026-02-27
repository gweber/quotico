"""
backend/app/services/match_service.py

Purpose:
    Match ingest and query service for the Greenfield Match domain. Validates
    leagues via LeagueRegistry, resolves team identities via TeamRegistry, and
    upserts matches idempotently using provider external IDs and Team-ID/date
    fallback matching.

Dependencies:
    - app.database
    - app.models.matches
    - app.services.league_service
    - app.services.team_registry_service
    - app.utils
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import app.database as _db
from app.models.matches import MatchStatus
from app.services.league_service import LeagueRegistry
from app.services.match_ingest_service import match_ingest_service
from app.services.match_ingest_types import MatchData
from app.services.team_registry_service import TeamRegistry
from app.utils import ensure_utc, parse_utc, utcnow

logger = logging.getLogger("quotico.match_service")

_MAX_DURATION: dict[int, timedelta] = {}
_DEFAULT_DURATION = timedelta(minutes=190)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return parse_utc(value)
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _derive_season(match_date: datetime) -> int:
    # Football season starts in July; season stores start year.
    return match_date.year if match_date.month >= 7 else match_date.year - 1


def _status_from_provider(provider_status: str | None, match_date: datetime, now: datetime) -> MatchStatus:
    raw = (provider_status or "").strip().lower()

    if raw in {"in_play", "live", "running", "paused", "halftime", "half_time"}:
        return MatchStatus.LIVE
    if raw in {"finished", "final", "complete", "completed"}:
        return MatchStatus.FINAL
    if raw in {"postponed"}:
        return MatchStatus.POSTPONED
    if raw in {"canceled", "cancelled"}:
        return MatchStatus.CANCELED
    if raw in {"timed", "scheduled"}:
        return MatchStatus.SCHEDULED

    if match_date > now:
        return MatchStatus.SCHEDULED
    if (now - match_date) <= _DEFAULT_DURATION:
        return MatchStatus.LIVE
    return MatchStatus.FINAL


def _normalize_score_detail(data: dict[str, Any] | None) -> dict[str, int | None] | None:
    if not isinstance(data, dict):
        return None
    home = data.get("home")
    away = data.get("away")
    if home is None and away is None:
        return None
    return {
        "home": int(home) if home is not None else None,
        "away": int(away) if away is not None else None,
    }


def _normalize_score(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}

    full_time = _normalize_score_detail(payload.get("full_time")) or {"home": None, "away": None}
    half_time = _normalize_score_detail(payload.get("half_time"))
    extra_time = _normalize_score_detail(payload.get("extra_time"))
    penalties = _normalize_score_detail(payload.get("penalties"))

    return {
        "full_time": full_time,
        "half_time": half_time,
        "extra_time": extra_time,
        "penalties": penalties,
    }


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_round_name(value: Any) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None
    key = text.lower()
    mapping = {
        "round of 16": "Round of 16",
        "last 16": "Round of 16",
        "quarter finals": "Quarter-Final",
        "quarterfinal": "Quarter-Final",
        "quarter-final": "Quarter-Final",
        "semi finals": "Semi-Final",
        "semifinal": "Semi-Final",
        "semi-final": "Semi-Final",
        "final": "Final",
        "3rd place final": "Third-Place",
    }
    return mapping.get(key, text)


def _legacy_result_from_score(score: dict[str, Any]) -> dict[str, Any]:
    ft = score.get("full_time") or {}
    home = ft.get("home")
    away = ft.get("away")
    outcome = None
    if home is not None and away is not None:
        if home > away:
            outcome = "1"
        elif home < away:
            outcome = "2"
        else:
            outcome = "X"

    half_time = score.get("half_time")
    return {
        "home_score": home,
        "away_score": away,
        "outcome": outcome,
        "half_time": half_time,
    }


def _extract_provider_match(
    match: dict[str, Any],
) -> tuple[
    str,
    str,
    datetime,
    str | None,
    str | None,
    dict[str, Any],
    str | None,
    str | None,
    dict[str, int | None] | None,
    dict[str, int | None] | None,
]:
    teams = match.get("teams") if isinstance(match.get("teams"), dict) else {}
    home_team = str(teams.get("home") or match.get("home_team") or "").strip()
    away_team = str(teams.get("away") or match.get("away_team") or "").strip()
    if not home_team or not away_team:
        raise ValueError("Provider match missing home/away team name.")

    match_date = _as_datetime(
        match.get("start_at")
        or match.get("commence_time")
        or match.get("utc_date")
    )

    provider_external_id = match.get("external_id")
    if provider_external_id is not None:
        provider_external_id = str(provider_external_id).strip() or None

    provider_status = match.get("status")
    if provider_status is not None:
        provider_status = str(provider_status).strip() or None

    score = _normalize_score(match.get("score"))
    if score["full_time"]["home"] is None and match.get("home_score") is not None:
        score["full_time"]["home"] = int(match.get("home_score"))
    if score["full_time"]["away"] is None and match.get("away_score") is not None:
        score["full_time"]["away"] = int(match.get("away_score"))

    round_name = _normalize_round_name(
        match.get("round_name")
        or match.get("round")
        or match.get("stage")
    )
    group_name = _normalize_text(
        match.get("group_name")
        or match.get("group")
    )
    score_extra_time = _normalize_score_detail(
        match.get("score_extra_time")
        or match.get("extra_time_score")
    ) or _normalize_score_detail(score.get("extra_time"))
    score_penalties = _normalize_score_detail(
        match.get("score_penalties")
        or match.get("penalties_score")
    ) or _normalize_score_detail(score.get("penalties"))

    return (
        home_team,
        away_team,
        match_date,
        provider_external_id,
        provider_status,
        score,
        round_name,
        group_name,
        score_extra_time,
        score_penalties,
    )


async def update_matches_from_provider(provider_name: str, provider_data: list[dict]) -> dict[str, int]:
    """Upsert provider match payloads into the unified Match model.

    Dedup order:
    1) external_ids[provider_name]
    2) (league_id, home_team_id, away_team_id, match_date +-24h)
    """
    processed = 0
    skipped = 0
    normalized: list[MatchData] = []
    provider_name = str(provider_name or "").strip().lower()

    for raw_match in provider_data:
        try:
            league_raw = raw_match.get("league_id")
            if not isinstance(league_raw, int):
                logger.warning("Provider match rejected: non-int league_id value=%r type=%s", league_raw, type(league_raw).__name__)
                raise ValueError("Provider match league_id must be int.")
            league_id = league_raw
            if league_id <= 0:
                raise ValueError("Provider match missing league_id.")

            (
                home_team,
                away_team,
                match_date,
                external_id,
                provider_status,
                score,
                round_name,
                group_name,
                score_extra_time,
                score_penalties,
            ) = _extract_provider_match(raw_match)
            if not external_id:
                raise ValueError("Provider match missing external_id.")

            metadata: dict[str, Any] = {}
            if round_name is not None:
                metadata["round_name"] = round_name
            if group_name is not None:
                metadata["group_name"] = group_name
            if score_extra_time is not None:
                metadata["score_extra_time"] = score_extra_time
            if score_penalties is not None:
                metadata["score_penalties"] = score_penalties

            normalized.append(
                {
                    "external_id": external_id,
                    "source": provider_name,  # type: ignore[typeddict-item]
                    "league_external_id": str(league_id),
                    "season": int(raw_match.get("season") or _derive_season(match_date)),
                    "league_id": league_id,
                    "match_date": ensure_utc(match_date),
                    "home_team": {"external_id": None, "name": home_team},
                    "away_team": {"external_id": None, "name": away_team},
                    "status": provider_status,
                    "matchday": (
                        int(raw_match["matchday_number"])
                        if raw_match.get("matchday_number") is not None
                        else (int(raw_match["matchday"]) if raw_match.get("matchday") is not None else None)
                    ),
                    "score": score,
                    "metadata": metadata,
                }
            )
            processed += 1
        except Exception:
            logger.exception("Match ingest normalization failed for provider=%s payload=%s", provider_name, raw_match)
            skipped += 1

    ingest = await match_ingest_service.process_matches(normalized, dry_run=False)
    return {
        "processed": processed,
        "created": int(ingest.get("created", 0)),
        "updated": int(ingest.get("updated", 0)),
        "skipped": skipped + int(ingest.get("skipped", 0)),
    }


async def sports_with_live_action() -> set[int]:
    now = utcnow()
    live_states = [MatchStatus.LIVE.value, MatchStatus.SCHEDULED.value]
    live_leagues: set[int] = set()

    active_leagues = await _db.db.league_registry_v3.find(
        {"is_active": True}, {"league_id": 1}
    ).to_list(100)
    active_league_ids = [l["league_id"] for l in active_leagues if isinstance(l.get("league_id"), int)]

    for league_id in active_league_ids:
        max_dur = _MAX_DURATION.get(league_id, _DEFAULT_DURATION)
        has_live = await _db.db.matches_v3.find_one(
            {
                "league_id": league_id,
                "status": {"$in": live_states},
                "start_at": {
                    "$lte": now,
                    "$gte": now - max_dur,
                },
            },
            {"_id": 1},
        )
        if has_live:
            live_leagues.add(league_id)

    return live_leagues


async def next_kickoff_in() -> timedelta | None:
    now = utcnow()
    nxt = await _db.db.matches_v3.find_one(
        {
            "status": MatchStatus.SCHEDULED.value,
            "start_at": {"$gt": now},
        },
        sort=[("start_at", 1)],
        projection={"start_at": 1},
    )
    if nxt:
        return ensure_utc(nxt["start_at"]) - now
    return None


async def get_match_by_id(match_id: str) -> dict | None:
    from bson import ObjectId

    try:
        return await _db.db.matches_v3.find_one({"_id": int(match_id)})
    except Exception:
        return None


async def get_matches(
    league_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    query: dict[str, Any] = {}
    if league_id is not None:
        query["league_id"] = league_id

    if status:
        normalized = status.strip().lower()
        aliases = {
            "upcoming": MatchStatus.SCHEDULED.value,
            "completed": MatchStatus.FINAL.value,
            "live": MatchStatus.LIVE.value,
            "final": MatchStatus.FINAL.value,
            "cancelled": MatchStatus.CANCELED.value,
            "canceled": MatchStatus.CANCELED.value,
        }
        query["status"] = aliases.get(normalized, normalized)
    else:
        query["status"] = {
            "$in": [
                MatchStatus.SCHEDULED.value,
                MatchStatus.LIVE.value,
            ]
        }

    cursor = _db.db.matches_v3.find(query).sort("start_at", 1).limit(limit)
    return await cursor.to_list(length=limit)
