"""
backend/app/services/parlay_service.py

Purpose:
    Parlay betting service for multi-leg bets with aggregated odds_meta market
    validation and optional wallet funding integration.

Dependencies:
    - app.database
    - app.services.wallet_service
    - app.services.odds_meta_service
"""

import logging
import math

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.models.wallet import BetStatus
from app.services.odds_meta_service import build_legacy_like_odds
from app.services import wallet_service
from app.utils import utcnow, ensure_utc

logger = logging.getLogger("quotico.parlay_service")

REQUIRED_LEGS = 3


async def create_parlay(
    user_id: str, squad_id: str, matchday_id: str,
    legs: list[dict], stake: float | None,
) -> dict:
    """Create a parlay (combo bet) with exactly 3 legs.

    Legs format: [{"match_id": "...", "prediction": "1", "displayed_odds": 2.1}, ...]
    Available in Classic and Bankroll modes.
    """
    now = utcnow()

    # Validate squad
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")

    game_mode = squad.get("game_mode", "classic")
    if game_mode not in ("classic", "bankroll"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Parlay only available in classic or bankroll mode.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of this squad.")

    # Validate matchday
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Matchday not found.")

    # Check exactly 3 legs
    if len(legs) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Exactly {REQUIRED_LEGS} matches required.")

    # Validate all legs use different matches
    match_ids = [leg["match_id"] for leg in legs]
    if len(set(match_ids)) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each match may only appear once.")

    # Check for existing parlay
    existing = await _db.db.parlays.find_one({
        "user_id": user_id, "squad_id": squad_id, "matchday_id": matchday_id,
    })
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have a parlay for this matchday.")

    # Validate each leg and lock odds
    validated_legs = []
    combined_odds = 1.0

    for leg in legs:
        match = await _db.db.matches_v3.find_one({"_id": int(leg["match_id"])})
        if not match:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Match {leg['match_id']} not found.")

        commence = ensure_utc(match["start_at"])
        if commence <= now or match["status"] != "scheduled":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One of the matches has already started.")

        prediction = leg["prediction"]
        displayed_odds = leg["displayed_odds"]

        # Determine which odds map to use
        if prediction in ("over", "under"):
            totals = build_legacy_like_odds(match).get("totals", {})
            if not totals or prediction not in totals:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No over/under odds for match {leg['match_id']}.")
            locked_odds = totals[prediction]
        else:
            current_odds = build_legacy_like_odds(match).get("h2h", {})
            if prediction not in current_odds:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid prediction '{prediction}'.")
            locked_odds = current_odds[prediction]

        # Validate displayed odds
        if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Odds have changed.")

        combined_odds *= locked_odds
        validated_legs.append({
            "match_id": leg["match_id"],
            "prediction": prediction,
            "locked_odds": locked_odds,
            "result": "pending",
        })

    combined_odds = round(combined_odds, 3)

    # Calculate potential win
    if game_mode == "bankroll" and stake and stake > 0:
        potential_win = stake * combined_odds
    else:
        potential_win = combined_odds * 10  # Classic mode: bonus points
        stake = None

    # Handle wallet deduction for bankroll mode
    wallet_id = None
    if game_mode == "bankroll" and stake:
        league_id = matchday["league_id"]
        season = matchday["season"]
        wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, league_id, season)
        wallet_id = str(wallet["_id"])
        await wallet_service.deduct_stake(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            stake=stake,
            reference_type="parlay",
            reference_id="",
            description=f"Parlay: {REQUIRED_LEGS} matches, odds {combined_odds:.2f} ({stake:.0f} coins)",
        )

    parlay_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "matchday_id": matchday_id,
        "league_id": matchday["league_id"],
        "season": matchday["season"],
        "matchday_number": matchday["matchday_number"],
        "legs": [l for l in validated_legs],
        "combined_odds": combined_odds,
        "stake": stake,
        "potential_win": round(potential_win, 2),
        "status": BetStatus.pending.value,
        "points_earned": None,
        "resolved_at": None,
        "created_at": now,
    }

    try:
        result = await _db.db.parlays.insert_one(parlay_doc)
        parlay_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Parlay for this matchday already exists.")

    logger.info(
        "Parlay created: user=%s squad=%s matchday=%s odds=%.2f stake=%s",
        user_id, squad_id, matchday_id, combined_odds, stake,
    )
    return parlay_doc


async def get_user_parlay(user_id: str, squad_id: str, matchday_id: str) -> dict | None:
    """Get user's parlay for a specific matchday."""
    return await _db.db.parlays.find_one({
        "user_id": user_id, "squad_id": squad_id, "matchday_id": matchday_id,
    })
