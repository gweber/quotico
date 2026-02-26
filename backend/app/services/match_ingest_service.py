"""
backend/app/services/match_ingest_service.py

Purpose:
    Unified match ingest service that normalizes provider payload handling for
    league/team resolution, idempotent match detection, upsert writes, dry-run
    previews, and optional hooks.

Dependencies:
    - app.database
    - app.services.team_registry_service
    - app.services.match_ingest_types
    - app.services.match_ingest_hooks
    - app.utils
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.config import settings
from app.services.event_bus import event_bus
from app.services.event_models import (
    MatchCancelledEvent,
    MatchCreatedEvent,
    MatchFinalizedEvent,
    MatchPostponedEvent,
    MatchUpdatedEvent,
    make_correlation_id,
)
from app.services.match_ingest_hooks import MatchIngestHooks
from app.services.match_ingest_types import (
    IngestConflict,
    IngestPreviewItem,
    IngestResult,
    MatchData,
    MatchIngestContext,
)
from app.services.team_registry_service import TeamRegistry
from app.utils import ensure_utc, parse_utc, utcnow

logger = logging.getLogger("quotico.match_ingest_service")

_IDENTITY_WINDOW = timedelta(hours=24)
_PREVIEW_LIMIT = 100
_CONFLICT_PREVIEW_LIMIT = 200
_EVENT_PUBLISH_BY_SOURCE = {
    "football_data": "EVENT_PUBLISH_FOOTBALL_DATA",
    "openligadb": "EVENT_PUBLISH_OPENLIGADB",
    "football_data_uk": "EVENT_PUBLISH_FOOTBALL_DATA_UK",
    "theoddsapi": "EVENT_PUBLISH_THEODDSAPI",
}


def _status_final_from_score(score: dict[str, Any] | None) -> bool:
    if not isinstance(score, dict):
        return False
    ft = score.get("full_time")
    if not isinstance(ft, dict):
        return False
    return ft.get("home") is not None and ft.get("away") is not None


def _generic_status_mapper(raw_status: str | None, match: MatchData) -> str:
    raw = (raw_status or "").strip().lower()
    if _status_final_from_score(match.get("score")):
        return "final"
    if raw in {"finished", "final", "complete", "completed"}:
        return "final"
    if raw in {"in_play", "live", "running", "paused", "halftime", "half_time"}:
        return "live"
    if raw in {"postponed", "suspended"}:
        return "postponed"
    if raw in {"cancelled", "canceled"}:
        return "canceled"
    if raw in {"timed", "scheduled"}:
        return "scheduled"

    return "scheduled" if ensure_utc(match["match_date"]) > utcnow() else "live"


def _football_data_status_mapper(raw_status: str | None, match: MatchData) -> str:
    status = (raw_status or "").strip().upper()
    if _status_final_from_score(match.get("score")):
        return "final"
    mapping = {
        "FINISHED": "final",
        "SCHEDULED": "scheduled",
        "TIMED": "scheduled",
        "IN_PLAY": "live",
        "PAUSED": "live",
        "POSTPONED": "postponed",
        "SUSPENDED": "postponed",
        "CANCELLED": "canceled",
    }
    if status in mapping:
        return mapping[status]
    return _generic_status_mapper(raw_status, match)


def _openligadb_status_mapper(raw_status: str | None, match: MatchData) -> str:
    status = (raw_status or "").strip().lower()
    if status in {"finished", "is_finished", "final"}:
        return "final"
    return _generic_status_mapper(raw_status, match)


def _football_data_uk_status_mapper(raw_status: str | None, match: MatchData) -> str:
    return _generic_status_mapper(raw_status, match)


def _theoddsapi_status_mapper(raw_status: str | None, match: MatchData) -> str:
    return _generic_status_mapper(raw_status, match)


STATUS_MAPPERS: dict[str, Callable[[str | None, MatchData], str]] = {
    "football_data": _football_data_status_mapper,
    "openligadb": _openligadb_status_mapper,
    "football_data_uk": _football_data_uk_status_mapper,
    "theoddsapi": _theoddsapi_status_mapper,
}


def _legacy_result_from_score(score: dict[str, Any] | None) -> dict[str, Any]:
    ft = score.get("full_time") if isinstance(score, dict) else None
    home = ft.get("home") if isinstance(ft, dict) else None
    away = ft.get("away") if isinstance(ft, dict) else None
    outcome = None
    if home is not None and away is not None:
        if home > away:
            outcome = "1"
        elif away > home:
            outcome = "2"
        else:
            outcome = "X"
    return {"home_score": home, "away_score": away, "outcome": outcome}


class MatchIngestService:
    async def process_matches(
        self,
        matches: list[MatchData],
        *,
        league_id: ObjectId | None = None,
        dry_run: bool = False,
        status_mapper: Callable[[str | None, MatchData], str] | None = None,
        hooks: MatchIngestHooks | None = None,
    ) -> IngestResult:
        team_registry = TeamRegistry.get()
        now = utcnow()

        result: IngestResult = {
            "processed": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "conflicts": 0,
            "unresolved_league": 0,
            "unresolved_team": 0,
            "team_name_conflict": 0,
            "other_conflicts": 0,
            "matched_by_external_id": 0,
            "matched_by_identity_window": 0,
            "dry_run": bool(dry_run),
            "items_preview": [],
            "conflicts_preview": [],
        }

        for item in matches:
            result["processed"] += 1
            src = str(item.get("source") or "").strip().lower()
            external_id = str(item.get("external_id") or "").strip()

            try:
                if not src or not external_id:
                    raise ValueError("source and external_id are required")

                match_date = item.get("match_date")
                if isinstance(match_date, str):
                    match_date = parse_utc(match_date)
                elif isinstance(match_date, datetime):
                    match_date = ensure_utc(match_date)
                else:
                    raise ValueError("match_date missing or invalid")

                resolved_league_id = await self._resolve_league_id(
                    src,
                    item,
                    explicit_league_id=league_id,
                )
                if resolved_league_id is None:
                    self._append_conflict(
                        result,
                        {
                            "code": "unresolved_league",
                            "source": src,
                            "external_id": external_id,
                            "message": "League mapping missing for source/external id.",
                            "detail": {
                                "league_external_id": str(item.get("league_external_id") or ""),
                                "sport_key": str(item.get("sport_key") or ""),
                            },
                        },
                    )
                    self._append_preview(result, "skip", src, external_id, None, "unresolved_league")
                    continue

                sport_key = str(item.get("sport_key") or "").strip()
                home_team = item.get("home_team") or {}
                away_team = item.get("away_team") or {}
                home_name = str(home_team.get("name") or "").strip()
                away_name = str(away_team.get("name") or "").strip()
                if not home_name or not away_name:
                    raise ValueError("home_team.name and away_team.name are required")

                home_team_id = await team_registry.resolve_by_external_id_or_name(
                    source=src,
                    external_id=str(home_team.get("external_id") or "").strip(),
                    name=home_name,
                    sport_key=sport_key,
                    create_if_missing=not dry_run,
                )
                away_team_id = await team_registry.resolve_by_external_id_or_name(
                    source=src,
                    external_id=str(away_team.get("external_id") or "").strip(),
                    name=away_name,
                    sport_key=sport_key,
                    create_if_missing=not dry_run,
                )

                if not home_team_id or not away_team_id:
                    conflict_code = await self._team_conflict_code(src, home_team, away_team)
                    await self._record_alias_suggestions_for_unresolved_teams(
                        team_registry=team_registry,
                        source=src,
                        sport_key=sport_key,
                        league_id=resolved_league_id,
                        league_external_id=str(item.get("league_external_id") or "").strip() or None,
                        match_external_id=external_id,
                        home_team=home_team,
                        away_team=away_team,
                        home_team_id=home_team_id,
                        away_team_id=away_team_id,
                        reason=conflict_code,
                    )
                    self._append_conflict(
                        result,
                        {
                            "code": conflict_code,
                            "source": src,
                            "external_id": external_id,
                            "message": "Team resolution failed.",
                            "detail": {
                                "home": home_team,
                                "away": away_team,
                                "sport_key": sport_key,
                            },
                        },
                    )
                    self._append_preview(result, "skip", src, external_id, None, conflict_code)
                    continue

                existing_doc, matched_by = await self._find_existing_match(
                    src=src,
                    external_id=external_id,
                    league_id=resolved_league_id,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    match_date=match_date,
                )
                if matched_by == "external":
                    result["matched_by_external_id"] += 1
                elif matched_by == "identity":
                    result["matched_by_identity_window"] += 1
                previous_status = str(existing_doc.get("status") or "") if existing_doc else ""

                mapper = status_mapper or STATUS_MAPPERS.get(src) or _generic_status_mapper
                mapped_status = mapper(item.get("status"), item)

                score = item.get("score") or {}
                set_fields: dict[str, Any] = {
                    "league_id": resolved_league_id,
                    "sport_key": sport_key,
                    "season": int(item.get("season") or ensure_utc(match_date).year),
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "match_date": match_date,
                    "match_date_hour": ensure_utc(match_date).replace(minute=0, second=0, microsecond=0),
                    "status": mapped_status,
                    "score": score,
                    "result": _legacy_result_from_score(score),
                    "updated_at": now,
                    "last_updated": now,
                    f"external_ids.{src}": external_id,
                    f"metadata.providers.{src}": dict(item.get("metadata") or {}),
                }
                if src == "theoddsapi":
                    # Temporary compatibility for workers still reading legacy metadata fields.
                    set_fields["metadata.theoddsapi_id"] = external_id
                    set_fields["metadata.source"] = "theoddsapi"
                if item.get("matchday") is not None:
                    set_fields["matchday"] = int(item["matchday"])
                    set_fields["matchday_number"] = int(item["matchday"])

                context: MatchIngestContext = {
                    "source": src,
                    "external_id": external_id,
                    "league_id": resolved_league_id,
                    "season": int(item.get("season") or ensure_utc(match_date).year),
                    # Read-only payload for hooks.
                    "match_data": item,
                }

                if dry_run:
                    action = "update" if existing_doc else "create"
                    if action == "create":
                        result["created"] += 1
                    else:
                        result["updated"] += 1
                    self._append_preview(
                        result,
                        action,
                        src,
                        external_id,
                        str(existing_doc.get("_id")) if existing_doc else None,
                        None,
                    )
                    continue

                query = {"_id": existing_doc["_id"]} if existing_doc else {
                    "league_id": resolved_league_id,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "match_date_hour": ensure_utc(match_date).replace(minute=0, second=0, microsecond=0),
                }
                update_doc = {
                    "$set": set_fields,
                    "$setOnInsert": {
                        "created_at": now,
                        "odds_meta": {"updated_at": None, "version": 0, "markets": {}},
                    },
                }
                write_result = await _db.db.matches.update_one(query, update_doc, upsert=True)
                if write_result.upserted_id is not None:
                    result["created"] += 1
                    match_oid = write_result.upserted_id
                    self._append_preview(result, "create", src, external_id, str(match_oid), None)
                    await self._publish_ingest_events(
                        action="create",
                        match_oid=match_oid,
                        source=src,
                        external_id=external_id,
                        league_id=resolved_league_id,
                        sport_key=sport_key,
                        season=int(item.get("season") or ensure_utc(match_date).year),
                        mapped_status=mapped_status,
                        previous_status=previous_status or None,
                        score=score,
                        changed_fields=["status", "score", "matchday", "match_date"],
                        correlation_id=str(item.get("correlation_id") or make_correlation_id()),
                    )
                    if hooks and hasattr(hooks, "on_match_created"):
                        try:
                            created_doc = await _db.db.matches.find_one({"_id": match_oid}) or {"_id": match_oid}
                            await hooks.on_match_created(created_doc, context)
                        except Exception:
                            logger.warning("match ingest create hook failed source=%s external_id=%s", src, external_id, exc_info=True)
                else:
                    result["updated"] += 1
                    match_oid = existing_doc.get("_id") if existing_doc else None
                    if match_oid is None:
                        resolved = await _db.db.matches.find_one(query, {"_id": 1})
                        match_oid = resolved.get("_id") if resolved else None
                    self._append_preview(result, "update", src, external_id, str(match_oid) if match_oid else None, None)
                    changed_fields = self._compute_changed_fields(
                        existing_doc=existing_doc,
                        mapped_status=mapped_status,
                        score=score,
                        match_date=match_date,
                        matchday=item.get("matchday"),
                    )
                    if match_oid is not None:
                        await self._publish_ingest_events(
                            action="update",
                            match_oid=match_oid,
                            source=src,
                            external_id=external_id,
                            league_id=resolved_league_id,
                            sport_key=sport_key,
                            season=int(item.get("season") or ensure_utc(match_date).year),
                            mapped_status=mapped_status,
                            previous_status=previous_status or None,
                            score=score,
                            changed_fields=changed_fields,
                            correlation_id=str(item.get("correlation_id") or make_correlation_id()),
                        )
                    if hooks and hasattr(hooks, "on_match_updated"):
                        try:
                            updated_doc = await _db.db.matches.find_one({"_id": match_oid}) if match_oid else None
                            await hooks.on_match_updated(updated_doc or {"_id": match_oid}, context)
                        except Exception:
                            logger.warning("match ingest update hook failed source=%s external_id=%s", src, external_id, exc_info=True)

            except Exception as exc:
                self._append_conflict(
                    result,
                    {
                        "code": "other_conflict",
                        "source": src or "unknown",
                        "external_id": external_id,
                        "message": str(exc),
                        "detail": {"type": type(exc).__name__},
                    },
                )
                self._append_preview(result, "skip", src or "unknown", external_id, None, "other_conflict")

        if hooks and hasattr(hooks, "on_batch_completed"):
            try:
                await hooks.on_batch_completed(result, {"league_id": str(league_id) if league_id else None, "dry_run": dry_run})
            except Exception:
                logger.warning("match ingest batch hook failed", exc_info=True)

        return result

    async def _resolve_league_id(
        self,
        source: str,
        match: MatchData,
        *,
        explicit_league_id: ObjectId | None,
    ) -> ObjectId | None:
        if explicit_league_id is not None:
            return explicit_league_id

        league_external_id = str(match.get("league_external_id") or "").strip()
        sport_key = str(match.get("sport_key") or "").strip()
        if not league_external_id:
            return None

        query: dict[str, Any] = {f"external_ids.{source}": league_external_id}
        if sport_key:
            query["sport_key"] = sport_key

        league = await _db.db.leagues.find_one(query, {"_id": 1})
        if league:
            return league["_id"]

        league = await _db.db.leagues.find_one({f"external_ids.{source}": league_external_id}, {"_id": 1})
        if league:
            return league["_id"]
        return None

    async def _find_existing_match(
        self,
        *,
        src: str,
        external_id: str,
        league_id: ObjectId,
        home_team_id: ObjectId,
        away_team_id: ObjectId,
        match_date: datetime,
    ) -> tuple[dict[str, Any] | None, str | None]:
        existing = await _db.db.matches.find_one(
            {f"external_ids.{src}": external_id},
            {"_id": 1, "status": 1, "score": 1, "matchday": 1, "matchday_number": 1, "match_date": 1},
        )
        if existing:
            return existing, "external"

        existing = await _db.db.matches.find_one(
            {
                "league_id": league_id,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "match_date": {
                    "$gte": ensure_utc(match_date) - _IDENTITY_WINDOW,
                    "$lte": ensure_utc(match_date) + _IDENTITY_WINDOW,
                },
            },
            {"_id": 1, "status": 1, "score": 1, "matchday": 1, "matchday_number": 1, "match_date": 1},
        )
        if existing:
            return existing, "identity"
        return None, None

    async def _record_alias_suggestions_for_unresolved_teams(
        self,
        *,
        team_registry: TeamRegistry,
        source: str,
        sport_key: str,
        league_id: ObjectId | None,
        league_external_id: str | None,
        match_external_id: str,
        home_team: dict[str, Any],
        away_team: dict[str, Any],
        home_team_id: ObjectId | None,
        away_team_id: ObjectId | None,
        reason: str,
    ) -> None:
        if home_team_id is None:
            home_name = str(home_team.get("name") or "").strip()
            if home_name:
                await team_registry.record_alias_suggestion(
                    source=source,
                    raw_team_name=home_name,
                    sport_key=sport_key,
                    league_id=league_id,
                    league_external_id=league_external_id,
                    reason=reason,
                    sample_ref={
                        "match_external_id": match_external_id,
                        "side": "home",
                        "external_team_id": str(home_team.get("external_id") or "").strip() or None,
                    },
                )
        if away_team_id is None:
            away_name = str(away_team.get("name") or "").strip()
            if away_name:
                await team_registry.record_alias_suggestion(
                    source=source,
                    raw_team_name=away_name,
                    sport_key=sport_key,
                    league_id=league_id,
                    league_external_id=league_external_id,
                    reason=reason,
                    sample_ref={
                        "match_external_id": match_external_id,
                        "side": "away",
                        "external_team_id": str(away_team.get("external_id") or "").strip() or None,
                    },
                )

    @staticmethod
    def _compute_changed_fields(
        *,
        existing_doc: dict[str, Any] | None,
        mapped_status: str,
        score: dict[str, Any],
        match_date: datetime,
        matchday: Any,
    ) -> list[str]:
        if not existing_doc:
            return ["status", "score", "matchday", "match_date"]

        changed: list[str] = []
        prev_status = str(existing_doc.get("status") or "")
        if prev_status != mapped_status:
            changed.append("status")

        prev_score = existing_doc.get("score") if isinstance(existing_doc.get("score"), dict) else {}
        if prev_score != (score or {}):
            changed.append("score")

        prev_match_date = existing_doc.get("match_date")
        try:
            prev_match_date = ensure_utc(prev_match_date) if prev_match_date is not None else None
        except Exception:
            prev_match_date = None
        if prev_match_date != ensure_utc(match_date):
            changed.append("match_date")

        prev_matchday = existing_doc.get("matchday")
        if prev_matchday is None:
            prev_matchday = existing_doc.get("matchday_number")
        next_matchday = int(matchday) if matchday is not None else None
        if prev_matchday != next_matchday:
            changed.append("matchday")

        return changed

    async def _publish_ingest_events(
        self,
        *,
        action: str,
        match_oid: ObjectId,
        source: str,
        external_id: str,
        league_id: ObjectId,
        sport_key: str,
        season: int,
        mapped_status: str,
        previous_status: str | None,
        score: dict[str, Any],
        changed_fields: list[str],
        correlation_id: str,
    ) -> None:
        if not settings.EVENT_BUS_ENABLED or not self._event_publish_enabled_for_source(source):
            return
        try:
            if action == "create":
                event_bus.publish(
                    MatchCreatedEvent(
                        source=source,
                        correlation_id=correlation_id,
                        match_id=str(match_oid),
                        league_id=str(league_id),
                        sport_key=sport_key,
                        season=int(season),
                        status=mapped_status,
                        ingest_source=source,
                        external_id=external_id,
                    )
                )
            else:
                if not changed_fields:
                    return
                event_bus.publish(
                    MatchUpdatedEvent(
                        source=source,
                        correlation_id=correlation_id,
                        match_id=str(match_oid),
                        league_id=str(league_id),
                        sport_key=sport_key,
                        season=int(season),
                        previous_status=previous_status,
                        new_status=mapped_status,
                        ingest_source=source,
                        external_id=external_id,
                        changed_fields=changed_fields,
                    )
                )

            if mapped_status == "final" and previous_status != "final":
                ft = score.get("full_time") if isinstance(score, dict) and isinstance(score.get("full_time"), dict) else {}
                event_bus.publish(
                    MatchFinalizedEvent(
                        source=source,
                        correlation_id=correlation_id,
                        match_id=str(match_oid),
                        league_id=str(league_id),
                        sport_key=sport_key,
                        season=int(season),
                        final_score={
                            "home": ft.get("home"),
                            "away": ft.get("away"),
                        },
                    )
                )
            elif mapped_status == "postponed" and previous_status != "postponed":
                event_bus.publish(
                    MatchPostponedEvent(
                        source=source,
                        correlation_id=correlation_id,
                        match_id=str(match_oid),
                        league_id=str(league_id),
                        sport_key=sport_key,
                        season=int(season),
                    )
                )
            elif mapped_status == "canceled" and previous_status != "canceled":
                event_bus.publish(
                    MatchCancelledEvent(
                        source=source,
                        correlation_id=correlation_id,
                        match_id=str(match_oid),
                        league_id=str(league_id),
                        sport_key=sport_key,
                        season=int(season),
                    )
                )
        except Exception:
            logger.warning(
                "Failed to publish ingest events match_id=%s source=%s external_id=%s",
                str(match_oid),
                source,
                external_id,
                exc_info=True,
            )

    @staticmethod
    def _event_publish_enabled_for_source(source: str) -> bool:
        attr = _EVENT_PUBLISH_BY_SOURCE.get(str(source or "").strip().lower())
        if not attr:
            return True
        return bool(getattr(settings, attr, True))

    async def _team_conflict_code(self, src: str, home_team: dict[str, Any], away_team: dict[str, Any]) -> str:
        for team in (home_team, away_team):
            ext_id = str(team.get("external_id") or "").strip()
            if not ext_id:
                continue
            existing = await _db.db.teams.find_one({f"external_ids.{src}": ext_id}, {"_id": 1})
            if existing:
                return "team_name_conflict"
        return "unresolved_team"

    @staticmethod
    def _append_preview(
        result: IngestResult,
        action: str,
        source: str,
        external_id: str,
        match_id: str | None,
        code: str | None,
    ) -> None:
        if len(result["items_preview"]) >= _PREVIEW_LIMIT:
            return
        result["items_preview"].append(
            {
                "action": action,
                "source": source,
                "external_id": external_id,
                "match_id": match_id,
                "code": code,
            }
        )

    @staticmethod
    def _append_conflict(result: IngestResult, conflict: IngestConflict) -> None:
        result["skipped"] += 1
        result["conflicts"] += 1
        code = conflict["code"]
        if code == "unresolved_league":
            result["unresolved_league"] += 1
        elif code == "unresolved_team":
            result["unresolved_team"] += 1
        elif code == "team_name_conflict":
            result["team_name_conflict"] += 1
        else:
            result["other_conflicts"] += 1

        if len(result["conflicts_preview"]) < _CONFLICT_PREVIEW_LIMIT:
            result["conflicts_preview"].append(conflict)


match_ingest_service = MatchIngestService()
