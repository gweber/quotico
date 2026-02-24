"""
Q-Tip Honest Backfill — Generate & resolve tips for historical matches.

Pipeline:
1. Normalize Odds (persist odds.h2h to DB for optimizer)
2. Calibrate Engine (run exploration grid search for rho/alpha/floor)
3. Backfill Tips (generate predictions using calibrated params)

Uses ``before_date`` filtering so each tip only sees data that existed
before the match was played — no temporal leakage.

Usage:
    # Full pipeline (recommended):
    python -m tools.qtip_backfill --rerun --calibrate --batch-size 500

    # Calibration only:
    python -m tools.qtip_backfill --calibrate-only

    # Standard backfill (uses existing config):
    python -m tools.qtip_backfill --batch-size 500
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from bisect import bisect_right
from datetime import datetime

from pymongo import UpdateOne

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
log = logging.getLogger("qtip_backfill")


async def normalize_match_odds(sport_key: str | None) -> int:
    """Write odds.h2h to match documents that only have bookmaker-format odds.

    The optimizer service queries database fields directly, so it needs
    'odds.h2h' to exist on the document.
    """
    import app.database as _db

    query = {
        "status": "final",
        "odds": {"$exists": True, "$ne": {}},
        "odds.h2h": {"$exists": False},  # Only target un-normalized matches
    }
    if sport_key:
        query["sport_key"] = sport_key

    t0 = time.monotonic()
    log.info("Scanning for matches needing odds normalization...")
    cursor = _db.db.matches.find(query, {"odds": 1})

    updates = []
    count = 0

    async for match in cursor:
        odds = match.get("odds", {})
        h2h = None

        # Extract H2H from first available bookmaker
        for key, val in odds.items():
            if key in ("updated_at", "h2h", "totals", "bookmakers"):
                continue
            if isinstance(val, dict) and "home" in val:
                h2h = {"1": val["home"], "2": val["away"]}
                if "draw" in val:
                    h2h["X"] = val["draw"]
                break

        if h2h:
            updates.append(UpdateOne(
                {"_id": match["_id"]},
                {"$set": {"odds.h2h": h2h}}
            ))

        if len(updates) >= 1000:
            await _db.db.matches.bulk_write(updates)
            count += len(updates)
            updates = []
            log.info("  Normalized %d matches so far...", count)

    if updates:
        await _db.db.matches.bulk_write(updates)
        count += len(updates)

    elapsed = time.monotonic() - t0
    log.info("Normalization complete: %d documents updated in %.1fs.", count, elapsed)
    return count


async def run_calibration_step(sport_key: str | None) -> None:
    """Run exploration calibration for leagues."""
    from app.services.optimizer_service import calibrate_league, CALIBRATED_LEAGUES

    target_leagues = [sport_key] if sport_key else CALIBRATED_LEAGUES
    log.info("Starting engine calibration (Exploration Mode) for %d leagues...", len(target_leagues))

    t0 = time.monotonic()
    results_summary = []

    for idx, lg in enumerate(target_leagues, 1):
        log.info("  [%d/%d] Calibrating %s ...", idx, len(target_leagues), lg)
        lg_t0 = time.monotonic()
        try:
            result = await calibrate_league(lg, mode="exploration")
            lg_elapsed = time.monotonic() - lg_t0
            status = result.get("status", "unknown")

            if status == "calibrated":
                log.info(
                    "  [%d/%d] %-25s  ρ=%.2f  α=%.3f  floor=%.2f  BS=%.4f  RBS=%.4f  N=%d  (%.1fs)",
                    idx, len(target_leagues), lg,
                    result["rho"], result["alpha"], result["floor"],
                    result["pure_brier"], result["regularized_brier"],
                    result["evaluated"], lg_elapsed,
                )
                results_summary.append((lg, status, result))
            elif status == "skipped":
                reason = result.get("matches") or result.get("data_points") or "?"
                log.info("  [%d/%d] %-25s  SKIPPED (data=%s)  (%.1fs)",
                         idx, len(target_leagues), lg, reason, lg_elapsed)
                results_summary.append((lg, status, result))
            elif status == "kept":
                log.info("  [%d/%d] %-25s  KEPT (improvement %.1f%% < threshold)  (%.1fs)",
                         idx, len(target_leagues), lg,
                         result.get("improvement_pct", 0), lg_elapsed)
                results_summary.append((lg, status, result))
            else:
                log.warning("  [%d/%d] %-25s  %s  (%.1fs)",
                            idx, len(target_leagues), lg, status, lg_elapsed)
                results_summary.append((lg, status, result))
        except Exception as e:
            lg_elapsed = time.monotonic() - lg_t0
            log.error("  [%d/%d] %-25s  FAILED: %s  (%.1fs)",
                      idx, len(target_leagues), lg, e, lg_elapsed)
            results_summary.append((lg, "error", {}))

    total_elapsed = time.monotonic() - t0
    log.info("─── Calibration Summary (%d leagues, %.1fs total) ───", len(target_leagues), total_elapsed)
    for lg, status, res in results_summary:
        if status == "calibrated":
            log.info("  %-25s  ρ=%.2f  α=%.3f  floor=%.2f  RBS=%.4f",
                     lg, res["rho"], res["alpha"], res["floor"], res["regularized_brier"])
        else:
            log.info("  %-25s  %s", lg, status.upper())


async def clear_engine_config_cache() -> None:
    """Force cache expiry so tip generation reads fresh params."""
    import app.services.quotico_tip_service as _qts
    _qts._engine_config_expires = 0.0
    _qts._engine_config_cache = {}
    log.info("Engine config cache cleared.")


# ---------------------------------------------------------------------------
# Temporal engine config lookup — use historical params for each match date
# ---------------------------------------------------------------------------

async def load_history_snapshots(sport_key: str | None) -> dict[str, list[tuple[datetime, dict]]]:
    """Load engine_config_history snapshots, grouped by sport_key.

    Returns dict mapping sport_key -> list of (snapshot_date_utc, cache_entry)
    sorted by snapshot_date ascending.
    """
    import app.database as _db
    from app.utils import ensure_utc

    query: dict = {}
    if sport_key:
        query["sport_key"] = sport_key

    cursor = _db.db.engine_config_history.find(
        query,
        {"sport_key": 1, "snapshot_date": 1, "params": 1, "reliability": 1, "meta.source": 1},
    ).sort("snapshot_date", 1)

    grouped_all: dict[str, list[tuple[datetime, dict, str]]] = {}
    async for doc in cursor:
        sk = doc["sport_key"]
        sd = ensure_utc(doc["snapshot_date"])
        source = ((doc.get("meta") or {}).get("source")) or "legacy"
        cache_entry = {
            "_id": sk,
            "rho": doc["params"]["rho"],
            "alpha_time_decay": doc["params"]["alpha"],
            "alpha_weight_floor": doc["params"]["floor"],
            "reliability": doc.get("reliability"),
        }
        grouped_all.setdefault(sk, []).append((sd, cache_entry, source))

    grouped: dict[str, list[tuple[datetime, dict]]] = {}
    for sk, snaps in grouped_all.items():
        has_retro = any(s in {"time_machine", "time_machine_carry_forward"} for _, _, s in snaps)
        filtered = [
            (sd, ce)
            for sd, ce, src in snaps
            if (not has_retro) or src in {"time_machine", "time_machine_carry_forward", "legacy"}
        ]
        grouped[sk] = filtered
    return grouped


def find_snapshot_for_date(
    snapshots: list[tuple[datetime, dict]], match_date: datetime,
) -> dict | None:
    """Find the latest snapshot where snapshot_date <= match_date.

    Uses binary search on the sorted snapshot list.
    Returns the cache_entry dict, or None if no applicable snapshot.
    """
    from app.utils import ensure_utc

    match_date = ensure_utc(match_date)
    dates = [s[0] for s in snapshots]
    idx = bisect_right(dates, match_date)

    if idx == 0:
        return None  # all snapshots are after this match
    return snapshots[idx - 1][1]


def inject_engine_params(sport_key: str, cache_entry: dict) -> None:
    """Inject historical params into the tip service's in-memory cache."""
    import app.services.quotico_tip_service as _qts
    _qts._engine_config_cache[sport_key] = cache_entry


