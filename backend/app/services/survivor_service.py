"""Survivor mode — pick one winning team per matchday, eliminated on loss."""

import logging

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.services.matchday_service import is_match_locked
from app.utils import utcnow

logger = logging.getLogger("quotico.survivor_service")


async def make_pick(
    user_id: str, squad_id: str, match_id: str, team: str,
) -> dict:
    """Make a survivor pick for the current matchday.

    Validates:
    - Squad is in survivor mode
    - User is alive
    - Team hasn't been used this season
    - Match is part of current matchday and not locked
    - Only one pick per matchday
    """
    now = utcnow()

    # Validate match first (need sport_key for league config check)
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    # Validate squad
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of this squad.")
    from app.services.squad_league_service import require_active_league_config
    require_active_league_config(squad, match["sport_key"], "survivor")
    if is_match_locked(match):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is locked.")

    # Validate team is part of this match
    home = match["home_team"]
    away = match["away_team"]
    if team not in (home, away):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Team must be '{home}' or '{away}'.")

    sport_key = match["sport_key"]
    season = match.get("matchday_season") or now.year
    matchday_number = match.get("matchday_number")
    if not matchday_number:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match is not assigned to a matchday.")

    # Get or create survivor entry
    entry = await _db.db.survivor_entries.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
    })

    if entry:
        if entry["status"] == "eliminated":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You have been eliminated.")

        # Check team not already used
        if team in entry.get("used_teams", []):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"'{team}' has already been used. Choose a different team.",
            )

        # Check no pick for this matchday already
        for pick in entry.get("picks", []):
            if pick["matchday_number"] == matchday_number:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "You already have a pick for this matchday.",
                )

        # Add pick to existing entry
        new_pick = {
            "matchday_number": matchday_number,
            "team": team,
            "match_id": match_id,
            "result": "pending",
        }
        await _db.db.survivor_entries.update_one(
            {"_id": entry["_id"]},
            {
                "$push": {
                    "picks": new_pick,
                    "used_teams": team,
                },
                "$set": {"updated_at": now},
            },
        )
        entry["picks"].append(new_pick)
        entry["used_teams"].append(team)
    else:
        # Create new survivor entry
        entry = {
            "user_id": user_id,
            "squad_id": squad_id,
            "sport_key": sport_key,
            "season": season,
            "status": "alive",
            "picks": [{
                "matchday_number": matchday_number,
                "team": team,
                "match_id": match_id,
                "result": "pending",
            }],
            "used_teams": [team],
            "streak": 0,
            "eliminated_at": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = await _db.db.survivor_entries.insert_one(entry)
            entry["_id"] = result.inserted_id
        except DuplicateKeyError:
            raise HTTPException(status.HTTP_409_CONFLICT, "Survivor entry already exists.")

    logger.info(
        "Survivor pick: user=%s squad=%s team=%s matchday=%d",
        user_id, squad_id, team, matchday_number,
    )
    return entry


async def get_entry(user_id: str, squad_id: str, sport_key: str, season: int) -> dict | None:
    """Get user's survivor entry."""
    return await _db.db.survivor_entries.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
    })


async def get_standings(squad_id: str, sport_key: str, season: int) -> list[dict]:
    """Get all survivor entries for a squad, with user aliases."""
    pipeline = [
        {"$match": {
            "squad_id": squad_id,
            "sport_key": sport_key,
            "season": season,
        }},
        {"$lookup": {
            "from": "users",
            "let": {"uid": {"$toObjectId": "$user_id"}},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$_id", "$$uid"]}}},
                {"$project": {"alias": 1}},
            ],
            "as": "user_info",
        }},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"status": 1, "streak": -1}},  # alive first, then by streak
    ]
    entries = await _db.db.survivor_entries.aggregate(pipeline).to_list(length=200)
    return [
        {
            "user_id": e["user_id"],
            "alias": e.get("user_info", {}).get("alias", "Unknown"),
            "status": e["status"],
            "streak": e["streak"],
            "last_pick": e["picks"][-1]["team"] if e.get("picks") else None,
            "eliminated_at": e.get("eliminated_at"),
        }
        for e in entries
    ]
