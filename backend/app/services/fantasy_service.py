"""Fantasy Matchups — pick a team, score based on real performance (GGL-compliant)."""

import logging

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.services.matchday_service import is_match_locked
from app.utils import utcnow

logger = logging.getLogger("quotico.fantasy_service")


def calculate_fantasy_points(
    goals_scored: int, goals_conceded: int, pure_stats_only: bool = True,
) -> int:
    """Calculate fantasy points for a team pick.

    Default (pure_stats_only=True, GGL-compliant):
      - 3 points per goal scored
      - 2 points for clean sheet (0 goals conceded)

    Extended (pure_stats_only=False, regulatory risk):
      - Adds win/draw bonus (event-bet territory)
    """
    points = goals_scored * 3
    if goals_conceded == 0:
        points += 2  # Clean sheet bonus (objective statistic)

    if not pure_stats_only:
        # Win/draw bonus — may qualify as "event bet" under GGL rules
        # Only enable if legally cleared
        if goals_scored > goals_conceded:
            points += 3  # Win bonus
        elif goals_scored == goals_conceded and goals_scored > 0:
            points += 1  # Draw bonus (not 0:0)

    return points


async def make_pick(
    user_id: str, squad_id: str, match_id: str, team: str,
) -> dict:
    """Make a fantasy team pick for a matchday."""
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
    require_active_league_config(squad, match["sport_key"], "fantasy")
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

    pick_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
        "matchday_number": matchday_number,
        "team": team,
        "match_id": match_id,
        "goals_scored": None,
        "goals_conceded": None,
        "match_result": None,
        "fantasy_points": None,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = await _db.db.fantasy_picks.insert_one(pick_doc)
        pick_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        # Update existing pick if match not locked
        await _db.db.fantasy_picks.update_one(
            {
                "user_id": user_id,
                "squad_id": squad_id,
                "sport_key": sport_key,
                "season": season,
                "matchday_number": matchday_number,
                "status": "pending",
            },
            {"$set": {
                "team": team,
                "match_id": match_id,
                "updated_at": now,
            }},
        )
        pick_doc = await _db.db.fantasy_picks.find_one({
            "user_id": user_id,
            "squad_id": squad_id,
            "sport_key": sport_key,
            "season": season,
            "matchday_number": matchday_number,
        })

    logger.info(
        "Fantasy pick: user=%s squad=%s team=%s matchday=%d",
        user_id, squad_id, team, matchday_number,
    )
    return pick_doc


async def get_user_pick(
    user_id: str, squad_id: str, sport_key: str, season: int, matchday_number: int,
) -> dict | None:
    """Get user's fantasy pick for a specific matchday."""
    return await _db.db.fantasy_picks.find_one({
        "user_id": user_id,
        "squad_id": squad_id,
        "sport_key": sport_key,
        "season": season,
        "matchday_number": matchday_number,
    })


async def get_standings(squad_id: str, sport_key: str, season: int) -> list[dict]:
    """Get fantasy standings for a squad — aggregated season points."""
    pipeline = [
        {"$match": {
            "squad_id": squad_id,
            "sport_key": sport_key,
            "season": season,
            "status": "resolved",
        }},
        {"$group": {
            "_id": "$user_id",
            "total_points": {"$sum": "$fantasy_points"},
            "matchdays_played": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "users",
            "let": {"uid": {"$toObjectId": "$_id"}},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$_id", "$$uid"]}}},
                {"$project": {"alias": 1}},
            ],
            "as": "user_info",
        }},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"total_points": -1}},
    ]
    entries = await _db.db.fantasy_picks.aggregate(pipeline).to_list(length=200)
    return [
        {
            "user_id": e["_id"],
            "alias": e.get("user_info", {}).get("alias", "Unknown"),
            "total_points": e["total_points"],
            "matchdays_played": e["matchdays_played"],
            "avg_points": round(e["total_points"] / max(e["matchdays_played"], 1), 1),
        }
        for e in entries
    ]
