"""QuoticoTip API — public value-bet recommendations powered by the EV engine."""

import logging

from fastapi import APIRouter, Depends, Query

import app.database as _db
from app.services.auth_service import get_admin_user
from app.services.quotico_tip_service import (
    QuoticoTipResponse,
    compute_team_evd,
    EVD_BOOST_THRESHOLD,
    EVD_DAMPEN_THRESHOLD,
)
from app.services.historical_service import resolve_team_key, sport_keys_for

logger = logging.getLogger("quotico.quotico_tips_router")
router = APIRouter(prefix="/api/quotico-tips", tags=["quotico-tips"])


# ---------------------------------------------------------------------------
# Public endpoints (no auth required — promotional feature)
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[QuoticoTipResponse])
async def list_tips(
    sport_key: str | None = Query(None, description="Filter by sport key"),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0, description="Minimum confidence"),
    include_no_signal: bool = Query(False, description="Include tips with no recommendation"),
    limit: int = Query(20, ge=1, le=100),
):
    """List QuoticoTips for upcoming matches, sorted by confidence."""
    statuses = ["active", "no_signal"] if include_no_signal else ["active"]
    query: dict = {"status": {"$in": statuses}}
    if sport_key:
        query["sport_key"] = sport_key
    if min_confidence > 0:
        query["confidence"] = {"$gte": min_confidence}

    tips = await _db.db.quotico_tips.find(
        query,
        {"_id": 0, "actual_result": 0, "was_correct": 0},
    ).sort("confidence", -1).limit(limit).to_list(length=limit)

    # Map stored docs to response model (match_commence_time → commence_time)
    results = []
    for tip in tips:
        results.append(QuoticoTipResponse(
            match_id=tip["match_id"],
            sport_key=tip["sport_key"],
            teams=tip.get("teams", {}),
            commence_time=tip["match_commence_time"],
            recommended_selection=tip["recommended_selection"],
            confidence=tip["confidence"],
            edge_pct=tip["edge_pct"],
            true_probability=tip["true_probability"],
            implied_probability=tip["implied_probability"],
            expected_goals_home=tip["expected_goals_home"],
            expected_goals_away=tip["expected_goals_away"],
            tier_signals=tip["tier_signals"],
            justification=tip["justification"],
            skip_reason=tip.get("skip_reason"),
            generated_at=tip["generated_at"],
        ))
    return results


@router.get("/{match_id}", response_model=QuoticoTipResponse)
async def get_tip(match_id: str):
    """Get the QuoticoTip for a specific match."""
    from fastapi import HTTPException

    tip = await _db.db.quotico_tips.find_one(
        {"match_id": match_id},
        {"_id": 0, "actual_result": 0, "was_correct": 0},
    )
    if not tip:
        raise HTTPException(status_code=404, detail="Kein QuoticoTip für dieses Spiel.")

    return QuoticoTipResponse(
        match_id=tip["match_id"],
        sport_key=tip["sport_key"],
        teams=tip.get("teams", {}),
        commence_time=tip["match_commence_time"],
        recommended_selection=tip["recommended_selection"],
        confidence=tip["confidence"],
        edge_pct=tip["edge_pct"],
        true_probability=tip["true_probability"],
        implied_probability=tip["implied_probability"],
        expected_goals_home=tip["expected_goals_home"],
        expected_goals_away=tip["expected_goals_away"],
        tier_signals=tip["tier_signals"],
        justification=tip["justification"],
        skip_reason=tip.get("skip_reason"),
        generated_at=tip["generated_at"],
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.post("/scan")
async def scan_tips(admin=Depends(get_admin_user)):
    """Force-refresh QuoticoTips for all upcoming matches (admin only)."""
    from app.workers.quotico_tip_worker import generate_quotico_tips
    await generate_quotico_tips()
    count = await _db.db.quotico_tips.count_documents({"status": "active"})
    return {"status": "ok", "active_tips": count}


@router.get("/performance")
async def tip_performance(admin=Depends(get_admin_user)):
    """Backtesting statistics for resolved QuoticoTips (admin only)."""
    pipeline = [
        {"$match": {"status": "resolved", "was_correct": {"$ne": None}}},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}},
            "avg_edge": {"$avg": "$edge_pct"},
            "avg_confidence": {"$avg": "$confidence"},
            "by_sport": {"$push": {
                "sport_key": "$sport_key",
                "was_correct": "$was_correct",
                "edge_pct": "$edge_pct",
            }},
        }},
    ]

    results = await _db.db.quotico_tips.aggregate(pipeline).to_list(length=1)

    if not results:
        return {
            "total_resolved": 0,
            "correct": 0,
            "win_rate": 0.0,
            "avg_edge": 0.0,
            "avg_confidence": 0.0,
        }

    data = results[0]
    total = data["total"]
    correct = data["correct"]

    return {
        "total_resolved": total,
        "correct": correct,
        "win_rate": round(correct / total, 3) if total else 0.0,
        "avg_edge": round(data["avg_edge"], 2),
        "avg_confidence": round(data["avg_confidence"], 3),
    }


