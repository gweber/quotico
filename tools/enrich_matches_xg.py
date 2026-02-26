"""
xG Enrichment — fetch match-level Expected Goals from Understat and write to MongoDB.

Enriches existing match documents with ``result.home_xg``, ``result.away_xg``,
and ``result.xg_provider`` fields.  Uses ``soccerdata`` + our team mapping
service for name resolution.

Usage:
    # Single league, single season:
    python -m tools.enrich_matches_xg --sport soccer_epl --season 2024

    # All supported leagues, current season:
    python -m tools.enrich_matches_xg

    # Backfill multiple seasons:
    python -m tools.enrich_matches_xg --sport soccer_epl --season 2014-2025

    # Dry run (show match counts without writing):
    python -m tools.enrich_matches_xg --dry-run

    # Force re-enrich (overwrite existing xG data):
    python -m tools.enrich_matches_xg --force
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
log = logging.getLogger("enrich_xg")


async def run_enrichment(
    sport_key: str | None,
    season_spec: str | None,
    dry_run: bool,
    force: bool,
) -> None:
    import app.database as _db
    from app.services.xg_enrichment_service import (
        enrich_matches,
        list_xg_target_sport_keys,
        parse_season_spec,
    )

    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    # Determine target leagues
    target_leagues = await list_xg_target_sport_keys(sport_key)
    if not target_leagues:
        log.error("No eligible leagues found for xG enrichment.")
        return

    # Determine target seasons
    seasons = parse_season_spec(season_spec)

    log.info(
        "xG enrichment: %d league(s) x %d season(s)%s%s",
        len(target_leagues), len(seasons),
        " [DRY RUN]" if dry_run else "",
        " [FORCE]" if force else "",
    )

    grand_matched = 0
    grand_unmatched = 0
    grand_skipped = 0
    grand_already = 0
    all_unmatched_teams: set[str] = set()
    t0_total = time.monotonic()

    for league in target_leagues:
        for season_year in seasons:
            label = f"{league} {season_year}/{season_year + 1}"
            log.info("--- %s ---", label)
            t0 = time.monotonic()

            try:
                result = await enrich_matches(
                    league, season_year, dry_run=dry_run, force=force,
                )
            except Exception as e:
                log.error("  FAILED: %s", e)
                continue

            elapsed = time.monotonic() - t0
            log.info(
                "  %s: %d matched, %d unmatched, %d skipped, %d already enriched "
                "(total=%d, %.1fs)%s",
                label,
                result["matched"], result["unmatched"],
                result["skipped"], result["already_enriched"],
                result["total"], elapsed,
                " [DRY RUN]" if dry_run else "",
            )

            if result["unmatched_teams"]:
                log.warning("  Unresolved teams: %s", result["unmatched_teams"])
                all_unmatched_teams.update(result["unmatched_teams"])

            grand_matched += result["matched"]
            grand_unmatched += result["unmatched"]
            grand_skipped += result["skipped"]
            grand_already += result["already_enriched"]

    # Summary
    total_elapsed = time.monotonic() - t0_total
    log.info("=" * 60)
    log.info("xG ENRICHMENT COMPLETE%s", " [DRY RUN]" if dry_run else "")
    log.info("  Matched:          %d", grand_matched)
    log.info("  Unmatched:        %d", grand_unmatched)
    log.info("  Skipped:          %d", grand_skipped)
    log.info("  Already enriched: %d", grand_already)
    log.info("  Total time:       %.1fs", total_elapsed)
    if all_unmatched_teams:
        log.warning("  Unresolved teams across all runs: %s", sorted(all_unmatched_teams))
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Enrich matches with xG data from Understat",
    )
    parser.add_argument(
        "--sport", type=str, default=None,
        help="Sport key (e.g. soccer_epl). Default: all supported leagues",
    )
    parser.add_argument(
        "--season", type=str, default=None,
        help="Season year or range (e.g. 2024 or 2014-2025). Default: current season",
    )
    parser.add_argument(
        "--provider", type=str, default="understat", choices=["understat"],
        help="xG data provider (default: understat)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show match counts without writing to DB",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-enrich matches that already have xG data",
    )

    args = parser.parse_args()

    asyncio.run(run_enrichment(
        sport_key=args.sport,
        season_spec=args.season,
        dry_run=args.dry_run,
        force=args.force,
    ))


if __name__ == "__main__":
    main()
