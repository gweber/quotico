"""
backend/app/services/metrics_heartbeat.py

Purpose:
    Automated background sync for odds (pre-match) and xG (post-match) data.
    Follows the event_bus pattern: start()/stop() with asyncio task loops,
    wired into the app lifespan behind METRICS_HEARTBEAT_ENABLED config flag.

    The odds scheduler uses urgency tiers that increase polling frequency
    as matches approach kickoff (6h → 30min → 10min).

Dependencies:
    - app.database
    - app.config
    - app.services.sportmonks_connector
    - app.workers._state
    - app.utils
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from bson import ObjectId

import app.database as _db
from app.config import settings
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.heartbeat")

_BACKOFF_BASE_S = 30
_BACKOFF_MAX_S = 300
_MAX_CONSECUTIVE_ERRORS = 5
_POST_MATCH_LOOKBACK_DAYS = 7

_TIER_PRIORITY = {"CLOSING": 0, "IMMINENT": 1, "APPROACHING": 2}


class OddsTickAlreadyRunningError(RuntimeError):
    """Raised when a manual odds tick is requested while another tick is active."""


class MetricsHeartbeat:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._odds_tick_lock = asyncio.Lock()

    async def start(self) -> None:
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._odds_scheduler_loop(), name="heartbeat-odds-scheduler"),
            asyncio.create_task(self._post_match_loop(), name="heartbeat-post-match"),
            asyncio.create_task(self._xg_crawler_loop(), name="heartbeat-xg-crawler"),
        ]
        xg_tick = await self._xg_tick_interval()
        logger.info(
            "Metrics heartbeat started (odds tick=%ds, xg tick=%ds, post-match delay=%dh)",
            settings.ODDS_SCHEDULER_TICK_SECONDS,
            xg_tick,
            settings.METRICS_HEARTBEAT_POST_MATCH_DELAY_HOURS,
        )

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Metrics heartbeat stopped")

    # ------------------------------------------------------------------
    # Tiered odds scheduler
    # ------------------------------------------------------------------
    async def _odds_scheduler_loop(self) -> None:
        interval = settings.ODDS_SCHEDULER_TICK_SECONDS
        await asyncio.sleep(10)  # let the app boot
        while not self._stopping:
            try:
                await self._run_odds_scheduler_tick(triggered_by="scheduler_loop")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Odds scheduler tick failed")
            await asyncio.sleep(interval)

    async def run_odds_tick_now(self, *, triggered_by: str) -> dict[str, Any]:
        """Run one odds scheduler tick immediately for admin-triggered sync."""
        started_at = utcnow()
        if self._odds_tick_lock.locked():
            raise OddsTickAlreadyRunningError("Odds scheduler tick already running")
        acquired = False
        try:
            await asyncio.wait_for(self._odds_tick_lock.acquire(), timeout=0.01)
            acquired = True
        except TimeoutError as exc:
            raise OddsTickAlreadyRunningError("Odds scheduler tick already running") from exc

        try:
            tick_result = await self._odds_scheduler_tick(triggered_by=triggered_by)
        finally:
            if acquired:
                self._odds_tick_lock.release()

        finished_at = utcnow()
        duration_ms = max(
            0,
            int((finished_at - started_at).total_seconds() * 1000),
        )
        return {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "triggered_by": triggered_by,
            "tick": tick_result,
        }

    async def _run_odds_scheduler_tick(self, *, triggered_by: str) -> dict[str, Any]:
        async with self._odds_tick_lock:
            return await self._odds_scheduler_tick(triggered_by=triggered_by)

    async def _odds_scheduler_tick(self, *, triggered_by: str) -> dict[str, Any]:
        from app.services.sportmonks_connector import sportmonks_connector
        from app.workers._state import set_synced

        now = utcnow()
        tick_deadline = now + timedelta(seconds=settings.ODDS_SCHEDULER_MAX_TICK_SECONDS)
        lookahead = timedelta(days=settings.ODDS_SCHEDULER_LOOKAHEAD_DAYS)

        # 1. Query upcoming scheduled matches
        upcoming = await _db.db.matches_v3.find(
            {"status": "SCHEDULED", "start_at": {"$gte": now, "$lte": now + lookahead}},
            {
                "_id": 1, "season_id": 1, "round_id": 1, "start_at": 1,
                "odds_meta.updated_at": 1, "odds_meta.updated_at_utc": 1, "odds_meta.source": 1,
            },
        ).to_list(length=2000)

        if not upcoming:
            logger.debug("Odds scheduler: no upcoming matches in next %dd", settings.ODDS_SCHEDULER_LOOKAHEAD_DAYS)
            return {
                "status": "ok",
                "triggered_by": triggered_by,
                "matches_in_window": 0,
                "rounds_synced": 0,
                "fixtures_synced": 0,
                "repairs": 0,
                "tier_breakdown": {},
                "deferred_rounds": 0,
            }

        # 2. Classify and collect stale rounds by tier
        # Key: (tier, season_id, round_id) → list of fixture IDs
        stale_rounds: dict[tuple[str, int, int], list[int]] = defaultdict(list)

        for doc in upcoming:
            tier, needs_sync = self._classify_match(doc, now)
            if tier == "FAR" or not needs_sync:
                continue
            sid = doc.get("season_id")
            rid = doc.get("round_id")
            if not isinstance(sid, int) or not isinstance(rid, int):
                continue
            stale_rounds[(tier, sid, rid)].append(doc["_id"])

        if not stale_rounds:
            logger.debug("Odds scheduler: %d matches all fresh", len(upcoming))
            return {
                "status": "ok",
                "triggered_by": triggered_by,
                "matches_in_window": len(upcoming),
                "rounds_synced": 0,
                "fixtures_synced": 0,
                "repairs": 0,
                "tier_breakdown": {},
                "deferred_rounds": 0,
            }

        # 3. Sort by tier priority (CLOSING first)
        work_plan = sorted(stale_rounds.keys(), key=lambda k: _TIER_PRIORITY.get(k[0], 99))
        tier_counts = defaultdict(int)
        for tier, _, _ in work_plan:
            tier_counts[tier] += 1

        # 4. Execute
        synced_rounds = 0
        synced_fixtures = 0
        repair_count = 0
        backoff = _BACKOFF_BASE_S
        errors = 0

        job_id = await self._create_job("heartbeat_odds_sync")
        deferred_rounds = 0

        for tier, season_id, round_id in work_plan:
            if utcnow() > tick_deadline:
                deferred_rounds = len(work_plan) - synced_rounds
                logger.info("Odds scheduler: tick deadline reached, deferring %d rounds", deferred_rounds)
                break
            if self._stopping:
                break
            try:
                result = await sportmonks_connector.sync_round_odds_summary(
                    round_id=round_id, season_id=season_id,
                )
                synced_rounds += 1
                synced_fixtures += int(result.get("fixtures_processed") or 0)
                backoff = _BACKOFF_BASE_S
                errors = 0

                # CLOSING tier: per-fixture repair for matches <2h to kickoff
                if tier == "CLOSING":
                    fixture_ids = stale_rounds[(tier, season_id, round_id)]
                    for fid in fixture_ids:
                        if self._stopping or utcnow() > tick_deadline:
                            break
                        try:
                            await sportmonks_connector.sync_fixture_odds_summary(
                                fid, source="heartbeat_closing",
                            )
                            repair_count += 1
                        except Exception as exc:
                            logger.warning("Odds scheduler: fixture %d repair failed: %s", fid, exc)

            except Exception as exc:
                errors += 1
                logger.warning("Odds scheduler: round %d failed: %s (backoff %ds)",
                               round_id, exc, backoff)
                if errors >= _MAX_CONSECUTIVE_ERRORS:
                    error_msg = f"Max errors after {errors} consecutive failures"
                    await self._fail_job(job_id, error_msg)
                    return {
                        "status": "failed",
                        "triggered_by": triggered_by,
                        "error": error_msg,
                        "matches_in_window": len(upcoming),
                        "rounds_synced": synced_rounds,
                        "fixtures_synced": synced_fixtures,
                        "repairs": repair_count,
                        "tier_breakdown": dict(tier_counts),
                        "deferred_rounds": deferred_rounds,
                    }
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

        await self._succeed_job(job_id, {
            "rounds_synced": synced_rounds,
            "fixtures_synced": synced_fixtures,
            "repairs": repair_count,
            "matches_in_window": len(upcoming),
            "tiers": dict(tier_counts),
        })

        # Persist scheduler status for admin observability
        await _db.db.meta.update_one(
            {"_id": "odds_scheduler_status"},
            {"$set": {
                "last_tick_at": utcnow(),
                "rounds_synced": synced_rounds,
                "fixtures_synced": synced_fixtures,
                "repairs": repair_count,
                "matches_in_window": len(upcoming),
                "tier_breakdown": dict(tier_counts),
                "triggered_by": triggered_by,
                "updated_at_utc": utcnow(),
            }},
            upsert=True,
        )

        await set_synced("heartbeat:odds", {
            "rounds_synced": synced_rounds,
            "fixtures_synced": synced_fixtures,
            "matches_in_window": len(upcoming),
        })
        logger.info("Odds scheduler: synced %d rounds (%d fixtures, %d repairs) | %s",
                     synced_rounds, synced_fixtures, repair_count, dict(tier_counts))

        await self._publish_metrics_event("odds_scheduler", synced_fixtures)

        # v3.2: fixed-snapshot passes (no API calls, local data only)
        omega_count = await self._omega_finalizer_pass()
        anchor_count = await self._fixed_snapshot_pass(upcoming)

        return {
            "status": "ok",
            "triggered_by": triggered_by,
            "matches_in_window": len(upcoming),
            "rounds_synced": synced_rounds,
            "fixtures_synced": synced_fixtures,
            "repairs": repair_count,
            "tier_breakdown": dict(tier_counts),
            "deferred_rounds": deferred_rounds,
            "omega_finalized": omega_count,
            "anchors_written": anchor_count,
        }

    @staticmethod
    def _classify_match(doc: dict[str, Any], now) -> tuple[str, bool]:
        """Classify a match into urgency tier and check if it needs sync.

        Returns (tier, needs_sync).
        """
        start_at = doc.get("start_at")
        if start_at is None:
            return ("FAR", False)
        kickoff = ensure_utc(start_at)
        hours_until = (kickoff - now).total_seconds() / 3600.0

        if hours_until < 0:
            return ("FAR", False)
        elif hours_until < 2:
            tier = "CLOSING"
            max_age_minutes = settings.ODDS_SCHEDULER_TIER_CLOSING_MINUTES
        elif hours_until < 24:
            tier = "IMMINENT"
            max_age_minutes = settings.ODDS_SCHEDULER_TIER_IMMINENT_MINUTES
        elif hours_until < 24 * settings.ODDS_SCHEDULER_LOOKAHEAD_DAYS:
            tier = "APPROACHING"
            max_age_minutes = settings.ODDS_SCHEDULER_TIER_APPROACHING_HOURS * 60
        else:
            return ("FAR", False)

        odds_meta = doc.get("odds_meta") if isinstance(doc.get("odds_meta"), dict) else {}
        updated_at = (odds_meta or {}).get("updated_at_utc") or (odds_meta or {}).get("updated_at")
        if updated_at is None:
            return (tier, True)
        age_minutes = (now - ensure_utc(updated_at)).total_seconds() / 60.0
        return (tier, age_minutes >= max_age_minutes)

    # ------------------------------------------------------------------
    # Post-match tick: sync xG for recently finished matches every 30min
    # ------------------------------------------------------------------
    async def _post_match_loop(self) -> None:
        interval = 30 * 60  # 30 minutes
        await asyncio.sleep(30)  # stagger from odds scheduler
        while not self._stopping:
            try:
                await self._post_match_tick()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Heartbeat post-match tick failed unexpectedly")
            await asyncio.sleep(interval)

    async def _post_match_tick(self) -> None:
        now = utcnow()
        delay = timedelta(hours=settings.METRICS_HEARTBEAT_POST_MATCH_DELAY_HOURS)
        lookback = timedelta(days=_POST_MATCH_LOOKBACK_DAYS)

        # Find finished matches not yet synced, within the recovery window
        candidates = await _db.db.matches_v3.find(
            {
                "status": "FINISHED",
                "start_at": {"$gte": now - lookback, "$lte": now - delay},
                "_heartbeat_xg_synced": {"$ne": True},
            },
            {"_id": 1, "season_id": 1},
        ).to_list(length=500)

        if not candidates:
            logger.debug("Heartbeat post-match: no matches need xG sync")
            return

        synced = 0
        backoff = _BACKOFF_BASE_S
        errors = 0

        job_id = await self._create_job("heartbeat_xg_sync")

        from app.services.sportmonks_connector import sportmonks_connector

        for doc in candidates:
            if self._stopping:
                break
            fixture_id = doc.get("_id")
            if not isinstance(fixture_id, int):
                continue
            try:
                await sportmonks_connector.sync_fixture_odds_summary(fixture_id, source="heartbeat_post_match")
                await _db.db.matches_v3.update_one(
                    {"_id": fixture_id},
                    {"$set": {"_heartbeat_xg_synced": True, "updated_at": utcnow(), "updated_at_utc": utcnow()}},
                )
                synced += 1
                backoff = _BACKOFF_BASE_S
                errors = 0
            except Exception as exc:
                errors += 1
                logger.warning("Heartbeat xG sync error (fixture %d): %s (backoff %ds)", fixture_id, exc, backoff)
                if errors >= _MAX_CONSECUTIVE_ERRORS:
                    await self._fail_job(job_id, f"Max errors reached after {errors} consecutive failures")
                    logger.error("Heartbeat post-match: aborting after %d consecutive errors", errors)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

        await self._succeed_job(job_id, {"matches_synced": synced, "candidates": len(candidates)})
        logger.info("Heartbeat post-match: synced xG for %d/%d matches", synced, len(candidates))

        await self._publish_metrics_event("post_match", synced)

    # ------------------------------------------------------------------
    # xG crawler: two-pointer accordion design
    #   Fresh pointer (pages 1-3): catches newly available xG in ~60-90s
    #   Deep pointer (page N→end): historical backfill
    # ------------------------------------------------------------------
    _XG_CRAWLER_STATE_KEY = "xg_crawler_state"
    _HEARTBEAT_CONFIG_KEY = "heartbeat_config"

    _FRESH_CHECK_INTERVAL = 6   # every 6th tick (~60s) run fresh instead of deep
    _FRESH_MAX_PAGE = 3         # fresh pointer cycles pages 1→3→1
    _XG_RESOLVE_INTERVAL_PAGES = 50  # run resolve pass every N deep pages

    async def _xg_tick_interval(self) -> int:
        """Read xG crawler tick interval from runtime config (meta), fallback to config.py."""
        doc = await _db.db.meta.find_one({"_id": self._HEARTBEAT_CONFIG_KEY})
        if doc and isinstance(doc.get("xg_crawler_tick_seconds"), (int, float)):
            return max(1, int(doc["xg_crawler_tick_seconds"]))
        return max(1, settings.XG_CRAWLER_TICK_SECONDS)

    async def _xg_crawler_loop(self) -> None:
        await asyncio.sleep(60)  # stagger from other loops
        tick_count = 0
        deep_pages_since_resolve = 0
        while not self._stopping:
            try:
                is_fresh_tick = (tick_count % self._FRESH_CHECK_INTERVAL == 0)
                tick_count += 1

                if is_fresh_tick:
                    new_count = await self._xg_fresh_tick()
                    # Immediate resolve when fresh data arrives (UX-critical path)
                    if new_count > 0:
                        resolved = await self._xg_resolve_pass()
                        if resolved:
                            logger.info("xG resolve (fresh): wrote xG for %d matches", resolved)
                else:
                    deep_done = await self._xg_deep_tick()
                    deep_pages_since_resolve += 1

                    # Periodic resolve during backfill
                    if deep_done or deep_pages_since_resolve >= self._XG_RESOLVE_INTERVAL_PAGES:
                        resolved = await self._xg_resolve_pass()
                        if resolved:
                            logger.info("xG resolve (deep): wrote xG for %d matches", resolved)
                        deep_pages_since_resolve = 0

                    if deep_done:
                        logger.info("xG crawler: deep backfill complete, sleeping 1h then restarting")
                        await asyncio.sleep(3600)
                        await _db.db.meta.update_one(
                            {"_id": self._XG_CRAWLER_STATE_KEY},
                            {"$set": {"deep_next_page": 1, "deep_done": False, "updated_at": utcnow(), "updated_at_utc": utcnow()}},
                            upsert=True,
                        )

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("xG crawler tick failed")
            await asyncio.sleep(await self._xg_tick_interval())

    # ------------------------------------------------------------------
    # Shared: parse API page into xg_raw batch documents
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_xg_page(rows: list[dict]) -> list[dict]:
        """Convert raw API rows into xg_raw documents."""
        batch = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not isinstance(row_id, int) or row_id <= 0:
                continue
            batch.append({
                "_id": row_id,
                "fixture_id": row.get("fixture_id") if isinstance(row.get("fixture_id"), int) else None,
                "type_id": row.get("type_id") if isinstance(row.get("type_id"), int) else None,
                "participant_id": row.get("participant_id") if isinstance(row.get("participant_id"), int) else None,
                "location": str(row.get("location") or "").strip().lower(),
                "value": ((row.get("data") or {}).get("value") if isinstance(row.get("data"), dict) else None),
            })
        return batch

    @staticmethod
    async def _insert_xg_batch(batch: list[dict]) -> int:
        """Insert batch into xg_raw, returning count of newly stored rows."""
        if not batch:
            return 0
        from pymongo.errors import BulkWriteError
        try:
            result = await _db.db.xg_raw.insert_many(batch, ordered=False)
            return len(result.inserted_ids)
        except BulkWriteError as bwe:
            return bwe.details.get("nInserted", 0)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Fresh pointer: pages 1-3, catches new xG near-realtime
    # ------------------------------------------------------------------
    async def _xg_fresh_tick(self) -> int:
        """Fetch one fresh page and store in xg_raw. Returns count of new rows."""
        from app.providers.sportmonks import sportmonks_provider

        state = await _db.db.meta.find_one({"_id": self._XG_CRAWLER_STATE_KEY})
        fresh_page = int((state or {}).get("fresh_next_page") or 1)
        total_stored = int((state or {}).get("total_stored") or 0)
        prev_newest_id = int((state or {}).get("fresh_newest_id") or 0)

        try:
            page = await sportmonks_provider.get_expected_fixtures_page(page=fresh_page)
        except Exception as exc:
            logger.warning("xG crawler: fresh API error on page %d: %s", fresh_page, exc)
            return 0

        payload = page.get("payload") or {}
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            return 0

        batch = self._parse_xg_page(rows)
        stored = await self._insert_xg_batch(batch)
        total_stored += stored

        # Track newest ID from page 1 for anomaly detection
        page_max_id = max((doc["_id"] for doc in batch), default=0) if batch else 0
        update_fields: dict[str, Any] = {
            "fresh_next_page": (fresh_page % self._FRESH_MAX_PAGE) + 1,
            "total_stored": total_stored,
            "updated_at": utcnow(),
            "updated_at_utc": utcnow(),
        }

        if fresh_page == 1 and page_max_id > 0:
            if prev_newest_id > 0 and page_max_id < prev_newest_id:
                logger.warning(
                    "xG crawler: possible Sportmonks re-index, newest_id regressed %d → %d",
                    prev_newest_id, page_max_id,
                )
            update_fields["fresh_newest_id"] = page_max_id

        if stored > 0:
            update_fields["fresh_last_found_at"] = utcnow()

        await _db.db.meta.update_one(
            {"_id": self._XG_CRAWLER_STATE_KEY},
            {"$set": update_fields},
            upsert=True,
        )

        logger.info("xG crawler: fresh page %d, +%d new rows (newest_id=%d)",
                     fresh_page, stored, page_max_id)
        return stored

    # ------------------------------------------------------------------
    # Deep pointer: historical backfill, advances page by page
    # ------------------------------------------------------------------
    async def _xg_deep_tick(self) -> bool:
        """Fetch one deep backfill page. Returns True when backfill is complete."""
        from app.providers.sportmonks import sportmonks_provider

        state = await _db.db.meta.find_one({"_id": self._XG_CRAWLER_STATE_KEY})
        # Fall back to legacy "next_page" for smooth migration from single-pointer crawler
        next_page = int((state or {}).get("deep_next_page") or (state or {}).get("next_page") or 1)
        total_stored = int((state or {}).get("total_stored") or 0)

        try:
            page = await sportmonks_provider.get_expected_fixtures_page(page=next_page)
        except Exception as exc:
            logger.warning("xG crawler: deep API error on page %d: %s", next_page, exc)
            return False

        payload = page.get("payload") or {}
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            return True  # no more data

        batch = self._parse_xg_page(rows)
        stored = await self._insert_xg_batch(batch)
        total_stored += stored

        pagination = payload.get("pagination") or {}
        has_more = bool(pagination.get("has_more"))

        await _db.db.meta.update_one(
            {"_id": self._XG_CRAWLER_STATE_KEY},
            {"$set": {
                "deep_next_page": next_page + 1 if has_more else next_page,
                "deep_done": not has_more,
                "total_stored": total_stored,
                "updated_at": utcnow(),
                "updated_at_utc": utcnow(),
            }},
            upsert=True,
        )

        if next_page % 50 == 0 or not has_more:
            logger.info("xG crawler: deep page %d, +%d rows (%d total), has_more=%s",
                        next_page, stored, total_stored, has_more)

        return not has_more

    # ------------------------------------------------------------------
    # Resolve pass: xg_raw → matches_v3
    # ------------------------------------------------------------------
    async def _xg_resolve_pass(self, batch_size: int = 100) -> int:
        """Resolve xg_raw rows into matches_v3 for any FINISHED match missing xG."""
        from app.services.sportmonks_connector import SportmonksConnector

        connector = SportmonksConnector()
        cursor = _db.db.matches_v3.find(
            {"status": "FINISHED", "has_advanced_stats": {"$ne": True}},
            {"_id": 1, "teams.home.sm_id": 1, "teams.away.sm_id": 1},
        ).limit(batch_size)

        resolved = 0
        async for match in cursor:
            fid = match["_id"]
            teams = match.get("teams") or {}
            home_sm = (teams.get("home") or {}).get("sm_id")
            away_sm = (teams.get("away") or {}).get("sm_id")
            if not home_sm or not away_sm:
                continue

            xg_rows = await _db.db.xg_raw.find(
                {"fixture_id": fid, "type_id": 5304},
            ).to_list(length=10)
            if not xg_rows:
                continue

            team_lookup = {"home_sm_id": home_sm, "away_sm_id": away_sm}
            synced, _ = await connector._write_fixture_xg(
                fixture_id=fid,
                xg_rows=xg_rows,
                team_lookup=team_lookup,
                job_id=None,
                season_id=0,
            )
            if synced:
                resolved += 1

        return resolved

    # ------------------------------------------------------------------
    # Job tracking helpers
    # ------------------------------------------------------------------
    async def _create_job(self, job_type: str, *, season_id: int | None = None) -> ObjectId:
        now = utcnow()
        doc: dict[str, Any] = {
            "type": job_type,
            "source": "heartbeat",
            "status": "running",
            "phase": "running",
            "active_lock": False,
            "admin_id": "system",
            "created_at": now,
            "started_at": now,
            "updated_at": now,
            "updated_at_utc": now,
            "finished_at": None,
            "error": None,
            "error_log": [],
            "results": None,
        }
        if season_id is not None:
            doc["season_id"] = season_id
        result = await _db.db.admin_import_jobs.insert_one(doc)
        return result.inserted_id

    async def _succeed_job(self, job_id: ObjectId, results: dict[str, Any]) -> None:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "succeeded", "phase": "done", "results": results, "updated_at": now, "updated_at_utc": now, "finished_at": now}},
        )

    async def _fail_job(self, job_id: ObjectId, message: str) -> None:
        now = utcnow()
        await _db.db.admin_import_jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "failed", "phase": "failed", "error": {"message": message, "type": "HeartbeatError"}, "updated_at": now, "updated_at_utc": now, "finished_at": now}},
        )

    # ------------------------------------------------------------------
    # v3.2: Omega-Finalizer & fixed-snapshot safety-net passes
    # ------------------------------------------------------------------

    # FIXME: ODDS_V3_BREAK — reads odds_timeline and writes fixed_snapshots.closing from stale data
    async def _omega_finalizer_pass(self) -> int:
        """Write closing snapshot from last pre-kickoff odds_timeline entry.

        Runs after the main odds sync loop. Queries matches past kickoff that
        still lack a closing snapshot. Pulls the value from timeline history —
        no Sportmonks API calls.
        """
        now = utcnow()
        cutoff = now - timedelta(hours=24)
        candidates = await _db.db.matches_v3.find(
            {
                "start_at": {"$gte": cutoff, "$lte": now},
                "odds_meta.fixed_snapshots.closing": {"$exists": False},
                "odds_timeline.0": {"$exists": True},
            },
            {"_id": 1, "start_at": 1, "odds_timeline": 1},
        ).to_list(length=500)

        finalized = 0
        for doc in candidates:
            start_at = doc.get("start_at")
            timeline = doc.get("odds_timeline")
            if not isinstance(timeline, list) or not timeline or start_at is None:
                continue
            kickoff = ensure_utc(start_at)

            # Find the last timeline entry before kickoff
            pre_kickoff = [
                e for e in timeline
                if isinstance(e, dict)
                and e.get("timestamp") is not None
                and ensure_utc(e["timestamp"]) <= kickoff
            ]
            entry = pre_kickoff[-1] if pre_kickoff else timeline[0]

            try:
                closing_snap = {
                    "h": float(entry.get("home", 0)),
                    "d": float(entry.get("draw", 0)),
                    "a": float(entry.get("away", 0)),
                    "ts_utc": entry.get("timestamp", now),
                }
            except (TypeError, ValueError):
                continue

            result = await _db.db.matches_v3.update_one(
                {
                    "_id": doc["_id"],
                    "odds_meta.fixed_snapshots.closing": {"$exists": False},
                },
                {"$set": {
                    "odds_meta.fixed_snapshots.closing": closing_snap,
                    "schema_version": "v3.2.0",
                }},
            )
            if result.modified_count:
                finalized += 1

        if finalized:
            logger.info("Omega-finalizer: wrote closing snapshot for %d matches", finalized)
        return finalized

    _SNAPSHOT_WINDOWS: list[tuple[str, float, float]] = [
        ("alpha_24h", 23.0, 25.0),
        ("beta_6h", 5.0, 7.0),
        ("omega_1h", 0.75, 1.25),
    ]

    # FIXME: ODDS_V3_BREAK — reads odds_meta.summary_1x2 and writes fixed_snapshots from stale data
    async def _fixed_snapshot_pass(self, upcoming: list[dict[str, Any]]) -> int:
        """Safety-net pass: lock time-window anchors for matches in the upcoming list.

        Catches cases where the connector path missed a window because the sync
        didn't run at the right moment.
        """
        now = utcnow()
        written = 0

        for doc in upcoming:
            start_at = doc.get("start_at")
            if start_at is None:
                continue
            kickoff = ensure_utc(start_at)
            hours_until = (kickoff - now).total_seconds() / 3600.0

            # Determine which slots are in-window
            slots_to_check: list[str] = []
            for slot_name, h_min, h_max in self._SNAPSHOT_WINDOWS:
                if h_min <= hours_until <= h_max:
                    slots_to_check.append(slot_name)

            if not slots_to_check:
                continue

            # Fetch current state for this match
            match_doc = await _db.db.matches_v3.find_one(
                {"_id": doc["_id"]},
                {"odds_meta.summary_1x2": 1, "odds_meta.fixed_snapshots": 1},
            )
            if not match_doc:
                continue
            summary = ((match_doc.get("odds_meta") or {}).get("summary_1x2") or {})
            existing_snaps = ((match_doc.get("odds_meta") or {}).get("fixed_snapshots") or {})
            if not summary:
                continue

            try:
                snap = {
                    "h": float(((summary.get("home") or {}).get("avg"))),
                    "d": float(((summary.get("draw") or {}).get("avg"))),
                    "a": float(((summary.get("away") or {}).get("avg"))),
                    "ts_utc": now,
                }
            except (TypeError, ValueError):
                continue

            for slot_name in slots_to_check:
                if existing_snaps.get(slot_name):
                    continue
                result = await _db.db.matches_v3.update_one(
                    {
                        "_id": doc["_id"],
                        f"odds_meta.fixed_snapshots.{slot_name}": {"$exists": False},
                    },
                    {"$set": {
                        f"odds_meta.fixed_snapshots.{slot_name}": snap,
                        "schema_version": "v3.2.0",
                    }},
                )
                if result.modified_count:
                    written += 1

        if written:
            logger.info("Fixed-snapshot pass: wrote %d anchors", written)
        return written

    async def _publish_metrics_event(self, tick_type: str, count: int) -> None:
        """Publish METRICS_UPDATED event via the event bus if enabled."""
        if not settings.EVENT_BUS_ENABLED:
            return
        try:
            from app.services.event_bus import event_bus
            from app.services.event_models import MetricsUpdatedEvent

            await event_bus.publish(
                MetricsUpdatedEvent(
                    source="metrics_heartbeat",
                    tick_type=str(tick_type or ""),
                    matches_synced=max(0, int(count or 0)),
                )
            )
        except Exception:
            logger.debug("Could not publish metrics.updated event", exc_info=True)


metrics_heartbeat = MetricsHeartbeat()