@router.post("/backtest-evd")
async def backtest_evd(
    backfill: bool = Query(False, description="Also write btb data into resolved tips"),
    admin=Depends(get_admin_user),
):
    """Backtest EVD signal against resolved QuoticoTips.

    Computes EVD as-of each match date (no temporal leakage) and segments
    win rates by EVD bucket: positive (>threshold), negative (<threshold),
    neutral, and no-data.

    With backfill=true, also writes tier_signals.btb into each resolved tip.
    """
    resolved = await _db.db.quotico_tips.find(
        {"status": "resolved", "was_correct": {"$ne": None}},
    ).to_list(length=5000)

    if not resolved:
        return {"total": 0, "message": "Keine resolved Tips vorhanden."}

    buckets: dict[str, dict] = {
        "evd_positive": {"total": 0, "correct": 0, "tips": []},
        "evd_negative": {"total": 0, "correct": 0, "tips": []},
        "evd_neutral":  {"total": 0, "correct": 0, "tips": []},
        "no_data":      {"total": 0, "correct": 0, "tips": []},
    }
    backfill_count = 0

    for tip in resolved:
        teams = tip.get("teams", {})
        sport_key = tip.get("sport_key", "")
        selection = tip.get("recommended_selection", "-")
        was_correct = tip.get("was_correct", False)
        match_date = tip.get("match_commence_time")

        if selection == "-" or not match_date:
            continue

        related_keys = sport_keys_for(sport_key)
        home_key = await resolve_team_key(teams.get("home", ""), related_keys)
        away_key = await resolve_team_key(teams.get("away", ""), related_keys)

        if not home_key or not away_key:
            buckets["no_data"]["total"] += 1
            if was_correct:
                buckets["no_data"]["correct"] += 1
            continue

        # Compute EVD as-of match date (temporal correctness)
        evd_home = await compute_team_evd(home_key, related_keys, before_date=match_date)
        evd_away = await compute_team_evd(away_key, related_keys, before_date=match_date)

        picked_evd = evd_home if selection == "1" else (evd_away if selection == "2" else None)

        if picked_evd and picked_evd["contributes"]:
            evd_val = picked_evd["evd"]
            if evd_val > EVD_BOOST_THRESHOLD:
                bucket = "evd_positive"
            elif evd_val < EVD_DAMPEN_THRESHOLD:
                bucket = "evd_negative"
            else:
                bucket = "evd_neutral"
        else:
            bucket = "no_data"

        buckets[bucket]["total"] += 1
        if was_correct:
            buckets[bucket]["correct"] += 1

        # Backfill tier_signals.btb into the stored tip
        if backfill:
            await _db.db.quotico_tips.update_one(
                {"_id": tip["_id"]},
                {"$set": {"tier_signals.btb": {"home": evd_home, "away": evd_away}}},
            )
            backfill_count += 1

    # Build summary
    summary = {}
    for name, data in buckets.items():
        t = data["total"]
        c = data["correct"]
        summary[name] = {
            "total": t,
            "correct": c,
            "win_rate": round(c / t, 3) if t > 0 else 0.0,
        }

    total_tips = sum(b["total"] for b in buckets.values())
    total_correct = sum(b["correct"] for b in buckets.values())

    return {
        "total_analyzed": total_tips,
        "overall_win_rate": round(total_correct / total_tips, 3) if total_tips > 0 else 0.0,
        "buckets": summary,
        "backfilled": backfill_count if backfill else None,
        "interpretation": {
            "evd_positive": f"Tips wo gepicktes Team EVD > {EVD_BOOST_THRESHOLD:+.0%} hat (systematisch unterschätzt)",
            "evd_negative": f"Tips wo gepicktes Team EVD < {EVD_DAMPEN_THRESHOLD:+.0%} hat (systematisch überschätzt)",
            "evd_neutral": "Tips wo EVD zwischen den Schwellwerten liegt",
            "no_data": "Tips ohne ausreichende EVD-Daten (< 5 Spiele mit Odds)",
        },
    }
