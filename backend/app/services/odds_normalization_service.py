"""Normalize legacy odds documents to the canonical ``odds.h2h`` shape."""

import logging
import time

from pymongo import UpdateOne

import app.database as _db

logger = logging.getLogger("quotico.odds_normalization")


async def normalize_match_odds(sport_key: str | None = None) -> int:
    """Persist ``odds.h2h`` for final matches that still use bookmaker-style odds.

    Legacy documents store prices under bookmaker keys such as:
    ``odds.william_hill.home/draw/away``.
    Calibration expects canonical ``odds.h2h.{1,X,2}``.
    """
    query: dict = {
        "status": "final",
        "odds": {"$exists": True, "$ne": {}},
        "odds.h2h": {"$exists": False},
    }
    if sport_key:
        query["sport_key"] = sport_key

    t0 = time.monotonic()
    cursor = _db.db.matches.find(query, {"odds": 1})

    updates: list[UpdateOne] = []
    updated = 0

    async for match in cursor:
        odds = match.get("odds", {})
        h2h = None

        for key, val in odds.items():
            if key in ("updated_at", "h2h", "totals", "bookmakers", "spreads"):
                continue
            if isinstance(val, dict) and "home" in val and "away" in val:
                h2h = {"1": val["home"], "2": val["away"]}
                if "draw" in val:
                    h2h["X"] = val["draw"]
                break

        if h2h:
            updates.append(
                UpdateOne({"_id": match["_id"]}, {"$set": {"odds.h2h": h2h}})
            )

        if len(updates) >= 1000:
            await _db.db.matches.bulk_write(updates)
            updated += len(updates)
            updates = []

    if updates:
        await _db.db.matches.bulk_write(updates)
        updated += len(updates)

    elapsed = time.monotonic() - t0
    logger.info("Odds normalization complete: %d docs updated in %.1fs", updated, elapsed)
    return updated
