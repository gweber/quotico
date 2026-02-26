"""
backend/app/routers/quotico_tips.py

Purpose:
    HTTP API for QuoticoTip listing, generation, resolution, and diagnostics.

Dependencies:
    - app.services.quotico_tip_service
    - app.services.qbot_intelligence_service
    - app.database
"""

import asyncio
import logging
import time

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.utils import ensure_utc
from app.services.qbot_intelligence_service import enrich_tip
from app.services.quotico_tip_service import (
    QuoticoTipResponse,
    compute_team_evd,
    generate_quotico_tip,
    resolve_tip,
    EVD_BOOST_THRESHOLD,
    EVD_DAMPEN_THRESHOLD,
)
from app.services.historical_service import sport_keys_for
from app.services.team_registry_service import TeamRegistry

logger = logging.getLogger("quotico.quotico_tips_router")
router = APIRouter(prefix="/api/quotico-tips", tags=["quotico-tips"])

# In-memory cache for public performance endpoint (60s TTL), keyed by sport_key
_perf_cache: dict[str, dict] = {}
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
            raw_confidence=tip.get("raw_confidence"),
            edge_pct=tip["edge_pct"],
            true_probability=tip["true_probability"],
            implied_probability=tip["implied_probability"],
            expected_goals_home=tip["expected_goals_home"],
            expected_goals_away=tip["expected_goals_away"],
            tier_signals=tip["tier_signals"],
            justification=tip["justification"],
            skip_reason=tip.get("skip_reason"),
            qbot_logic=tip.get("qbot_logic"),
            decision_trace=tip.get("decision_trace"),
            generated_at=ensure_utc(tip["generated_at"]),
        ))
    return results


@router.get("/public-performance")
async def public_performance(
    sport_key: str | None = Query(None, description="Filter by sport key"),
):
    """Public Q-Tip track record — aggregated stats, no auth required."""
    now = time.time()
    cache_key = sport_key or "_all"
    cached = _perf_cache.get(cache_key)
    if cached and cached["expires"] > now:
        return cached["data"]

    base_match: dict = {"status": "resolved", "was_correct": {"$ne": None}}
    if sport_key:
        base_match["sport_key"] = sport_key

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
    _perf_cache[cache_key] = {"data": data, "expires": now + _PERF_CACHE_TTL}
    return data


