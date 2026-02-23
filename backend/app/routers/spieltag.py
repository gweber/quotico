"""Spieltag-Modus API endpoints."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

import app.database as _db
from app.config_spieltag import SPIELTAG_SPORTS
from app.models.matchday import (
    AdminPredictionRequest,
    AdminUnlockRequest,
    MatchdayDetailMatch,
    MatchdayResponse,
    PredictionResponse,
    SavePredictionsRequest,
    SpieltagLeaderboardEntry,
    SpieltagPredictionResponse,
)
from app.services.historical_service import build_match_context
from app.services.auth_service import get_current_user
from app.services.spieltag_service import (
    LOCK_MINUTES,
    admin_save_prediction,
    admin_unlock_match,
    get_user_predictions,
    is_match_locked,
    save_predictions,
)

logger = logging.getLogger("quotico.spieltag")

router = APIRouter(prefix="/api/spieltag", tags=["spieltag"])


@router.get("/sports")
async def get_spieltag_sports():
    """Return list of sports available for Spieltag mode."""
    return [
        {
            "sport_key": key,
            "label": config["label_template"].replace("{n}", ""),
            "matchdays_per_season": config["matchdays_per_season"],
        }
        for key, config in SPIELTAG_SPORTS.items()
    ]


@router.get("/matchdays", response_model=list[MatchdayResponse])
async def get_matchdays(
    sport: str = Query(..., description="Sport key"),
    season: int | None = Query(None, description="Season year"),
):
    """Get all matchdays for a sport/season."""
    if sport not in SPIELTAG_SPORTS:
        raise HTTPException(status_code=400, detail="Ungültige Sportart.")

    query: dict = {"sport_key": sport}
    if season:
        query["season"] = season

    matchdays = await _db.db.matchdays.find(query).sort(
        "matchday_number", 1
    ).to_list(length=100)

    return [
        MatchdayResponse(
            id=str(md["_id"]),
            sport_key=md["sport_key"],
            season=md["season"],
            matchday_number=md["matchday_number"],
            label=md["label"],
            match_count=md.get("match_count", 0),
            first_kickoff=md.get("first_kickoff"),
            last_kickoff=md.get("last_kickoff"),
            status=md.get("status", "upcoming"),
            all_resolved=md.get("all_resolved", False),
        )
        for md in matchdays
    ]


@router.get("/matchdays/{matchday_id}", response_model=dict)
async def get_matchday_detail(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Squad context for lock deadline"),
):
    """Get matchday with all matches (teams, times, odds, scores, historical context)."""
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status_code=404, detail="Spieltag nicht gefunden.")

    sport_key = matchday["sport_key"]

    # Resolve squad-specific lock deadline
    lock_mins = LOCK_MINUTES
    if squad_id:
        squad = await _db.db.squads.find_one(
            {"_id": ObjectId(squad_id)}, {"lock_minutes": 1}
        )
        if squad:
            lock_mins = squad.get("lock_minutes", LOCK_MINUTES)

    # Fetch all matches
    match_ids = matchday.get("match_ids", [])
    matches = await _db.db.matches.find(
        {"_id": {"$in": [ObjectId(mid) for mid in match_ids]}}
    ).sort("commence_time", 1).to_list(length=len(match_ids))

    # Fetch historical context for all matches in parallel (cached server-side).
    # Uses return_exceptions so one failed resolution doesn't block the response.
    match_id_strs = [str(m["_id"]) for m in matches]

    h2h_task = asyncio.gather(
        *(
            asyncio.wait_for(
                build_match_context(
                    m.get("teams", {}).get("home", ""),
                    m.get("teams", {}).get("away", ""),
                    sport_key,
                ),
                timeout=3.0,
            )
            for m in matches
        ),
        return_exceptions=True,
    )

    # Fetch QuoticoTips for all matches in one query
    async def _fetch_tips() -> dict[str, dict]:
        tips = await _db.db.quotico_tips.find(
            {"match_id": {"$in": match_id_strs}, "status": {"$in": ["active", "no_signal"]}},
            {"_id": 0, "actual_result": 0, "was_correct": 0},
        ).to_list(length=len(match_id_strs))
        return {t["match_id"]: t for t in tips}

    h2h_results, tips_map = await asyncio.gather(
        h2h_task, _fetch_tips(), return_exceptions=False,
    )

    h2h_contexts = [
        r if not isinstance(r, BaseException) else None
        for r in h2h_results
    ]

    match_responses = []
    for m, ctx in zip(matches, h2h_contexts):
        mid = str(m["_id"])
        tip_doc = tips_map.get(mid)
        # Reshape to match the QuoticoTipResponse format
        qtip = None
        if tip_doc:
            qtip = {
                "match_id": tip_doc["match_id"],
                "sport_key": tip_doc["sport_key"],
                "teams": tip_doc.get("teams", {}),
                "commence_time": tip_doc["match_commence_time"],
                "recommended_selection": tip_doc["recommended_selection"],
                "confidence": tip_doc["confidence"],
                "edge_pct": tip_doc["edge_pct"],
                "true_probability": tip_doc["true_probability"],
                "implied_probability": tip_doc["implied_probability"],
                "expected_goals_home": tip_doc["expected_goals_home"],
                "expected_goals_away": tip_doc["expected_goals_away"],
                "tier_signals": tip_doc["tier_signals"],
                "justification": tip_doc["justification"],
                "generated_at": tip_doc["generated_at"],
            }
        match_responses.append(
            MatchdayDetailMatch(
                id=str(m["_id"]),
                teams=m.get("teams", {}),
                commence_time=m["commence_time"],
                status=m.get("status", "upcoming"),
                current_odds=m.get("current_odds", {}),
                totals_odds=m.get("totals_odds", {}),
                spreads_odds=m.get("spreads_odds", {}),
                result=m.get("result"),
                home_score=m.get("home_score"),
                away_score=m.get("away_score"),
                is_locked=is_match_locked(m, lock_mins),
                h2h_context=ctx,
                quotico_tip=qtip,
            )
        )

    return {
        "matchday": MatchdayResponse(
            id=str(matchday["_id"]),
            sport_key=matchday["sport_key"],
            season=matchday["season"],
            matchday_number=matchday["matchday_number"],
            label=matchday["label"],
            match_count=matchday.get("match_count", 0),
            first_kickoff=matchday.get("first_kickoff"),
            last_kickoff=matchday.get("last_kickoff"),
            status=matchday.get("status", "upcoming"),
            all_resolved=matchday.get("all_resolved", False),
        ),
        "matches": match_responses,
    }


@router.get(
    "/matchdays/{matchday_id}/predictions",
    response_model=SpieltagPredictionResponse | None,
)
async def get_predictions(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Squad context"),
    user=Depends(get_current_user),
):
    """Get current user's predictions for a matchday (optionally squad-scoped)."""
    user_id = str(user["_id"])
    pred = await get_user_predictions(user_id, matchday_id, squad_id=squad_id)
    if not pred:
        return None

    return SpieltagPredictionResponse(
        matchday_id=matchday_id,
        squad_id=pred.get("squad_id"),
        auto_tipp_strategy=pred.get("auto_tipp_strategy", "none"),
        predictions=[
            PredictionResponse(
                match_id=p["match_id"],
                home_score=p["home_score"],
                away_score=p["away_score"],
                is_auto=p.get("is_auto", False),
                is_admin_entry=p.get("is_admin_entry", False),
                points_earned=p.get("points_earned"),
            )
            for p in pred.get("predictions", [])
        ],
        admin_unlocked_matches=pred.get("admin_unlocked_matches", []),
        total_points=pred.get("total_points"),
        status=pred.get("status", "open"),
    )


