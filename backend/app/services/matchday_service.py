"""Matchday mode: scoring, predictions, and auto-bet logic."""

import logging
from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.matchday_service")

# Deadline: predictions lock 15 minutes before kickoff
LOCK_MINUTES = 15


DEFAULT_POINT_WEIGHTS = {"exact": 3, "diff": 2, "tendency": 1, "miss": 0}


def calculate_points(
    pred_home: int, pred_away: int, actual_home: int, actual_away: int,
    weights: dict | None = None,
) -> int:
    """Calculate matchday points for a single match prediction.

    Args:
        weights: Optional dict with keys "exact", "diff", "tendency", "miss".
                 Defaults to {exact: 3, diff: 2, tendency: 1, miss: 0}.

    Returns:
        Points based on prediction accuracy tier.
    """
    w = weights or DEFAULT_POINT_WEIGHTS

    # Exact match
    if pred_home == actual_home and pred_away == actual_away:
        return w.get("exact", 3)

    # Goal difference
    pred_diff = pred_home - pred_away
    actual_diff = actual_home - actual_away
    if pred_diff == actual_diff:
        return w.get("diff", 2)

    # Tendency (sign of difference: positive=home win, 0=draw, negative=away win)
    pred_sign = (pred_home > pred_away) - (pred_home < pred_away)
    actual_sign = (actual_home > actual_away) - (actual_home < actual_away)
    if pred_sign == actual_sign:
        return w.get("tendency", 1)

    return w.get("miss", 0)


def _favorite_prediction(match: dict) -> tuple[int, int]:
    """Predict based on odds favorite, fallback to 1:1."""
    odds = match.get("odds", {}).get("h2h", {})
    home_odds = odds.get("1", 0)
    away_odds = odds.get("2", 0)

    if not home_odds or not away_odds:
        return (1, 1)

    if home_odds < away_odds:
        return (2, 1)  # Home favorite
    elif away_odds < home_odds:
        return (1, 2)  # Away favorite
    else:
        return (1, 1)  # Equal odds → draw


def _qbot_prediction(quotico_tip: dict | None) -> tuple[int, int] | None:
    """Convert a QuoticoTip recommendation to a score prediction."""
    if not quotico_tip:
        return None

    sel = quotico_tip.get("recommended_selection")
    if sel == "1":
        return (2, 1)  # Home win
    elif sel == "X":
        return (1, 1)  # Draw
    elif sel == "2":
        return (1, 2)  # Away win
    return None


def generate_auto_prediction(
    strategy: str, match: dict, *, quotico_tip: dict | None = None,
) -> Optional[tuple[int, int]]:
    """Generate an auto-bet prediction for a match.

    Args:
        strategy: "q_bot" | "draw" | "favorite" | "none"
        match: Match document from DB
        quotico_tip: Optional QuoticoTip document for this match

    Returns:
        (home_score, away_score) or None if strategy is "none"
    """
    if strategy == "none":
        return None

    if strategy == "draw":
        return (1, 1)

    if strategy == "favorite":
        return _favorite_prediction(match)

    if strategy == "q_bot":
        # Chain: QuoticoTip → odds favorite → 1:1
        qbot = _qbot_prediction(quotico_tip)
        if qbot:
            return qbot
        return _favorite_prediction(match)

    return None


def is_match_locked(match: dict, lock_minutes: int = LOCK_MINUTES) -> bool:
    """Check if a match is locked for predictions.

    Locked when current time >= (kickoff - lock_minutes) or match has started.
    """
    now = utcnow()
    commence_time = match.get("match_date")
    if not commence_time:
        return True
    commence_time = ensure_utc(commence_time)

    deadline = commence_time - timedelta(minutes=lock_minutes)
    return now >= deadline


