import logging
from datetime import timedelta

import app.database as _db
from app.providers.odds_api import SUPPORTED_SPORTS, odds_provider
from app.services.match_service import sync_matches_for_sport
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.odds_poller")


async def poll_odds() -> None:
    """Schedule-aware odds polling.

    Only polls sports that have matches in the next 48 hours,
    avoiding unnecessary API calls at 3 AM for matches at 3 PM.
    Tracks synced_at per sport to avoid re-polling after restarts.
    """
    now = utcnow()
    window = now + timedelta(hours=48)
    any_polled = False
    total_matches = 0
    total_odds_changed = 0

    for sport_key in SUPPORTED_SPORTS:
        # Check if there are upcoming matches in the next 48h for this sport
        has_upcoming = await _db.db.matches.find_one({
            "sport_key": sport_key,
            "status": {"$in": ["upcoming", "live"]},
            "commence_time": {"$lte": window},
        })

        # Always poll if we have no matches yet (initial load) or if matches are soon
        should_poll = has_upcoming is not None or await _is_initial_load(sport_key)

        if not should_poll:
            continue

        # Skip if this sport was polled recently (e.g. after a restart)
        state_key = f"odds:{sport_key}"
        if await recently_synced(state_key, timedelta(minutes=25)):
            logger.debug("Smart sleep: %s odds polled recently, skipping", sport_key)
            continue

        try:
            result = await sync_matches_for_sport(sport_key)
            matches_count = result["matches"]
            odds_changed = result["odds_changed"]
            await set_synced(state_key, metrics={"matches": matches_count, "odds_changed": odds_changed})
            any_polled = True
            total_matches += matches_count
            total_odds_changed += odds_changed
            if matches_count > 0:
                logger.info("Polled %s: %d matches, %d odds changed", sport_key, matches_count, odds_changed)
                await _snapshot_odds(sport_key)
        except Exception as e:
            logger.error("Poll failed for %s: %s", sport_key, e)

    # Always record that the worker ran (so admin panel shows accurate "last sync")
    await set_synced("odds_poller", metrics={
        "matches": total_matches,
        "odds_changed": total_odds_changed,
    })

    if any_polled:
        usage = odds_provider.api_usage
        logger.info(
            "API usage: %s used, %s remaining",
            usage.get("requests_used", "?"),
            usage.get("requests_remaining", "?"),
        )


async def _snapshot_odds(sport_key: str) -> None:
    """Record current odds as a point-in-time snapshot for line movement tracking."""
    now = utcnow()
    matches = await _db.db.matches.find(
        {"sport_key": sport_key, "status": "upcoming"},
        {"_id": 1, "external_id": 1, "sport_key": 1, "current_odds": 1, "totals_odds": 1, "spreads_odds": 1},
    ).to_list(length=200)

    if not matches:
        return

    docs = [
        {
            "match_id": str(m["_id"]),
            "external_id": m["external_id"],
            "sport_key": m["sport_key"],
            "odds": m["current_odds"],
            "totals_odds": m.get("totals_odds", {}),
            "spreads_odds": m.get("spreads_odds", {}),
            "snapshot_at": now,
        }
        for m in matches
    ]
    await _db.db.odds_snapshots.insert_many(docs, ordered=False)
    logger.debug("Snapshotted odds for %d %s matches", len(docs), sport_key)


async def _is_initial_load(sport_key: str) -> bool:
    """Check if we have any matches at all for this sport (first run)."""
    existing = await _db.db.matches.find_one({"sport_key": sport_key})
    return existing is None