def _normalize_odds(match: dict) -> dict:
    """Ensure match has odds.h2h format (in-memory helper for backfill loop)."""
    odds = match.get("odds", {})
    if odds.get("h2h"):
        return match

    for key, val in odds.items():
        if key in ("updated_at", "h2h", "totals"):
            continue
        if isinstance(val, dict) and "home" in val:
            h2h: dict[str, float] = {
                "1": val["home"],
                "2": val["away"],
            }
            if "draw" in val:
                h2h["X"] = val["draw"]
            match.setdefault("odds", {})["h2h"] = h2h
            if "bookmakers" not in match["odds"]:
                match["odds"]["bookmakers"] = {key: val}
            return match
    return match


async def run_backfill(
    sport_key: str | None,
    batch_size: int,
    skip: int,
    dry_run: bool,
    max_batches: int,
    rerun: bool = False,
    calibrate: bool = False,
    calibrate_only: bool = False,
) -> None:
    import app.database as _db
    from app.services.quotico_tip_service import generate_quotico_tip, resolve_tip

    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    # --- Load historical engine params for temporal correctness ---
    import app.services.quotico_tip_service as _qts
    from app.utils import ensure_utc

    history_snapshots = await load_history_snapshots(sport_key)
    if history_snapshots:
        total_snaps = sum(len(v) for v in history_snapshots.values())
        for sk, snaps in history_snapshots.items():
            log.info("  Loaded %d engine snapshots for %s (%s \u2192 %s)",
                     len(snaps), sk,
                     snaps[0][0].strftime("%Y-%m-%d"),
                     snaps[-1][0].strftime("%Y-%m-%d"))
        log.info("Total engine history snapshots: %d across %d leagues",
                 total_snaps, len(history_snapshots))
        # Lock cache: prevent _get_engine_params from refreshing from live DB
        _qts._engine_config_expires = time.time() + 999_999
    else:
        log.warning("No engine_config_history snapshots found \u2014 using live/default params")

    warned_no_history: set[str] = set()

    # --- Phase 1: Normalization & Calibration ---
    if calibrate or calibrate_only:
        log.info("=== PHASE 1: PREPARATION ===")
        await normalize_match_odds(sport_key)
        
        # Only dry-run the backfill, not the calibration (calibration needs to write config)
        if dry_run:
            log.info("DRY RUN: Skipping actual calibration execution.")
        else:
            await run_calibration_step(sport_key)
            await clear_engine_config_cache()
            # Re-lock cache after calibration cleared it
            if history_snapshots:
                _qts._engine_config_expires = time.time() + 999_999

        if calibrate_only:
            log.info("Calibration only requested. Exiting.")
            return

    # --- Phase 2: Backfill ---
    log.info("=== PHASE 2: BACKFILL ===")

    # --rerun: delete existing tips so they get regenerated
    if rerun and not dry_run:
        del_query: dict = {"status": {"$in": ["resolved", "no_signal"]}}
        if sport_key:
            del_query["sport_key"] = sport_key
        result = await _db.db.quotico_tips.delete_many(del_query)
        log.info("RERUN: deleted %d existing tips (resolved + no_signal)", result.deleted_count)
    elif rerun and dry_run:
        log.info("RERUN: would delete existing tips (dry-run, skipping delete)")

    query: dict = {
        "status": "final",
        "odds": {"$exists": True, "$ne": {}},
        "result.outcome": {"$ne": None},
    }
    if sport_key:
        query["sport_key"] = sport_key

    total_available = await _db.db.matches.count_documents(query)
    log.info("Total eligible matches: %d (skip=%d)", total_available, skip)

    grand_generated = 0
    grand_correct = 0
    grand_no_signal = 0
    grand_skipped = 0
    grand_errors = 0
    batch_num = 0

    while batch_num < max_batches:
        offset = skip + batch_num * batch_size
        if offset >= total_available:
            break

        matches = await _db.db.matches.find(query).sort("match_date", 1).skip(offset).to_list(length=batch_size)
        if not matches:
            break

        match_ids = [str(m["_id"]) for m in matches]
        existing = await _db.db.quotico_tips.find(
            {"match_id": {"$in": match_ids}}, {"match_id": 1},
        ).to_list(length=len(match_ids))
        existing_ids = {e["match_id"] for e in existing}

        generated = 0
        correct = 0
        no_signal = 0
        skipped_count = 0
        errors = 0
        t0 = time.monotonic()

        for i, match in enumerate(matches):
            mid = str(match["_id"])
            if mid in existing_ids:
                skipped_count += 1
                continue
            try:
                # Ensure in-memory dict has h2h (even if normalize_match_odds ran, fetch might act on old state or racing)
                match = _normalize_odds(match)

                # Inject temporally-correct engine params for this match date
                match_sport = match.get("sport_key", "")
                sport_snaps = history_snapshots.get(match_sport)
                if sport_snaps:
                    snap_entry = find_snapshot_for_date(sport_snaps, match["match_date"])
                    if snap_entry:
                        inject_engine_params(match_sport, snap_entry)
                    elif match_sport not in warned_no_history:
                        log.warning("No snapshot covers %s before %s — using live/default params",
                                    match_sport, ensure_utc(match["match_date"]).strftime("%Y-%m-%d"))
                        warned_no_history.add(match_sport)
                elif match_sport not in warned_no_history:
                    log.warning("No engine history for %s — using live/default params", match_sport)
                    warned_no_history.add(match_sport)

                # Generate tip
                tip = await generate_quotico_tip(match, before_date=match["match_date"])
                tip = resolve_tip(tip, match)
                
                if not dry_run:
                    await _db.db.quotico_tips.insert_one(tip)
                
                if tip["status"] == "resolved" and tip.get("was_correct") is not None:
                    generated += 1
                    if tip["was_correct"]:
                        correct += 1
                else:
                    no_signal += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    log.error("Error for %s: %s", mid, e)

            if (i + 1) % 50 == 0:
                elapsed = time.monotonic() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                log.info(
                    "  Batch %d progress: %d/%d (%.1f/s) — gen=%d correct=%d no_sig=%d skip=%d err=%d",
                    batch_num + 1, i + 1, len(matches), rate,
                    generated, correct, no_signal, skipped_count, errors,
                )

        elapsed = time.monotonic() - t0
        grand_generated += generated
        grand_correct += correct
        grand_no_signal += no_signal
        grand_skipped += skipped_count
        grand_errors += errors
        batch_num += 1

        batch_wr = f" — win rate {correct}/{generated} = {correct/generated*100:.1f}%" if generated else ""
        log.info(
            "Batch %d done in %.1fs: %d matches — gen=%d correct=%d no_sig=%d skip=%d err=%d%s %s",
            batch_num, elapsed, len(matches),
            generated, correct, no_signal, skipped_count, errors,
            batch_wr,
            "[DRY RUN]" if dry_run else "",
        )

    # Reset cache to normal state after backfill
    await clear_engine_config_cache()

    # Summary
    total_processed = grand_generated + grand_no_signal + grand_skipped + grand_errors
    log.info("=" * 60)
    log.info("BACKFILL COMPLETE%s", " [DRY RUN]" if dry_run else "")
    log.info("  Batches:   %d", batch_num)
    log.info("  Processed: %d", total_processed)
    log.info("  Generated: %d (resolved tips)", grand_generated)
    log.info("  Correct:   %d", grand_correct)
    log.info("  No signal: %d", grand_no_signal)
    log.info("  Skipped:   %d", grand_skipped)
    log.info("  Errors:    %d", grand_errors)
    if grand_generated > 0:
        log.info("  ─── Closing Line ───")
        log.info("  Win rate:    %.1f%% (%d/%d correct)",
                 grand_correct / grand_generated * 100, grand_correct, grand_generated)
    if grand_generated + grand_no_signal > 0:
        log.info("  Signal rate: %.1f%% (%d/%d had actionable signal)",
                 grand_generated / (grand_generated + grand_no_signal) * 100,
                 grand_generated, grand_generated + grand_no_signal)
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Q-Tip honest backfill for historical matches")
    parser.add_argument("--sport", type=str, default=None, help="Filter by sport_key")
    parser.add_argument("--batch-size", type=int, default=500, help="Matches per batch")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N matches")
    parser.add_argument("--max-batches", type=int, default=9999, help="Max batches to run")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--rerun", action="store_true", help="Delete existing tips and regenerate")
    
    # New flags
    parser.add_argument("--calibrate", action="store_true", help="Run odds norm + calibration before backfill")
    parser.add_argument("--calibrate-only", action="store_true", help="Run calibration only, then exit")
    
    args = parser.parse_args()

    asyncio.run(run_backfill(
        sport_key=args.sport,
        batch_size=args.batch_size,
        skip=args.skip,
        dry_run=args.dry_run,
        max_batches=args.max_batches,
        rerun=args.rerun,
        calibrate=args.calibrate,
        calibrate_only=args.calibrate_only,
    ))


if __name__ == "__main__":
    main()