@router.post("/matchdays/{matchday_id}/predictions")
async def save_matchday_predictions(
    matchday_id: str,
    body: SavePredictionsRequest,
    user=Depends(get_current_user),
):
    """Save or update predictions for a matchday."""
    user_id = str(user["_id"])

    predictions = [
        {
            "match_id": p.match_id,
            "home_score": p.home_score,
            "away_score": p.away_score,
        }
        for p in body.predictions
    ]

    result = await save_predictions(
        user_id=user_id,
        matchday_id=matchday_id,
        predictions=predictions,
        auto_tipp_strategy=body.auto_tipp_strategy.value,
        squad_id=body.squad_id,
    )

    return {
        "saved": len(result.get("predictions", [])),
        "auto_tipp_strategy": result.get("auto_tipp_strategy"),
    }


@router.get("/matchdays/{matchday_id}/leaderboard")
async def get_matchday_leaderboard(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Filter by squad"),
):
    """Get leaderboard for a specific matchday (optionally squad-scoped)."""
    matchday = await _db.db.matchdays.find_one({"_id": ObjectId(matchday_id)})
    if not matchday:
        raise HTTPException(status_code=404, detail="Spieltag nicht gefunden.")

    # Build match filter
    match_filter: dict = {"matchday_id": matchday_id, "status": "resolved"}
    if squad_id:
        match_filter["squad_id"] = squad_id
        # Also restrict to squad members
        squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
        if squad:
            match_filter["user_id"] = {"$in": squad.get("members", [])}

    # Aggregate predictions for this matchday
    pipeline = [
        {"$match": match_filter},
        {"$sort": {"total_points": -1}},
        {"$limit": 100},
        {
            "$lookup": {
                "from": "users",
                "let": {"uid": {"$toObjectId": "$user_id"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$uid"]}}},
                    {"$project": {"alias": 1}},
                ],
                "as": "user_info",
            }
        },
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}},
    ]

    entries = await _db.db.spieltag_predictions.aggregate(pipeline).to_list(length=100)

    leaderboard = []
    for i, entry in enumerate(entries):
        preds = entry.get("predictions", [])
        exact = sum(1 for p in preds if p.get("points_earned") == 3)
        diff = sum(1 for p in preds if p.get("points_earned") == 2)
        tendency = sum(1 for p in preds if p.get("points_earned") == 1)

        leaderboard.append({
            "rank": i + 1,
            "user_id": entry["user_id"],
            "alias": entry.get("user_info", {}).get("alias", "Anonym"),
            "total_points": entry.get("total_points", 0),
            "exact_count": exact,
            "diff_count": diff,
            "tendency_count": tendency,
        })

    return leaderboard


