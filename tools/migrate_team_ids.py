"""Backfill team_id references across all collections.

Usage:
    python -m tools.migrate_team_ids
    python -m tools.migrate_team_ids --collection matches
    python -m tools.migrate_team_ids --dry-run
    python -m tools.migrate_team_ids --batch-size 500
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from bson import ObjectId
from pymongo import UpdateOne

sys.path.insert(0, "backend")

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017/quotico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_team_ids")


@dataclass
class Stats:
    migrated: int = 0
    skipped: int = 0
    errors: int = 0


def _to_object_id(value: Any) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return None


async def _flush_ops(collection, ops: list[UpdateOne], dry_run: bool) -> int:
    if not ops:
        return 0
    count = len(ops)
    if not dry_run:
        await collection.bulk_write(ops, ordered=False)
    ops.clear()
    return count


async def migrate_matches(registry, batch_size: int, dry_run: bool) -> Stats:
    import app.database as _db

    stats = Stats()
    ops: list[UpdateOne] = []
    cursor = _db.db.matches.find(
        {"$or": [{"home_team_id": {"$exists": False}}, {"away_team_id": {"$exists": False}}]},
        {"home_team": 1, "away_team": 1, "sport_key": 1},
    )

    async for doc in cursor:
        try:
            sport_key = doc.get("sport_key", "")
            home_name = doc.get("home_team", "")
            away_name = doc.get("away_team", "")
            home_id = await registry.resolve(home_name, sport_key)
            away_id = await registry.resolve(away_name, sport_key)

            ops.append(
                UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"home_team_id": home_id, "away_team_id": away_id}},
                )
            )
            if len(ops) >= batch_size:
                stats.migrated += await _flush_ops(_db.db.matches, ops, dry_run)
        except Exception as exc:
            stats.errors += 1
            log.error("matches %s failed: %s", doc.get("_id"), exc)

    stats.migrated += await _flush_ops(_db.db.matches, ops, dry_run)
    return stats


async def migrate_quotico_tips(batch_size: int, dry_run: bool) -> Stats:
    import app.database as _db

    stats = Stats()
    ops: list[UpdateOne] = []
    cursor = _db.db.quotico_tips.find(
        {"$or": [{"home_team_id": {"$exists": False}}, {"away_team_id": {"$exists": False}}], "match_id": {"$exists": True}},
        {"match_id": 1},
    )

    async for tip in cursor:
        try:
            match_oid = _to_object_id(tip.get("match_id"))
            if not match_oid:
                stats.skipped += 1
                continue

            match = await _db.db.matches.find_one(
                {"_id": match_oid},
                {"home_team_id": 1, "away_team_id": 1},
            )
            if not match or not match.get("home_team_id") or not match.get("away_team_id"):
                stats.skipped += 1
                continue

            ops.append(
                UpdateOne(
                    {"_id": tip["_id"]},
                    {"$set": {
                        "home_team_id": match["home_team_id"],
                        "away_team_id": match["away_team_id"],
                    }},
                )
            )
            if len(ops) >= batch_size:
                stats.migrated += await _flush_ops(_db.db.quotico_tips, ops, dry_run)
        except Exception as exc:
            stats.errors += 1
            log.error("quotico_tips %s failed: %s", tip.get("_id"), exc)

    stats.migrated += await _flush_ops(_db.db.quotico_tips, ops, dry_run)
    return stats


async def migrate_fantasy_picks(registry, batch_size: int, dry_run: bool) -> Stats:
    import app.database as _db

    stats = Stats()
    ops: list[UpdateOne] = []
    cursor = _db.db.fantasy_picks.find(
        {"team_id": {"$exists": False}},
        {"team": 1, "team_name": 1, "match_id": 1},
    )

    async for pick in cursor:
        try:
            match_oid = _to_object_id(pick.get("match_id"))
            if not match_oid:
                stats.skipped += 1
                continue

            match = await _db.db.matches.find_one(
                {"_id": match_oid},
                {"sport_key": 1},
            )
            if not match:
                stats.skipped += 1
                continue

            team_name = pick.get("team_name") or pick.get("team") or ""
            team_id = await registry.resolve(team_name, match.get("sport_key", ""))
            ops.append(
                UpdateOne(
                    {"_id": pick["_id"]},
                    {"$set": {"team_id": team_id, "team_name": team_name}},
                )
            )
            if len(ops) >= batch_size:
                stats.migrated += await _flush_ops(_db.db.fantasy_picks, ops, dry_run)
        except Exception as exc:
            stats.errors += 1
            log.error("fantasy_picks %s failed: %s", pick.get("_id"), exc)

    stats.migrated += await _flush_ops(_db.db.fantasy_picks, ops, dry_run)
    return stats


async def migrate_survivor_entries(registry, batch_size: int, dry_run: bool) -> Stats:
    import app.database as _db

    stats = Stats()
    ops: list[UpdateOne] = []
    cursor = _db.db.survivor_entries.find({}, {"picks": 1, "sport_key": 1, "used_team_ids": 1})

    async for entry in cursor:
        try:
            changed = False
            sport_key = entry.get("sport_key", "")
            used_team_ids = list(entry.get("used_team_ids", []))
            used_set = set(used_team_ids)
            picks = entry.get("picks", [])

            for pick in picks:
                if pick.get("team_id"):
                    used_set.add(pick["team_id"])
                    continue
                team_name = pick.get("team_name") or pick.get("team") or ""
                team_id = await registry.resolve(team_name, sport_key)
                pick["team_id"] = team_id
                pick["team_name"] = team_name
                used_set.add(team_id)
                changed = True

            new_used = list(used_set)
            if set(entry.get("used_team_ids", [])) != set(new_used):
                changed = True

            if not changed:
                stats.skipped += 1
                continue

            ops.append(
                UpdateOne(
                    {"_id": entry["_id"]},
                    {"$set": {"picks": picks, "used_team_ids": new_used}},
                )
            )
            if len(ops) >= batch_size:
                stats.migrated += await _flush_ops(_db.db.survivor_entries, ops, dry_run)
        except Exception as exc:
            stats.errors += 1
            log.error("survivor_entries %s failed: %s", entry.get("_id"), exc)

    stats.migrated += await _flush_ops(_db.db.survivor_entries, ops, dry_run)
    return stats


async def migrate_betting_slips(registry, batch_size: int, dry_run: bool) -> Stats:
    import app.database as _db

    stats = Stats()
    ops: list[UpdateOne] = []
    cursor = _db.db.betting_slips.find(
        {"selections": {"$elemMatch": {"market": {"$in": ["survivor_pick", "fantasy_pick"]}}}},
        {"selections": 1},
    )

    async for slip in cursor:
        try:
            changed = False
            selections = slip.get("selections", [])

            for sel in selections:
                market = sel.get("market")
                if market not in ("survivor_pick", "fantasy_pick"):
                    continue
                if sel.get("team_id"):
                    continue

                match_oid = _to_object_id(sel.get("match_id"))
                if not match_oid:
                    continue
                match = await _db.db.matches.find_one({"_id": match_oid}, {"sport_key": 1})
                if not match:
                    continue

                team_name = sel.get("team_name") or sel.get("pick") or ""
                team_id = await registry.resolve(team_name, match.get("sport_key", ""))
                sel["team_id"] = team_id
                sel["team_name"] = team_name
                changed = True

            if slip.get("type") == "survivor":
                used_ids = set(slip.get("used_team_ids", []))
                for sel in selections:
                    if sel.get("market") == "survivor_pick" and sel.get("team_id"):
                        used_ids.add(sel["team_id"])
                if set(slip.get("used_team_ids", [])) != used_ids:
                    slip["used_team_ids"] = list(used_ids)
                    changed = True

            if not changed:
                stats.skipped += 1
                continue

            update_doc = {"selections": selections}
            if "used_team_ids" in slip:
                update_doc["used_team_ids"] = slip["used_team_ids"]
            ops.append(UpdateOne({"_id": slip["_id"]}, {"$set": update_doc}))
            if len(ops) >= batch_size:
                stats.migrated += await _flush_ops(_db.db.betting_slips, ops, dry_run)
        except Exception as exc:
            stats.errors += 1
            log.error("betting_slips %s failed: %s", slip.get("_id"), exc)

    stats.migrated += await _flush_ops(_db.db.betting_slips, ops, dry_run)
    return stats


async def verify_post_migration() -> None:
    import app.database as _db

    checks = [
        ("matches", "home_team_id", {"home_team_id": {"$exists": False}}),
        ("matches", "away_team_id", {"away_team_id": {"$exists": False}}),
        ("quotico_tips", "home_team_id", {"home_team_id": {"$exists": False}, "match_id": {"$exists": True}}),
        ("fantasy_picks", "team_id", {"team_id": {"$exists": False}}),
    ]
    for coll, field, query in checks:
        missing = await _db.db[coll].count_documents(query)
        if missing != 0:
            raise AssertionError(f"{coll}.{field}: {missing} documents still missing")

    pipeline = [
        {"$match": {"home_team_id": {"$exists": True}}},
        {"$lookup": {"from": "teams", "localField": "home_team_id", "foreignField": "_id", "as": "team"}},
        {"$match": {"team": {"$size": 0}}},
        {"$count": "orphaned"},
    ]
    result = await _db.db.matches.aggregate(pipeline).to_list(length=1)
    if result and result[0].get("orphaned", 0) != 0:
        raise AssertionError(f"matches.home_team_id orphaned: {result[0]['orphaned']}")


async def run(collection: str, batch_size: int, dry_run: bool) -> int:
    import app.database as _db
    from app.services.team_registry_service import TeamRegistry

    await _db.connect_db()
    log.info("Connected to MongoDB: %s", _db.db.name)

    # Stage 7 hard gate: migration only allowed after substantial seed.
    seed_count = await _db.db.teams.count_documents({"source": "seed"})
    log.info("Seed precheck: teams with source='seed' => %d", seed_count)
    if seed_count < 70:
        log.error("Only %d seeded teams found (expected 70+). Run seed_teams.py first.", seed_count)
        return 1

    registry = TeamRegistry.get()
    await registry.initialize()
    log.info("TeamRegistry initialized successfully.")

    auto_before = await _db.db.teams.count_documents({"source": "auto"})

    runners = {
        "matches": lambda: migrate_matches(registry, batch_size, dry_run),
        "quotico_tips": lambda: migrate_quotico_tips(batch_size, dry_run),
        "fantasy_picks": lambda: migrate_fantasy_picks(registry, batch_size, dry_run),
        "survivor_entries": lambda: migrate_survivor_entries(registry, batch_size, dry_run),
        "betting_slips": lambda: migrate_betting_slips(registry, batch_size, dry_run),
    }
    order = ["matches", "quotico_tips", "fantasy_picks", "survivor_entries", "betting_slips"]

    targets = [collection] if collection != "all" else order
    summaries: dict[str, Stats] = {}
    for target in targets:
        log.info("--- Migrating %s%s ---", target, " [DRY RUN]" if dry_run else "")
        summaries[target] = await runners[target]()
        s = summaries[target]
        log.info("%s: migrated=%d skipped=%d errors=%d", target, s.migrated, s.skipped, s.errors)

    auto_after = await _db.db.teams.count_documents({"source": "auto"})
    log.warning("Auto-created teams during run: %d", max(0, auto_after - auto_before))

    if collection == "all":
        await verify_post_migration()
        log.info("Verification passed.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill team_id references across all collections.")
    parser.add_argument(
        "--collection",
        choices=["all", "matches", "quotico_tips", "fantasy_picks", "survivor_entries", "betting_slips"],
        default="all",
        help="Migrate a single collection or all.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing.")
    parser.add_argument("--batch-size", type=int, default=500, help="Bulk write batch size.")
    args = parser.parse_args()

    code = asyncio.run(run(args.collection, args.batch_size, args.dry_run))
    sys.exit(code)


if __name__ == "__main__":
    main()
