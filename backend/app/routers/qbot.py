"""Q-Bot dashboard API — public performance metrics and bet history."""

import asyncio
import logging
import time

from fastapi import APIRouter

import app.database as _db
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.qbot_router")
router = APIRouter(prefix="/api/qbot", tags=["qbot"])

# Simple in-memory cache (60s TTL)
_cache: dict = {"data": None, "expires": 0.0}
_CACHE_TTL = 60


async def _get_qbot() -> tuple[str, float] | None:
    """Get Q-Bot user_id and points."""
    user = await _db.db.users.find_one(
        {"email": "qbot@quotico.de", "is_bot": True},
        {"_id": 1, "points": 1},
    )
    if not user:
        return None
    return str(user["_id"]), user.get("points", 0.0)


def _slip_projection(doc: dict) -> dict:
    """Project an unwound slip + joined match/qbet data into the response shape."""
    sel = doc.get("sel", {})
    mi = doc.get("match_info") or {}
    qi = doc.get("qbet_info") or {}
    return {
        "match_id": sel.get("match_id", ""),
        "home_team": mi.get("home_team", ""),
        "away_team": mi.get("away_team", ""),
        "sport_key": mi.get("sport_key", ""),
        "match_date": mi.get("match_date", ""),
        "selection": sel.get("pick", ""),
        "locked_odds": sel.get("locked_odds"),
        "status": doc.get("status", ""),
        "points_earned": sel.get("points_earned"),
        "confidence": qi.get("confidence"),
        "edge_pct": qi.get("edge_pct"),
        "created_at": doc.get("submitted_at") or doc.get("created_at"),
    }


async def _fetch_bets_with_joins(qbot_id: str, status_filter: dict, limit: int, sort_field: str = "submitted_at") -> list[dict]:
    """Fetch betting slips with $lookup to matches and quotico_tips."""
    pipeline = [
        {"$match": {"user_id": qbot_id, "type": "single", **status_filter}},
        {"$sort": {sort_field: -1}},
        {"$limit": limit},
        # Unwind selections (singles have exactly one)
        {"$unwind": "$selections"},
        {"$addFields": {"sel": "$selections"}},
        {"$lookup": {
            "from": "matches",
            "let": {"mid": {"$toObjectId": "$sel.match_id"}},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$_id", "$$mid"]}}},
                {"$project": {"home_team": 1, "away_team": 1, "sport_key": 1, "match_date": 1}},
            ],
            "as": "match_info",
        }},
        {"$unwind": {"path": "$match_info", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "quotico_tips",
            "localField": "sel.match_id",
            "foreignField": "match_id",
            "pipeline": [{"$project": {"confidence": 1, "edge_pct": 1}}],
            "as": "qbet_info",
        }},
        {"$unwind": {"path": "$qbet_info", "preserveNullAndEmptyArrays": True}},
    ]
    docs = await _db.db.betting_slips.aggregate(pipeline).to_list(length=limit)
    return [_slip_projection(d) for d in docs]


