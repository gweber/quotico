"""Spieltag-Modus: resolve predictions after matches complete."""

import logging
from datetime import datetime

from bson import ObjectId

import app.database as _db
from app.utils import utcnow
from app.services.spieltag_service import (
    calculate_points,
    generate_auto_prediction,
)

logger = logging.getLogger("quotico.spieltag_resolver")


async def resolve_spieltag_predictions() -> None:
    """Check completed matchdays and resolve predictions.

    For each matchday that has all matches completed:
    1. Apply auto-tipps for missing predictions
    2. Score each prediction
    3. Sum total points
    4. Mark prediction as resolved
    """
    # Find matchdays where all matches are completed but not yet marked resolved
    matchdays = await _db.db.matchdays.find({
        "status": {"$in": ["in_progress", "completed"]},
    }).to_list(length=100)

    for matchday in matchdays:
        try:
            await _resolve_matchday(matchday)
        except Exception as e:
            logger.error(
                "Spieltag resolver error for %s: %s",
                matchday.get("label", "?"), e,
            )


async def _resolve_matchday(matchday: dict) -> None:
    """Resolve all predictions for a single matchday."""
    matchday_id = str(matchday["_id"])
    match_ids = matchday.get("match_ids", [])

    if not match_ids:
        return

    # Get all matches
    matches = await _db.db.matches.find(
        {"_id": {"$in": [ObjectId(mid) for mid in match_ids]}}
    ).to_list(length=len(match_ids))

    matches_by_id = {str(m["_id"]): m for m in matches}

    # Check which matches are completed
    completed_matches = {
        str(m["_id"]): m for m in matches
        if m.get("status") == "completed"
        and m.get("home_score") is not None
        and m.get("away_score") is not None
    }

    if not completed_matches:
        return

    # Check if entire matchday is done
    all_done = len(completed_matches) == len(match_ids)

    # Get all predictions for this matchday that need resolving
    predictions = await _db.db.spieltag_predictions.find({
        "matchday_id": matchday_id,
        "status": {"$ne": "resolved"},
    }).to_list(length=10000)

    now = utcnow()
    resolved_count = 0

    for pred_doc in predictions:
        updated = await _score_prediction(
            pred_doc, matches_by_id, completed_matches, all_done, now
        )
        if updated:
            resolved_count += 1

    # Update matchday status
    if all_done:
        await _db.db.matchdays.update_one(
            {"_id": matchday["_id"]},
            {"$set": {"status": "completed", "all_resolved": True, "updated_at": now}},
        )

    if resolved_count > 0:
        logger.info(
            "Resolved %d predictions for %s (%s)",
            resolved_count, matchday.get("label"), matchday.get("sport_key"),
        )


async def _score_prediction(
    pred_doc: dict,
    matches_by_id: dict[str, dict],
    completed_matches: dict[str, dict],
    all_done: bool,
    now: datetime,
) -> bool:
    """Score a single user's predictions and optionally apply auto-tipps.

    Returns True if the prediction was resolved.
    """
    existing_preds = {p["match_id"]: p for p in pred_doc.get("predictions", [])}
    auto_strategy = pred_doc.get("auto_tipp_strategy", "none")
    matchday_id = pred_doc["matchday_id"]

    updated_predictions = []
    total_points = 0
    any_scored = False

    for match_id, match in matches_by_id.items():
        pred = existing_preds.get(match_id)

        # Apply auto-tipp if no prediction exists and matchday is fully done
        if pred is None and all_done and auto_strategy != "none":
            auto = generate_auto_prediction(auto_strategy, match)
            if auto:
                pred = {
                    "match_id": match_id,
                    "home_score": auto[0],
                    "away_score": auto[1],
                    "is_auto": True,
                    "points_earned": None,
                }

        if pred is None:
            continue

        # Score if match is completed and not yet scored
        if match_id in completed_matches and pred.get("points_earned") is None:
            actual_home = completed_matches[match_id]["home_score"]
            actual_away = completed_matches[match_id]["away_score"]
            points = calculate_points(
                pred["home_score"], pred["away_score"],
                actual_home, actual_away,
            )
            pred = {**pred, "points_earned": points}
            any_scored = True

        if pred.get("points_earned") is not None:
            total_points += pred["points_earned"]

        updated_predictions.append(pred)

    if not any_scored:
        return False

    # Determine status
    all_scored = all(
        p.get("points_earned") is not None for p in updated_predictions
    )
    new_status = "resolved" if (all_done and all_scored) else "partial"

    update: dict = {
        "predictions": updated_predictions,
        "status": new_status,
        "updated_at": now,
    }
    if new_status == "resolved":
        update["total_points"] = total_points

    await _db.db.spieltag_predictions.update_one(
        {"_id": pred_doc["_id"]},
        {"$set": update},
    )

    return new_status == "resolved"
