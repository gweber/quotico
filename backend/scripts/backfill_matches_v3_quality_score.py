"""
backend/scripts/backfill_matches_v3_quality_score.py

Purpose:
    Backfill quality scoring fields for matches_v3 based on odds/meta anomalies.
    Uses a MongoDB update pipeline to compute severity and quality_score in-place.

Usage:
    cd backend
    python scripts/backfill_matches_v3_quality_score.py
    python scripts/backfill_matches_v3_quality_score.py --kickoff-window-hours 8 --min-snapshots 3 --entropy-threshold 0.12 --overround-floor 0.99
"""

from __future__ import annotations

import argparse
from typing import Any

from pymongo import MongoClient

from app.config import settings


def build_pipeline(
    *,
    kickoff_window_hours: int,
    min_snapshots: int,
    entropy_threshold: float,
    overround_floor: float,
) -> list[dict[str, Any]]:
    kickoff_window_ms = int(kickoff_window_hours) * 60 * 60 * 1000
    min_snapshots_val = int(min_snapshots)
    entropy_val = float(entropy_threshold)
    overround_floor_val = float(overround_floor)

    # FIXME: ODDS_V3_BREAK — quality score reads odds_timeline, market_entropy, and summary_1x2 no longer produced by connector
    return [
        {
            "$set": {
                "_oqm_snapshot_count": {"$size": {"$ifNull": ["$odds_timeline", []]}},
                "_oqm_lineup_count": {"$size": {"$ifNull": ["$lineups", []]}},
                "_oqm_spread": {"$ifNull": ["$odds_meta.market_entropy.current_spread_pct", None]},
                "_oqm_drift": {"$ifNull": ["$odds_meta.market_entropy.drift_velocity_3h", None]},
                "_oqm_home_avg": {"$ifNull": ["$odds_meta.summary_1x2.home.avg", None]},
                "_oqm_draw_avg": {"$ifNull": ["$odds_meta.summary_1x2.draw.avg", None]},
                "_oqm_away_avg": {"$ifNull": ["$odds_meta.summary_1x2.away.avg", None]},
                "_oqm_near_kickoff": {
                    "$and": [
                        {"$in": ["$status", ["SCHEDULED", "LIVE"]]},
                        {"$gte": ["$start_at", "$$NOW"]},
                        {"$lte": ["$start_at", {"$add": ["$$NOW", kickoff_window_ms]}]},
                    ]
                },
            }
        },
        {
            "$set": {
                "_oqm_overround": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gt": ["$_oqm_home_avg", 0]},
                                {"$gt": ["$_oqm_draw_avg", 0]},
                                {"$gt": ["$_oqm_away_avg", 0]},
                            ]
                        },
                        {
                            "$add": [
                                {"$divide": [1, "$_oqm_home_avg"]},
                                {"$divide": [1, "$_oqm_draw_avg"]},
                                {"$divide": [1, "$_oqm_away_avg"]},
                            ]
                        },
                        None,
                    ]
                },
                "_oqm_sev_low_snapshot_count": {
                    "$cond": [
                        {"$and": ["$_oqm_near_kickoff", {"$lt": ["$_oqm_snapshot_count", min_snapshots_val]}]},
                        30,
                        0,
                    ]
                },
                "_oqm_sev_high_entropy": {
                    "$cond": [
                        {
                            "$or": [
                                {"$gte": ["$_oqm_spread", entropy_val]},
                                {"$gte": ["$_oqm_drift", entropy_val]},
                            ]
                        },
                        35,
                        0,
                    ]
                },
                "_oqm_sev_negative_overround": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$_oqm_overround", None]},
                                {"$lt": ["$_oqm_overround", overround_floor_val]},
                            ]
                        },
                        45,
                        0,
                    ]
                },
                "_oqm_sev_missing_lineups": {
                    "$cond": [
                        {"$and": ["$_oqm_near_kickoff", {"$eq": ["$_oqm_lineup_count", 0]}]},
                        25,
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "quality_score": {
                    "$max": [
                        0,
                        {
                            "$min": [
                                100,
                                {
                                    "$subtract": [
                                        100,
                                        {
                                            "$add": [
                                                "$_oqm_sev_low_snapshot_count",
                                                "$_oqm_sev_high_entropy",
                                                "$_oqm_sev_negative_overround",
                                                "$_oqm_sev_missing_lineups",
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                    ]
                },
                "quality_severity_score": {
                    "$add": [
                        "$_oqm_sev_low_snapshot_count",
                        "$_oqm_sev_high_entropy",
                        "$_oqm_sev_negative_overround",
                        "$_oqm_sev_missing_lineups",
                    ]
                },
            }
        },
        {
            "$unset": [
                "_oqm_snapshot_count",
                "_oqm_lineup_count",
                "_oqm_spread",
                "_oqm_drift",
                "_oqm_home_avg",
                "_oqm_draw_avg",
                "_oqm_away_avg",
                "_oqm_near_kickoff",
                "_oqm_overround",
                "_oqm_sev_low_snapshot_count",
                "_oqm_sev_high_entropy",
                "_oqm_sev_negative_overround",
                "_oqm_sev_missing_lineups",
            ]
        },
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill quality_score for matches_v3.")
    parser.add_argument("--kickoff-window-hours", type=int, default=8)
    parser.add_argument("--min-snapshots", type=int, default=3)
    parser.add_argument("--entropy-threshold", type=float, default=0.12)
    parser.add_argument("--overround-floor", type=float, default=0.99)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB_NAME]
    pipeline = build_pipeline(
        kickoff_window_hours=args.kickoff_window_hours,
        min_snapshots=args.min_snapshots,
        entropy_threshold=args.entropy_threshold,
        overround_floor=args.overround_floor,
    )
    result = db.matches_v3.update_many({}, pipeline)
    print(
        f"Backfill complete: matched={result.matched_count} modified={result.modified_count} "
        f"(kickoff_window_hours={args.kickoff_window_hours}, min_snapshots={args.min_snapshots}, "
        f"entropy_threshold={args.entropy_threshold}, overround_floor={args.overround_floor})"
    )


if __name__ == "__main__":
    main()
