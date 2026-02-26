"""
backend/app/routers/matchday.py

Purpose:
    Matchday API endpoints for v3.1-only sports, rounds, predictions, and
    leaderboard data backed by matches_v3.

Dependencies:
    - app.database
    - app.config
    - app.config_matchday
    - app.services.matchday_v3_cache_service
    - app.services.matchday_service
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

import app.database as _db
from app.config import settings
from app.config_matchday import MATCHDAY_V3_SPORTS
from app.services.historical_service import build_h2h
from app.models.matchday import (
    MatchdayDetailMatch,
    MatchdayResponse,
    PredictionResponse,
    SavePredictionsRequest,
    MatchdayPredictionResponse,
)
from app.services.auth_service import get_current_user
from app.services.matchday_v3_cache_service import (
    build_matchday_cache_key,
    get_matchday_list_cache,
    set_matchday_list_cache,
)
from app.utils import as_utc
from app.services.matchday_service import (
    LOCK_MINUTES,
    get_user_predictions,
    save_predictions,
)

logger = logging.getLogger("quotico.matchday")

router = APIRouter(prefix="/api/matchday", tags=["matchday"])


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _v3_matchday_id(*, sport_key: str, season_id: int, round_id: int) -> str:
    return f"v3:{sport_key}:{int(season_id)}:{int(round_id)}"


def _parse_v3_matchday_id(matchday_id: str) -> tuple[str, int, int] | None:
    parts = str(matchday_id or "").split(":")
    if len(parts) != 4 or parts[0] != "v3":
        return None
    sport_key = str(parts[1] or "").strip()
    season_id = _as_int(parts[2])
    round_id = _as_int(parts[3])
    if not sport_key or season_id is None or round_id is None:
        return None
    return sport_key, int(season_id), int(round_id)


def _map_v3_status_to_matchday(status_value: str) -> str:
    status = str(status_value or "").upper()
    if status in {"LIVE"}:
        return "in_progress"
    if status in {"FINISHED"}:
        return "completed"
    return "upcoming"


def _map_v3_status_to_legacy_match(status_value: str) -> str:
    status = str(status_value or "").upper()
    if status == "FINISHED":
        return "final"
    if status == "LIVE":
        return "live"
    if status in {"POSTPONED", "CANCELED", "WALKOVER"}:
        return "cancelled"
    return "scheduled"


async def _resolve_v3_league_ids_for_sport(sport_key: str) -> list[int]:
    cfg = MATCHDAY_V3_SPORTS.get(sport_key) or {}
    return [int(x) for x in (cfg.get("league_ids") or []) if _as_int(x) is not None]


async def _list_v3_matchdays_for_sport(sport_key: str, season: int | None) -> list[MatchdayResponse]:
    league_ids = await _resolve_v3_league_ids_for_sport(sport_key)
    if not league_ids:
        return []
    base_match: dict[str, Any] = {"league_id": {"$in": league_ids}}
    if season is not None:
        base_match["season_id"] = int(season)
    season_ids = await _db.db.matches_v3.distinct("season_id", base_match)
    season_ids_int = [int(sid) for sid in season_ids if _as_int(sid) is not None]
    if not season_ids_int:
        return []
    selected_season_id = max(season_ids_int)
    cache_key = build_matchday_cache_key(sport_key=sport_key, season_id=int(selected_season_id))
    cached = await get_matchday_list_cache(cache_key=cache_key)
    if isinstance(cached, list):
        return [MatchdayResponse.model_validate(row) for row in cached]
    pipeline = [
        {"$match": {"league_id": {"$in": league_ids}, "season_id": int(selected_season_id)}},
        {
            "$group": {
                "_id": {"season_id": "$season_id", "round_id": "$round_id"},
                "match_count": {"$sum": 1},
                "first_kickoff": {"$min": "$start_at"},
                "last_kickoff": {"$max": "$start_at"},
                "statuses": {"$addToSet": "$status"},
            }
        },
        {"$sort": {"first_kickoff": 1}},
    ]
    rows = await _db.db.matches_v3.aggregate(pipeline).to_list(length=500)
    items: list[MatchdayResponse] = []
    label_template = str((MATCHDAY_V3_SPORTS.get(sport_key) or {}).get("label_template") or "Matchday {n}")
    for idx, row in enumerate(rows, start=1):
        rid_node = (row or {}).get("_id") if isinstance((row or {}).get("_id"), dict) else {}
        round_id = _as_int((rid_node or {}).get("round_id"))
        if round_id is None:
            continue
        statuses = {(str(s or "").upper()) for s in ((row or {}).get("statuses") or [])}
        if statuses and statuses.issubset({"FINISHED"}):
            md_status = "completed"
        elif "LIVE" in statuses:
            md_status = "in_progress"
        else:
            first = row.get("first_kickoff")
            first_dt = as_utc(first) if first else None
            if first_dt and first_dt <= datetime.now(timezone.utc):
                md_status = "in_progress"
            else:
                md_status = "upcoming"
        items.append(
            MatchdayResponse(
                id=_v3_matchday_id(sport_key=sport_key, season_id=int(selected_season_id), round_id=int(round_id)),
                sport_key=sport_key,
                season=int(selected_season_id),
                matchday_number=int(idx),
                label=label_template.replace("{n}", str(idx)),
                match_count=int((row or {}).get("match_count") or 0),
                first_kickoff=as_utc((row or {}).get("first_kickoff")),
                last_kickoff=as_utc((row or {}).get("last_kickoff")),
                status=md_status,
                all_resolved=md_status == "completed",
            )
        )
    await set_matchday_list_cache(
        cache_key=cache_key,
        payload=[item.model_dump(mode="json") for item in items],
        ttl_seconds=int(settings.MATCHDAY_V3_CACHE_TTL_SECONDS),
    )
    return items


async def _get_v3_matchday_detail(matchday_id: str, lock_mins: int) -> dict | None:
    parsed = _parse_v3_matchday_id(matchday_id)
    if parsed is None:
        return None
    sport_key, season_id, round_id = parsed
    league_ids = await _resolve_v3_league_ids_for_sport(sport_key)
    if not league_ids:
        return None
    rows = await _db.db.matches_v3.find(
        {"league_id": {"$in": league_ids}, "season_id": int(season_id), "round_id": int(round_id)},
        {
            "_id": 1,
            "teams": 1,
            "start_at": 1,
            "status": 1,
            "odds_meta": 1,
            "has_advanced_stats": 1,
            "referee_id": 1,
            "referee_name": 1,
        },
    ).sort("start_at", 1).to_list(length=64)
    if not rows:
        return None
    first_kickoff = as_utc(rows[0].get("start_at")) if rows[0].get("start_at") else None
    last_kickoff = as_utc(rows[-1].get("start_at")) if rows[-1].get("start_at") else None
    statuses = {_map_v3_status_to_matchday(str((r or {}).get("status") or "")) for r in rows}
    md_status = "completed" if statuses == {"completed"} else ("in_progress" if "in_progress" in statuses else "upcoming")
    match_responses: list[MatchdayDetailMatch] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        teams = (row or {}).get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        start_at = as_utc((row or {}).get("start_at"))
        deadline = (start_at - timedelta(minutes=lock_mins)) if start_at else None
        is_locked = bool(deadline is None or now >= deadline)
        match_responses.append(
            MatchdayDetailMatch(
                id=str(int((row or {}).get("_id"))),
                home_team=str(home.get("name") or ""),
                away_team=str(away.get("name") or ""),
                match_date=start_at or datetime.now(timezone.utc),
                status=_map_v3_status_to_legacy_match(str((row or {}).get("status") or "")),
                odds={},
                result={"home_score": home.get("score"), "away_score": away.get("score")},
                odds_meta=(row or {}).get("odds_meta"),
                has_advanced_stats=bool((row or {}).get("has_advanced_stats", False)),
                referee_id=(row or {}).get("referee_id"),
                referee_name=(row or {}).get("referee_name"),
                teams=teams,
                is_locked=is_locked,
                h2h_context=None,
                quotico_tip=None,
            )
        )
    # Embed H2H context — parallel indexed queries for all fixtures
    team_pairs = []
    for row in rows:
        teams = (row or {}).get("teams") or {}
        h_id = (teams.get("home") or {}).get("sm_id")
        a_id = (teams.get("away") or {}).get("sm_id")
        team_pairs.append((h_id, a_id))

    h2h_results = await asyncio.gather(*(
        build_h2h(h_id, a_id) if (h_id and a_id) else asyncio.sleep(0)
        for h_id, a_id in team_pairs
    ))
    for match_resp, h2h, (h_id, a_id) in zip(match_responses, h2h_results, team_pairs):
        if h2h:
            match_resp.h2h_context = {
                "h2h": h2h,
                "home_form": [],
                "away_form": [],
                "home_team_id": h_id,
                "away_team_id": a_id,
            }

    return {
        "matchday": MatchdayResponse(
            id=matchday_id,
            sport_key=sport_key,
            season=int(season_id),
            matchday_number=int(round_id),
            label=f"Round {int(round_id)}",
            match_count=len(match_responses),
            first_kickoff=first_kickoff,
            last_kickoff=last_kickoff,
            status=md_status,
            all_resolved=md_status == "completed",
        ),
        "matches": match_responses,
    }


@router.get("/sports")
async def get_matchday_sports():
    """Return list of sports available for v3.1 Matchday mode."""
    visible = []
    for key, config in MATCHDAY_V3_SPORTS.items():
        if not bool(config.get("enabled", True)):
            continue
        v3_matchdays = await _list_v3_matchdays_for_sport(key, season=None)
        if not v3_matchdays:
            continue
        visible.append(
            {
                "sport_key": key,
                "label": str(config.get("label_template") or "Matchday {n}").replace("{n}", ""),
                "matchdays_per_season": int(config.get("matchdays_per_season") or 0),
            }
        )
    return visible


@router.get("/matchdays", response_model=list[MatchdayResponse])
async def get_matchdays(
    sport: str = Query(..., description="Sport key"),
    season: int | None = Query(None, description="Season year"),
):
    """Get all matchdays for a sport/season."""
    if sport not in MATCHDAY_V3_SPORTS:
        raise HTTPException(status_code=400, detail="Invalid sport.")
    if not bool((MATCHDAY_V3_SPORTS.get(sport) or {}).get("enabled", True)):
        raise HTTPException(status_code=404, detail="Sport not available.")
    return await _list_v3_matchdays_for_sport(sport_key=sport, season=season)


@router.get("/matchdays/{matchday_id}", response_model=dict)
async def get_matchday_detail(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Squad context for lock deadline"),
):
    """Get v3.1 matchday with matches (teams, times, odds, scores)."""
    parsed = _parse_v3_matchday_id(matchday_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid matchday_id format.")
    lock_mins = LOCK_MINUTES
    if squad_id:
        try:
            squad_oid = ObjectId(squad_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid squad_id.") from exc
        squad = await _db.db.squads.find_one({"_id": squad_oid}, {"lock_minutes": 1})
        if squad:
            lock_mins = squad.get("lock_minutes", LOCK_MINUTES)
    v3_detail = await _get_v3_matchday_detail(matchday_id, lock_mins=lock_mins)
    if v3_detail is None:
        raise HTTPException(status_code=404, detail="Matchday not found.")
    return v3_detail


@router.get(
    "/matchdays/{matchday_id}/predictions",
    response_model=MatchdayPredictionResponse | None,
)
async def get_predictions(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Squad context"),
    user=Depends(get_current_user),
):
    """Get current user's predictions for a matchday (optionally squad-scoped)."""
    if _parse_v3_matchday_id(matchday_id) is None:
        raise HTTPException(status_code=400, detail="Invalid matchday_id format.")
    user_id = str(user["_id"])
    pred = await get_user_predictions(user_id, matchday_id, squad_id=squad_id)
    if not pred:
        return None

    return MatchdayPredictionResponse(
        matchday_id=matchday_id,
        squad_id=pred.get("squad_id"),
        auto_bet_strategy=pred.get("auto_bet_strategy", "none"),
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
    if _parse_v3_matchday_id(matchday_id) is None:
        raise HTTPException(status_code=400, detail="Invalid matchday_id format.")
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
        auto_bet_strategy=body.auto_bet_strategy.value,
        squad_id=body.squad_id,
    )

    return {
        "saved": len(result.get("predictions", [])),
        "auto_bet_strategy": result.get("auto_bet_strategy"),
    }


