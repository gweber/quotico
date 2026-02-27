"""
backend/app/services/bankroll_service.py

Purpose:
    Bankroll betting logic with wallet accounting and odds validation based on
    aggregated match odds_meta markets.

Dependencies:
    - app.database
    - app.services.wallet_service
    - app.services.odds_meta_service
"""

import logging

from bson import ObjectId
from fastapi import HTTPException, status

import app.database as _db
from app.models.game_mode import GAME_MODE_DEFAULTS
from app.models.wallet import BetStatus
from app.services.odds_meta_service import build_legacy_like_odds
from app.services import wallet_service
from app.utils import utcnow, ensure_utc

logger = logging.getLogger("quotico.bankroll_service")


async def place_bet(
    user_id: str, squad_id: str, match_id: str,
    prediction: str, stake: float, displayed_odds: float,
) -> dict:
    """Place a bankroll bet with atomic wallet deduction.

    Validates:
    - Squad is in bankroll mode
    - User is a squad member
    - Match exists and is upcoming
    - Prediction is valid
    - Stake is within limits
    - Odds are fresh and match displayed odds
    - No duplicate bet
    """
    now = utcnow()

    # Validate match first (need league_id for league config check)
    match = await _db.db.matches_v3.find_one({"_id": int(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    # Validate squad and mode
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of this squad.")
    from app.services.squad_league_service import require_active_league_config
    require_active_league_config(squad, match["league_id"], "bankroll")

    commence = ensure_utc(match["start_at"])
    if commence <= now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match has already started.")
    if match["status"] != "scheduled":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bets only for upcoming matches.")

    # Validate prediction
    current_odds = build_legacy_like_odds(match).get("h2h", {})
    if prediction not in current_odds:
        valid = ", ".join(current_odds.keys())
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid prediction. Allowed: {valid}")

    locked_odds = current_odds[prediction]

    # Validate displayed odds (20% tolerance)
    if abs(displayed_odds - locked_odds) / max(locked_odds, 0.01) > 0.2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Odds have changed. Please reload.")

    # Validate stake limits
    config = squad.get("game_mode_config", {})
    min_bet = config.get("min_bet", GAME_MODE_DEFAULTS["bankroll"]["min_bet"])
    max_bet_pct = config.get("max_bet_pct", GAME_MODE_DEFAULTS["bankroll"]["max_bet_pct"])

    if stake < min_bet:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Minimum stake: {min_bet} coins.")

    # Get/create wallet
    league_id = match["league_id"]
    season = match.get("matchday_season") or now.year
    wallet = await wallet_service.get_or_create_wallet(user_id, squad_id, league_id, season)
    wallet_id = str(wallet["_id"])

    # Check max bet percentage
    max_stake = wallet["balance"] * max_bet_pct / 100
    if stake > max_stake:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Max stake: {max_stake:.0f} coins ({max_bet_pct}% of your wallet).",
        )

    # Check duplicate bet
    existing = await _db.db.bankroll_bets.find_one({
        "user_id": user_id, "squad_id": squad_id, "match_id": match_id,
    })
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already placed a bet on this match.")

    # Get matchday_id if available
    matchday_id = ""
    if match.get("matchday_number") and match.get("matchday_season"):
        md = await _db.db.matchdays.find_one({
            "league_id": league_id,
            "season": match["matchday_season"],
            "matchday_number": match["matchday_number"],
        })
        if md:
            matchday_id = str(md["_id"])

    potential_win = stake * locked_odds

    # Deduct stake from wallet (atomic)
    await wallet_service.deduct_stake(
        wallet_id=wallet_id,
        user_id=user_id,
        squad_id=squad_id,
        stake=stake,
        reference_type="bankroll_bet",
        reference_id="",  # will update after bet creation
        description=f"Bet: {match['home_team']} vs {match['away_team']} → {prediction} ({stake:.0f} coins)",
    )

    # Reset bonus counter on bet
    await wallet_service.reset_bonus_counter(wallet_id)

    # Create the bet
    bet_doc = {
        "user_id": user_id,
        "squad_id": squad_id,
        "wallet_id": wallet_id,
        "match_id": match_id,
        "matchday_id": matchday_id,
        "prediction": prediction,
        "stake": stake,
        "locked_odds": locked_odds,
        "potential_win": potential_win,
        "points_earned": None,
        "status": BetStatus.pending.value,
        "resolved_at": None,
        "created_at": now,
    }

    result = await _db.db.bankroll_bets.insert_one(bet_doc)
    bet_doc["_id"] = result.inserted_id

    logger.info(
        "Bankroll bet: user=%s squad=%s match=%s pred=%s stake=%.0f odds=%.2f",
        user_id, squad_id, match_id, prediction, stake, locked_odds,
    )
    return bet_doc


async def get_user_bets(
    user_id: str, squad_id: str, matchday_id: str = "",
) -> list[dict]:
    """Get user's bankroll bets, optionally filtered by matchday."""
    query: dict = {"user_id": user_id, "squad_id": squad_id}
    if matchday_id:
        query["matchday_id"] = matchday_id
    return await _db.db.bankroll_bets.find(query).sort("created_at", -1).to_list(length=100)