@router.get("/{match_id}", response_model=QuoticoTipResponse)
async def get_tip(match_id: str):
    """Get the QuoticoTip for a specific match."""
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
        raw_confidence=tip.get("raw_confidence"),
        edge_pct=tip["edge_pct"],
        true_probability=tip["true_probability"],
        implied_probability=tip["implied_probability"],
        expected_goals_home=tip["expected_goals_home"],
        expected_goals_away=tip["expected_goals_away"],
        tier_signals=tip["tier_signals"],
        justification=tip["justification"],
        skip_reason=tip.get("skip_reason"),
        qbot_logic=tip.get("qbot_logic"),
        decision_trace=tip.get("decision_trace"),
        generated_at=ensure_utc(tip["generated_at"]),
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.post("/{match_id}/refresh")
async def refresh_single_tip(match_id: str, admin=Depends(get_admin_user)):
    """Recalculate Q-Tip for a single match and return full metrics (admin only)."""
    match = await _db.db.matches.find_one({"_id": ObjectId(match_id)})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")

    tip = await generate_quotico_tip(match)

    if match.get("status") == "final" and match.get("result", {}).get("outcome"):
        tip = resolve_tip(tip, match)

    # Enrich with Qbot intelligence (graceful — skips if no strategy)
    try:
        tip = await enrich_tip(tip, match=match)
    except Exception:
        logger.warning("Qbot enrichment failed for %s", match_id, exc_info=True)

    # Upsert into DB
    await _db.db.quotico_tips.replace_one(
        {"match_id": match_id}, tip, upsert=True,
    )

    # Serialise for JSON (ObjectId / datetime)
    tip.pop("_id", None)
    for key in ("match_date", "generated_at", "resolved_at"):
        if key in tip and tip[key] is not None:
            tip[key] = ensure_utc(tip[key]).isoformat()

    return tip


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
        "odds_meta.markets.h2h.current": {"$exists": True, "$ne": {}},
        "result.outcome": {"$ne": None},
    }
    if sport_key:
        query["sport_key"] = sport_key

    max_matches = int(settings.QTIP_BACKFILL_ADMIN_MAX_MATCHES)
    if batch_size > max_matches:
        raise HTTPException(
            status_code=400,
            detail=f"Requested batch_size={batch_size} exceeds admin limit={max_matches}. Use CLI for mass reruns.",
        )

    total_scope = await _db.db.matches.count_documents(query)
    if (total_scope - skip) > max_matches:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Scope too large ({max(total_scope - skip, 0)} matches). "
                f"Admin backfill limit is {max_matches}; use CLI for mass reruns."
            ),
        )

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
    skipped_missing_odds_meta = 0
    errors = 0
    xg_seen = 0
    xg_total = 0
    archetype_distribution: dict[str, int] = {}

    for match in matches:
        mid = str(match["_id"])
        if mid in existing_ids:
            skipped_count += 1
            continue
        h2h_current = ((((match.get("odds_meta") or {}).get("markets") or {}).get("h2h") or {}).get("current") or {})
        if not all(h2h_current.get(k) for k in ("1", "X", "2")):
            skipped_missing_odds_meta += 1
            continue
        try:
            tip = await generate_quotico_tip(match, before_date=match["match_date"])
            tip = await enrich_tip(tip, match=match)
            tip = resolve_tip(tip, match)
            if not dry_run:
                await _db.db.quotico_tips.insert_one(tip)

            qbot_logic = tip.get("qbot_logic") if isinstance(tip.get("qbot_logic"), dict) else {}
            archetype = str(qbot_logic.get("archetype") or "")
            if archetype:
                archetype_distribution[archetype] = int(archetype_distribution.get(archetype, 0)) + 1
            xg_total += 1
            post_reasoning = qbot_logic.get("post_match_reasoning")
            if isinstance(post_reasoning, dict) and (
                post_reasoning.get("xg_home") is not None or post_reasoning.get("xg_away") is not None
            ):
                xg_seen += 1

            if tip["status"] == "resolved" and tip.get("was_correct") is not None:
                generated += 1
            else:
                no_signal += 1
        except Exception as e:
            errors += 1
            logger.error("Backfill error for %s: %s", mid, e)

    no_signal_den = generated + no_signal
    no_signal_rate_pct = (no_signal / no_signal_den * 100.0) if no_signal_den > 0 else 0.0
    xg_coverage_pct = (xg_seen / xg_total * 100.0) if xg_total > 0 else 0.0

    return {
        "processed": len(matches),
        "generated": generated,
        "no_signal": no_signal,
        "no_signal_rate_pct": round(no_signal_rate_pct, 2),
        "xg_coverage_pct": round(xg_coverage_pct, 2),
        "archetype_distribution": archetype_distribution,
        "skipped_missing_odds_meta": skipped_missing_odds_meta,
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

        team_registry = TeamRegistry.get()
        related_keys = sport_keys_for(sport_key)
        home_team_id = tip.get("home_team_id")
        away_team_id = tip.get("away_team_id")
        if not home_team_id:
            home_team_id = await team_registry.resolve(tip.get("home_team", ""), sport_key)
        if not away_team_id:
            away_team_id = await team_registry.resolve(tip.get("away_team", ""), sport_key)

        if not home_team_id or not away_team_id:
            buckets["no_data"]["total"] += 1
            if was_correct:
                buckets["no_data"]["correct"] += 1
            continue

        # Compute EVD as-of match date (temporal correctness)
        evd_home = await compute_team_evd(home_team_id, related_keys, before_date=match_date)
        evd_away = await compute_team_evd(away_team_id, related_keys, before_date=match_date)

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
