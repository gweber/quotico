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
import traceback
from collections import defaultdict
from datetime import timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.config import settings
from app.providers.sportmonks import sportmonks_provider
from app.utils import ensure_utc, parse_utc, utcnow


class SportmonksConnector:
    """Connector for v3 sync + ingest with immutable created_at semantics."""

    def __init__(self, database=None) -> None:
        self._db = database
        self._remaining: int | None = None
        self._reset_at: int | None = None

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
        for item in items:
            lid = self._to_int(item.get("_id"))
            if lid is None:
                continue
            existing = await self.db.league_registry_v3.find_one({"_id": lid}, {"available_seasons": 1})
            merged_seasons = self._merge_seasons(existing, item.get("available_seasons") or [])
            payload = {
                "name": self._norm_name(item.get("name")),
                "country": self._norm_name(item.get("country")),
                "is_cup": bool(item.get("is_cup", False)),
                "available_seasons": merged_seasons,
                "last_synced_at": now,
                "updated_at": now,
            }
            result = await self.db.league_registry_v3.update_one(
                {"_id": lid},
                {
                    "$set": payload,
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            if result.upserted_id is not None:
                inserted += 1
            else:
                updated += 1
        return {"inserted": inserted, "updated": updated}

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
        await self._job_update(job_id, status="running", phase="loading_rounds", started_at=utcnow())
        rounds_response = await sportmonks_provider.get_season_rounds(int(season_id))
        self._remaining = self._to_int(rounds_response.get("remaining"))
        self._reset_at = self._to_int(rounds_response.get("reset_at"))
        rounds = (rounds_response.get("payload") or {}).get("data") or []
        total_rounds = len(rounds)
        processed_rounds = 0
        counters = {"matches_upserted": 0, "persons_upserted": 0, "odds_synced": 0}
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
                await self._pause_if_needed(job_id=job_id)
                await self._job_update(
                    job_id,
                    phase="ingesting_round",
                    current_round_name=round_name,
                    rate_limit_remaining=self._remaining,
                    rate_limit_paused=False,
                )

                fixtures_response = await sportmonks_provider.get_round_fixtures(round_id)
                self._remaining = self._to_int(fixtures_response.get("remaining"))
                self._reset_at = self._to_int(fixtures_response.get("reset_at"))
                fixtures = (fixtures_response.get("payload") or {}).get("data") or []
                for fixture in fixtures:
                    fixture_id = self._to_int((fixture or {}).get("id"))
                    if fixture_id is None:
                        continue
                    try:
                        people_count = await self._sync_people_from_fixture(fixture)
                        await self.upsert_match_v3(fixture_id, self._map_fixture_to_match(fixture, season_id))
                        await self._pause_if_needed(job_id=job_id)
                        odds_saved = await self.sync_fixture_odds_summary(fixture_id)
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
        return {
            "season_id": int(season_id),
            "processed_rounds": processed_rounds,
            "total_rounds": total_rounds,
            **counters,
        }

    async def run_metrics_sync(self, season_id: int, *, job_id: ObjectId | None = None) -> dict[str, int]:
        """Run dedicated metrics sync (bulk odds -> repair -> paginated xG) for a season."""
        await self._job_update(
            job_id,
            status="running",
            phase="metrics_sync",
            started_at=utcnow(),
            pages_processed=0,
            pages_total=None,
            rows_processed=0,
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
                if await self.sync_fixture_odds_summary(int(fixture_id), source="sportmonks_fixture_repair"):
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

        xg_result = await self.sync_season_xg(int(season_id), job_id=job_id)
        total_fixtures = len(seen_fixture_ids) if seen_fixture_ids else len(docs)
        saved_calls_estimate = max(0, int(total_fixtures) - (int(bulk_round_calls) + int(repair_calls)))
        savings_ratio = round((saved_calls_estimate / int(total_fixtures)), 4) if int(total_fixtures) > 0 else 0.0

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
        }
        await self._job_update(
            job_id,
            status="succeeded",
            phase="done",
            active_lock=False,
            finished_at=utcnow(),
            results=result,
        )
        return result

    async def sync_round_odds_summary(
        self,
        *,
        round_id: int,
        season_id: int,
        job_id: ObjectId | None = None,
    ) -> dict[str, Any]:
        """Bulk-sync odds summaries for one round from fixtures.odds include."""
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
                update_fields: dict[str, Any] = {"updated_at": utcnow()}
                if summary:
                    update_fields["odds_meta.summary_1x2"] = summary
                    update_fields["odds_meta.source"] = "sportmonks_round_bulk"
                    update_fields["odds_meta.updated_at"] = utcnow()
                else:
                    repair_candidates.append(int(fixture_id))
                await self.db.matches_v3.update_one(
                    {"_id": int(fixture_id), "season_id": int(season_id)},
                    {"$set": update_fields},
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
        """Sync xG from expected endpoint and patch existing matches_v3 documents."""
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        next_page: str | None = None
        page_count = 0
        rows_processed = 0
        while True:
            page = await sportmonks_provider.get_expected_fixtures_page(
                season_id=int(season_id),
                next_page_url=next_page,
            )
            self._remaining = self._to_int(page.get("remaining"))
            self._reset_at = self._to_int(page.get("reset_at"))
            payload = page.get("payload") or {}
            rows = payload.get("data") or []
            page_count += 1
            rows_processed += len(rows) if isinstance(rows, list) else 0
            for row in rows if isinstance(rows, list) else []:
                if self._to_int((row or {}).get("type_id")) != 5304:
                    continue
                fixture_id = self._to_int((row or {}).get("fixture_id"))
                if fixture_id is None:
                    continue
                grouped[int(fixture_id)].append(row)
            pagination = payload.get("pagination") or {}
            next_page_value = pagination.get("next_page")
            next_page = str(next_page_value).strip() if next_page_value else None
            await self._job_update(
                job_id,
                phase="metrics_xg_sync",
                pages_processed=page_count,
                pages_total=None,
                rows_processed=rows_processed,
                rate_limit_remaining=self._remaining,
            )
            if not bool(pagination.get("has_more")) or not next_page:
                break

        matches_synced = 0
        partial_warnings = 0
        for fixture_id, xg_rows in grouped.items():
            match_doc = await self.db.matches_v3.find_one(
                {"_id": int(fixture_id)},
                {"teams.home.sm_id": 1, "teams.away.sm_id": 1},
            )
            if not isinstance(match_doc, dict):
                continue
            teams = (match_doc.get("teams") or {}) if isinstance(match_doc.get("teams"), dict) else {}
            home_id = self._to_int(((teams.get("home") or {}).get("sm_id")) if isinstance(teams.get("home"), dict) else None)
            away_id = self._to_int(((teams.get("away") or {}).get("sm_id")) if isinstance(teams.get("away"), dict) else None)
            home_xg: float | None = None
            away_xg: float | None = None
            for row in xg_rows:
                location = str((row or {}).get("location") or "").strip().lower()
                participant_id = self._to_int((row or {}).get("participant_id"))
                value_raw = ((row or {}).get("data") or {}).get("value") if isinstance((row or {}).get("data"), dict) else None
                try:
                    value = float(value_raw)
                except (TypeError, ValueError):
                    continue
                if location == "home":
                    home_xg = value
                    continue
                if location == "away":
                    away_xg = value
                    continue
                if participant_id is not None and participant_id == home_id:
                    home_xg = value
                    continue
                if participant_id is not None and participant_id == away_id:
                    away_xg = value

            update_fields: dict[str, Any] = {"updated_at": utcnow()}
            if home_xg is not None:
                update_fields["teams.home.xg"] = home_xg
            if away_xg is not None:
                update_fields["teams.away.xg"] = away_xg
            if home_xg is not None and away_xg is not None:
                update_fields["has_advanced_stats"] = True
            elif home_xg is not None or away_xg is not None:
                partial_warnings += 1
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
            if len(update_fields) <= 1:
                continue
            await self.db.matches_v3.update_one({"_id": int(fixture_id)}, {"$set": update_fields})
            matches_synced += 1

        return {
            "fixtures_seen": len(grouped),
            "matches_synced": matches_synced,
            "partial_warnings": partial_warnings,
            "pages_processed": page_count,
            "rows_processed": rows_processed,
        }

    async def sync_fixture_odds_summary(self, fixture_id: int, *, source: str = "sportmonks_pre_match") -> bool:
        """Store compact 1X2 odds snapshot in matches_v3.odds_meta."""
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
        await self.db.matches_v3.update_one(
            {"_id": int(fixture_id)},
            {
                "$set": {
                    "odds_meta.summary_1x2": summary,
                    "odds_meta.source": source,
                    "odds_meta.updated_at": now,
                    "updated_at": now,
                }
            },
        )
        return True

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
        stale_after_minutes = 60
        if delta_minutes > (24 * 60):
            stale_after_minutes = 360
        elif delta_minutes < (2 * 60):
            stale_after_minutes = 15
        updated_at = odds_meta.get("updated_at")
        if updated_at is None:
            return True
        age_minutes = (now - ensure_utc(updated_at)).total_seconds() / 60.0
        if age_minutes >= stale_after_minutes:
            return True
        if str(odds_meta.get("source") or "") == "sportmonks_round_bulk" and delta_minutes < (2 * 60):
            return True
        return False

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
            {"$set": {"last_synced_at": now, "updated_at": now}},
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
        stats_rows = (fixture or {}).get("statistics") or []
        xg_home = None
        xg_away = None
        for row in stats_rows:
            key = str((row or {}).get("type") or "").lower()
            if "xg" not in key:
                continue
            team_id = self._to_int((row or {}).get("participant_id"))
            value = row.get("data") if isinstance(row, dict) else None
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
        return {
            "league_id": self._to_int((fixture or {}).get("league_id"), 0) or 0,
            "season_id": int(season_id),
            "round_id": self._to_int((fixture or {}).get("round_id")),
            "referee_id": self._to_int((referee_node or {}).get("id")),
            "start_at": parse_utc((fixture or {}).get("starting_at") or utcnow()),
            "has_advanced_stats": has_advanced,
            "status": self._map_status(fixture),
            "teams": {
                "home": {"sm_id": self._to_int(home.get("id"), 0) or 0, "xg": xg_home},
                "away": {"sm_id": self._to_int(away.get("id"), 0) or 0, "xg": xg_away},
            },
            "lineups": self._map_lineups(fixture),
            "penalty_info": self._map_penalty_info(fixture),
        }

    def _map_status(self, fixture: dict[str, Any]) -> str:
        raw = str((((fixture or {}).get("state") or {}).get("short_name")) or (fixture or {}).get("status") or "").upper()
        if raw in {"FT", "AET", "PEN"}:
            return "FINISHED"
        if raw in {"LIVE", "HT", "ET", "IN_PLAY"}:
            return "LIVE"
        if raw in {"TBA", "NS"}:
            return "SCHEDULED"
        if raw in {"PST"}:
            return "POSTPONED"
        if raw in {"WO", "CANCL"}:
            return "WALKOVER"
        return "SCHEDULED"

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

    async def _pause_if_needed(self, *, job_id: ObjectId | None) -> None:
        reserve = int(settings.SPORTMONKS_RESERVE_CREDITS)
        if self._remaining is None or self._remaining > reserve:
            return
        await self._job_update(
            job_id,
            status="paused",
            rate_limit_paused=True,
            rate_limit_remaining=self._remaining,
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
        payload = dict(fields)
        payload["updated_at"] = utcnow()
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
                "$set": {"updated_at": now},
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
            if key not in {"_id", "created_at", "updated_at"}
        }
        await collection.update_one(
            {"_id": int(doc_id)},
            {
                "$set": {
                    **safe_payload,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )


sportmonks_connector = SportmonksConnector()