@router.get("/matchdays/{matchday_id}/leaderboard")
async def get_matchday_leaderboard(
    matchday_id: str,
    squad_id: str | None = Query(None, description="Filter by squad"),
):
    """Get leaderboard for a specific matchday (optionally squad-scoped)."""
    parsed = _parse_v3_matchday_id(matchday_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid matchday_id format.")

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

    entries = await _db.db.matchday_predictions.aggregate(pipeline).to_list(length=100)

    leaderboard = []
    for i, entry in enumerate(entries):
        preds = entry.get("predictions", [])
        exact = sum(1 for p in preds if p.get("points_earned") == 3)
        diff = sum(1 for p in preds if p.get("points_earned") == 2)
        tendency = sum(1 for p in preds if p.get("points_earned") == 1)

        leaderboard.append({
            "rank": i + 1,
            "user_id": entry["user_id"],
            "alias": entry.get("user_info", {}).get("alias", "Anonymous"),
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
    if sport not in MATCHDAY_V3_SPORTS:
        raise HTTPException(status_code=400, detail="Invalid sport.")

    query: dict = {"sport_key": sport}
    if season:
        query["season"] = season
    if squad_id:
        query["squad_id"] = squad_id

    entries = await _db.db.matchday_leaderboard.find(query).sort(
        "total_points", -1
    ).to_list(length=100)

    return [
        {
            "rank": i + 1,
            "user_id": e["user_id"],
            "alias": e.get("alias", "Anonymous"),
            "total_points": e.get("total_points", 0),
            "matchdays_played": e.get("matchdays_played", 0),
            "exact_count": e.get("exact_count", 0),
            "diff_count": e.get("diff_count", 0),
            "tendency_count": e.get("tendency_count", 0),
        }
        for i, e in enumerate(entries)
    ]
