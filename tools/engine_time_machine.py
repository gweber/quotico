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
        {"sport_key": sport_key},
        {"snapshot_date": 1},
        sort=[("snapshot_date", -1)],
    )
    if latest:
        from app.utils import ensure_utc
        return ensure_utc(latest["snapshot_date"])
    return None


async def _process_league(
    sport_key: str,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Process a single league through historical time steps."""
    async with semaphore:
        import app.database as _db
        from app.services.optimizer_service import calibrate_league, MIN_CALIBRATION_MATCHES
        from app.utils import utcnow

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
            start = latest_snapshot + timedelta(days=interval_days)
            log.info("  %s: resuming from %s (last snapshot: %s)",
                     sport_key, start.strftime("%Y-%m-%d"),
                     latest_snapshot.strftime("%Y-%m-%d"))
        else:
            start = first_viable
            log.info("  %s: starting from %s (earliest match: %s)",
                     sport_key, start.strftime("%Y-%m-%d"),
                     earliest.strftime("%Y-%m-%d"))

        # Generate date steps
        steps: list[datetime] = []
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
        errors = 0
        t0 = time.monotonic()

        for i, step_date in enumerate(steps, 1):
            step_t0 = time.monotonic()
            try:
                result = await calibrate_league(
                    sport_key, mode=mode, before_date=step_date,
                )

                status = result.get("status", "error")
                if status != "calibrated":
                    log.info("  [%d/%d] %s @ %s: %s (N=%s)",
                             i, len(steps), sport_key,
                             step_date.strftime("%Y-%m-%d"), status.upper(),
                             result.get("matches") or result.get("data_points") or "?")
                    snapshots_skipped += 1
                    continue

                # Build snapshot document
                snapshot = {
                    "sport_key": sport_key,
                    "snapshot_date": step_date,
                    "params": {
                        "rho": result["rho"],
                        "alpha": result["alpha"],
                        "floor": result["floor"],
                    },
                    "scores": {
                        "pure_brier": result["pure_brier"],
                        "regularized_brier": result["regularized_brier"],
                        "calibration_error": result["calibration_error"],
                    },
                    "baselines": result.get("baselines"),
                    "reliability": None,
                    "meta": {
                        "matches_analyzed": result["evaluated"],
                        "mode": mode,
                        "is_retroactive": True,
                        "landscape_range": result.get("landscape_range"),
                    },
                }

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
                    "BS=%.4f%s%s  (N=%d, %.1fs)%s",
                    i, len(steps), sport_key,
                    step_date.strftime("%Y-%m-%d"),
                    result["rho"], result["alpha"], result["floor"],
                    result["pure_brier"],
                    baselines_str, rel_str,
                    result["evaluated"], step_elapsed,
                    " [DRY]" if dry_run else "",
                )
                snapshots_written += 1

            except Exception as e:
                step_elapsed = time.monotonic() - step_t0
                errors += 1
                log.error("  [%d/%d] %s @ %s: ERROR: %s (%.1fs)",
                          i, len(steps), sport_key,
                          step_date.strftime("%Y-%m-%d"), e, step_elapsed)

        total_elapsed = time.monotonic() - t0
        log.info("  %s: done — %d written, %d skipped, %d errors (%.1fs total)",
                 sport_key, snapshots_written, snapshots_skipped, errors, total_elapsed)

        return {
            "sport_key": sport_key,
            "status": "completed",
            "snapshots_written": snapshots_written,
            "snapshots_skipped": snapshots_skipped,
            "errors": errors,
            "elapsed": round(total_elapsed, 1),
        }


async def run_time_machine(
    sport_key: str | None,
    interval_days: int,
    mode: str,
    dry_run: bool,
    with_reliability: bool,
    concurrency: int,
) -> None:
    import app.database as _db
    from app.services.optimizer_service import CALIBRATED_LEAGUES

    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    target_leagues = [sport_key] if sport_key else CALIBRATED_LEAGUES
    log.info("=== ENGINE TIME MACHINE ===")
    log.info("Leagues: %d | Interval: %dd | Mode: %s | Reliability: %s%s",
             len(target_leagues), interval_days, mode,
             "ON" if with_reliability else "OFF",
             " [DRY RUN]" if dry_run else "")

    semaphore = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()

    tasks = [
        _process_league(
            league, interval_days, mode, dry_run, with_reliability, semaphore,
        )
        for league in target_leagues
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.monotonic() - t0

    # Summary
    log.info("=" * 60)
    log.info("TIME MACHINE COMPLETE%s (%.1fs)", " [DRY RUN]" if dry_run else "", total_elapsed)
    for league, result in zip(target_leagues, results):
        if isinstance(result, Exception):
            log.error("  %-30s  EXCEPTION: %s", league, result)
        elif isinstance(result, dict):
            status = result.get("status", "?")
            if status == "completed":
                log.info("  %-30s  %d snapshots, %d skipped, %d errors (%.1fs)",
                         league, result["snapshots_written"],
                         result["snapshots_skipped"], result["errors"],
                         result["elapsed"])
            else:
                log.info("  %-30s  %s", league, status.upper())
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Engine Time Machine — retroactive calibration across historical data",
    )
    parser.add_argument("--sport", type=str, default=None,
                        help="Filter to one sport_key (default: all calibrated leagues)")
    parser.add_argument("--interval-days", type=int, default=30,
                        help="Step size in days (default: 30 = monthly)")
    parser.add_argument("--mode", type=str, default="exploration",
                        choices=["exploration", "refinement"],
                        help="Grid search mode (default: exploration)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--with-reliability", action="store_true",
                        help="Also compute reliability stats at each snapshot")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="Max leagues to process in parallel (default: 2)")

    args = parser.parse_args()

    asyncio.run(run_time_machine(
        sport_key=args.sport,
        interval_days=args.interval_days,
        mode=args.mode,
        dry_run=args.dry_run,
        with_reliability=args.with_reliability,
        concurrency=args.concurrency,
    ))


if __name__ == "__main__":
    main()
