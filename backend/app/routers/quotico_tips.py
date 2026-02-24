"""QuoticoTip API — public value-bet recommendations powered by the EV engine."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Query

import app.database as _db
from app.services.auth_service import get_admin_user
from app.utils import ensure_utc
from app.services.quotico_tip_service import (
    QuoticoTipResponse,
    compute_team_evd,
    generate_quotico_tip,
    resolve_tip,
    EVD_BOOST_THRESHOLD,
    EVD_DAMPEN_THRESHOLD,
)
from app.services.historical_service import sport_keys_for
from app.services.team_mapping_service import resolve_team_key

logger = logging.getLogger("quotico.quotico_tips_router")
router = APIRouter(prefix="/api/quotico-tips", tags=["quotico-tips"])

# In-memory cache for public performance endpoint (60s TTL)
_perf_cache: dict = {"data": None, "expires": 0.0}
_PERF_CACHE_TTL = 60


# ---------------------------------------------------------------------------
# Public endpoints (no auth required — promotional feature)
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[QuoticoTipResponse])
async def list_tips(
    sport_key: str | None = Query(None, description="Filter by sport key"),
    match_ids: str | None = Query(None, description="Comma-separated match IDs to fetch tips for"),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0, description="Minimum confidence"),
    include_no_signal: bool = Query(False, description="Include tips with no recommendation"),
    limit: int = Query(20, ge=1, le=200),
):
    """List QuoticoTips for upcoming matches, sorted by confidence."""
    statuses = ["active", "no_signal"] if include_no_signal else ["active"]
    query: dict = {"status": {"$in": statuses}}
    if sport_key:
        query["sport_key"] = sport_key
    if match_ids:
        ids = [mid.strip() for mid in match_ids.split(",") if mid.strip()]
        if ids:
            query["match_id"] = {"$in": ids}
    if min_confidence > 0:
        query["confidence"] = {"$gte": min_confidence}

    effective_limit = min(limit, 200)
    quotico_tips = await _db.db.quotico_tips.find(
        query,
        {"_id": 0, "actual_result": 0, "was_correct": 0},
    ).sort("confidence", -1).limit(effective_limit).to_list(length=effective_limit)

    results = []
    for tip in quotico_tips:
        results.append(QuoticoTipResponse(
            match_id=tip["match_id"],
            sport_key=tip["sport_key"],
            home_team=tip["home_team"],
            away_team=tip["away_team"],
            match_date=ensure_utc(tip["match_date"]),
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
            generated_at=ensure_utc(tip["generated_at"]),
        ))
    return results


@router.get("/public-performance")
async def public_performance():
    """Public Q-Tip track record — aggregated stats, no auth required."""
    now = time.time()
    if _perf_cache["data"] and _perf_cache["expires"] > now:
        return _perf_cache["data"]

    base_match = {"status": "resolved", "was_correct": {"$ne": None}}

    async def _overall():
        results = await _db.db.quotico_tips.aggregate([
            {"$match": base_match},
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}},
                "avg_confidence": {"$avg": "$confidence"},
                "avg_edge": {"$avg": "$edge_pct"},
            }},
        ]).to_list(length=1)
        if not results:
            return {"total_resolved": 0, "correct": 0, "win_rate": 0.0, "avg_confidence": 0.0, "avg_edge": 0.0}
        d = results[0]
        return {
            "total_resolved": d["total"],
            "correct": d["correct"],
            "win_rate": round(d["correct"] / d["total"], 3) if d["total"] else 0.0,
            "avg_confidence": round(d["avg_confidence"], 3),
            "avg_edge": round(d["avg_edge"], 2),
        }

    async def _by_sport():
        results = await _db.db.quotico_tips.aggregate([
            {"$match": base_match},
            {"$group": {
                "_id": "$sport_key",
                "total": {"$sum": 1},
                "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}},
                "avg_confidence": {"$avg": "$confidence"},
                "avg_edge": {"$avg": "$edge_pct"},
            }},
            {"$sort": {"total": -1}},
        ]).to_list(length=50)
        return [{
            "sport_key": r["_id"],
            "total": r["total"],
            "correct": r["correct"],
            "win_rate": round(r["correct"] / r["total"], 3) if r["total"] else 0.0,
            "avg_confidence": round(r["avg_confidence"], 3),
            "avg_edge": round(r["avg_edge"], 2),
        } for r in results]

    async def _by_confidence():
        labels = {0.0: "<50%", 0.50: "50-60%", 0.60: "60-70%", 0.70: "70-80%", 0.80: "80%+"}
        results = await _db.db.quotico_tips.aggregate([
            {"$match": base_match},
            {"$bucket": {
                "groupBy": "$confidence",
                "boundaries": [0.0, 0.50, 0.60, 0.70, 0.80, 1.01],
                "default": "other",
                "output": {
                    "total": {"$sum": 1},
                    "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}},
                    "avg_confidence": {"$avg": "$confidence"},
                },
            }},
        ]).to_list(length=10)
        return [{
            "bucket": labels.get(r["_id"], str(r["_id"])),
            "total": r["total"],
            "correct": r["correct"],
            "win_rate": round(r["correct"] / r["total"], 3) if r["total"] else 0.0,
            "avg_confidence": round(r["avg_confidence"], 3),
        } for r in results]

    async def _by_signal():
        results = await _db.db.quotico_tips.aggregate([
            {"$match": base_match},
            {"$facet": {
                "poisson": [
                    {"$match": {"tier_signals.poisson": {"$ne": None}}},
                    {"$group": {"_id": None, "total": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}}}},
                ],
                "momentum": [
                    {"$match": {"tier_signals.momentum.contributes": True}},
                    {"$group": {"_id": None, "total": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}}}},
                ],
                "sharp": [
                    {"$match": {"tier_signals.sharp_movement.has_sharp_movement": True}},
                    {"$group": {"_id": None, "total": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}}}},
                ],
                "kings": [
                    {"$match": {"tier_signals.kings_choice.has_kings_choice": True}},
                    {"$group": {"_id": None, "total": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}}}},
                ],
                "btb": [
                    {"$match": {"$or": [
                        {"tier_signals.btb.home.contributes": True},
                        {"tier_signals.btb.away.contributes": True},
                    ]}},
                    {"$group": {"_id": None, "total": {"$sum": 1}, "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}}}},
                ],
            }},
        ]).to_list(length=1)
        if not results:
            return []
        facets = results[0]
        out = []
        for signal_name in ["poisson", "momentum", "sharp", "kings", "btb"]:
            data = facets.get(signal_name, [])
            if data:
                d = data[0]
                out.append({
                    "signal": signal_name,
                    "total": d["total"],
                    "correct": d["correct"],
                    "win_rate": round(d["correct"] / d["total"], 3) if d["total"] else 0.0,
                })
        return out

    async def _recent():
        tips = await _db.db.quotico_tips.find(
            base_match,
            {
                "_id": 0, "match_id": 1, "sport_key": 1,
                "home_team": 1, "away_team": 1, "match_date": 1,
                "recommended_selection": 1, "actual_result": 1,
                "was_correct": 1, "confidence": 1, "edge_pct": 1,
            },
        ).sort("match_date", -1).limit(30).to_list(length=30)
        for t in tips:
            t["match_date"] = ensure_utc(t["match_date"]).isoformat()
            t["confidence"] = round(t["confidence"], 3)
            t["edge_pct"] = round(t["edge_pct"], 2)
        return tips

    overall, by_sport, by_confidence, by_signal, recent = await asyncio.gather(
        _overall(), _by_sport(), _by_confidence(), _by_signal(), _recent(),
    )

    data = {
        "overall": overall,
        "by_sport": by_sport,
        "by_confidence": by_confidence,
        "by_signal": by_signal,
        "recent_tips": recent,
    }
    _perf_cache["data"] = data
    _perf_cache["expires"] = now + _PERF_CACHE_TTL
    return data


@router.get("/{match_id}", response_model=QuoticoTipResponse)
async def get_tip(match_id: str):
    """Get the QuoticoTip for a specific match."""
    from fastapi import HTTPException

    tip = await _db.db.quotico_tips.find_one(
        {"match_id": match_id},
        {"_id": 0, "actual_result": 0, "was_correct": 0},
    )
    if not tip:
        raise HTTPException(status_code=404, detail="No QuoticoTip found for this match.")

    return QuoticoTipResponse(
        match_id=tip["match_id"],
        sport_key=tip["sport_key"],
        home_team=tip["home_team"],
        away_team=tip["away_team"],
        match_date=ensure_utc(tip["match_date"]),
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
        generated_at=ensure_utc(tip["generated_at"]),
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


@router.post("/backfill")
async def backfill_quotico_tips(
    sport_key: str | None = Query(None, description="Filter by sport key"),
    batch_size: int = Query(500, ge=1, le=2000, description="Matches per batch"),
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    dry_run: bool = Query(True, description="If true, don't write to DB"),
    admin=Depends(get_admin_user),
):
    """Honest backfill: generate & resolve tips for historical matches (admin only).

    Uses ``before_date`` filtering so each tip only sees data that existed
    before the match was played — no temporal leakage.

    Processes one batch at a time. Use ``skip`` + ``batch_size`` to paginate.
    """
    query: dict = {
        "status": "final",
        "odds.h2h": {"$exists": True, "$ne": {}},
        "result.outcome": {"$ne": None},
    }
    if sport_key:
        query["sport_key"] = sport_key

    matches = await _db.db.matches.find(query).sort("match_date", 1).skip(skip).to_list(length=batch_size)

    # Check which matches already have tips
    existing_ids: set[str] = set()
    if matches:
        match_ids = [str(m["_id"]) for m in matches]
        existing = await _db.db.quotico_tips.find(
            {"match_id": {"$in": match_ids}}, {"match_id": 1},
        ).to_list(length=len(match_ids))
        existing_ids = {e["match_id"] for e in existing}

    generated = 0
    no_signal = 0
    skipped_count = 0
    errors = 0

    for match in matches:
        mid = str(match["_id"])
        if mid in existing_ids:
            skipped_count += 1
            continue
        try:
            tip = await generate_quotico_tip(match, before_date=match["match_date"])
            tip = resolve_tip(tip, match)
            if not dry_run:
                await _db.db.quotico_tips.insert_one(tip)
            if tip["status"] == "resolved" and tip.get("was_correct") is not None:
                generated += 1
            else:
                no_signal += 1
        except Exception as e:
            errors += 1
            logger.error("Backfill error for %s: %s", mid, e)

    return {
        "processed": len(matches),
        "generated": generated,
        "no_signal": no_signal,
        "skipped": skipped_count,
        "errors": errors,
        "next_skip": skip + batch_size,
        "dry_run": dry_run,
    }


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
        return {"total": 0, "message": "No resolved tips available."}

    buckets: dict[str, dict] = {
        "evd_positive": {"total": 0, "correct": 0, "tips": []},
        "evd_negative": {"total": 0, "correct": 0, "tips": []},
        "evd_neutral":  {"total": 0, "correct": 0, "tips": []},
        "no_data":      {"total": 0, "correct": 0, "tips": []},
    }
    backfill_count = 0

    for tip in resolved:
        sport_key = tip.get("sport_key", "")
        selection = tip.get("recommended_selection", "-")
        was_correct = tip.get("was_correct", False)
        match_date = tip.get("match_date")

        if selection == "-" or not match_date:
            continue

        related_keys = sport_keys_for(sport_key)
        home_key = await resolve_team_key(tip.get("home_team", ""), related_keys)
        away_key = await resolve_team_key(tip.get("away_team", ""), related_keys)

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
                {"_id": tip["_id"]},  # noqa: loop var is `tip`
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
            "evd_positive": f"Tips where picked team EVD > {EVD_BOOST_THRESHOLD:+.0%} (systematically underestimated)",
            "evd_negative": f"Tips where picked team EVD < {EVD_DAMPEN_THRESHOLD:+.0%} (systematically overestimated)",
            "evd_neutral": "Tips where EVD is between the thresholds",
            "no_data": "Tips without sufficient EVD data (< 5 matches with odds)",
        },
    }
