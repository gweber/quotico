"""
Engine Time Machine — retroactive calibration across historical data.

Steps through history in configurable intervals, performing point-in-time
Dixon-Coles calibrations and (optionally) reliability analysis at each step.
Results are stored in the ``engine_config_history`` collection.

Each snapshot only sees data that existed at the snapshot date — no temporal
leakage.  The live ``engine_config`` document is never modified.

Usage:
    # Single league, monthly snapshots:
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga

    # All leagues, quarterly, with reliability:
    python -m tools.engine_time_machine --interval-days 90 --with-reliability

    # Resume an interrupted run (auto-detects last snapshot):
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga

    # Dry run (no DB writes):
    python -m tools.engine_time_machine --sport soccer_germany_bundesliga --dry-run
"""

import argparse
import asyncio
import concurrent.futures
import logging
import os
import sys
import time

# Add backend to Python path so we can import app modules
sys.path.insert(0, "backend")

# Default to local MongoDB when not set
if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engine_time_machine")


from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "engine_time_machine_v2"
DEFAULT_MODE = "auto"
MAX_ACCEPTABLE_RBS_WORSENING_PCT = 25.0


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(dt: datetime, months: int = 1) -> datetime:
    total = (dt.year * 12 + (dt.month - 1)) + months
    year = total // 12
    month = (total % 12) + 1
    return dt.replace(year=year, month=month, day=1)


def _is_quarter_boundary(dt: datetime) -> bool:
    return dt.month in (1, 4, 7, 10)


def _get_rbs(snapshot: dict | None) -> float | None:
    if not snapshot:
        return None
    val = (snapshot.get("scores") or {}).get("regularized_brier")
    return float(val) if val is not None else None


def _build_snapshot_doc(
    sport_key: str,
    step_date: datetime,
    *,
    source: str,
    status: str,
    mode: str,
    evaluated: int,
    window_days: int,
    params: dict | None = None,
    scores: dict | None = None,
    baselines: dict | None = None,
    reliability: dict | None = None,
    extra_meta: dict | None = None,
) -> dict:
    meta = {
        "source": source,
        "status": status,
        "mode": mode,
        "is_retroactive": True,
        "matches_analyzed": int(evaluated),
        "window_start": (step_date - timedelta(days=window_days)).isoformat(),
        "window_end": step_date.isoformat(),
        "script_version": SCRIPT_VERSION,
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "sport_key": sport_key,
        "snapshot_date": step_date,
        "params": params,
        "scores": scores,
        "baselines": baselines,
        "reliability": reliability,
        "meta": meta,
    }


async def _find_earliest_match(db, sport_key: str) -> datetime | None:
    """Find the earliest resolved match with odds for a league."""
    earliest = await db.matches.find_one(
        {
            "sport_key": sport_key,
            "status": "final",
            "odds.h2h.1": {"$gt": 0},
            "odds.h2h.X": {"$gt": 0},
            "odds.h2h.2": {"$gt": 0},
            "result.home_score": {"$exists": True},
        },
        {"match_date": 1},
        sort=[("match_date", 1)],
    )
    if earliest:
        from app.utils import ensure_utc
        return ensure_utc(earliest["match_date"])
    return None


