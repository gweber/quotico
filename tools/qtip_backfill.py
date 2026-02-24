"""
Q-Tip Honest Backfill — Generate & resolve tips for historical matches.

Uses ``before_date`` filtering so each tip only sees data that existed
before the match was played — no temporal leakage.

Usage:
    # From project root, with venv activated:
    python -m tools.qtip_backfill --dry-run --batch-size 10          # preview
    python -m tools.qtip_backfill --batch-size 500                   # real run
    python -m tools.qtip_backfill --sport soccer_germany_bundesliga  # single league
    python -m tools.qtip_backfill --skip 5000 --batch-size 1000      # resume
    python -m tools.qtip_backfill --rerun --batch-size 500            # delete & regenerate all
"""

import argparse
import asyncio
import logging
import os
import sys
import time

# Add backend to Python path so we can import app modules
sys.path.insert(0, "backend")

# Default to local MongoDB when not set (same as dev.sh)
if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qtip_backfill")


def _normalize_odds(match: dict) -> dict:
    """Ensure match has odds.h2h format for the tip engine.

    Historical matches store odds under bookmaker names (e.g. odds.william_hill
    = {home: 1.5, draw: 3.0, away: 5.0}). The tip engine expects odds.h2h
    = {"1": 1.5, "X": 3.0, "2": 5.0}. Convert if needed.
    """
    odds = match.get("odds", {})
    if odds.get("h2h"):
        return match  # already in expected format

    # Try to extract from first bookmaker
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
            # Also store as bookmakers for EVD signal
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
) -> None:
    # Import app modules after path setup
    import app.database as _db
    from app.services.quotico_tip_service import generate_quotico_tip, resolve_tip

    # Connect to MongoDB
    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    # --rerun: delete existing tips so they get regenerated
    if rerun and not dry_run:
        del_query: dict = {"status": "resolved"}
        if sport_key:
            del_query["sport_key"] = sport_key
        result = await _db.db.quotico_tips.delete_many(del_query)
        log.info("RERUN: deleted %d existing resolved tips", result.deleted_count)
    elif rerun and dry_run:
        log.info("RERUN: would delete existing tips (dry-run, skipping delete)")

    # Historical matches store odds under bookmaker names (e.g. odds.william_hill),
    # while current matches use odds.h2h. Accept either format.
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
            log.info("No more matches to process (offset=%d >= total=%d)", offset, total_available)
            break

        matches = await _db.db.matches.find(query).sort("match_date", 1).skip(offset).to_list(length=batch_size)
        if not matches:
            break

        # Check which already have tips
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
                match = _normalize_odds(match)
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

            # Progress every 50 matches
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

    # Summary
    total_processed = grand_generated + grand_no_signal + grand_skipped + grand_errors
    log.info("=" * 60)
    log.info("BACKFILL COMPLETE%s", " [DRY RUN]" if dry_run else "")
    log.info("  Batches:   %d", batch_num)
    log.info("  Processed: %d", total_processed)
    log.info("  Generated: %d (resolved tips)", grand_generated)
    log.info("  Correct:   %d", grand_correct)
    log.info("  No signal: %d", grand_no_signal)
    log.info("  Skipped:   %d (already had tips)", grand_skipped)
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
    parser.add_argument("--sport", type=str, default=None, help="Filter by sport_key (e.g. soccer_germany_bundesliga)")
    parser.add_argument("--batch-size", type=int, default=500, help="Matches per batch (default: 500)")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N matches (for resuming)")
    parser.add_argument("--max-batches", type=int, default=9999, help="Max batches to run (default: unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB, just compute and report")
    parser.add_argument("--rerun", action="store_true", help="Delete existing resolved tips and regenerate them")
    args = parser.parse_args()

    asyncio.run(run_backfill(
        sport_key=args.sport,
        batch_size=args.batch_size,
        skip=args.skip,
        dry_run=args.dry_run,
        max_batches=args.max_batches,
        rerun=args.rerun,
    ))


if __name__ == "__main__":
    main()
