"""
tools/qtip_backfill.py

Purpose:
    Q-Tip backfill v3 for historical matches using Greenfield odds_meta data,
    temporal-safe engine snapshots, and full Qbot enrichment (2.4).

Dependencies:
    - app.services.quotico_tip_service
    - app.services.qbot_intelligence_service
    - app.services.optimizer_service
    - app.database
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from typing import Any

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


def _valid_h2h_current(match: dict[str, Any]) -> bool:
    """Check if a match has valid h2h current odds in odds_meta."""
    node = ((((match.get("odds_meta") or {}).get("markets") or {}).get("h2h") or {}).get("current") or {})
    try:
        return float(node.get("1", 0) or 0) > 0 and float(node.get("X", 0) or 0) > 0 and float(node.get("2", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


async def validate_match_odds_meta(league_id: int | None) -> dict[str, int]:
    """Read-only quality scan for final matches and odds_meta.h2h availability."""
    import app.database as _db

    query: dict[str, Any] = {"status": "final", "result.outcome": {"$ne": None}}
    if league_id is not None:
        query["league_id"] = league_id

    projection = {"odds_meta.markets.h2h.current": 1}
    t0 = time.monotonic()
    cursor = _db.db.matches.find(query, projection)
    checked = 0
    valid = 0
    missing_odds_meta = 0
    invalid_h2h_shape = 0

    async for match in cursor:
        checked += 1
        odds_meta = match.get("odds_meta")
        if not isinstance(odds_meta, dict):
            missing_odds_meta += 1
            continue
        if _valid_h2h_current(match):
            valid += 1
        else:
            invalid_h2h_shape += 1

    elapsed = time.monotonic() - t0
    stats = {
        "checked": checked,
        "valid": valid,
        "missing_odds_meta": missing_odds_meta,
        "invalid_h2h_shape": invalid_h2h_shape,
    }
    log.info(
        "odds_meta validation in %.1fs: checked=%d valid=%d missing_odds_meta=%d invalid_h2h_shape=%d",
        elapsed,
        checked,
        valid,
        missing_odds_meta,
        invalid_h2h_shape,
    )
    return stats


async def run_calibration_step(league_id: int | None) -> None:
    """Run exploration calibration for leagues."""
    from app.services.optimizer_service import _get_calibrated_league_ids, calibrate_league

    target_leagues = [league_id] if league_id is not None else await _get_calibrated_league_ids()
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
                log.info("  [%d/%d] %-25s  SKIPPED (data=%s)  (%.1fs)", idx, len(target_leagues), lg, reason, lg_elapsed)
                results_summary.append((lg, status, result))
            elif status == "kept":
                log.info(
                    "  [%d/%d] %-25s  KEPT (improvement %.1f%% < threshold)  (%.1fs)",
                    idx,
                    len(target_leagues),
                    lg,
                    result.get("improvement_pct", 0),
                    lg_elapsed,
                )
                results_summary.append((lg, status, result))
            else:
                log.warning("  [%d/%d] %-25s  %s  (%.1fs)", idx, len(target_leagues), lg, status, lg_elapsed)
                results_summary.append((lg, status, result))
        except Exception as e:
            lg_elapsed = time.monotonic() - lg_t0
            log.error("  [%d/%d] %-25s  FAILED: %s  (%.1fs)", idx, len(target_leagues), lg, e, lg_elapsed)
            results_summary.append((lg, "error", {}))

    total_elapsed = time.monotonic() - t0
    log.info("─── Calibration Summary (%d leagues, %.1fs total) ───", len(target_leagues), total_elapsed)
    for lg, status, res in results_summary:
        if status == "calibrated":
            log.info("  %-25s  ρ=%.2f  α=%.3f  floor=%.2f  RBS=%.4f", lg, res["rho"], res["alpha"], res["floor"], res["regularized_brier"])
        else:
            log.info("  %-25s  %s", lg, status.upper())


async def clear_engine_config_cache() -> None:
    """Force cache expiry so tip generation reads fresh params."""
    import app.services.quotico_tip_service as _qts

    _qts._engine_config_expires = 0.0
    _qts._engine_config_cache = {}
    log.info("Engine config cache cleared.")


async def load_history_snapshots(league_id: int | None) -> dict[int, list[tuple[datetime, dict[str, Any]]]]:
    """Load engine_config_history snapshots grouped by league_id (v3-aware)."""
    import app.database as _db
    from app.utils import ensure_utc

    query: dict[str, Any] = {}
    if league_id is not None:
        query["league_id"] = league_id

    cursor = _db.db.engine_config_history.find(
        query,
        {
            "league_id": 1,
            "snapshot_date": 1,
            "params": 1,
            "reliability": 1,
            "market_performance": 1,
            "statistical_integrity": 1,
            "meta.source": 1,
            "meta.schema_version": 1,
        },
    ).sort("snapshot_date", 1)

    grouped_all: dict[int, list[tuple[datetime, dict[str, Any], str]]] = {}
    async for doc in cursor:
        params = doc.get("params") or {}
        meta = doc.get("meta") or {}
        doc_league_id = doc.get("league_id")
        if not isinstance(doc_league_id, int) or not isinstance(doc.get("snapshot_date"), datetime):
            continue
        sd = ensure_utc(doc["snapshot_date"])
        source = str(meta.get("source") or "legacy")
        cache_entry = {
            "_id": doc_league_id,
            "rho": params.get("rho"),
            "alpha_time_decay": params.get("alpha"),
            "alpha_weight_floor": params.get("floor"),
            "reliability": doc.get("reliability"),
            "schema_version": str(meta.get("schema_version") or "legacy"),
            "market_performance": doc.get("market_performance"),
            "statistical_integrity": doc.get("statistical_integrity"),
        }
        grouped_all.setdefault(doc_league_id, []).append((sd, cache_entry, source))

    grouped: dict[int, list[tuple[datetime, dict[str, Any]]]] = {}
    for doc_league_id, snaps in grouped_all.items():
        has_retro = any(s in {"time_machine", "time_machine_carry_forward"} for _, _, s in snaps)
        filtered = [
            (sd, ce)
            for sd, ce, src in snaps
            if (not has_retro) or src in {"time_machine", "time_machine_carry_forward", "legacy"}
        ]
        grouped[doc_league_id] = filtered
    return grouped


def find_snapshot_for_date(
    snapshots: list[tuple[datetime, dict[str, Any]]],
    match_date: datetime,
) -> dict[str, Any] | None:
    """Find latest snapshot where snapshot_date <= match_date using binary search."""
    from app.utils import ensure_utc

    normalized = ensure_utc(match_date)
    dates = [s[0] for s in snapshots]
    idx = bisect_right(dates, normalized)
    if idx == 0:
        return None
    return snapshots[idx - 1][1]


def inject_engine_params(league_id: int, cache_entry: dict[str, Any]) -> None:
    """Inject historical params into quotico_tip_service in-memory cache."""
    import app.services.quotico_tip_service as _qts

    _qts._engine_config_cache[league_id] = cache_entry


async def _upsert_error_tip(match: dict[str, Any], reason: str) -> None:
    """Persist a minimal error tip to support --rerun-failed retries."""
    import app.database as _db
    from app.utils import utcnow

    tip_doc = {
        "match_id": str(match["_id"]),
        "league_id": match.get("league_id"),
        "home_team": str(match.get("home_team") or ""),
        "away_team": str(match.get("away_team") or ""),
        "home_team_id": match.get("home_team_id"),
        "away_team_id": match.get("away_team_id"),
        "match_date": match.get("match_date"),
        "status": "error",
        "error_reason": reason[:500],
        "generated_at": utcnow(),
    }
    await _db.db.quotico_tips.replace_one({"match_id": tip_doc["match_id"]}, tip_doc, upsert=True)


async def run_backfill(
    league_id: int | None,
    batch_size: int,
    skip: int,
    dry_run: bool,
    max_batches: int,
    rerun: bool = False,
    rerun_failed: bool = False,
    calibrate: bool = False,
    calibrate_only: bool = False,
) -> None:
    import app.database as _db
    import app.services.quotico_tip_service as _qts
    from app.config import settings
    from app.services.qbot_intelligence_service import enrich_tip
    from app.services.quotico_tip_service import generate_quotico_tip, resolve_tip
    from app.utils import ensure_utc

    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    history_snapshots = await load_history_snapshots(league_id)
    if history_snapshots:
        total_snaps = sum(len(v) for v in history_snapshots.values())
        for sk, snaps in history_snapshots.items():
            log.info(
                "  Loaded %d engine snapshots for %s (%s -> %s, schema=%s)",
                len(snaps),
                sk,
                snaps[0][0].strftime("%Y-%m-%d"),
                snaps[-1][0].strftime("%Y-%m-%d"),
                snaps[-1][1].get("schema_version", "legacy"),
            )
        log.info("Total engine history snapshots: %d across %d leagues", total_snaps, len(history_snapshots))
        _qts._engine_config_expires = time.time() + 999_999
    else:
        log.warning("No engine_config_history snapshots found — using live/default params")

    warned_no_history: set[str] = set()
    no_signal_warn_threshold = float(settings.QTIP_BACKFILL_NO_SIGNAL_WARN_PCT)

    if calibrate or calibrate_only:
        log.info("=== PHASE 1: PREPARATION ===")
        await validate_match_odds_meta(league_id)
        if dry_run:
            log.info("DRY RUN: skipping calibration execution.")
        else:
            await run_calibration_step(league_id)
            await clear_engine_config_cache()
            if history_snapshots:
                _qts._engine_config_expires = time.time() + 999_999
        if calibrate_only:
            log.info("Calibration only requested. Exiting.")
            return

    log.info("=== PHASE 2: BACKFILL ===")
    if rerun and rerun_failed:
        raise ValueError("--rerun and --rerun-failed are mutually exclusive")

    if (rerun or rerun_failed) and not dry_run:
        del_query: dict[str, Any] = {}
        if league_id is not None:
            del_query["league_id"] = league_id
        if rerun_failed:
            del_query["status"] = "error"
        result = await _db.db.quotico_tips.delete_many(del_query)
        mode = "rerun-failed" if rerun_failed else "rerun"
        log.info("%s: deleted %d tips in scope", mode.upper(), result.deleted_count)
    elif (rerun or rerun_failed) and dry_run:
        mode = "rerun-failed" if rerun_failed else "rerun"
        log.info("%s: would delete matching tips (dry-run)", mode.upper())

    query: dict[str, Any] = {
        "status": "final",
        "result.outcome": {"$ne": None},
        "odds_meta.markets.h2h.current": {"$exists": True, "$ne": {}},
    }
    if league_id is not None:
        query["league_id"] = league_id

    projection = {
        "_id": 1,
        "league_id": 1,
        "home_team": 1,
        "away_team": 1,
        "home_team_id": 1,
        "away_team_id": 1,
        "match_date": 1,
        "status": 1,
        "result": 1,
        "stats": 1,
        "odds_meta": 1,
    }

    total_available = await _db.db.matches.count_documents(query)
    log.info("Total eligible matches: %d (skip=%d)", total_available, skip)

    grand_generated = 0
    grand_correct = 0
    grand_no_signal = 0
    grand_skipped = 0
    grand_errors = 0
    grand_skipped_missing_odds_meta = 0
    grand_xg_seen = 0
    grand_xg_total = 0
    grand_archetypes: Counter[str] = Counter()
    batch_num = 0

    while batch_num < max_batches:
        offset = skip + batch_num * batch_size
        if offset >= total_available:
            break

        matches = await _db.db.matches.find(query, projection).sort("match_date", 1).skip(offset).to_list(length=batch_size)
        if not matches:
            break

        match_ids = [str(m["_id"]) for m in matches]
        existing = await _db.db.quotico_tips.find({"match_id": {"$in": match_ids}}, {"match_id": 1}).to_list(length=len(match_ids))
        existing_ids = {e["match_id"] for e in existing}

        generated = 0
        correct = 0
        no_signal = 0
        skipped_count = 0
        errors = 0
        skipped_missing_odds_meta = 0
        xg_seen = 0
        xg_total = 0
        archetypes: Counter[str] = Counter()
        t0 = time.monotonic()

        for i, match in enumerate(matches):
            mid = str(match["_id"])
            if mid in existing_ids:
                skipped_count += 1
                continue
            if not _valid_h2h_current(match):
                skipped_missing_odds_meta += 1
                continue
            try:
                match_league_id = match.get("league_id")
                if not isinstance(match_league_id, int):
                    raise ValueError("match.league_id must be int")
                league_snaps = history_snapshots.get(match_league_id)
                if league_snaps:
                    snap_entry = find_snapshot_for_date(league_snaps, match["match_date"])
                    if snap_entry:
                        inject_engine_params(match_league_id, snap_entry)
                    elif str(match_league_id) not in warned_no_history:
                        log.warning(
                            "No snapshot covers %s before %s — using live/default params",
                            match_league_id,
                            ensure_utc(match["match_date"]).strftime("%Y-%m-%d"),
                        )
                        warned_no_history.add(str(match_league_id))
                elif str(match_league_id) not in warned_no_history:
                    log.warning("No engine history for %s — using live/default params", match_league_id)
                    warned_no_history.add(str(match_league_id))

                tip = await generate_quotico_tip(match, before_date=match["match_date"])
                tip = await enrich_tip(tip, match=match)
                tip = resolve_tip(tip, match)

                if not dry_run:
                    await _db.db.quotico_tips.insert_one(tip)

                qbot_logic = tip.get("qbot_logic") or {}
                archetype = str(qbot_logic.get("archetype") or "").strip()
                if archetype:
                    archetypes[archetype] += 1
                post_reasoning = qbot_logic.get("post_match_reasoning") if isinstance(qbot_logic, dict) else None
                xg_total += 1
                if isinstance(post_reasoning, dict) and (
                    post_reasoning.get("xg_home") is not None or post_reasoning.get("xg_away") is not None
                ):
                    xg_seen += 1

                if tip.get("status") == "resolved" and tip.get("was_correct") is not None:
                    generated += 1
                    if tip.get("was_correct"):
                        correct += 1
                else:
                    no_signal += 1
            except Exception as e:
                errors += 1
                if not dry_run:
                    await _upsert_error_tip(match, str(e))
                if errors <= 5:
                    log.error("Error for %s: %s", mid, e)

            if (i + 1) % 50 == 0:
                elapsed = time.monotonic() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                log.info(
                    "  Batch %d progress: %d/%d (%.1f/s) — gen=%d correct=%d no_sig=%d skip=%d skip_odds=%d err=%d",
                    batch_num + 1,
                    i + 1,
                    len(matches),
                    rate,
                    generated,
                    correct,
                    no_signal,
                    skipped_count,
                    skipped_missing_odds_meta,
                    errors,
                )

        elapsed = time.monotonic() - t0
        batch_num += 1
        grand_generated += generated
        grand_correct += correct
        grand_no_signal += no_signal
        grand_skipped += skipped_count
        grand_errors += errors
        grand_skipped_missing_odds_meta += skipped_missing_odds_meta
        grand_xg_seen += xg_seen
        grand_xg_total += xg_total
        grand_archetypes.update(archetypes)

        no_signal_den = generated + no_signal
        no_signal_rate_pct = (no_signal / no_signal_den * 100.0) if no_signal_den > 0 else 0.0
        xg_coverage_pct = (xg_seen / xg_total * 100.0) if xg_total > 0 else 0.0
        error_rate_pct = (errors / len(matches) * 100.0) if matches else 0.0
        batch_wr = f" — win rate {correct}/{generated} = {correct/generated*100:.1f}%" if generated else ""

        log.info(
            "Batch %d done in %.1fs: %d matches — gen=%d correct=%d no_sig=%d (%.1f%%) skip=%d skip_odds=%d err=%d (%.1f%%)%s %s",
            batch_num,
            elapsed,
            len(matches),
            generated,
            correct,
            no_signal,
            no_signal_rate_pct,
            skipped_count,
            skipped_missing_odds_meta,
            errors,
            error_rate_pct,
            batch_wr,
            "[DRY RUN]" if dry_run else "",
        )
        log.info("Batch %d metrics: xg_coverage_pct=%.1f archetypes=%s", batch_num, xg_coverage_pct, dict(sorted(archetypes.items())))
        if no_signal_rate_pct > no_signal_warn_threshold:
            log.warning(
                "Batch %d no_signal_rate_pct=%.1f exceeded threshold=%.1f",
                batch_num,
                no_signal_rate_pct,
                no_signal_warn_threshold,
            )

    await clear_engine_config_cache()

    total_processed = grand_generated + grand_no_signal + grand_skipped + grand_errors + grand_skipped_missing_odds_meta
    no_signal_den = grand_generated + grand_no_signal
    total_no_signal_rate = (grand_no_signal / no_signal_den * 100.0) if no_signal_den > 0 else 0.0
    total_xg_coverage = (grand_xg_seen / grand_xg_total * 100.0) if grand_xg_total > 0 else 0.0
    total_error_rate = (grand_errors / total_processed * 100.0) if total_processed > 0 else 0.0

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE%s", " [DRY RUN]" if dry_run else "")
    log.info("  Batches:                 %d", batch_num)
    log.info("  Processed:               %d", total_processed)
    log.info("  Generated (resolved):    %d", grand_generated)
    log.info("  Correct:                 %d", grand_correct)
    log.info("  No signal:               %d (%.1f%%)", grand_no_signal, total_no_signal_rate)
    log.info("  Skipped (existing):      %d", grand_skipped)
    log.info("  Skipped (missing odds):  %d", grand_skipped_missing_odds_meta)
    log.info("  Errors:                  %d (%.1f%%)", grand_errors, total_error_rate)
    log.info("  xG coverage:             %.1f%%", total_xg_coverage)
    log.info("  Archetypes:              %s", dict(sorted(grand_archetypes.items())))
    if grand_generated > 0:
        log.info("  Win rate:                %.1f%% (%d/%d correct)", grand_correct / grand_generated * 100, grand_correct, grand_generated)
    if no_signal_den > 0:
        log.info("  Signal rate:             %.1f%% (%d/%d had actionable signal)", grand_generated / no_signal_den * 100, grand_generated, no_signal_den)
    log.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Q-Tip honest backfill for historical matches (v3)")
    if "--sport" in sys.argv:
        parser.error(
            "Error: --sport is deprecated. Use --league-id <int>. "
            "Example: --league-id 82 (Bundesliga)."
        )
    parser.add_argument("--league-id", type=int, default=None, help="Filter by league_id (int, Sportmonks league id)")
    parser.add_argument("--batch-size", type=int, default=500, help="Matches per batch")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N matches")
    parser.add_argument("--max-batches", type=int, default=9999, help="Max batches to run")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--rerun", action="store_true", help="Delete all existing tips in scope and regenerate")
    parser.add_argument("--rerun-failed", action="store_true", help="Delete only failed tips (status=error) and regenerate")
    parser.add_argument("--calibrate", action="store_true", help="Run validation + calibration before backfill")
    parser.add_argument("--calibrate-only", action="store_true", help="Run calibration only, then exit")
    args = parser.parse_args()

    if args.rerun and args.rerun_failed:
        parser.error("--rerun and --rerun-failed are mutually exclusive")

    asyncio.run(
        run_backfill(
            league_id=args.league_id,
            batch_size=args.batch_size,
            skip=args.skip,
            dry_run=args.dry_run,
            max_batches=args.max_batches,
            rerun=args.rerun,
            rerun_failed=args.rerun_failed,
            calibrate=args.calibrate,
            calibrate_only=args.calibrate_only,
        )
    )


if __name__ == "__main__":
    main()
