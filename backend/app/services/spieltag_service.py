"""Spieltag-Modus: scoring, predictions, and auto-tipp logic."""

import logging
from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.spieltag_service")

# Deadline: predictions lock 15 minutes before kickoff
LOCK_MINUTES = 15


def calculate_points(
    pred_home: int, pred_away: int, actual_home: int, actual_away: int
) -> int:
    """Calculate Spieltag points for a single match prediction.

    Returns:
        3 — exact score match
        2 — correct goal difference (e.g. predicted 2:0, actual 3:1)
        1 — correct tendency (home win / draw / away win)
        0 — wrong
    """
    # Exact match
    if pred_home == actual_home and pred_away == actual_away:
        return 3

    # Goal difference
    pred_diff = pred_home - pred_away
    actual_diff = actual_home - actual_away
    if pred_diff == actual_diff:
        return 2

    # Tendency (sign of difference: positive=home win, 0=draw, negative=away win)
    pred_sign = (pred_home > pred_away) - (pred_home < pred_away)
    actual_sign = (actual_home > actual_away) - (actual_home < actual_away)
    if pred_sign == actual_sign:
        return 1

    return 0


def generate_auto_prediction(
    strategy: str, match: dict
) -> Optional[tuple[int, int]]:
    """Generate an auto-tipp prediction for a match.

    Args:
        strategy: "draw" | "favorite" | "none"
        match: Match document from DB

    Returns:
        (home_score, away_score) or None if strategy is "none"
    """
    if strategy == "none":
        return None

    if strategy == "draw":
        return (1, 1)

    if strategy == "favorite":
        odds = match.get("current_odds", {})
        home_odds = odds.get("1", 0)
        away_odds = odds.get("2", 0)

        if not home_odds or not away_odds:
            return (1, 1)  # Fallback to draw if no odds

        # Lower odds = favorite
        if home_odds < away_odds:
            return (2, 1)  # Home favorite → predict home win
        elif away_odds < home_odds:
            return (1, 2)  # Away favorite → predict away win
        else:
            return (1, 1)  # Equal odds → draw

    return None


def is_match_locked(match: dict) -> bool:
    """Check if a match is locked for predictions (< 15 min to kickoff or started)."""
    now = utcnow()
    commence_time = match.get("commence_time")
    if not commence_time:
        return True
    commence_time = ensure_utc(commence_time)

    deadline = commence_time - timedelta(minutes=LOCK_MINUTES)
    return now >= deadline


async def save_predictions(
    user_id: str, matchday_id: str, predictions: list[dict],
    auto_tipp_strategy: str = "none",
    squad_id: str | None = None,
) -> dict:
    """Save or update predictions for a matchday.

    Only saves predictions for matches that aren't locked.
    Merges with any existing locked predictions.
    If squad_id is set, validates the squad has spieltag mode for this sport.
    """
    # Get matchday
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spieltag nicht gefunden.",
        )

    if matchday.get("all_resolved"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dieser Spieltag ist bereits abgeschlossen.",
        )

    # Validate squad context if provided
    if squad_id:
        squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
        if not squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Squad nicht gefunden.",
            )
        if user_id not in squad.get("members", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Du bist kein Mitglied dieses Squads.",
            )
        from app.services.squad_league_service import require_active_league_config
        require_active_league_config(squad, matchday["sport_key"], "spieltag")

    # Get all matches for this matchday
    match_ids = matchday.get("match_ids", [])
    matches = await _db.db.matches.find(
        {"_id": {"$in": [ObjectId(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))
    matches_by_id = {str(m["_id"]): m for m in matches}

    # Get existing prediction doc (squad-scoped)
    existing = await _db.db.spieltag_predictions.find_one({
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
    })

    # Build map of existing locked predictions
    existing_preds: dict[str, dict] = {}
    if existing:
        for p in existing.get("predictions", []):
            existing_preds[p["match_id"]] = p

    # Process new predictions
    now = utcnow()
    final_predictions: list[dict] = []

    # Keep locked predictions unchanged
    for match_id, pred in existing_preds.items():
        match = matches_by_id.get(match_id)
        if match and is_match_locked(match):
            final_predictions.append(pred)

    # Add/update unlocked predictions from input
    submitted_match_ids = set()
    for pred_input in predictions:
        match_id = pred_input["match_id"]
        match = matches_by_id.get(match_id)
        if not match:
            continue

        if is_match_locked(match):
            continue  # Silently skip locked matches

        if match_id not in [str(mid) for mid in match_ids]:
            continue  # Match not part of this matchday

        submitted_match_ids.add(match_id)
        final_predictions.append({
            "match_id": match_id,
            "home_score": pred_input["home_score"],
            "away_score": pred_input["away_score"],
            "is_auto": False,
            "points_earned": None,
        })

    # Keep existing unlocked predictions that weren't re-submitted
    for match_id, pred in existing_preds.items():
        match = matches_by_id.get(match_id)
        if match and not is_match_locked(match) and match_id not in submitted_match_ids:
            # User didn't re-submit this one, keep it
            final_predictions.append(pred)

    pred_status = "partial" if final_predictions else "open"

    doc = {
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
        "sport_key": matchday["sport_key"],
        "season": matchday["season"],
        "matchday_number": matchday["matchday_number"],
        "auto_tipp_strategy": auto_tipp_strategy,
        "predictions": final_predictions,
        "total_points": None,
        "status": pred_status,
        "updated_at": now,
    }

    filter_key = {"user_id": user_id, "matchday_id": matchday_id, "squad_id": squad_id}
    try:
        await _db.db.spieltag_predictions.update_one(
            filter_key,
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        # Race condition — retry as update
        await _db.db.spieltag_predictions.update_one(
            filter_key,
            {"$set": doc},
        )

    logger.info(
        "Saved %d predictions for user=%s matchday=%s (auto=%s)",
        len(final_predictions), user_id, matchday_id, auto_tipp_strategy,
    )

    return doc


async def get_user_predictions(
    user_id: str, matchday_id: str, squad_id: str | None = None,
) -> Optional[dict]:
    """Get a user's predictions for a matchday (optionally squad-scoped)."""
    return await _db.db.spieltag_predictions.find_one({
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
    })