async def save_predictions(
    user_id: str, matchday_id: str, predictions: list[dict],
    auto_bet_strategy: str = "none",
    squad_id: str | None = None,
) -> dict:
    """Save or update predictions for a matchday.

    Only saves predictions for matches that aren't locked.
    Merges with any existing locked predictions.
    If squad_id is set, validates the squad has matchday mode for this sport.
    """
    # Get matchday
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matchday not found.",
        )

    if matchday.get("all_resolved"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This matchday is already resolved.",
        )

    # Validate squad context if provided
    lock_mins = LOCK_MINUTES  # Default; overridden by squad setting
    if squad_id:
        squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
        if not squad:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Squad not found.",
            )
        if user_id not in squad.get("members", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this squad.",
            )
        from app.services.squad_league_service import require_active_league_config
        require_active_league_config(squad, matchday["sport_key"], "classic")
        if auto_bet_strategy != "none" and squad.get("auto_bet_blocked", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Auto-bet is disabled in this squad.",
            )
        lock_mins = squad.get("lock_minutes", LOCK_MINUTES)

    # Get all matches for this matchday
    match_ids = matchday.get("match_ids", [])
    matches = await _db.db.matches.find(
        {"_id": {"$in": [ObjectId(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))
    matches_by_id = {str(m["_id"]): m for m in matches}

    # Get existing prediction doc (squad-scoped)
    existing = await _db.db.matchday_predictions.find_one({
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
    })

    # Build map of existing locked predictions
    existing_preds: dict[str, dict] = {}
    if existing:
        for p in existing.get("predictions", []):
            existing_preds[p["match_id"]] = p

    # Admin-unlocked matches bypass lock for this user
    admin_unlocked = set(existing.get("admin_unlocked_matches", [])) if existing else set()

    def _is_locked(match: dict, match_id: str) -> bool:
        if match_id in admin_unlocked:
            return False
        return is_match_locked(match, lock_mins)

    # Process new predictions
    now = utcnow()
    final_predictions: list[dict] = []

    # Keep locked predictions unchanged
    for match_id, pred in existing_preds.items():
        match = matches_by_id.get(match_id)
        if match and _is_locked(match, match_id):
            final_predictions.append(pred)

    # Add/update unlocked predictions from input
    submitted_match_ids = set()
    for pred_input in predictions:
        match_id = pred_input["match_id"]
        match = matches_by_id.get(match_id)
        if not match:
            continue

        if _is_locked(match, match_id):
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
        if match and not _is_locked(match, match_id) and match_id not in submitted_match_ids:
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
        "auto_bet_strategy": auto_bet_strategy,
        "predictions": final_predictions,
        "total_points": None,
        "status": pred_status,
        "updated_at": now,
    }

    filter_key = {"user_id": user_id, "matchday_id": matchday_id, "squad_id": squad_id}
    try:
        await _db.db.matchday_predictions.update_one(
            filter_key,
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        # Race condition — retry as update
        await _db.db.matchday_predictions.update_one(
            filter_key,
            {"$set": doc},
        )

    logger.info(
        "Saved %d predictions for user=%s matchday=%s (auto_bet=%s)",
        len(final_predictions), user_id, matchday_id, auto_bet_strategy,
    )

    return doc


async def get_user_predictions(
    user_id: str, matchday_id: str, squad_id: str | None = None,
) -> Optional[dict]:
    """Get a user's predictions for a matchday (optionally squad-scoped)."""
    return await _db.db.matchday_predictions.find_one({
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
    })


# ---------- Squad admin helpers ----------

async def _validate_squad_admin(
    admin_id: str, squad_id: str, user_id: str, matchday_id: str, match_id: str,
) -> tuple[dict, dict, dict]:
    """Shared validation for admin unlock/prediction endpoints.

    Returns (squad, matchday, match) or raises HTTPException.
    """
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Squad not found.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the squad admin can do this.")
    if user_id not in squad.get("members", []):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User is not a member of this squad.")

    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Matchday not found.")
    if match_id not in matchday.get("match_ids", []):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Match does not belong to this matchday.")

    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Match not found.")

    return squad, matchday, match


async def admin_unlock_match(
    admin_id: str, squad_id: str, user_id: str, matchday_id: str, match_id: str,
) -> dict:
    """Squad admin unlocks a specific match for a user, bypassing the lock deadline."""
    squad, matchday, match = await _validate_squad_admin(
        admin_id, squad_id, user_id, matchday_id, match_id,
    )

    now = utcnow()
    filter_key = {"user_id": user_id, "matchday_id": matchday_id, "squad_id": squad_id}

    await _db.db.matchday_predictions.update_one(
        filter_key,
        {
            "$addToSet": {"admin_unlocked_matches": match_id},
            "$set": {"updated_at": now},
            "$setOnInsert": {
                "sport_key": matchday["sport_key"],
                "season": matchday["season"],
                "matchday_number": matchday["matchday_number"],
                "auto_bet_strategy": "none",
                "predictions": [],
                "total_points": None,
                "status": "open",
                "created_at": now,
            },
        },
        upsert=True,
    )

    logger.info(
        "Admin %s unlocked match %s for user %s (squad=%s)",
        admin_id, match_id, user_id, squad_id,
    )
    return {"unlocked": True, "match_id": match_id, "user_id": user_id}


async def admin_save_prediction(
    admin_id: str, squad_id: str, user_id: str,
    matchday_id: str, match_id: str,
    home_score: int, away_score: int,
) -> dict:
    """Squad admin enters a prediction on behalf of a user. Bypasses lock.

    If the match is already completed, points are calculated immediately
    and the prediction doc's total_points is recalculated.
    """
    squad, matchday, match = await _validate_squad_admin(
        admin_id, squad_id, user_id, matchday_id, match_id,
    )

    now = utcnow()
    filter_key = {"user_id": user_id, "matchday_id": matchday_id, "squad_id": squad_id}

    # Score immediately if match is completed
    points_earned = None
    match_result = match.get("result", {})
    if (
        match.get("status") == "final"
        and match_result.get("home_score") is not None
        and match_result.get("away_score") is not None
    ):
        points_earned = calculate_points(
            home_score, away_score,
            match_result["home_score"], match_result["away_score"],
        )

    new_pred = {
        "match_id": match_id,
        "home_score": home_score,
        "away_score": away_score,
        "is_auto": False,
        "is_admin_entry": True,
        "points_earned": points_earned,
    }

    # Get existing prediction doc
    existing = await _db.db.matchday_predictions.find_one(filter_key)

    if existing:
        # Replace or append the prediction for this match
        preds = [p for p in existing.get("predictions", []) if p["match_id"] != match_id]
        preds.append(new_pred)

        # Recalculate total_points
        all_scored = all(p.get("points_earned") is not None for p in preds)
        total_pts = sum(p["points_earned"] for p in preds if p.get("points_earned") is not None)

        # Determine status
        match_ids = matchday.get("match_ids", [])
        all_matches_done = await _db.db.matches.count_documents({
            "_id": {"$in": [ObjectId(mid) for mid in match_ids]},
            "status": "final",
        }) == len(match_ids)

        new_status = "resolved" if (all_matches_done and all_scored) else "partial"

        await _db.db.matchday_predictions.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "predictions": preds,
                "total_points": total_pts if new_status == "resolved" else None,
                "status": new_status,
                "updated_at": now,
            }},
        )
    else:
        # Create new prediction doc
        doc = {
            "user_id": user_id,
            "matchday_id": matchday_id,
            "squad_id": squad_id,
            "sport_key": matchday["sport_key"],
            "season": matchday["season"],
            "matchday_number": matchday["matchday_number"],
            "auto_bet_strategy": "none",
            "predictions": [new_pred],
            "admin_unlocked_matches": [],
            "total_points": None,
            "status": "partial",
            "created_at": now,
            "updated_at": now,
        }
        await _db.db.matchday_predictions.insert_one(doc)

    logger.info(
        "Admin %s saved prediction %d:%d for match %s user %s (squad=%s, pts=%s)",
        admin_id, home_score, away_score, match_id, user_id, squad_id, points_earned,
    )

    return {
        "match_id": match_id,
        "home_score": home_score,
        "away_score": away_score,
        "points_earned": points_earned,
    }
