"""
backend/app/services/sportmonks_connector.py

Purpose:
    Greenfield v3 Sportmonks connector for league discovery sync, season ingest,
    and admin job progress/error tracking with rate-limit safety.

Dependencies:
    - app.database
    - app.providers.sportmonks
    - app.config
    - app.utils.utcnow
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from collections import defaultdict
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne

import app.database as _db
from app.config import settings
from app.providers.sportmonks import sportmonks_provider
from app.services.matchday_v3_cache_service import invalidate_matchday_list_cache_for_season
from app.services.team_alias_normalizer import normalize_team_alias
from app.utils import ensure_utc, parse_utc, utcnow

logger = logging.getLogger("quotico.sportmonks_connector")

CRITICAL_MANUAL_CHECK_REASONS = frozenset(
    {
        "finished_without_scores",
        "walkover_with_scores",
        "postponed_with_scores",
    }
)


class SportmonksConnector:
    """Connector for v3 sync + ingest with immutable created_at semantics."""

    def __init__(self, database=None) -> None:
        self._db = database
        self._remaining: int | None = None
        self._reset_at: int | None = None
        self._request_windows: dict[str, dict[str, list[Any]]] = {}

    @property
    def db(self):
        """Resolve DB lazily so module-level singleton works before connect_db()."""
        resolved = self._db or _db.db
        if resolved is None:
            raise RuntimeError("Database is not initialized yet.")
        return resolved

    @staticmethod
    def _norm_name(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _first_non_empty(*values: Any) -> str | None:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return None

    @staticmethod
    def _to_int(value: Any, default: int | None = None) -> int | None:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_slug(value: Any) -> str:
        raw = str(value or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        return slug

    @staticmethod
    def _stamp_updated_fields(target: dict[str, Any], now=None) -> dict[str, Any]:
        ts = now or utcnow()
        target["updated_at"] = ts
        target["updated_at_utc"] = ts
        return target

    def _extract_sport_key(self, row: dict[str, Any]) -> str | None:
        sport = (row or {}).get("sport")
        sport_name = self._first_non_empty((sport or {}).get("name"))
        country_name = self._first_non_empty(((row or {}).get("country") or {}).get("name"))
        league_name = self._first_non_empty((row or {}).get("name"))
        if not sport_name or not country_name or not league_name:
            return None
        sport_slug = self._to_slug(sport_name)
        country_slug = self._to_slug(country_name)
        league_slug = self._to_slug(league_name)
        if not sport_slug or not country_slug or not league_slug:
            return None
        return f"{sport_slug}_{country_slug}_{league_slug}"

    def _recompute_manual_check_fields(self, reasons: list[str] | None) -> dict[str, Any]:
        """Normalize reasons and derive manual_check_required from critical reasons."""
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in reasons or []:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        requires_manual = bool(set(normalized) & set(CRITICAL_MANUAL_CHECK_REASONS))
        return {
            "manual_check_reasons": normalized,
            "manual_check_required": requires_manual,
        }

    def _manual_check_reasons_expr(self, *, clear_reasons: list[str] | None = None) -> dict[str, Any]:
        reasons_expr: dict[str, Any] = {"$ifNull": ["$manual_check_reasons", []]}
        to_clear = [str(value).strip() for value in (clear_reasons or []) if str(value).strip()]
        if to_clear:
            reasons_expr = {"$setDifference": [reasons_expr, to_clear]}
        return {"$setUnion": [reasons_expr, []]}

    async def _apply_match_manual_check_update(
        self,
        fixture_id: int,
        *,
        set_fields: dict[str, Any],
        clear_reasons: list[str] | None = None,
        timeline_entry: dict[str, Any] | None = None,
    ) -> None:
        """Atomic helper for match updates that also heals/recomputes manual-check fields."""
        match_id = int(fixture_id)
        now = utcnow()
        stage_set = dict(set_fields)
        stage_set.setdefault("updated_at", now)
        stage_set.setdefault("updated_at_utc", now)
        reasons_expr = self._manual_check_reasons_expr(clear_reasons=clear_reasons)
        stage_set["manual_check_reasons"] = reasons_expr
        stage_set["manual_check_required"] = {
            "$gt": [
                {
                    "$size": {
                        "$setIntersection": [
                            reasons_expr,
                            sorted(CRITICAL_MANUAL_CHECK_REASONS),
                        ]
                    }
                },
                0,
            ]
        }
        if timeline_entry is not None:
            stage_set["odds_timeline"] = {
                "$concatArrays": [
                    {"$ifNull": ["$odds_timeline", []]},
                    [timeline_entry],
                ]
            }
        try:
            await self.db.matches_v3.update_one({"_id": match_id}, [{"$set": stage_set}])
            return
        except Exception:
            # Fallback for environments without update-pipeline support.
            doc = await self.db.matches_v3.find_one(
                {"_id": match_id},
                {"manual_check_reasons": 1},
            )
            current_reasons = list((doc or {}).get("manual_check_reasons") or [])
            if clear_reasons:
                cleared = {str(value).strip() for value in clear_reasons if str(value).strip()}
                current_reasons = [value for value in current_reasons if str(value).strip() not in cleared]
            recomputed = self._recompute_manual_check_fields(current_reasons)
            update_doc: dict[str, Any] = {
                "$set": {
                    **set_fields,
                    "updated_at": stage_set["updated_at"],
                    "updated_at_utc": stage_set["updated_at_utc"],
                    "manual_check_reasons": recomputed["manual_check_reasons"],
                    "manual_check_required": recomputed["manual_check_required"],
                }
            }
            if timeline_entry is not None:
                update_doc["$push"] = {
                    "odds_timeline": {
                        "$each": [timeline_entry],
                    }
                }
            await self.db.matches_v3.update_one({"_id": match_id}, update_doc)

    async def upsert_match_v3(self, fixture_sm_id: int, match_data_v3: dict[str, Any]) -> None:
        await self._upsert_v3_document(
            collection=self.db.matches_v3,
            doc_id=int(fixture_sm_id),
            payload=match_data_v3,
        )

    async def upsert_person(self, person_sm_id: int, person_data: dict[str, Any]) -> None:
        await self._upsert_v3_document(
            collection=self.db.persons,
            doc_id=int(person_sm_id),
            payload=person_data,
        )

    async def upsert_league_registry_v3(self, league_sm_id: int, league_data: dict[str, Any]) -> None:
        await self._upsert_v3_document(
            collection=self.db.league_registry_v3,
            doc_id=int(league_sm_id),
            payload=league_data,
        )

    async def get_available_leagues(self) -> dict[str, Any]:
        """Fetch leagues from Sportmonks and return normalized list + headers."""
        response = await sportmonks_provider.get_leagues_with_seasons_country()
        self._remaining = self._to_int(response.get("remaining"))
        self._reset_at = self._to_int(response.get("reset_at"))
        rows = (response.get("payload") or {}).get("data") or []
        items: list[dict[str, Any]] = []
        for row in rows:
            league_id = self._to_int((row or {}).get("id"))
            if league_id is None:
                continue
            country_node = (row or {}).get("country") or {}
            seasons_raw = (row or {}).get("seasons") or []
            seasons: list[dict[str, Any]] = []
            seen: set[int] = set()
            for season in seasons_raw:
                sid = self._to_int((season or {}).get("id"))
                if sid is None or sid in seen:
                    continue
                seen.add(sid)
                seasons.append(
                    {
                        "id": sid,
                        "name": self._norm_name((season or {}).get("name") or (season or {}).get("display_name")),
                    }
                )
            items.append(
                {
                    "_id": league_id,
                    "sport_key": self._extract_sport_key(row) or "",
                    "name": self._norm_name((row or {}).get("name")),
                    "country": self._norm_name(country_node.get("name")),
                    "is_cup": bool((row or {}).get("is_cup", False)),
                    "available_seasons": seasons,
                }
            )
        return {
            "items": self._sort_discovery_items(items),
            "remaining": self._remaining,
            "reset_at": self._reset_at,
        }

    def _sort_discovery_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        top = {"bundesliga", "premier league", "la liga", "serie a", "ligue 1"}

        def _rank(item: dict[str, Any]) -> tuple[int, str, str]:
            name = str(item.get("name") or "").strip().lower()
            country = str(item.get("country") or "").strip().lower()
            return (0 if name in top else 1, country, name)

        return sorted(items, key=_rank)

    async def sync_leagues_to_registry(self, items: list[dict[str, Any]]) -> dict[str, int]:
        now = utcnow()
        updated = 0
        inserted = 0
        rejected = 0
        for item in items:
            lid = self._to_int(item.get("_id"))
            if lid is None:
                rejected += 1
                continue
            existing = await self.db.league_registry_v3.find_one(
                {"_id": lid},
                {"available_seasons": 1, "sport_key": 1, "is_active": 1, "features": 1, "ui_order": 1},
            )
            if not isinstance(existing, dict):
                logger.warning("Discovery league rejected: missing runtime fields for _id=%s (not pre-provisioned).", lid)
                rejected += 1
                continue
            if not isinstance(existing.get("sport_key"), str) or not str(existing.get("sport_key")).strip():
                logger.warning("Discovery league rejected: missing sport_key for _id=%s.", lid)
                rejected += 1
                continue
            # Auto-provision missing runtime fields with safe defaults
            if not isinstance(existing.get("features"), dict):
                logger.info("Discovery: auto-provisioning default features for _id=%s.", lid)
                existing["features"] = {"tipping": False, "match_load": False, "xg_sync": False, "odds_sync": False}
            if not isinstance(existing.get("is_active"), bool):
                logger.info("Discovery: auto-provisioning is_active=false for _id=%s.", lid)
                existing["is_active"] = False
            if not isinstance(existing.get("ui_order"), int):
                logger.info("Discovery: auto-provisioning ui_order=999 for _id=%s.", lid)
                existing["ui_order"] = 999
            merged_seasons = self._merge_seasons(existing, item.get("available_seasons") or [])
            payload: dict[str, Any] = {
                "name": self._norm_name(item.get("name")),
                "country": self._norm_name(item.get("country")),
                "is_cup": bool(item.get("is_cup", False)),
                "available_seasons": merged_seasons,
                "features": existing["features"],
                "is_active": existing["is_active"],
                "ui_order": existing["ui_order"],
                "last_synced_at": now,
                "updated_at": now,
                "updated_at_utc": now,
            }
            result = await self.db.league_registry_v3.update_one(
                {"_id": lid},
                {
                    "$set": payload,
                },
                upsert=False,
            )
            if result.upserted_id is not None:
                inserted += 1
            else:
                updated += 1
        return {"inserted": inserted, "updated": updated, "rejected": rejected}

    def _merge_seasons(self, existing: dict[str, Any] | None, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing_rows = ((existing or {}).get("available_seasons") or []) if isinstance(existing, dict) else []
        merged: dict[int, dict[str, Any]] = {}
        for row in existing_rows:
            sid = self._to_int((row or {}).get("id"))
            if sid is None:
                continue
            merged[sid] = {"id": sid, "name": self._norm_name((row or {}).get("name"))}
        for row in incoming:
            sid = self._to_int((row or {}).get("id"))
            if sid is None:
                continue
            merged[sid] = {"id": sid, "name": self._norm_name((row or {}).get("name"))}
        return sorted(merged.values(), key=lambda x: int(x["id"]), reverse=True)

    async def ingest_season(self, season_id: int, *, job_id: ObjectId | None = None) -> dict[str, Any]:
        sync_odds = bool(settings.SPORTMONKS_DEEP_INGEST_SYNC_ODDS)
        max_runtime_minutes = int(settings.SPORTMONKS_MAX_RUNTIME_DEEP_MINUTES)
        timeout_at = utcnow() + timedelta(minutes=max_runtime_minutes)
        await self._job_update(job_id, status="running", phase="loading_rounds", started_at=utcnow())
        await self._job_update(
            job_id,
            timeout_at=timeout_at,
            max_runtime_minutes=max_runtime_minutes,
            page_requests_total=0,
            duplicate_page_blocks=0,
            phase_page_requests={},
        )
        await self._guard_before_external_call(
            job_id=job_id,
            phase="loading_rounds",
            request_key=f"rounds:season:{int(season_id)}",
        )
        rounds_response = await sportmonks_provider.get_season_rounds(int(season_id))
        self._remaining = self._to_int(rounds_response.get("remaining"))
        self._reset_at = self._to_int(rounds_response.get("reset_at"))
        rounds = (rounds_response.get("payload") or {}).get("data") or []
        total_rounds = len(rounds)
        processed_rounds = 0
        counters = {"matches_upserted": 0, "persons_upserted": 0, "teams_upserted": 0, "odds_synced": 0}
        await self._job_update(
            job_id,
            total_rounds=total_rounds,
            processed_rounds=0,
            rate_limit_remaining=self._remaining,
            rate_limit_paused=False,
            counters=counters,
        )

        try:
            for round_row in rounds:
                round_id = self._to_int((round_row or {}).get("id"))
                if round_id is None:
                    continue
                round_name = self._norm_name((round_row or {}).get("name") or f"Round {round_id}")
                teams_bulk_ops: list[UpdateOne] = []
                seen_team_ids: set[int] = set()
                await self._pause_if_needed(job_id=job_id)
                await self._job_update(
                    job_id,
                    phase="ingesting_round",
                    current_round_name=round_name,
                    rate_limit_remaining=self._remaining,
                    rate_limit_paused=False,
                )

                await self._guard_before_external_call(
                    job_id=job_id,
                    phase="ingesting_round",
                    request_key=f"round:{int(round_id)}:include_odds:{int(sync_odds)}",
                )
                fixtures_response = await sportmonks_provider.get_round_fixtures(
                    round_id,
                    include_odds=sync_odds,
                )
                self._remaining = self._to_int(fixtures_response.get("remaining"))
                self._reset_at = self._to_int(fixtures_response.get("reset_at"))
                fixtures = (fixtures_response.get("payload") or {}).get("data") or []
                for fixture in fixtures:
                    fixture_id = self._to_int((fixture or {}).get("id"))
                    if fixture_id is None:
                        continue
                    try:
                        round_team_ops = self._build_team_upsert_ops_from_fixture(
                            fixture,
                            seen_team_ids=seen_team_ids,
                        )
                        if round_team_ops:
                            teams_bulk_ops.extend(round_team_ops)
                        people_count = await self._sync_people_from_fixture(fixture)
                        await self.upsert_match_v3(fixture_id, self._map_fixture_to_match(fixture, season_id))
                        await self._pause_if_needed(job_id=job_id)
                        odds_saved = False
                        if sync_odds:
                            odds_saved = await self.sync_fixture_odds_summary(
                                fixture_id,
                                job_id=job_id,
                                phase="deep_odds_sync",
                            )
                        counters["matches_upserted"] += 1
                        counters["persons_upserted"] += people_count
                        counters["odds_synced"] += 1 if odds_saved else 0
                    except Exception as row_exc:
                        await self._append_job_error(
                            job_id=job_id,
                            round_id=round_id,
                            message=str(row_exc),
                            trace=self._safe_trace(),
                        )
                if teams_bulk_ops:
                    await self.db.teams_v3.bulk_write(teams_bulk_ops, ordered=False)
                    counters["teams_upserted"] += len(teams_bulk_ops)
                processed_rounds += 1
                percent = round((processed_rounds / total_rounds) * 100.0, 2) if total_rounds else 0.0
                await self._job_update(
                    job_id,
                    processed_rounds=processed_rounds,
                    total_rounds=total_rounds,
                    progress={"processed": processed_rounds, "total": total_rounds, "percent": percent},
                    rate_limit_remaining=self._remaining,
                    current_round_name=round_name,
                    counters=counters,
                )
            counters["xg_matches_synced"] = 0
        except Exception as exc:
            await self._append_job_error(
                job_id=job_id,
                round_id=None,
                message=str(exc),
                trace=self._safe_trace(),
            )
            await self._job_update(
                job_id,
                status="failed",
                phase="failed",
                active_lock=False,
                finished_at=utcnow(),
                error={"message": str(exc), "type": type(exc).__name__},
            )
            self._clear_request_window(job_id)
            raise

        await self._job_update(
            job_id,
            status="succeeded",
            phase="done",
            active_lock=False,
            finished_at=utcnow(),
            results={
                "season_id": int(season_id),
                "processed_rounds": processed_rounds,
                "total_rounds": total_rounds,
                **counters,
            },
        )
        try:
            await invalidate_matchday_list_cache_for_season(season_id=int(season_id))
        except Exception:
            pass
        self._clear_request_window(job_id)
        return {
            "season_id": int(season_id),
            "processed_rounds": processed_rounds,
            "total_rounds": total_rounds,
            **counters,
        }

    async def run_metrics_sync(self, season_id: int, *, job_id: ObjectId | None = None) -> dict[str, int]:
        """Run dedicated metrics sync (bulk odds -> repair -> paginated xG) for a season."""
        max_runtime_minutes = int(settings.SPORTMONKS_MAX_RUNTIME_METRICS_MINUTES)
        timeout_at = utcnow() + timedelta(minutes=max_runtime_minutes)
        await self._job_update(
            job_id,
            status="running",
            phase="metrics_sync",
            started_at=utcnow(),
            timeout_at=timeout_at,
            max_runtime_minutes=max_runtime_minutes,
            page_requests_total=0,
            duplicate_page_blocks=0,
            phase_page_requests={},
            pages_processed=0,
            pages_total=None,
            rows_processed=0,
        )
        await self._guard_before_external_call(
            job_id=job_id,
            phase="metrics_loading_rounds",
            request_key=f"rounds:season:{int(season_id)}",
        )
        rounds_response = await sportmonks_provider.get_season_rounds(int(season_id))
        self._remaining = self._to_int(rounds_response.get("remaining"))
        self._reset_at = self._to_int(rounds_response.get("reset_at"))
        rounds = (rounds_response.get("payload") or {}).get("data") or []
        total_rounds = len(rounds)

        bulk_round_calls = 0
        bulk_fixtures_processed = 0
        repair_candidates: set[int] = set()
        seen_fixture_ids: set[int] = set()
        for idx, round_row in enumerate(rounds, start=1):
            round_id = self._to_int((round_row or {}).get("id"))
            if round_id is None:
                continue
            await self._pause_if_needed(job_id=job_id)
            bulk = await self.sync_round_odds_summary(round_id=int(round_id), season_id=int(season_id), job_id=job_id)
            bulk_round_calls += int(bulk.get("bulk_calls") or 0)
            bulk_fixtures_processed += int(bulk.get("fixtures_processed") or 0)
            seen_fixture_ids.update({int(fid) for fid in (bulk.get("fixture_ids") or [])})
            repair_candidates.update({int(fid) for fid in (bulk.get("repair_candidates") or [])})
            await self._job_update(
                job_id,
                phase="metrics_bulk_odds",
                processed_rounds=idx,
                total_rounds=total_rounds,
                progress={
                    "processed": idx,
                    "total": total_rounds,
                    "percent": round((idx / total_rounds) * 100.0, 2) if total_rounds else 0.0,
                },
                rate_limit_remaining=self._remaining,
            )

        repair_calls = 0
        repair_success = 0
        repair_failures = 0
        docs = await self.db.matches_v3.find(
            {"season_id": int(season_id)},
            {"_id": 1, "start_at": 1, "odds_meta": 1},
        ).to_list(length=250_000)
        for idx, doc in enumerate(docs, start=1):
            fixture_id = self._to_int((doc or {}).get("_id"))
            if fixture_id is None:
                continue
            if not self.needs_odds_repair(doc, force_missing_market=(int(fixture_id) in repair_candidates)):
                continue
            await self._pause_if_needed(job_id=job_id)
            repair_calls += 1
            try:
                if await self.sync_fixture_odds_summary(
                    int(fixture_id),
                    source="sportmonks_fixture_repair",
                    job_id=job_id,
                    phase="metrics_repair",
                ):
                    repair_success += 1
            except Exception as exc:
                repair_failures += 1
                await self._append_job_error(
                    job_id=job_id,
                    round_id=None,
                    message=f"repair failed fixture={fixture_id}: {exc}",
                    trace=self._safe_trace(),
                )
            if idx % 25 == 0 or idx == len(docs):
                await self._job_update(
                    job_id,
                    phase="metrics_repair",
                    processed_rounds=idx,
                    total_rounds=len(docs),
                    progress={
                        "processed": idx,
                        "total": len(docs),
                        "percent": round((idx / len(docs)) * 100.0, 2) if docs else 0.0,
                    },
                )

        # xG sync skipped — the background xG crawler (two-pointer walker)
        # continuously fills xg_raw and resolves to matches_v3.
        xg_result: dict[str, int] = {"skipped": True}
        total_fixtures = len(seen_fixture_ids) if seen_fixture_ids else len(docs)
        saved_calls_estimate = max(0, int(total_fixtures) - (int(bulk_round_calls) + int(repair_calls)))
        savings_ratio = round((saved_calls_estimate / int(total_fixtures)), 4) if int(total_fixtures) > 0 else 0.0

        # Post-sync data guard: flag cross-pipeline issues (xG/odds)
        guard_result = await self._post_sync_data_guard(int(season_id))

        result = {
            "season_id": int(season_id),
            "bulk_round_calls": int(bulk_round_calls),
            "bulk_fixtures_processed": int(bulk_fixtures_processed),
            "repair_candidates": int(len(repair_candidates)),
            "repair_calls": int(repair_calls),
            "repair_success": int(repair_success),
            "repair_failures": int(repair_failures),
            "total_fixtures": int(total_fixtures),
            "saved_calls_estimate": int(saved_calls_estimate),
            "api_savings_ratio": savings_ratio,
            "fixtures_seen": int(xg_result.get("fixtures_seen") or 0),
            "xg_matches_synced": int(xg_result.get("matches_synced") or 0),
            "xg_partial_warnings": int(xg_result.get("partial_warnings") or 0),
            "odds_matches_synced": int(bulk_fixtures_processed + repair_success),
            "odds_errors": int(repair_failures),
            "total_matches": int(total_fixtures),
            "guard_xg_flagged": int(guard_result.get("xg_flagged") or 0),
            "guard_odds_flagged": int(guard_result.get("odds_flagged") or 0),
        }
        await self._job_update(
            job_id,
            status="succeeded",
            phase="done",
            active_lock=False,
            finished_at=utcnow(),
            results=result,
        )
        try:
            await invalidate_matchday_list_cache_for_season(season_id=int(season_id))
        except Exception:
            pass
        self._clear_request_window(job_id)
        return result

    async def sync_round_odds_summary(
        self,
        *,
        round_id: int,
        season_id: int,
        job_id: ObjectId | None = None,
    ) -> dict[str, Any]:
        """Bulk-sync odds summaries for one round from fixtures.odds include."""
        await self._guard_before_external_call(
            job_id=job_id,
            phase="metrics_bulk_odds",
            request_key=f"round:{int(round_id)}:include_odds:1",
        )
        response = await sportmonks_provider.get_round_fixtures(int(round_id))
        self._remaining = self._to_int(response.get("remaining"))
        self._reset_at = self._to_int(response.get("reset_at"))
        fixtures = (response.get("payload") or {}).get("data") or []
        repair_candidates: list[int] = []
        fixture_ids: list[int] = []
        processed = 0
        for fixture in fixtures:
            fixture_id = self._to_int((fixture or {}).get("id"))
            if fixture_id is None:
                continue
            fixture_ids.append(int(fixture_id))
            try:
                odds_rows = (fixture or {}).get("odds") or []
                summary, has_market_1 = self._build_1x2_summary_from_rows(odds_rows)
                del odds_rows
                now = utcnow()
                update_fields: dict[str, Any] = self._stamp_updated_fields({}, now=now)
                push_fields: dict[str, Any] = {}
                if summary:
                    update_fields["odds_meta.summary_1x2"] = summary
                    update_fields["odds_meta.source"] = "sportmonks_round_bulk"
                    update_fields["odds_meta.updated_at"] = now
                    update_fields["odds_meta.updated_at_utc"] = now
                    existing = await self.db.matches_v3.find_one(
                        {"_id": int(fixture_id)},
                        {"odds_timeline": 1, "start_at": 1, "odds_meta.fixed_snapshots": 1},
                    )
                    entropy = self._compute_market_entropy(summary, (existing or {}).get("odds_timeline"), now)
                    update_fields["odds_meta.market_entropy"] = entropy
                    if self._should_append_odds_timeline(existing, summary, now):
                        push_fields["odds_timeline"] = {
                            "$each": [self._build_timeline_entry(summary=summary, source="sportmonks_round_bulk", ts=now)],
                        }
                else:
                    repair_candidates.append(int(fixture_id))
                update_doc: dict[str, Any] = {"$set": update_fields}
                if push_fields:
                    update_doc["$push"] = push_fields
                await self.db.matches_v3.update_one(
                    {"_id": int(fixture_id), "season_id": int(season_id)},
                    update_doc,
                )
                # v3.2: try to lock fixed snapshot anchors
                if summary:
                    _start_at = (existing or {}).get("start_at")
                    await self._try_set_fixed_snapshots(
                        int(fixture_id), summary, now,
                        start_at=ensure_utc(_start_at) if _start_at else None,
                        existing_snapshots=((existing or {}).get("odds_meta") or {}).get("fixed_snapshots"),
                    )
                if not has_market_1:
                    repair_candidates.append(int(fixture_id))
                processed += 1
            except Exception as exc:
                repair_candidates.append(int(fixture_id))
                await self._append_job_error(
                    job_id=job_id,
                    round_id=int(round_id),
                    message=f"bulk odds parse failed fixture={fixture_id}: {exc}",
                    trace=self._safe_trace(),
                )
            try:
                del fixture["odds"]
            except Exception:
                pass
        return {
            "bulk_calls": 1,
            "fixtures_processed": processed,
            "repair_candidates": sorted(set(repair_candidates)),
            "fixture_ids": fixture_ids,
        }

    async def sync_season_xg(self, season_id: int, *, job_id: ObjectId | None = None) -> dict[str, int]:
        """Sync xG via a local mirror collection (xg_raw), then resolve to matches.

        The Sportmonks /football/expected/fixtures endpoint returns xG for ALL
        leagues/seasons globally (no filter, 25 rows/page, newest-first).
        Hundreds of leagues × ~300 matches/season = millions of rows.

        Strategy — mirror then match:
        Phase A: Append new rows from the API into db.xg_raw (stop at last seen _id).
                 First run does the heavy lift; subsequent runs grab 1-2 pages.
        Phase B: For the requested season, join xg_raw against matches_v3 to
                 write home/away xG on any FINISHED match still missing it.
        """
        # ── Phase A: mirror new xG rows into xg_raw ─────────────────────
        # Find the highest row_id we already have
        latest_doc = await self.db.xg_raw.find_one(sort=[("_id", -1)])
        high_water = int((latest_doc or {}).get("_id") or 0)

        current_page = 1
        page_count = 0
        rows_stored = 0
        max_pages = 50  # quick front-catch only; background crawler handles backfill
        hit_existing = False

        while page_count < max_pages:
            await self._guard_before_external_call(
                job_id=job_id,
                phase="metrics_xg_sync",
                request_key=f"expected:page:{int(current_page)}",
            )
            page = await sportmonks_provider.get_expected_fixtures_page(
                page=current_page,
            )
            self._remaining = self._to_int(page.get("remaining"))
            self._reset_at = self._to_int(page.get("reset_at"))
            payload = page.get("payload") or {}
            rows = payload.get("data") or []
            if not isinstance(rows, list) or not rows:
                break
            page_count += 1

            batch: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = self._to_int(row.get("id")) or 0
                if row_id <= high_water:
                    hit_existing = True
                    break
                batch.append({
                    "_id": row_id,
                    "fixture_id": self._to_int(row.get("fixture_id")),
                    "type_id": self._to_int(row.get("type_id")),
                    "participant_id": self._to_int(row.get("participant_id")),
                    "location": str(row.get("location") or "").strip().lower(),
                    "value": ((row.get("data") or {}).get("value") if isinstance(row.get("data"), dict) else None),
                })

            if batch:
                try:
                    await self.db.xg_raw.insert_many(batch, ordered=False)
                except Exception:
                    # Duplicate-key errors are expected at overlap boundaries
                    pass
                rows_stored += len(batch)

            await self._job_update(
                job_id,
                phase="metrics_xg_sync",
                pages_processed=page_count,
                pages_total=None,
                rows_processed=rows_stored,
                rate_limit_remaining=self._remaining,
            )

            if hit_existing:
                break
            pagination = payload.get("pagination") or {}
            if not bool(pagination.get("has_more")):
                break
            current_page += 1

        # ── Phase B: resolve xG for this season's matches ────────────────
        season_fixtures: dict[int, dict[str, int | None]] = {}
        async for doc in self.db.matches_v3.find(
            {"season_id": int(season_id), "has_advanced_stats": {"$ne": True}, "status": "FINISHED"},
            {"_id": 1, "teams.home.sm_id": 1, "teams.away.sm_id": 1},
        ):
            fid = self._to_int((doc or {}).get("_id"))
            if fid is None:
                continue
            teams = (doc.get("teams") or {}) if isinstance(doc.get("teams"), dict) else {}
            season_fixtures[int(fid)] = {
                "home_sm_id": self._to_int(((teams.get("home") or {}).get("sm_id")) if isinstance(teams.get("home"), dict) else None),
                "away_sm_id": self._to_int(((teams.get("away") or {}).get("sm_id")) if isinstance(teams.get("away"), dict) else None),
            }

        matches_synced = 0
        partial_warnings = 0
        for fid, team_lookup in season_fixtures.items():
            xg_rows = await self.db.xg_raw.find(
                {"fixture_id": int(fid), "type_id": 5304},
            ).to_list(length=10)
            if not xg_rows:
                continue
            synced, partial = await self._write_fixture_xg(
                fixture_id=fid,
                xg_rows=xg_rows,
                team_lookup=team_lookup,
                job_id=job_id,
                season_id=season_id,
            )
            matches_synced += synced
            partial_warnings += partial

        return {
            "fixtures_seen": len(season_fixtures),
            "matches_synced": matches_synced,
            "partial_warnings": partial_warnings,
            "pages_processed": page_count,
            "rows_processed": rows_stored,
            "hit_watermark": hit_existing,
            "watermark_row_id": high_water,
        }

    async def _write_fixture_xg(
        self,
        *,
        fixture_id: int,
        xg_rows: list[dict[str, Any]],
        team_lookup: dict[str, int | None],
        job_id: ObjectId | None,
        season_id: int,
    ) -> tuple[int, int]:
        """Write xG values for a single fixture. Returns (synced, partial_warnings)."""
        home_id = team_lookup.get("home_sm_id")
        away_id = team_lookup.get("away_sm_id")
        home_xg: float | None = None
        away_xg: float | None = None
        for row in xg_rows:
            location = str((row or {}).get("location") or "").strip().lower()
            participant_id = self._to_int((row or {}).get("participant_id"))
            # xg_raw stores value flat; raw API nests under data.value
            value_raw = (row or {}).get("value")
            if value_raw is None:
                value_raw = ((row or {}).get("data") or {}).get("value") if isinstance((row or {}).get("data"), dict) else None
            try:
                value = float(value_raw)
            except (TypeError, ValueError):
                continue
            if location == "home":
                home_xg = value
            elif location == "away":
                away_xg = value
            elif participant_id is not None and participant_id == home_id:
                home_xg = value
            elif participant_id is not None and participant_id == away_id:
                away_xg = value

        update_fields: dict[str, Any] = {}
        if home_xg is not None:
            update_fields["teams.home.xg"] = home_xg
        if away_xg is not None:
            update_fields["teams.away.xg"] = away_xg
        partial = 0
        clear_reasons: list[str] = []
        if home_xg is not None and away_xg is not None:
            update_fields["has_advanced_stats"] = True
            clear_reasons = ["finished_without_xg"]
        elif home_xg is not None or away_xg is not None:
            partial = 1
            missing_side = "away" if home_xg is not None else "home"
            await self._append_job_error(
                job_id=job_id,
                round_id=None,
                message=(
                    f"warning: partial xg mapping fixture={fixture_id} "
                    f"season_id={int(season_id)} missing_side={missing_side}"
                ),
                trace="",
            )
        if not update_fields:
            return 0, partial
        await self._apply_match_manual_check_update(
            int(fixture_id),
            set_fields=update_fields,
            clear_reasons=clear_reasons,
        )
        return 1, partial

    async def sync_fixture_odds_summary(
        self,
        fixture_id: int,
        *,
        source: str = "sportmonks_pre_match",
        job_id: ObjectId | None = None,
        phase: str = "metrics_repair",
    ) -> bool:
        """Store compact 1X2 odds snapshot in matches_v3.odds_meta."""
        await self._guard_before_external_call(
            job_id=job_id,
            phase=phase,
            request_key=f"prematch-odds:fixture:{int(fixture_id)}",
        )
        response = await sportmonks_provider.get_prematch_odds_by_fixture(int(fixture_id))
        self._remaining = self._to_int(response.get("remaining"))
        self._reset_at = self._to_int(response.get("reset_at"))
        rows = (response.get("payload") or {}).get("data") or []
        if not isinstance(rows, list) or not rows:
            return False

        summary, _ = self._build_1x2_summary_from_rows(rows)
        del rows
        if not summary:
            return False

        now = utcnow()
        existing = await self.db.matches_v3.find_one(
            {"_id": int(fixture_id)},
            {"odds_timeline": 1, "start_at": 1, "odds_meta.fixed_snapshots": 1},
        )
        entropy = self._compute_market_entropy(summary, (existing or {}).get("odds_timeline"), now)
        update_doc: dict[str, Any] = {
            "$set": {
                "odds_meta.summary_1x2": summary,
                "odds_meta.source": source,
                "odds_meta.updated_at": now,
                "odds_meta.updated_at_utc": now,
                "odds_meta.market_entropy": entropy,
                "updated_at": now,
                "updated_at_utc": now,
            }
        }
        timeline_entry = None
        if self._should_append_odds_timeline(existing, summary, now):
            timeline_entry = self._build_timeline_entry(summary=summary, source=source, ts=now)
        clear_reasons: list[str] = []
        if self._summary_has_valid_odds(summary):
            clear_reasons.append("finished_without_odds")
        await self._apply_match_manual_check_update(
            int(fixture_id),
            set_fields=update_doc["$set"],
            clear_reasons=clear_reasons,
            timeline_entry=timeline_entry,
        )
        # v3.2: try to lock fixed snapshot anchors
        _start_at = (existing or {}).get("start_at")
        await self._try_set_fixed_snapshots(
            int(fixture_id), summary, now,
            start_at=ensure_utc(_start_at) if _start_at else None,
            existing_snapshots=((existing or {}).get("odds_meta") or {}).get("fixed_snapshots"),
        )
        return True

    def _build_timeline_entry(
        self,
        *,
        summary: dict[str, dict[str, float | int]],
        source: str,
        ts,
    ) -> dict[str, Any]:
        return {
            "timestamp": ts,
            "home": float(((summary.get("home") or {}).get("avg"))),
            "draw": float(((summary.get("draw") or {}).get("avg"))),
            "away": float(((summary.get("away") or {}).get("avg"))),
            "source": str(source or ""),
        }

    # ------------------------------------------------------------------
    # v3.2: Fixed snapshots & market entropy
    # ------------------------------------------------------------------

    def _build_fixed_snapshot(
        self,
        summary: dict[str, dict[str, float | int]],
        ts,
    ) -> dict[str, Any] | None:
        """Build a compact {h, d, a, ts_utc} snapshot from a 1x2 summary."""
        try:
            return {
                "h": float(((summary.get("home") or {}).get("avg"))),
                "d": float(((summary.get("draw") or {}).get("avg"))),
                "a": float(((summary.get("away") or {}).get("avg"))),
                "ts_utc": ts,
            }
        except (TypeError, ValueError):
            return None

    def _compute_market_entropy(
        self,
        summary: dict[str, dict[str, float | int]],
        timeline: list[dict[str, Any]] | None,
        now,
    ) -> dict[str, float]:
        """Compute mutable market-entropy metrics from summary + timeline."""
        # --- current_spread_pct: avg of per-outcome (max-min)/avg ---
        spreads: list[float] = []
        for label in ("home", "draw", "away"):
            node = summary.get(label)
            if not isinstance(node, dict):
                continue
            mn = self._to_float(node.get("min"))
            mx = self._to_float(node.get("max"))
            avg = self._to_float(node.get("avg"))
            if mn is not None and mx is not None and avg is not None:
                spreads.append((mx - mn) / max(avg, 0.01))
        current_spread_pct = round(sum(spreads) / max(len(spreads), 1), 6)

        # --- drift_velocity_3h: max abs change / hour across outcomes ---
        drift_velocity_3h = 0.0
        if isinstance(timeline, list) and len(timeline) >= 2:
            cutoff = now - timedelta(hours=3)
            recent = [
                e for e in timeline
                if isinstance(e, dict) and e.get("timestamp") is not None
                and ensure_utc(e["timestamp"]) >= cutoff
            ]
            if len(recent) >= 2:
                first, last = recent[0], recent[-1]
                try:
                    hours_elapsed = max(
                        (ensure_utc(last["timestamp"]) - ensure_utc(first["timestamp"])).total_seconds() / 3600.0,
                        0.083,  # floor ~5 min to prevent velocity explosion
                    )
                    max_delta = max(
                        abs(float(last.get("home", 0)) - float(first.get("home", 0))),
                        abs(float(last.get("draw", 0)) - float(first.get("draw", 0))),
                        abs(float(last.get("away", 0)) - float(first.get("away", 0))),
                    )
                    drift_velocity_3h = round(max_delta / hours_elapsed, 6)
                except (TypeError, ValueError, KeyError):
                    pass

        return {
            "current_spread_pct": current_spread_pct,
            "drift_velocity_3h": drift_velocity_3h,
        }

    _FIXED_SNAPSHOT_WINDOWS: list[tuple[str, float, float]] = [
        # (slot_name, hours_min, hours_max)
        ("alpha_24h", 23.0, 25.0),
        ("beta_6h", 5.0, 7.0),
        ("omega_1h", 0.75, 1.25),
    ]

    async def _try_set_fixed_snapshots(
        self,
        fixture_id: int,
        summary: dict[str, dict[str, float | int]],
        now,
        start_at,
        existing_snapshots: dict[str, Any] | None,
    ) -> None:
        """Atomically write immutable fixed_snapshot slots + schema_version (write-once)."""
        snap = self._build_fixed_snapshot(summary, now)
        if snap is None:
            return
        existing = existing_snapshots or {}

        # Collect slots to write
        slots: dict[str, dict[str, Any]] = {}

        # Opening: first time we ever get valid odds
        if not existing.get("opening"):
            slots["opening"] = snap

        # Time-window anchors (only if start_at is known)
        if start_at is not None:
            try:
                hours_until = (ensure_utc(start_at) - now).total_seconds() / 3600.0
            except Exception:
                hours_until = None
            if hours_until is not None:
                for slot_name, h_min, h_max in self._FIXED_SNAPSHOT_WINDOWS:
                    if h_min <= hours_until <= h_max and not existing.get(slot_name):
                        slots[slot_name] = snap

        if not slots:
            return

        # Write each slot atomically (write-once via $exists filter)
        for slot_name, slot_snap in slots.items():
            try:
                await self.db.matches_v3.update_one(
                    {
                        "_id": int(fixture_id),
                        f"odds_meta.fixed_snapshots.{slot_name}": {"$exists": False},
                    },
                    {"$set": {
                        f"odds_meta.fixed_snapshots.{slot_name}": slot_snap,
                        "schema_version": "v3.2.0",
                    }},
                )
            except Exception:
                logger.debug("Fixed snapshot write-once failed for %s on fixture %d", slot_name, fixture_id)

    def _should_append_odds_timeline(
        self,
        existing: dict[str, Any] | None,
        summary: dict[str, dict[str, float | int]],
        now,
    ) -> bool:
        try:
            home = float(((summary.get("home") or {}).get("avg")))
            draw = float(((summary.get("draw") or {}).get("avg")))
            away = float(((summary.get("away") or {}).get("avg")))
        except (TypeError, ValueError):
            return False
        timeline = (existing or {}).get("odds_timeline") if isinstance(existing, dict) else None
        if not isinstance(timeline, list) or not timeline:
            return True
        last = timeline[-1] if isinstance(timeline[-1], dict) else {}
        last_ts = last.get("timestamp")
        last_home = self._to_float(last.get("home"))
        last_draw = self._to_float(last.get("draw"))
        last_away = self._to_float(last.get("away"))
        min_delta = float(settings.SPORTMONKS_ODDS_TIMELINE_MIN_DELTA)
        time_gate = timedelta(minutes=int(settings.SPORTMONKS_ODDS_TIMELINE_MINUTES))
        if last_ts is not None:
            try:
                if now - ensure_utc(last_ts) >= time_gate:
                    return True
            except Exception:
                return True
        if last_home is None or last_draw is None or last_away is None:
            return True
        return (
            abs(home - last_home) >= min_delta
            or abs(draw - last_draw) >= min_delta
            or abs(away - last_away) >= min_delta
        )

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None


    def _build_1x2_summary_from_rows(
        self,
        rows: list[dict[str, Any]] | Any,
    ) -> tuple[dict[str, dict[str, float | int]], bool]:
        buckets: dict[str, list[tuple[float, float | None]]] = {"home": [], "draw": [], "away": []}
        has_market_1 = False
        for row in rows if isinstance(rows, list) else []:
            if self._to_int((row or {}).get("market_id")) != 1:
                continue
            has_market_1 = True
            label = str((row or {}).get("label") or "").strip().lower()
            if label not in buckets:
                continue
            try:
                odd = float((row or {}).get("value"))
            except (TypeError, ValueError):
                continue
            probability_raw = str((row or {}).get("probability") or "").replace("%", "").strip()
            try:
                prob = float(probability_raw) / 100.0
            except (TypeError, ValueError):
                prob = None
            buckets[label].append((odd, prob))

        summary: dict[str, dict[str, float | int]] = {}
        for label in ("home", "draw", "away"):
            values = buckets[label]
            if not values:
                continue
            odds_only = [v[0] for v in values]
            prob_sum = sum(v[1] for v in values if isinstance(v[1], float) and v[1] > 0)
            if prob_sum > 0:
                weighted_avg = sum(v[0] * (v[1] or 0.0) for v in values if isinstance(v[1], float) and v[1] > 0) / prob_sum
            else:
                weighted_avg = sum(odds_only) / float(len(odds_only))
            summary[label] = {
                "min": round(min(odds_only), 4),
                "max": round(max(odds_only), 4),
                "avg": round(weighted_avg, 4),
                "count": len(values),
            }
        return summary, has_market_1

    def _summary_has_valid_odds(self, summary: dict[str, dict[str, float | int]]) -> bool:
        for label in ("home", "draw", "away"):
            node = summary.get(label)
            if not isinstance(node, dict):
                return False
            value = self._to_float(node.get("avg"))
            if value is None or value <= 1.0:
                return False
        return True

    def needs_odds_repair(self, match_doc: dict[str, Any], *, force_missing_market: bool = False) -> bool:
        if force_missing_market:
            return True
        odds_meta = (match_doc.get("odds_meta") or {}) if isinstance(match_doc.get("odds_meta"), dict) else {}
        summary = (odds_meta.get("summary_1x2") or {}) if isinstance(odds_meta.get("summary_1x2"), dict) else {}
        for label in ("home", "draw", "away"):
            node = summary.get(label)
            if not isinstance(node, dict):
                return True
            if any(node.get(key) is None for key in ("min", "max", "avg")):
                return True

        start_at = match_doc.get("start_at")
        if start_at is None:
            return False
        kickoff = ensure_utc(start_at)
        now = utcnow()
        delta_minutes = (kickoff - now).total_seconds() / 60.0
        stale_after_minutes = int(settings.ODDS_SCHEDULER_TIER_IMMINENT_MINUTES)
        if delta_minutes > (24 * 60):
            stale_after_minutes = int(settings.ODDS_SCHEDULER_TIER_APPROACHING_HOURS) * 60
        elif delta_minutes < (2 * 60):
            stale_after_minutes = int(settings.ODDS_SCHEDULER_TIER_CLOSING_MINUTES)
        updated_at = odds_meta.get("updated_at_utc") or odds_meta.get("updated_at")
        if updated_at is None:
            return True
        age_minutes = (now - ensure_utc(updated_at)).total_seconds() / 60.0
        if age_minutes >= stale_after_minutes:
            return True
        if str(odds_meta.get("source") or "") == "sportmonks_round_bulk" and delta_minutes < (2 * 60):
            return True
        return False

    async def _guard_before_external_call(
        self,
        *,
        job_id: ObjectId | None,
        phase: str,
        request_key: str,
    ) -> None:
        if job_id is None:
            return
        max_total = int(settings.SPORTMONKS_MAX_PAGE_REQUESTS_TOTAL)
        max_per_phase = int(settings.SPORTMONKS_MAX_PAGE_REQUESTS_PER_PHASE)
        duplicate_window_seconds = int(settings.SPORTMONKS_DUPLICATE_PAGE_WINDOW_SECONDS)
        duplicate_max_hits = int(settings.SPORTMONKS_DUPLICATE_PAGE_MAX_HITS)
        now = utcnow()
        job_doc = await self.db.admin_import_jobs.find_one(
            {"_id": job_id},
            {
                "timeout_at": 1,
                "status": 1,
                "page_requests_total": 1,
                "phase_page_requests": 1,
                "duplicate_page_blocks": 1,
            },
        )
        if not isinstance(job_doc, dict):
            return
        timeout_at = job_doc.get("timeout_at")
        if timeout_at is not None and now > ensure_utc(timeout_at):
            await self._inc_guard_metric("runtime_timeouts")
            await self._append_job_error(
                job_id=job_id,
                round_id=None,
                message="Timeout: max runtime exceeded",
                trace="",
            )
            await self._job_update(
                job_id,
                status="failed",
                phase="failed_timeout",
                active_lock=False,
                finished_at=now,
                error={"message": "Timeout: max runtime exceeded", "type": "TimeoutError"},
            )
            self._clear_request_window(job_id)
            raise RuntimeError("Timeout: max runtime exceeded")

        page_requests_total = int(job_doc.get("page_requests_total") or 0) + 1
        phase_counts = job_doc.get("phase_page_requests") if isinstance(job_doc.get("phase_page_requests"), dict) else {}
        phase_total = int(phase_counts.get(phase) or 0) + 1
        if page_requests_total > max_total or phase_total > max_per_phase:
            await self._append_job_error(
                job_id=job_id,
                round_id=None,
                message=(
                    f"Page request limit exceeded: max_total={max_total} max_per_phase={max_per_phase} "
                    f"phase={phase} total={page_requests_total} phase_total={phase_total}"
                ),
                trace="",
            )
            await self._job_update(
                job_id,
                status="failed",
                phase="failed_page_limit",
                active_lock=False,
                finished_at=now,
                page_requests_total=page_requests_total,
                error={"message": "Page request limit exceeded", "type": "PageLimitError"},
            )
            self._clear_request_window(job_id)
            raise RuntimeError("Page request limit exceeded")

        job_key = str(job_id)
        key = f"{phase}|{request_key}"
        phase_map = self._request_windows.setdefault(job_key, {})
        timestamps = phase_map.get(key) or []
        timestamps = [ts for ts in timestamps if (now - ts).total_seconds() <= duplicate_window_seconds]
        timestamps.append(now)
        phase_map[key] = timestamps
        duplicate_blocks = int(job_doc.get("duplicate_page_blocks") or 0)
        if len(timestamps) > duplicate_max_hits:
            duplicate_blocks += 1
            await self._inc_guard_metric("page_guard_blocks")
            await self._append_job_error(
                job_id=job_id,
                round_id=None,
                message=(
                    "Duplicate page blocked: "
                    f"phase={phase} request={request_key} hits={len(timestamps)} "
                    f"window_seconds={duplicate_window_seconds}"
                ),
                trace="",
            )
            await self._job_update(
                job_id,
                status="failed",
                phase="failed_duplicate_page_guard",
                active_lock=False,
                finished_at=now,
                page_requests_total=page_requests_total,
                duplicate_page_blocks=duplicate_blocks,
                error={"message": "Duplicate page blocked", "type": "DuplicatePageGuardError"},
            )
            self._clear_request_window(job_id)
            raise RuntimeError("Duplicate page blocked")

        phase_counts[phase] = phase_total
        await self._job_update(
            job_id,
            page_requests_total=page_requests_total,
            phase_page_requests=phase_counts,
            duplicate_page_blocks=duplicate_blocks,
        )

    async def _inc_guard_metric(self, field: str) -> None:
        await self.db.meta.update_one(
            {"_id": "sportmonks_guard_metrics"},
            {
                "$inc": {str(field): 1},
                "$set": {"updated_at": utcnow(), "updated_at_utc": utcnow()},
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )

    def _clear_request_window(self, job_id: ObjectId | None) -> None:
        if job_id is None:
            return
        self._request_windows.pop(str(job_id), None)

    async def sync_leagues_on_startup(self) -> None:
        if not settings.SPORTMONKS_STARTUP_DISCOVERY_ENABLED:
            return
        now = utcnow()
        meta = await self.db.meta.find_one({"_id": "sportmonks_discovery_startup"})
        last = meta.get("last_synced_at") if isinstance(meta, dict) else None
        if last is not None:
            from app.utils import ensure_utc

            ttl = timedelta(minutes=int(settings.SPORTMONKS_DISCOVERY_TTL_MINUTES))
            if now - ensure_utc(last) <= ttl:
                return
        discovery = await self.get_available_leagues()
        if discovery.get("remaining") is not None and int(discovery["remaining"]) <= 1:
            return
        await self.sync_leagues_to_registry(discovery.get("items") or [])
        await self.db.meta.update_one(
            {"_id": "sportmonks_discovery_startup"},
            {"$set": {"last_synced_at": now, "updated_at": now, "updated_at_utc": now}},
            upsert=True,
        )

    async def _sync_people_from_fixture(self, fixture: dict[str, Any]) -> int:
        count = 0
        referees = (fixture or {}).get("referees") or []
        referee = None
        if isinstance(referees, list) and referees:
            referee = referees[0]
        elif isinstance((fixture or {}).get("referee"), dict):
            referee = (fixture or {}).get("referee")
        referee_node = (referee or {}).get("referee") if isinstance(referee, dict) else None
        rid = self._to_int(
            ((referee_node or {}).get("id") if isinstance(referee_node, dict) else None)
            or ((referee or {}).get("id") if isinstance(referee, dict) else None)
        )
        if rid is not None:
            ref_name = self._first_non_empty(
                (referee_node or {}).get("common_name") if isinstance(referee_node, dict) else None,
                (referee_node or {}).get("name") if isinstance(referee_node, dict) else None,
                (referee or {}).get("common_name") if isinstance(referee, dict) else None,
                (referee or {}).get("name") if isinstance(referee, dict) else None,
            )
            ref_common_name = self._first_non_empty(
                (referee_node or {}).get("common_name") if isinstance(referee_node, dict) else None,
                (referee or {}).get("common_name") if isinstance(referee, dict) else None,
            )
            ref_image_path = self._first_non_empty(
                (referee_node or {}).get("image_path") if isinstance(referee_node, dict) else None,
                (referee or {}).get("image_path") if isinstance(referee, dict) else None,
            )
            referee_payload: dict[str, Any] = {
                "type": "referee",
                "stats_cache": {"matches_officiated": 0, "avg_yellow_cards": 0.0, "goals_total": 0},
            }
            if ref_name is not None:
                referee_payload["name"] = ref_name
            if ref_common_name is not None:
                referee_payload["common_name"] = ref_common_name
            if ref_image_path is not None:
                referee_payload["image_path"] = ref_image_path
            await self.upsert_person(
                rid,
                referee_payload,
            )
            count += 1
        for lineup in (fixture or {}).get("lineups") or []:
            player = (lineup or {}).get("player") or {}
            pid = self._to_int((lineup or {}).get("player_id") or player.get("id"))
            if pid is None:
                continue
            player_name = self._first_non_empty(
                player.get("display_name"),
                player.get("common_name"),
                player.get("name"),
                (lineup or {}).get("display_name"),
                (lineup or {}).get("common_name"),
                (lineup or {}).get("name"),
            )
            player_common_name = self._first_non_empty(
                player.get("common_name"),
                player.get("display_name"),
                (lineup or {}).get("common_name"),
            )
            player_image_path = self._first_non_empty(
                player.get("image_path"),
                (lineup or {}).get("image_path"),
            )
            player_payload: dict[str, Any] = {
                "type": "player",
                "stats_cache": {"matches_officiated": 0, "avg_yellow_cards": 0.0, "goals_total": 0},
            }
            if player_name is not None:
                player_payload["name"] = player_name
            if player_common_name is not None:
                player_payload["common_name"] = player_common_name
            if player_image_path is not None:
                player_payload["image_path"] = player_image_path
            await self.upsert_person(
                pid,
                player_payload,
            )
            count += 1
        return count

    def _map_fixture_to_match(self, fixture: dict[str, Any], season_id: int) -> dict[str, Any]:
        participants = (fixture or {}).get("participants") or []
        home = participants[0] if len(participants) > 0 else {}
        away = participants[1] if len(participants) > 1 else {}
        home_score, away_score = self._extract_scores(fixture)
        stats_rows = (fixture or {}).get("statistics") or []
        xg_home = None
        xg_away = None
        for row in stats_rows:
            type_id = self._to_int((row or {}).get("type_id"))
            if type_id != 5304:
                continue
            team_id = self._to_int((row or {}).get("participant_id"))
            value = ((row or {}).get("data") or {}).get("value") if isinstance((row or {}).get("data"), dict) else None
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if team_id == self._to_int(home.get("id")):
                xg_home = parsed
            elif team_id == self._to_int(away.get("id")):
                xg_away = parsed
        has_advanced = xg_home is not None and xg_away is not None
        referees = (fixture or {}).get("referees") or []
        referee_node = None
        if isinstance(referees, list) and referees:
            first_ref = referees[0] if isinstance(referees[0], dict) else {}
            nested = first_ref.get("referee") if isinstance(first_ref, dict) else None
            referee_node = nested if isinstance(nested, dict) else first_ref
        if not isinstance(referee_node, dict):
            referee_node = ((fixture or {}).get("referee") or {}) if isinstance((fixture or {}).get("referee"), dict) else {}

        status = self._map_status(fixture)
        period_scores = self._extract_period_scores(fixture)
        lineups = self._map_lineups(fixture)

        # Extract finish_type from raw state (FT / AET / PEN)
        state = (fixture or {}).get("state") or {}
        raw_state = str((state or {}).get("short_name") or "").upper().strip()
        finish_type = raw_state if raw_state in {"FT", "AET", "PEN"} else None

        # --- Data guard: flag, don't gate ---
        reasons: list[str] = []
        ft = period_scores.get("full_time") or {}
        if status == "FINISHED":
            if ft.get("home") is None or ft.get("away") is None:
                # Fallback: fill full_time from CURRENT scores
                if home_score is not None and away_score is not None:
                    period_scores["full_time"] = {"home": home_score, "away": away_score}
                else:
                    reasons.append("finished_without_scores")
            if not lineups:
                reasons.append("finished_without_lineups")
        if status == "WALKOVER" and (home_score is not None or away_score is not None):
            reasons.append("walkover_with_scores")
        if status == "POSTPONED" and (home_score is not None or away_score is not None):
            reasons.append("postponed_with_scores")

        recomputed = self._recompute_manual_check_fields(reasons)

        return {
            "league_id": self._to_int((fixture or {}).get("league_id"), 0) or 0,
            "season_id": int(season_id),
            "round_id": self._to_int((fixture or {}).get("round_id")),
            "referee_id": self._to_int((referee_node or {}).get("id")),
            "referee_name": self._first_non_empty(
                (referee_node or {}).get("common_name"),
                (referee_node or {}).get("name"),
            ),
            "start_at": parse_utc((fixture or {}).get("starting_at") or utcnow()),
            "has_advanced_stats": has_advanced,
            "status": status,
            "finish_type": finish_type,
            "teams": {
                "home": {
                    "sm_id": self._to_int(home.get("id"), 0) or 0,
                    "name": self._norm_name(home.get("name")),
                    "short_code": self._first_non_empty(home.get("short_code")),
                    "image_path": self._first_non_empty(home.get("image_path")),
                    "score": home_score,
                    "xg": xg_home,
                },
                "away": {
                    "sm_id": self._to_int(away.get("id"), 0) or 0,
                    "name": self._norm_name(away.get("name")),
                    "short_code": self._first_non_empty(away.get("short_code")),
                    "image_path": self._first_non_empty(away.get("image_path")),
                    "score": away_score,
                    "xg": xg_away,
                },
            },
            "events": self._map_events(fixture),
            "scores": period_scores,
            "lineups": lineups,
            "penalty_info": self._map_penalty_info(fixture),
            "manual_check_required": bool(recomputed["manual_check_required"]),
            "manual_check_reasons": recomputed["manual_check_reasons"],
        }

    def _map_status(self, fixture: dict[str, Any]) -> str:
        state = (fixture or {}).get("state") or {}
        raw = str((state or {}).get("short_name") or (state or {}).get("state") or (fixture or {}).get("status") or "").upper()
        state_id = self._to_int((fixture or {}).get("state_id"))
        if raw in {"FT", "AET", "PEN"}:
            return "FINISHED"
        if state_id in {5, 6, 7}:
            return "FINISHED"
        if raw in {"LIVE", "HT", "ET", "IN_PLAY"}:
            return "LIVE"
        if state_id in {2, 3, 4, 8, 9}:
            return "LIVE"
        if raw in {"TBA", "NS"}:
            return "SCHEDULED"
        if state_id in {1}:
            return "SCHEDULED"
        if raw in {"PST"}:
            return "POSTPONED"
        if state_id in {10}:
            return "POSTPONED"
        if raw in {"WO", "CANCL"}:
            return "WALKOVER"
        if state_id in {11, 12}:
            return "WALKOVER"
        return "SCHEDULED"

    def _extract_scores(self, fixture: dict[str, Any]) -> tuple[int | None, int | None]:
        rows = (fixture or {}).get("scores") or []
        if not isinstance(rows, list) or not rows:
            return None, None
        preferred = [row for row in rows if str((row or {}).get("description") or "").upper() == "CURRENT"]
        candidates = preferred if preferred else rows
        home_score = None
        away_score = None
        for row in candidates:
            score = (row or {}).get("score") or {}
            side = str(score.get("participant") or "").strip().lower()
            goals = self._to_int(score.get("goals"))
            if goals is None:
                continue
            if side == "home":
                home_score = goals
            elif side == "away":
                away_score = goals
        return home_score, away_score

    def _extract_period_scores(self, fixture: dict[str, Any]) -> dict[str, Any]:
        """Extract half_time and full_time period scores from fixture score rows."""
        rows = (fixture or {}).get("scores") or []
        result: dict[str, dict[str, int | None]] = {
            "half_time": {"home": None, "away": None},
            "full_time": {"home": None, "away": None},
        }
        if not isinstance(rows, list) or not rows:
            return result
        desc_map = {"1ST_HALF": "half_time", "CURRENT": "full_time"}
        for row in rows:
            desc = str((row or {}).get("description") or "").upper().strip()
            period_key = desc_map.get(desc)
            if period_key is None:
                continue
            score = (row or {}).get("score") or {}
            side = str(score.get("participant") or "").strip().lower()
            goals = self._to_int(score.get("goals"))
            if goals is None:
                continue
            if side in ("home", "away"):
                result[period_key][side] = goals
        return result

    def _map_lineups(self, fixture: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in (fixture or {}).get("lineups") or []:
            pid = self._to_int((row or {}).get("player_id") or ((row or {}).get("player") or {}).get("id"))
            tid = self._to_int((row or {}).get("team_id") or (row or {}).get("participant_id"))
            if pid is None or tid is None:
                continue
            out.append(
                {
                    "player_id": pid,
                    "formation_field": (row or {}).get("formation_field"),
                    "team_id": tid,
                }
            )
        return out

    def _build_team_upsert_ops_from_fixture(
        self,
        fixture: dict[str, Any],
        *,
        seen_team_ids: set[int],
    ) -> list[UpdateOne]:
        ops: list[UpdateOne] = []
        for participant in (fixture or {}).get("participants") or []:
            team_id = self._to_int((participant or {}).get("id"))
            if team_id is None or team_id in seen_team_ids:
                continue
            seen_team_ids.add(team_id)
            set_payload: dict[str, Any] = {"updated_at": utcnow(), "updated_at_utc": utcnow()}
            name = self._first_non_empty((participant or {}).get("name"))
            if name is not None:
                set_payload["name"] = name
            short_code = self._first_non_empty((participant or {}).get("short_code"))
            if short_code is not None:
                set_payload["short_code"] = short_code
            image_path = self._first_non_empty((participant or {}).get("image_path"))
            if image_path is not None:
                set_payload["image_path"] = image_path
            normalized = normalize_team_alias(str(name or ""))
            default_alias = (
                {
                    "name": str(name or "").strip(),
                    "normalized": normalized,
                    "source": "provider_unknown",
                    "sport_key": None,
                    "alias_key": f"{normalized}|*|provider_unknown",
                    "is_default": True,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                    "updated_at_utc": utcnow(),
                }
                if normalized
                else None
            )
            ops.append(
                UpdateOne(
                    {"_id": int(team_id)},
                    {
                        "$set": set_payload,
                        "$setOnInsert": {
                            "created_at": utcnow(),
                            "aliases": [default_alias] if default_alias else [],
                        },
                    },
                    upsert=True,
                )
            )
        return ops

    def _map_penalty_info(self, fixture: dict[str, Any]) -> dict[str, Any]:
        events = (fixture or {}).get("events") or []
        awarded_minutes: list[int] = []
        goal_minutes: set[int] = set()
        for event in events:
            type_id = self._to_int((event or {}).get("type_id"))
            minute = self._to_int((event or {}).get("minute"), 0) or 0
            subtype = str((event or {}).get("sub_type") or "").lower()
            if type_id == 11:
                awarded_minutes.append(minute)
            if type_id == 14 and "pen" in subtype:
                goal_minutes.add(minute)
        details: list[dict[str, Any]] = []
        for minute in sorted(awarded_minutes):
            details.append({"minute": minute, "converted": minute in goal_minutes})
        return {"occurred": bool(details), "details": details}

    # Sportmonks v3 event type_id constants
    _EVT_VAR = 10
    _EVT_GOAL = 14
    _EVT_OWN_GOAL = 15
    _EVT_PENALTY_SCORED = 16
    _EVT_MISSED_PENALTY = 17
    _EVT_YELLOW_CARD = 19
    _EVT_RED_CARD = 20
    _EVT_YELLOW_RED_CARD = 21

    _EVT_TYPE_MAP: dict[int, tuple[str, str]] = {
        # type_id -> (mapped_type, default_detail)
        _EVT_VAR: ("var", ""),
        _EVT_GOAL: ("goal", "regular"),
        _EVT_OWN_GOAL: ("goal", "own_goal"),
        _EVT_PENALTY_SCORED: ("goal", "penalty"),
        _EVT_MISSED_PENALTY: ("missed_penalty", ""),
        _EVT_YELLOW_CARD: ("card", "yellow"),
        _EVT_RED_CARD: ("card", "red"),
        _EVT_YELLOW_RED_CARD: ("card", "yellow_red"),
    }

    def _map_events(self, fixture: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract goals, cards, VAR, and missed penalties from fixture events."""
        raw_events = (fixture or {}).get("events") or []
        mapped: list[dict[str, Any]] = []
        for event in raw_events:
            type_id = self._to_int((event or {}).get("type_id"))
            entry = self._EVT_TYPE_MAP.get(type_id)  # type: ignore[arg-type]
            if entry is None:
                continue
            mapped_type, default_detail = entry
            minute = self._to_int((event or {}).get("minute"))
            extra_minute = self._to_int((event or {}).get("extra_minute"))
            player_name = str((event or {}).get("player_name") or "").strip()
            player_id = self._to_int((event or {}).get("player_id"))
            team_id = self._to_int((event or {}).get("team_id") or (event or {}).get("participant_id"))
            sort_order = self._to_int((event or {}).get("sort_order"))
            info = str((event or {}).get("info") or "").strip()

            # Determine detail
            if type_id == self._EVT_GOAL:
                sub_type = str((event or {}).get("sub_type") or "").lower()
                detail = "penalty" if "pen" in sub_type else "regular"
            elif type_id in (self._EVT_VAR, self._EVT_MISSED_PENALTY):
                detail = info or default_detail
            else:
                detail = default_detail

            mapped.append({
                "type": mapped_type,
                "minute": minute,
                "extra_minute": extra_minute,
                "player_name": player_name,
                "player_id": player_id,
                "team_id": team_id,
                "detail": detail,
                "sort_order": sort_order,
            })
        # Sort by sort_order (Sportmonks canonical), fallback to minute
        mapped.sort(key=lambda e: (e.get("sort_order") or 0, e.get("minute") or 0))
        return mapped

    async def _post_sync_data_guard(self, season_id: int) -> dict[str, int]:
        """Flag FINISHED matches missing xG or odds after full metrics sync."""
        now = utcnow()
        result_xg = await self.db.matches_v3.update_many(
            {
                "season_id": int(season_id),
                "status": "FINISHED",
                "has_advanced_stats": {"$ne": True},
            },
            {
                "$addToSet": {"manual_check_reasons": "finished_without_xg"},
                "$set": {"updated_at": now, "updated_at_utc": now},
            },
        )
        result_odds = await self.db.matches_v3.update_many(
            {
                "season_id": int(season_id),
                "status": "FINISHED",
                "odds_meta.summary_1x2.home.avg": {"$exists": False},
            },
            {
                "$addToSet": {"manual_check_reasons": "finished_without_odds"},
                "$set": {"updated_at": now, "updated_at_utc": now},
            },
        )
        # Keep manual_check_required aligned for all flagged docs.
        await self.db.matches_v3.update_many(
            {
                "season_id": int(season_id),
                "status": "FINISHED",
                "manual_check_reasons": {
                    "$in": sorted(CRITICAL_MANUAL_CHECK_REASONS),
                },
            },
            {"$set": {"manual_check_required": True, "updated_at": now, "updated_at_utc": now}},
        )
        return {
            "xg_flagged": result_xg.modified_count,
            "odds_flagged": result_odds.modified_count,
        }

    async def auto_heal_manual_check_flags(
        self,
        *,
        season_id: int | None = None,
    ) -> dict[str, int]:
        """Bulk-heal stale finished_without_* reasons and recompute manual-check fields."""
        base_query: dict[str, Any] = {"status": "FINISHED"}
        if season_id is not None:
            base_query["season_id"] = int(season_id)

        stale_xg_query = {
            **base_query,
            "has_advanced_stats": True,
            "manual_check_reasons": "finished_without_xg",
        }
        stale_odds_query = {
            **base_query,
            "manual_check_reasons": "finished_without_odds",
            "odds_meta.summary_1x2.home.avg": {"$gt": 1.0},
            "odds_meta.summary_1x2.draw.avg": {"$gt": 1.0},
            "odds_meta.summary_1x2.away.avg": {"$gt": 1.0},
        }

        xg_ids = await self.db.matches_v3.find(stale_xg_query, {"_id": 1}).to_list(length=200_000)
        odds_ids = await self.db.matches_v3.find(stale_odds_query, {"_id": 1}).to_list(length=200_000)
        target_ids = {int(row["_id"]) for row in [*xg_ids, *odds_ids] if row.get("_id") is not None}
        now = utcnow()
        healed_xg = 0
        healed_odds = 0
        healed_total = 0

        for match_id in sorted(target_ids):
            doc = await self.db.matches_v3.find_one(
                {"_id": int(match_id)},
                {
                    "manual_check_reasons": 1,
                    "has_advanced_stats": 1,
                    "odds_meta.summary_1x2.home.avg": 1,
                    "odds_meta.summary_1x2.draw.avg": 1,
                    "odds_meta.summary_1x2.away.avg": 1,
                },
            )
            reasons = list((doc or {}).get("manual_check_reasons") or [])
            reasons_set = {str(value).strip() for value in reasons if str(value).strip()}
            changed = False
            has_valid_xg = bool((doc or {}).get("has_advanced_stats") is True)
            odds_summary = (((doc or {}).get("odds_meta") or {}).get("summary_1x2") or {})
            has_valid_odds = self._summary_has_valid_odds(odds_summary if isinstance(odds_summary, dict) else {})
            if "finished_without_xg" in reasons_set and has_valid_xg:
                reasons_set.discard("finished_without_xg")
                healed_xg += 1
                changed = True
            if "finished_without_odds" in reasons_set and has_valid_odds:
                reasons_set.discard("finished_without_odds")
                healed_odds += 1
                changed = True
            if not changed:
                continue
            recomputed = self._recompute_manual_check_fields(sorted(reasons_set))
            await self.db.matches_v3.update_one(
                {"_id": int(match_id)},
                {
                    "$set": {
                        "manual_check_reasons": recomputed["manual_check_reasons"],
                        "manual_check_required": recomputed["manual_check_required"],
                        "updated_at": now,
                        "updated_at_utc": now,
                    }
                },
            )
            healed_total += 1

        logger.info(
            "Auto-healed manual-check flags: season=%s healed_total=%d healed_xg=%d healed_odds=%d",
            str(season_id) if season_id is not None else "all",
            healed_total,
            healed_xg,
            healed_odds,
        )
        return {
            "healed_total": int(healed_total),
            "healed_xg": int(healed_xg),
            "healed_odds": int(healed_odds),
        }

    async def _pause_if_needed(self, *, job_id: ObjectId | None) -> None:
        reserve = int(settings.SPORTMONKS_RESERVE_CREDITS)
        if self._remaining is None or self._remaining > reserve:
            return
        await self._job_update(
            job_id,
            status="paused",
            rate_limit_paused=True,
            rate_limit_remaining=self._remaining,
            rate_limit_reset_at=self._reset_at,
            phase="rate_limit_pause",
        )
        sleep_for = 30
        if self._reset_at:
            now_ts = int(utcnow().timestamp())
            sleep_for = max(1, min(120, int(self._reset_at) - now_ts))
        await asyncio.sleep(sleep_for)
        await self._job_update(job_id, status="running", rate_limit_paused=False, phase="ingesting_round")

    async def _job_update(self, job_id: ObjectId | None, **fields: Any) -> None:
        if job_id is None:
            return
        now = utcnow()
        payload = dict(fields)
        if "rate_limit_remaining" not in payload and self._remaining is not None:
            payload["rate_limit_remaining"] = self._remaining
        if "rate_limit_reset_at" not in payload and self._reset_at is not None:
            payload["rate_limit_reset_at"] = self._reset_at
        payload["updated_at"] = now
        payload["updated_at_utc"] = now
        await self.db.admin_import_jobs.update_one({"_id": job_id}, {"$set": payload})

    async def _append_job_error(
        self,
        *,
        job_id: ObjectId | None,
        round_id: int | None,
        message: str,
        trace: str | None,
    ) -> None:
        if job_id is None:
            return
        now = utcnow()
        await self.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {
                "$push": {
                    "error_log": {
                        "timestamp": now,
                        "round_id": round_id,
                        "error_msg": str(message or "")[:3000],
                        "trace": str(trace or "")[:4000],
                    }
                },
                "$set": {"updated_at": now, "updated_at_utc": now},
            },
        )

    def _safe_trace(self) -> str:
        trace = traceback.format_exc() or ""
        # Trim potentially huge stack traces for operator UX + storage safety.
        return trace[:4000]

    async def _upsert_v3_document(self, *, collection, doc_id: int, payload: dict[str, Any]) -> None:
        now = utcnow()
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"_id", "created_at", "updated_at", "updated_at_utc"}
        }
        await collection.update_one(
            {"_id": int(doc_id)},
            {
                "$set": {
                    **safe_payload,
                    "updated_at": now,
                    "updated_at_utc": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )


sportmonks_connector = SportmonksConnector()
