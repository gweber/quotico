"""
backend/app/services/matchday_service.py

Purpose:
    Matchday scoring, prediction locking, and auto-bet helper logic based on
    canonical match identity and aggregated odds_meta data.

Dependencies:
    - app.database
    - app.services.odds_meta_service
"""

import logging
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

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
    """Predict based on v3 odds_meta.summary_1x2 averages, fallback to 1:1."""
    summary = (((match or {}).get("odds_meta") or {}).get("summary_1x2") or {}) if isinstance((match or {}).get("odds_meta"), dict) else {}
    home_avg = ((summary.get("home") or {}).get("avg")) if isinstance(summary.get("home"), dict) else None
    away_avg = ((summary.get("away") or {}).get("avg")) if isinstance(summary.get("away"), dict) else None
    try:
        home_odds = Decimal(str(home_avg))
        away_odds = Decimal(str(away_avg))
        if home_odds <= 0 or away_odds <= 0:
            return (1, 1)
        # Higher implied probability (1/odds) means favorite.
        home_prob = Decimal("1") / home_odds
        away_prob = Decimal("1") / away_odds
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return (1, 1)
    if home_prob > away_prob:
        return (2, 1)
    if away_prob > home_prob:
        return (1, 2)
    return (1, 1)


def _qbot_prediction(quotico_tip: dict | None) -> tuple[int, int] | None:
    """Convert a QuoticoTip recommendation to a score prediction.

    Prefers Player Mode exact score from qbot_logic.player if available.
    Falls back to outcome-based default mapping.
    """
    if not quotico_tip:
        return None

    # Prefer Player Mode exact score
    player = (quotico_tip.get("qbot_logic") or {}).get("player")
    if player and player.get("predicted_score"):
        s = player["predicted_score"]
        return (s.get("home", 1), s.get("away", 1))

    # Fallback to outcome-based mapping
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
    commence_time = match.get("match_date") or match.get("start_at")
    if not commence_time:
        return True
    commence_time = ensure_utc(commence_time)

    deadline = commence_time - timedelta(minutes=lock_minutes)
    return now >= deadline


def _parse_v3_matchday_id(matchday_id: str) -> tuple[int, int, int]:
    parts = str(matchday_id or "").split(":")
    if len(parts) != 4 or parts[0] != "v3":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid matchday_id format.")
    try:
        league_id = int(parts[1])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid matchday_id format.") from exc
    try:
        season_id = int(parts[2])
        round_id = int(parts[3])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid matchday_id format.") from exc
    return int(league_id), season_id, round_id


async def _resolve_v3_matchday_context(matchday_id: str) -> tuple[dict, dict[str, dict]]:
    league_id, season_id, round_id = _parse_v3_matchday_id(matchday_id)
    rows = await _db.db.matches_v3.find(
        {"season_id": int(season_id), "round_id": int(round_id), "league_id": int(league_id)},
        {"_id": 1, "start_at": 1, "status": 1, "league_id": 1, "odds_meta": 1, "teams": 1},
    ).to_list(length=200)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matchday not found.")
    match_ids = [str(int((row or {}).get("_id"))) for row in rows if (row or {}).get("_id") is not None]
    context = {
        "league_id": league_id,
        "season": int(season_id),
        "matchday_number": int(round_id),
        "match_ids": match_ids,
        "all_resolved": all(str((row or {}).get("status") or "").upper() == "FINISHED" for row in rows),
    }
    matches_by_id = {str(int((row or {}).get("_id"))): row for row in rows if (row or {}).get("_id") is not None}
    return context, matches_by_id


async def get_user_predictions(
    user_id: str, matchday_id: str, squad_id: str | None = None,
) -> Optional[dict]:
    """Get a user's predictions for a matchday from the betting_slips collection.

    Returns a dict shaped like the old matchday_predictions doc for API compat:
    {matchday_id, squad_id, auto_bet_strategy, predictions: [...], total_points, status, ...}
    """
    slip = await _db.db.betting_slips.find_one({
        "user_id": user_id,
        "matchday_id": matchday_id,
        "squad_id": squad_id,
        "type": "matchday_round",
    })
    if not slip:
        return None

    # Map selections to prediction-compatible format
    predictions = []
    for sel in slip.get("selections", []):
        pick = sel.get("pick", {})
        predictions.append({
            "match_id": sel["match_id"],
            "home_score": pick.get("home") if isinstance(pick, dict) else None,
            "away_score": pick.get("away") if isinstance(pick, dict) else None,
            "is_auto": sel.get("is_auto", False),
            "is_admin_entry": sel.get("is_admin_entry", False),
            "points_earned": sel.get("points_earned"),
        })

    return {
        "matchday_id": matchday_id,
        "squad_id": squad_id,
        "auto_bet_strategy": slip.get("auto_bet_strategy", "none"),
        "predictions": predictions,
        "admin_unlocked_matches": slip.get("admin_unlocked_matches", []),
        "total_points": slip.get("total_points"),
        "status": slip.get("status", "draft"),
    }
