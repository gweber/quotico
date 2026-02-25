"""Resolve survivor picks for completed matches."""

import logging

from bson import ObjectId

import app.database as _db
from app.utils import utcnow
from app.workers._state import recently_synced, set_synced

logger = logging.getLogger("quotico.survivor_resolver")


async def resolve_survivor_picks() -> None:
    """Check survivor entries with pending picks and resolve them.

    Smart sleep: skips if no survivor entries have pending picks.
    """
    from datetime import timedelta

    state_key = "survivor_resolver"
    if await recently_synced(state_key, timedelta(hours=6)):
        has_pending = await _db.db.survivor_entries.find_one({
            "status": "alive",
            "picks.result": "pending",
        })
        if not has_pending:
            logger.debug("Smart sleep: no pending survivor picks")
            return

    now = utcnow()
    resolved_count = 0

    # Find alive entries with pending picks
    entries = await _db.db.survivor_entries.find({
        "status": "alive",
        "picks.result": "pending",
    }).to_list(length=5000)

    for entry in entries:
        updated = False
        for i, pick in enumerate(entry.get("picks", [])):
            if pick["result"] != "pending":
                continue

            match = await _db.db.matches.find_one({"_id": ObjectId(pick["match_id"])})
            if not match or match["status"] != "final" or not match.get("result", {}).get("outcome"):
                continue

            team_id = pick.get("team_id")
            team_name = pick.get("team_name") or pick.get("team")
            result = match["result"]["outcome"]
            home_team_id = match.get("home_team_id")
            away_team_id = match.get("away_team_id")
            if not team_id or not home_team_id or not away_team_id:
                logger.error("Survivor identity missing: entry=%s match=%s", entry.get("_id"), pick["match_id"])
                continue

            # Determine if the picked team won
            if team_id == home_team_id:
                team_won = result == "1"
                team_draw = result == "X"
            elif team_id == away_team_id:
                team_won = result == "2"
                team_draw = result == "X"
            else:
                logger.error("Survivor team_id %s not found in match %s", team_id, pick["match_id"])
                continue

            # Get squad config for draw_eliminates
            squad = await _db.db.squads.find_one({"_id": ObjectId(entry["squad_id"])})
            draw_eliminates = squad.get("game_mode_config", {}).get("draw_eliminates", True) if squad else True

            if team_won:
                pick_result = "won"
            elif team_draw:
                pick_result = "draw"
            else:
                pick_result = "lost"

            # Update pick result
            entry["picks"][i]["result"] = pick_result
            updated = True

            is_eliminated = pick_result == "lost" or (pick_result == "draw" and draw_eliminates)

            if is_eliminated:
                await _db.db.survivor_entries.update_one(
                    {"_id": entry["_id"]},
                    {"$set": {
                        f"picks.{i}.result": pick_result,
                        "status": "eliminated",
                        "eliminated_at": now,
                        "updated_at": now,
                    }},
                )
                logger.info(
                    "Survivor eliminated: user=%s squad=%s team=%s result=%s",
                    entry["user_id"], entry["squad_id"], team_name, pick_result,
                )
            else:
                # Team won — increment streak
                await _db.db.survivor_entries.update_one(
                    {"_id": entry["_id"]},
                    {
                        "$set": {
                            f"picks.{i}.result": pick_result,
                            "updated_at": now,
                        },
                        "$inc": {"streak": 1},
                    },
                )
                logger.info(
                    "Survivor survived: user=%s squad=%s team=%s streak+1",
                    entry["user_id"], entry["squad_id"], team_name,
                )

            resolved_count += 1

    if resolved_count:
        logger.info("Resolved %d survivor picks", resolved_count)
    await set_synced(state_key)
