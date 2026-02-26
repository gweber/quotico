"""
backend/app/services/over_under_service.py

Purpose:
    Over/Under betting service using aggregated totals odds from match
    odds_meta and wallet-safe stake handling.

Dependencies:
    - app.database
    - app.services.wallet_service
    - app.services.odds_meta_service
"""

import logging

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.models.wallet import BetStatus
from app.services.odds_meta_service import build_legacy_like_odds
from app.services import wallet_service
from app.utils import utcnow, ensure_utc

logger = logging.getLogger("quotico.over_under_service")


async def place_bet(
    user_id: str, squad_id: str, match_id: str,
    prediction: str, stake: float | None, displayed_odds: float,
) -> dict:
    """Place an over/under bet."""
    now = utcnow()

    # Validate prediction
    if prediction not in ("over", "under"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Prediction must be 'over' or 'under'.")

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
    require_active_league_config(squad, match["sport_key"], "over_under")

    commence = ensure_utc(match["match_date"])
    if commence <= now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match has already started.")
    if match["status"] != "scheduled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bets only for upcoming matches.")

    # Get totals odds
    totals = build_legacy_like_odds(match).get("totals", {})
    if not totals or "line" not in totals:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No over/under odds available for this match.")

    line = totals["line"]
    locked_odds = totals.get(prediction, 0)
    if not locked_odds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No odds for '{prediction}'.")

    # Validate displayed odds
    if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Odds have changed.")

    # Check duplicate
    existing = await _db.db.over_under_bets.find_one({
        "user_id": user_id, "squad_id": squad_id, "match_id": match_id,
    })
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already placed a bet on this match.")

    # Get matchday_id
    sport_key = match["sport_key"]
    matchday_id = ""
    if match.get("matchday_number") and match.get("matchday_season"):
        md = await _db.db.matchdays.find_one({
            "sport_key": sport_key,
            "season": match["matchday_season"],
            "matchday_number": match["matchday_number"],
        })
        if md:
            matchday_id = str(md["_id"])

    wallet_id = None

    # Handle wallet deduction if stake provided (bankroll combo or future feature)
    if stake and stake > 0:
        season = match.get("matchday_season") or now.year
        wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, sport_key, season)
        wallet_id = str(wallet["_id"])
        await wallet_service.deduct_stake(
            wallet_id=wallet_id,
            user_id=user_id,
            squad_id=squad_id,
            stake=stake,
            reference_type="over_under_bet",
            reference_id="",
            description=f"O/U Bet: {prediction} {line} ({stake:.0f} coins)",
        )

    bet_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "wallet_id": wallet_id,
        "match_id": match_id,
        "matchday_id": matchday_id,
        "prediction": prediction,
        "line": line,
        "locked_odds": locked_odds,
        "stake": stake,
        "status": BetStatus.pending.value,
        "points_earned": None,
        "resolved_at": None,
        "created_at": now,
    }

    result = await _db.db.over_under_bets.insert_one(bet_doc)
    bet_doc["_id"] = result.inserted_id

    logger.info(
        "O/U bet: user=%s squad=%s match=%s pred=%s line=%.1f odds=%.2f",
        user_id, squad_id, match_id, prediction, line, locked_odds,
    )
    return bet_doc


async def get_user_bets(
    user_id: str, squad_id: str, matchday_id: str = "",
) -> list[dict]:
    """Get user's over/under bets."""
    query: dict = {"user_id": user_id, "squad_id": squad_id}
    if matchday_id:
        query["matchday_id"] = matchday_id
    return await _db.db.over_under_bets.find(query).sort("created_at", -1).to_list(length=100)
