"""Parlay (Kombi-Joker) — combine 3 bets, multiply odds."""

import logging
import math

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.models.wallet import BetStatus
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad nicht gefunden.")

    game_mode = squad.get("game_mode", "classic")
    if game_mode not in ("classic", "bankroll"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Kombi-Joker nur im Tippspiel- oder Bankroll-Modus.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Du bist kein Mitglied dieser Squad.")

    # Validate matchday
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spieltag nicht gefunden.")

    # Check exactly 3 legs
    if len(legs) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Genau {REQUIRED_LEGS} Spiele erforderlich.")

    # Validate all legs use different matches
    match_ids = [leg["match_id"] for leg in legs]
    if len(set(match_ids)) != REQUIRED_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Jedes Spiel darf nur einmal vorkommen.")

    # Check for existing parlay
    existing = await _db.db.parlays.find_one({
        "user_id": user_id, "squad_id": squad_id, "matchday_id": matchday_id,
    })
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Du hast bereits einen Kombi-Joker für diesen Spieltag.")

    # Validate each leg and lock odds
    validated_legs = []
    combined_odds = 1.0

    for leg in legs:
        match = await _db.db.matches.find_one({"_id": ObjectId(leg["match_id"])})
        if not match:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Spiel {leg['match_id']} nicht gefunden.")

        commence = ensure_utc(match["commence_time"])
        if commence <= now or match["status"] != "upcoming":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Eines der Spiele hat bereits begonnen.")

        prediction = leg["prediction"]
        displayed_odds = leg["displayed_odds"]

        # Determine which odds map to use
        if prediction in ("over", "under"):
            totals = match.get("totals_odds", {})
            if not totals or prediction not in totals:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Keine Über/Unter-Quote für Spiel {leg['match_id']}.")
            locked_odds = totals[prediction]
        else:
            current_odds = match.get("current_odds", {})
            if prediction not in current_odds:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Ungültige Vorhersage '{prediction}'.")
            locked_odds = current_odds[prediction]

        # Validate displayed odds
        if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quoten haben sich geändert.")

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
        sport_key = matchday["sport_key"]
        season = matchday["season"]
        wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, sport_key, season)
        wallet_id = str(wallet["_id"])
        await wallet_service.deduct_stake(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            stake=stake,
            reference_type="parlay",
            reference_id="",
            description=f"Kombi-Joker: {REQUIRED_LEGS} Spiele, Quote {combined_odds:.2f} ({stake:.0f} Coins)",
        )

    parlay_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "matchday_id": matchday_id,
        "sport_key": matchday["sport_key"],
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
        raise HTTPException(status.HTTP_409_CONFLICT, "Kombi-Joker für diesen Spieltag existiert bereits.")

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