@router.get("/leaderboard")
async def get_season_leaderboard(
    sport: str = Query(..., description="Sport key"),
    season: int | None = Query(None, description="Season year"),
    squad_id: str | None = Query(None, description="Filter by squad"),
):
    """Get season-wide leaderboard for a sport (optionally squad-scoped)."""
    if sport not in SPIELTAG_SPORTS:
        raise HTTPException(status_code=400, detail="Ungültige Sportart.")

    query: dict = {"sport_key": sport}
    if season:
        query["season"] = season
    if squad_id:
        query["squad_id"] = squad_id

    entries = await _db.db.spieltag_leaderboard.find(query).sort(
        "total_points", -1
    ).to_list(length=100)

    return [
        {
            "rank": i + 1,
            "user_id": e["user_id"],
            "alias": e.get("alias", "Anonym"),
            "total_points": e.get("total_points", 0),
            "matchdays_played": e.get("matchdays_played", 0),
            "exact_count": e.get("exact_count", 0),
            "diff_count": e.get("diff_count", 0),
            "tendency_count": e.get("tendency_count", 0),
        }
        for i, e in enumerate(entries)
    ]


# ---------- Squad admin: prediction management ----------


@router.get("/admin/members/{squad_id}")
async def get_squad_members_for_admin(
    squad_id: str,
    user=Depends(get_current_user),
):
    """Get squad members with aliases (admin only). Used for the admin prediction panel."""
    user_id = str(user["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if squad["admin_id"] != user_id:
        raise HTTPException(status_code=403, detail="Nur der Squad-Admin kann das.")

    member_ids = squad.get("members", [])
    users = await _db.db.users.find(
        {"_id": {"$in": [ObjectId(uid) for uid in member_ids]}},
        {"alias": 1},
    ).to_list(length=len(member_ids))

    return [
        {"user_id": str(u["_id"]), "alias": u.get("alias", "Anonym")}
        for u in users
    ]


@router.get("/admin/predictions/{matchday_id}")
async def get_admin_user_predictions(
    matchday_id: str,
    squad_id: str = Query(..., description="Squad ID"),
    user_id: str = Query(..., description="Target user ID"),
    admin=Depends(get_current_user),
):
    """Get a specific user's predictions (squad admin only)."""
    admin_id = str(admin["_id"])
    squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
    if not squad:
        raise HTTPException(status_code=404, detail="Squad nicht gefunden.")
    if squad["admin_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Nur der Squad-Admin kann das.")

    pred = await get_user_predictions(user_id, matchday_id, squad_id=squad_id)
    if not pred:
        return None

    return SpieltagPredictionResponse(
        matchday_id=matchday_id,
        squad_id=pred.get("squad_id"),
        auto_tipp_strategy=pred.get("auto_tipp_strategy", "none"),
        predictions=[
            PredictionResponse(
                match_id=p["match_id"],
                home_score=p["home_score"],
                away_score=p["away_score"],
                is_auto=p.get("is_auto", False),
                is_admin_entry=p.get("is_admin_entry", False),
                points_earned=p.get("points_earned"),
            )
            for p in pred.get("predictions", [])
        ],
        admin_unlocked_matches=pred.get("admin_unlocked_matches", []),
        total_points=pred.get("total_points"),
        status=pred.get("status", "open"),
    )


@router.post("/admin/unlock")
async def unlock_match_for_user(
    body: AdminUnlockRequest,
    user=Depends(get_current_user),
):
    """Squad admin unlocks a match for a user to place a late prediction."""
    admin_id = str(user["_id"])
    return await admin_unlock_match(
        admin_id, body.squad_id, body.user_id, body.matchday_id, body.match_id,
    )


@router.post("/admin/prediction")
async def save_admin_prediction(
    body: AdminPredictionRequest,
    user=Depends(get_current_user),
):
    """Squad admin enters a prediction on behalf of a user. Points recalculated if match done."""
    admin_id = str(user["_id"])
    return await admin_save_prediction(
        admin_id, body.squad_id, body.user_id,
        body.matchday_id, body.match_id,
        body.home_score, body.away_score,
    )
