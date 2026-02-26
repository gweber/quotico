"""Seed well-known teams into the teams collection.

Usage:
    python -m tools.seed_teams
    python -m tools.seed_teams --sport soccer_epl
    python -m tools.seed_teams --dry-run
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "backend")

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

from app.services.team_seed_service import seed_core_teams


async def run(sport: str | None, dry_run: bool, verbose: bool) -> int:
    import app.database as _db

    await _db.connect_db()
    result = await seed_core_teams(sport=sport, dry_run=dry_run, verbose=verbose)

    print(
        f"{'planned' if dry_run else 'completed'} seed upserts: {result['upserted']} "
        f"(sport={sport or 'all'}, skipped_no_league={result['skipped_no_league']})"
    )
    return result["upserted"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed well-known teams into teams collection.")
    parser.add_argument("--sport", type=str, default=None, help="Optional sport_key filter.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes.")
    parser.add_argument("--verbose", action="store_true", help="Print each upsert.")
    args = parser.parse_args()
    asyncio.run(run(args.sport, args.dry_run, args.verbose))


if __name__ == "__main__":
    main()