async def _build_dashboard(qbot_id: str, qbot_points: float) -> dict:
    """Build the complete dashboard response."""

    async def get_rank():
        count = await _db.db.leaderboard.count_documents({"points": {"$gt": qbot_points}})
        return count + 1

    async def get_hero_stats():
        pipeline = [
            {"$match": {"user_id": qbot_id, "type": "single", "status": {"$in": ["won", "lost"]}}},
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
                "total_points": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, "$total_odds", 0]}},
            }},
        ]
        results = await _db.db.betting_slips.aggregate(pipeline).to_list(length=1)
        if not results:
            return {"total": 0, "won": 0, "total_points": 0.0}
        return results[0]

    async def get_streak():
        docs = await _db.db.betting_slips.find(
            {"user_id": qbot_id, "type": "single", "status": {"$in": ["won", "lost"]}},
            {"status": 1},
        ).sort("resolved_at", -1).to_list(length=50)
        if not docs:
            return {"type": None, "count": 0}
        streak_type = docs[0]["status"]
        count = 0
        for d in docs:
            if d["status"] == streak_type:
                count += 1
            else:
                break
        return {"type": streak_type, "count": count}

    async def get_recent_bets():
        return await _fetch_bets_with_joins(qbot_id, {"status": {"$in": ["won", "lost"]}}, 30, "resolved_at")

    async def get_active_bets():
        return await _fetch_bets_with_joins(qbot_id, {"status": "pending"}, 20)

    async def get_by_sport():
        pipeline = [
            {"$match": {"user_id": qbot_id, "type": "single", "status": {"$in": ["won", "lost"]}}},
            {"$unwind": "$selections"},
            {"$lookup": {
                "from": "matches",
                "let": {"mid": {"$toObjectId": "$selections.match_id"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$mid"]}}},
                    {"$project": {"sport_key": 1}},
                ],
                "as": "mi",
            }},
            {"$unwind": "$mi"},
            {"$group": {
                "_id": "$mi.sport_key",
                "total": {"$sum": 1},
                "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
                "total_points": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, "$total_odds", 0]}},
            }},
            {"$sort": {"total": -1}},
        ]
        docs = await _db.db.betting_slips.aggregate(pipeline).to_list(length=20)
        return [
            {
                "sport_key": d["_id"],
                "total": d["total"],
                "won": d["won"],
                "win_rate": round(d["won"] / d["total"], 3) if d["total"] else 0,
                "total_points": round(d["total_points"], 1),
            }
            for d in docs
        ]

    async def get_win_rate_trend():
        docs = await _db.db.betting_slips.find(
            {"user_id": qbot_id, "type": "single", "status": {"$in": ["won", "lost"]}},
            {"status": 1, "resolved_at": 1},
        ).sort("resolved_at", 1).to_list(length=200)
        if len(docs) < 10:
            return []
        window = 10
        trend = []
        for i in range(window - 1, len(docs)):
            window_slips = docs[i - window + 1: i + 1]
            wins = sum(1 for s in window_slips if s["status"] == "won")
            trend.append({
                "date": docs[i]["resolved_at"],
                "win_rate": round(wins / window, 3),
                "bet_number": i + 1,
            })
        return trend

    async def get_candidates():
        try:
            now = utcnow()
            docs = await _db.db.quotico_tips.find(
                {"status": "active", "match_date": {"$gt": now}},
            ).sort("match_date", 1).limit(30).to_list(length=30)

            results = []
            for doc in docs:
                justification = doc.get("justification", "")
                ts = doc.get("tier_signals", {})
                h2h = ts.get("h2h", {})
                momentum = ts.get("momentum", {})
                sharp = ts.get("sharp_movement", {})

                results.append({
                    "match_id": doc["match_id"],
                    "home_team": doc.get("home_team", ""),
                    "away_team": doc.get("away_team", ""),
                    "sport_key": doc.get("sport_key", ""),
                    "match_date": ensure_utc(doc["match_date"]).isoformat(),
                    "recommended_selection": doc.get("recommended_selection", "-"),
                    "confidence": doc.get("confidence", 0),
                    "edge_pct": doc.get("edge_pct", 0),
                    "true_probability": doc.get("true_probability", 0),
                    "implied_probability": doc.get("implied_probability", 0),
                    "justification": justification[:120] + "..." if len(justification) > 120 else justification,
                    "justification_full": justification,
                    "signals": {
                        "h2h_meetings": h2h.get("total_meetings") if h2h.get("contributes") else None,
                        "sharp_movement": sharp.get("has_sharp_movement", False),
                        "momentum_gap": round(momentum.get("gap", 0), 2) if momentum.get("contributes") else None,
                    },
                    "generated_at": ensure_utc(doc["generated_at"]).isoformat(),
                })
            return results
        except Exception as e:
            logger.error("Failed to fetch candidates: %s", e)
            return []

    async def get_calibration():
        pipeline = [
            {"$match": {"status": "resolved", "was_correct": {"$ne": None}, "confidence": {"$gte": 0.55}}},
            {"$bucket": {
                "groupBy": "$confidence",
                "boundaries": [0.55, 0.60, 0.70, 0.80, 1.01],
                "default": "other",
                "output": {
                    "total": {"$sum": 1},
                    "correct": {"$sum": {"$cond": ["$was_correct", 1, 0]}},
                    "avg_confidence": {"$avg": "$confidence"},
                },
            }},
        ]
        docs = await _db.db.quotico_tips.aggregate(pipeline).to_list(length=10)
        labels = {0.55: "55-60%", 0.60: "60-70%", 0.70: "70-80%", 0.80: "80%+"}
        return [
            {
                "bucket": labels.get(d["_id"], str(d["_id"])),
                "total": d["total"],
                "correct": d["correct"],
                "win_rate": round(d["correct"] / d["total"], 3) if d["total"] else 0,
                "avg_confidence": round(d["avg_confidence"], 3),
            }
            for d in docs
            if d["_id"] != "other"
        ]

    # Run all queries concurrently
    (
        rank,
        hero_raw,
        streak,
        recent_bets,
        active_bets,
        by_sport,
        trend,
        calibration,
        candidates,
    ) = await asyncio.gather(
        get_rank(),
        get_hero_stats(),
        get_streak(),
        get_recent_bets(),
        get_active_bets(),
        get_by_sport(),
        get_win_rate_trend(),
        get_calibration(),
        get_candidates(),
    )

    total = hero_raw["total"]
    won = hero_raw["won"]

    # Include pending bets in total count
    pending_count = await _db.db.betting_slips.count_documents({"user_id": qbot_id, "type": "single", "status": "pending"})

    return {
        "hero": {
            "total_bets": total + pending_count,
            "won": won,
            "lost": total - won,
            "win_rate": round(won / total, 3) if total else 0.0,
            "total_points": round(hero_raw["total_points"], 1),
            "rank": rank,
            "streak": streak,
        },
        "active_bets": active_bets,
        "candidates": candidates,
        "recent_bets": recent_bets,
        "by_sport": by_sport,
        "win_rate_trend": trend,
        "calibration": calibration,
    }


@router.get("/dashboard")
async def qbot_dashboard():
    """Q-Bot performance dashboard — public, no auth required."""
    now = time.monotonic()
    if _cache["data"] and _cache["expires"] > now:
        return _cache["data"]

    qbot = await _get_qbot()
    if not qbot:
        return {
            "hero": {"total_bets": 0, "won": 0, "lost": 0, "win_rate": 0, "total_points": 0, "rank": 0, "streak": {"type": None, "count": 0}},
            "active_bets": [], "candidates": [], "recent_bets": [], "by_sport": [], "win_rate_trend": [], "calibration": [],
        }

    qbot_id, qbot_points = qbot
    data = await _build_dashboard(qbot_id, qbot_points)

    _cache["data"] = data
    _cache["expires"] = now + _CACHE_TTL
    return data