async def _find_latest_snapshot(db, sport_key: str) -> datetime | None:
    """Find the most recent snapshot date for resume logic."""
    latest = await db.engine_config_history.find_one(
        {
            "sport_key": sport_key,
            "$or": [
                {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
                {"meta.is_retroactive": True},
            ],
        },
        {"snapshot_date": 1},
        sort=[("snapshot_date", -1)],
    )
    if latest:
        from app.utils import ensure_utc
        return ensure_utc(latest["snapshot_date"])
    return None


async def _clear_retro_snapshots(
    db,
    sport_key: str,
    *,
    dry_run: bool,
) -> int:
    """Delete only retro time-machine snapshots for a league."""
    query = {
        "sport_key": sport_key,
        "$or": [
            {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
            {"meta.is_retroactive": True},
        ],
    }
    if dry_run:
        return int(await db.engine_config_history.count_documents(query))
    result = await db.engine_config_history.delete_many(query)
    return int(result.deleted_count)


async def _process_league_inner(
    sport_key: str,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
) -> dict:
    """Process a single league through historical time steps."""
    import app.database as _db
    from app.services.optimizer_service import (
        CALIBRATION_WINDOW_DAYS,
        MIN_CALIBRATION_MATCHES,
        calibrate_league,
    )
    from app.utils import ensure_utc, utcnow

    db = _db.db
    now = utcnow()

    # Determine start date
    earliest = await _find_earliest_match(db, sport_key)
    if not earliest:
        log.warning("  %s: no eligible matches found — skipping", sport_key)
        return {"sport_key": sport_key, "status": "no_data"}

    # Need at least 1 year of data for the first calibration window
    first_viable = earliest + timedelta(days=365)
    if first_viable >= now:
        log.warning("  %s: not enough history (earliest=%s) — skipping",
                    sport_key, earliest.strftime("%Y-%m-%d"))
        return {"sport_key": sport_key, "status": "insufficient_history",
                "earliest": earliest.isoformat()}

    # Resume from last snapshot if available
    latest_snapshot = await _find_latest_snapshot(db, sport_key)
    if latest_snapshot:
        if interval_days == 30:
            start = _month_start(_add_months(latest_snapshot, 1))
        else:
            start = latest_snapshot + timedelta(days=interval_days)
        log.info("  %s: resuming from %s (last snapshot: %s)",
                 sport_key, start.strftime("%Y-%m-%d"),
                 latest_snapshot.strftime("%Y-%m-%d"))
    else:
        if interval_days == 30:
            start = _month_start(first_viable)
            if start < first_viable:
                start = _add_months(start, 1)
        else:
            start = first_viable
        log.info("  %s: starting from %s (earliest match: %s)",
                 sport_key, start.strftime("%Y-%m-%d"),
                 earliest.strftime("%Y-%m-%d"))

    # Generate date steps
    steps: list[datetime] = []
    if interval_days == 30:
        current = start
        while current < now:
            steps.append(current)
            current = _add_months(current, 1)
    else:
        current = start
        while current < now:
            steps.append(current)
            current += timedelta(days=interval_days)

    if not steps:
        log.info("  %s: already up to date", sport_key)
        return {"sport_key": sport_key, "status": "up_to_date",
                "last_snapshot": latest_snapshot.isoformat() if latest_snapshot else None}

    log.info("  %s: %d snapshots to compute (%s → %s)",
             sport_key, len(steps),
             steps[0].strftime("%Y-%m-%d"),
             steps[-1].strftime("%Y-%m-%d"))

    snapshots_written = 0
    snapshots_skipped = 0
    snapshots_carried = 0
    errors = 0
    t0 = time.monotonic()

    existing_snapshots = await db.engine_config_history.find(
        {
            "sport_key": sport_key,
            "$or": [
                {"meta.source": {"$in": ["time_machine", "time_machine_carry_forward"]}},
                {"meta.is_retroactive": True},
            ],
        },
        {"snapshot_date": 1, "params": 1, "scores": 1, "baselines": 1, "reliability": 1},
    ).sort("snapshot_date", 1).to_list(length=5000)
    for snap in existing_snapshots:
        if snap.get("snapshot_date") is not None:
            snap["snapshot_date"] = ensure_utc(snap["snapshot_date"])

    prev_snapshot: dict | None = None
    existing_idx = 0
    if steps:
        first_step = steps[0]
        while existing_idx < len(existing_snapshots) and existing_snapshots[existing_idx]["snapshot_date"] < first_step:
            prev_snapshot = existing_snapshots[existing_idx]
            existing_idx += 1

    for i, step_date in enumerate(steps, 1):
        step_t0 = time.monotonic()
        try:
            prev_rbs = _get_rbs(prev_snapshot)
            effective_mode = mode
            if mode == "auto":
                effective_mode = "exploration" if _is_quarter_boundary(step_date) else "refinement"
                if prev_rbs is not None and prev_rbs > 0:
                    # Force exploration if previous step already degraded hard.
                    # This keeps runtime low in normal months while reacting to drift.
                    if prev_rbs > 0.40:
                        effective_mode = "exploration"

            try:
                result = await asyncio.wait_for(
                    calibrate_league(sport_key, mode=effective_mode, before_date=step_date),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                errors += 1
                log.error(
                    "  [%d/%d] %s @ %s: TIMEOUT (300s)",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"),
                )
                continue

            status = result.get("status", "error")
            if status != "calibrated":
                carried = False
                if prev_snapshot:
                    carry_doc = _build_snapshot_doc(
                        sport_key,
                        step_date,
                        source="time_machine_carry_forward",
                        status=f"carry_forward_{status}",
                        mode=effective_mode,
                        evaluated=int(result.get("evaluated") or 0),
                        window_days=CALIBRATION_WINDOW_DAYS,
                        params=prev_snapshot.get("params"),
                        scores=prev_snapshot.get("scores"),
                        baselines=prev_snapshot.get("baselines"),
                        reliability=prev_snapshot.get("reliability"),
                    )
                    if not dry_run:
                        await db.engine_config_history.update_one(
                            {"sport_key": sport_key, "snapshot_date": step_date},
                            {"$set": carry_doc},
                            upsert=True,
                        )
                        prev_snapshot = carry_doc
                    carried = True
                    snapshots_carried += 1
                else:
                    snapshots_skipped += 1
                log.info(
                    "  [%d/%d] %s @ %s: %s (N=%s)%s",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"),
                    status.upper(),
                    result.get("matches") or result.get("data_points") or "?",
                    " [carried]" if carried else "",
                )
                continue

            evaluated = int(result.get("evaluated") or 0)
            candidate_rbs = float(result["regularized_brier"])
            quality_reject = False
            quality_reason = None
            if evaluated < MIN_CALIBRATION_MATCHES:
                quality_reject = True
                quality_reason = f"insufficient_matches:{evaluated}"
            elif prev_rbs is not None and prev_rbs > 0:
                worsening_pct = ((candidate_rbs - prev_rbs) / prev_rbs) * 100.0
                if worsening_pct > MAX_ACCEPTABLE_RBS_WORSENING_PCT:
                    quality_reject = True
                    quality_reason = f"rbs_worsened:{worsening_pct:.2f}%"

            if quality_reject and prev_snapshot:
                carry_doc = _build_snapshot_doc(
                    sport_key,
                    step_date,
                    source="time_machine_carry_forward",
                    status="carry_forward_quality_gate",
                    mode=effective_mode,
                    evaluated=evaluated,
                    window_days=CALIBRATION_WINDOW_DAYS,
                    params=prev_snapshot.get("params"),
                    scores=prev_snapshot.get("scores"),
                    baselines=prev_snapshot.get("baselines"),
                    reliability=prev_snapshot.get("reliability"),
                    extra_meta={
                        "quality_reason": quality_reason,
                        "candidate_scores": {
                            "pure_brier": result.get("pure_brier"),
                            "regularized_brier": result.get("regularized_brier"),
                        },
                    },
                )
                if not dry_run:
                    await db.engine_config_history.update_one(
                        {"sport_key": sport_key, "snapshot_date": step_date},
                        {"$set": carry_doc},
                        upsert=True,
                    )
                    prev_snapshot = carry_doc
                snapshots_carried += 1
                log.info(
                    "  [%d/%d] %s @ %s: QUALITY-GATE (%s) [carried]",
                    i, len(steps), sport_key, step_date.strftime("%Y-%m-%d"), quality_reason,
                )
                continue

            # Build snapshot document
            snapshot = _build_snapshot_doc(
                sport_key,
                step_date,
                source="time_machine",
                status="calibrated",
                mode=effective_mode,
                evaluated=evaluated,
                window_days=CALIBRATION_WINDOW_DAYS,
                params={
                    "rho": result["rho"],
                    "alpha": result["alpha"],
                    "floor": result["floor"],
                },
                scores={
                    "pure_brier": result["pure_brier"],
                    "regularized_brier": result["regularized_brier"],
                    "calibration_error": result["calibration_error"],
                },
                baselines=result.get("baselines"),
                reliability=None,
                extra_meta={
                    "landscape_range": result.get("landscape_range"),
                },
            )

            # Optional reliability analysis
            if with_reliability:
                try:
                    from app.services.reliability_service import analyze_engine_reliability
                    rel = await analyze_engine_reliability(
                        sport_key, before_date=step_date,
                    )
                    if rel:
                        snapshot["reliability"] = {
                            "multiplier": rel["multiplier"],
                            "cap": rel["cap"],
                            "regression_factor": rel["regression_factor"],
                            "avg_win_rate": rel["avg_win_rate"],
                        }
                except Exception:
                    log.debug("  Reliability analysis failed for %s @ %s",
                              sport_key, step_date.strftime("%Y-%m-%d"))

            if not dry_run:
                await db.engine_config_history.update_one(
                    {"sport_key": sport_key, "snapshot_date": step_date},
                    {"$set": snapshot},
                    upsert=True,
                )
                prev_snapshot = snapshot

            step_elapsed = time.monotonic() - step_t0
            baselines_str = ""
            if result.get("baselines"):
                b = result["baselines"]
                baselines_str = f"  H={b['avg_home']:.2f} A={b['avg_away']:.2f}"

            rel_str = ""
            if snapshot.get("reliability"):
                r = snapshot["reliability"]
                rel_str = f"  rel={r['multiplier']:.2f}"

            log.info(
                "  [%d/%d] %s @ %s: ρ=%.2f α=%.3f floor=%.2f  "
                "BS=%.4f%s%s  (N=%d, %.1fs)%s [mode=%s]",
                i, len(steps), sport_key,
                step_date.strftime("%Y-%m-%d"),
                result["rho"], result["alpha"], result["floor"],
                result["pure_brier"],
                baselines_str, rel_str,
                result["evaluated"], step_elapsed,
                " [DRY]" if dry_run else "", effective_mode,
            )
            snapshots_written += 1

        except Exception as e:
            step_elapsed = time.monotonic() - step_t0
            errors += 1
            log.error("  [%d/%d] %s @ %s: ERROR: %s (%.1fs)",
                      i, len(steps), sport_key,
                      step_date.strftime("%Y-%m-%d"), e, step_elapsed)

    total_elapsed = time.monotonic() - t0
    log.info("  %s: done — %d written, %d carried, %d skipped, %d errors (%.1fs total)",
             sport_key, snapshots_written, snapshots_carried, snapshots_skipped, errors, total_elapsed)

    return {
        "sport_key": sport_key,
        "status": "completed",
        "snapshots_written": snapshots_written,
        "snapshots_carried": snapshots_carried,
        "snapshots_skipped": snapshots_skipped,
        "errors": errors,
        "elapsed": round(total_elapsed, 1),
    }


def _process_league_sync(
    sport_key: str,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
) -> dict:
    """Sync wrapper for per-league time-machine run in a dedicated process."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        import app.database as _db
        loop.run_until_complete(_db.connect_db())
        try:
            result = loop.run_until_complete(
                _process_league_inner(
                    sport_key=sport_key,
                    interval_days=interval_days,
                    mode=mode,
                    dry_run=dry_run,
                    with_reliability=with_reliability,
                )
            )
            return result
        finally:
            loop.run_until_complete(_db.close_db())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def run_time_machine(
    sport_key: str | None,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    concurrency: int,
    rerun: bool,
) -> None:
    import app.database as _db
    from app.services.optimizer_service import CALIBRATED_LEAGUES

    await _db.connect_db()
    try:
        log.info("Connected to MongoDB: %s", _db.db.name)

        target_leagues = [sport_key] if sport_key else CALIBRATED_LEAGUES
        if len(target_leagues) != len(set(target_leagues)):
            log.warning("Duplicate leagues detected in target set — de-duplicating")
        target_leagues = list(dict.fromkeys(target_leagues))
        assert len(target_leagues) == len(set(target_leagues)), "Duplicate leagues detected"
        log.info("=== ENGINE TIME MACHINE ===")
        log.info("Leagues: %d | Interval: %dd | Mode: %s | Reliability: %s | Rerun: %s%s",
                 len(target_leagues), interval_days, mode,
                 "ON" if with_reliability else "OFF",
                 "ON" if rerun else "OFF",
                 " [DRY RUN]" if dry_run else "")

        if rerun:
            for league in target_leagues:
                deleted = await _clear_retro_snapshots(_db.db, league, dry_run=dry_run)
                if dry_run:
                    log.info("  %s: would delete %d retro snapshots", league, deleted)
                else:
                    log.info("  %s: deleted %d retro snapshots", league, deleted)

        t0 = time.monotonic()
        results: dict[str, dict] = {}
        if concurrency <= 1 or len(target_leagues) <= 1:
            for league in target_leagues:
                try:
                    results[league] = await _process_league_inner(
                        league, interval_days, mode, dry_run, with_reliability,
                    )
                except Exception as e:
                    log.error("League %s failed: %s", league, e)
                    results[league] = {"error": str(e)}
        else:
            max_workers = min(len(target_leagues), max(1, int(concurrency)))
            with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        _process_league_sync,
                        league,
                        interval_days,
                        mode,
                        dry_run,
                        with_reliability,
                    ): league
                    for league in target_leagues
                }
                for future in concurrent.futures.as_completed(futures):
                    league = futures[future]
                    try:
                        results[league] = future.result()
                    except Exception as e:
                        log.error("League %s failed: %s", league, e)
                        results[league] = {"error": str(e)}

        total_elapsed = time.monotonic() - t0

        # Summary
        log.info("=" * 60)
        log.info("TIME MACHINE COMPLETE%s (%.1fs)", " [DRY RUN]" if dry_run else "", total_elapsed)
        for league in target_leagues:
            result = results.get(league) or {}
            if "error" in result:
                log.error("  %-30s  EXCEPTION: %s", league, result["error"])
                continue
            status = result.get("status", "?")
            if status == "completed":
                log.info("  %-30s  %d snapshots, %d carried, %d skipped, %d errors (%.1fs)",
                         league, result["snapshots_written"],
                         result.get("snapshots_carried", 0),
                         result["snapshots_skipped"], result["errors"],
                         result["elapsed"])
            else:
                log.info("  %-30s  %s", league, str(status).upper())
        log.info("=" * 60)
    finally:
        await _db.close_db()


def main():
    parser = argparse.ArgumentParser(
        description="Engine Time Machine — retroactive calibration across historical data",
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Filter to one sport_key (default: all calibrated leagues)")
    parser.add_argument("--interval-days", type=int, default=30,
                        help="Step size in days (default: 30 = monthly)")
    parser.add_argument("--mode", type=str, default=DEFAULT_MODE,
                        choices=["auto", "exploration", "refinement"],
                        help="Grid search mode (default: auto)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--with-reliability", action="store_true",
                        help="Also compute reliability stats at each snapshot")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Max leagues to process in parallel (default: 8)")
    parser.add_argument("--rerun", action="store_true",
                        help="Delete retro time-machine snapshots first, then rebuild from scratch")

    args = parser.parse_args()

    asyncio.run(run_time_machine(
        sport_key=args.sport,
        interval_days=args.interval_days,
        mode=args.mode,
        dry_run=args.dry_run,
        with_reliability=args.with_reliability,
        concurrency=args.concurrency,
        rerun=args.rerun,
    ))


if __name__ == "__main__":
    main()
