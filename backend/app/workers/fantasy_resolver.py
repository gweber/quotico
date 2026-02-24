"""Resolve fantasy picks for completed matches."""

import logging

from bson import ObjectId

import app.database as _db
from app.services.fantasy_service import calculate_fantasy_points
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.fantasy_resolver")


async def resolve_fantasy_picks() -> None:
    """Score pending fantasy picks whose matches have completed.

    Smart sleep: skips if no pending fantasy picks exist.
    """
    from datetime import timedelta

    state_key = "fantasy_resolver"
    if await recently_synced(state_key, timedelta(hours=6)):
        has_pending = await _db.db.fantasy_picks.find_one({"status": "pending"})
        if not has_pending:
            logger.debug("Smart sleep: no pending fantasy picks")
            return

    now = utcnow()
    resolved_count = 0

    pending = await _db.db.fantasy_picks.find(
        {"status": "pending"}
    ).to_list(length=5000)

    for pick in pending:
        match = await _db.db.matches.find_one({"_id": ObjectId(pick["match_id"])})
        if not match or match["status"] != "final":
            continue
        result = match.get("result", {})
        if result.get("home_score") is None or result.get("away_score") is None:
            continue

        team = pick["team"]
        home = match["home_team"]
        away = match["away_team"]
        home_score = result["home_score"]
        away_score = result["away_score"]

        # Determine goals scored/conceded from the picked team's perspective
        if team == home:
            goals_scored = home_score
            goals_conceded = away_score
        elif team == away:
            goals_scored = away_score
            goals_conceded = home_score
        else:
            logger.warning("Fantasy pick team '%s' not in match %s", team, pick["match_id"])
            continue

        # Determine match result from picked team's perspective
        if goals_scored > goals_conceded:
            match_result = "won"
        elif goals_scored < goals_conceded:
            match_result = "lost"
        else:
            match_result = "draw"

        # Get squad config for scoring mode
        squad = await _db.db.squads.find_one({"_id": ObjectId(pick["squad_id"])})
        pure_stats_only = True
        if squad:
            config = squad.get("game_mode_config", {})
            pure_stats_only = config.get("pure_stats_only", True)

        fantasy_points = calculate_fantasy_points(
            goals_scored=goals_scored,
            goals_conceded=goals_conceded,
            pure_stats_only=pure_stats_only,
        )

        await _db.db.fantasy_picks.update_one(
            {"_id": pick["_id"]},
            {"$set": {
                "goals_scored": goals_scored,
                "goals_conceded": goals_conceded,
                "match_result": match_result,
                "fantasy_points": fantasy_points,
                "status": "resolved",
                "updated_at": now,
            }},
        )
        resolved_count += 1

    if resolved_count:
        logger.info("Resolved %d fantasy picks", resolved_count)
    await set_synced(state_key)
