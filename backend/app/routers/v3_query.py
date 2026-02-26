"""
backend/app/routers/v3_query.py

Purpose:
    Public v3 read/query endpoints for matches, teams, persons, stats, and
    qbot-oriented read models with strict payload validation.

Dependencies:
    - app.database
    - app.config
    - app.models.v3_query_models
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

import app.database as _db
from app.config import settings
from app.models.v3_query_models import (
    BatchIdsRequest,
    MatchesListResponse,
    MatchesQueryRequest,
    MatchV3Out,
    QbotTipsQueryRequest,
    QbotTipsQueryResponse,
    StatsQueryRequest,
    StatsQueryResponse,
    V3ListMeta,
)
from app.services.auth_service import get_current_user
from app.utils import ensure_utc, utcnow

router = APIRouter(prefix="/api/v3", tags=["v3"])

_QUERY_CACHE: dict[str, tuple[Any, Any]] = {}


def _query_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Any | None:
    row = _QUERY_CACHE.get(key)
    if not row:
        return None
    expires_at, data = row
    if ensure_utc(expires_at) < utcnow():
        _QUERY_CACHE.pop(key, None)
        return None
    return data


def _cache_set(key: str, value: Any) -> None:
    ttl = timedelta(seconds=int(settings.V3_QUERY_CACHE_TTL_SECONDS))
    _QUERY_CACHE[key] = (utcnow() + ttl, value)


def _base_query(
    *,
    season_id: int | None = None,
    league_id: int | None = None,
    team_id: int | None = None,
    statuses: list[str] | None = None,
    date_from=None,
    date_to=None,
    ids: list[int] | None = None,
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if ids:
        q["_id"] = {"$in": [int(x) for x in ids]}
    if season_id is not None:
        q["season_id"] = int(season_id)
    if league_id is not None:
        q["league_id"] = int(league_id)
    if team_id is not None:
        q["$or"] = [{"teams.home.sm_id": int(team_id)}, {"teams.away.sm_id": int(team_id)}]
    if statuses:
        q["status"] = {"$in": [str(s).upper() for s in statuses if str(s).strip()]}
    if date_from is not None or date_to is not None:
        date_q: dict[str, Any] = {}
        if date_from is not None:
            date_q["$gte"] = ensure_utc(date_from)
        if date_to is not None:
            date_q["$lte"] = ensure_utc(date_to)
        q["start_at"] = date_q
    return q


@router.get("/matches", response_model=MatchesListResponse)
async def list_matches_v3(
    status: str | None = Query(default=None),
    season_id: int | None = Query(default=None),
    league_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MatchesListResponse:
    statuses = [status] if status else []
    q = _base_query(
        season_id=season_id,
        league_id=league_id,
        team_id=team_id,
        statuses=statuses,
    )
    total = await _db.db.matches_v3.count_documents(q)
    rows = await _db.db.matches_v3.find(q).sort("start_at", -1).skip(offset).limit(limit).to_list(length=limit)
    items = [MatchV3Out.model_validate(row) for row in rows]
    return MatchesListResponse(items=items, meta=V3ListMeta(total=total, limit=limit, offset=offset))


@router.get("/matches/{match_id}", response_model=MatchV3Out)
async def get_match_v3(match_id: int) -> MatchV3Out:
    row = await _db.db.matches_v3.find_one({"_id": int(match_id)})
    if not isinstance(row, dict):
        raise HTTPException(status_code=404, detail="Match not found.")
    return MatchV3Out.model_validate(row)


@router.get("/matches/{match_id}/odds-timeline")
async def get_match_odds_timeline_v3(match_id: int) -> dict[str, Any]:
    row = await _db.db.matches_v3.find_one({"_id": int(match_id)}, {"odds_timeline": 1})
    if not isinstance(row, dict):
        raise HTTPException(status_code=404, detail="Match not found.")
    timeline = row.get("odds_timeline") if isinstance(row.get("odds_timeline"), list) else []
    return {"match_id": int(match_id), "items": timeline}


@router.post("/matches/query", response_model=MatchesListResponse)
async def query_matches_v3(body: MatchesQueryRequest) -> MatchesListResponse:
    ids = [int(x) for x in body.ids if isinstance(x, int)]
    max_ids = int(settings.V3_QUERY_MAX_IDS)
    if len(ids) > max_ids:
        raise HTTPException(status_code=422, detail=f"ids length exceeds limit ({max_ids}).")

    payload = body.model_dump(mode="json")
    key = _query_hash(payload)
    cached = _cache_get(key)
    if cached is not None:
        cached["meta"]["source"] = "cache"
        cached["meta"]["query_hash"] = key
        return MatchesListResponse.model_validate(cached)

    q = _base_query(
        ids=ids,
        season_id=body.season_id,
        league_id=body.league_id,
        team_id=body.team_id,
        statuses=body.statuses,
        date_from=body.date_from,
        date_to=body.date_to,
    )
    sort_field = body.sort_by
    sort_dir = 1 if body.sort_dir == "asc" else -1
    total = await _db.db.matches_v3.count_documents(q)
    rows = await _db.db.matches_v3.find(q).sort(sort_field, sort_dir).skip(body.offset).limit(body.limit).to_list(length=body.limit)
    data = {
        "items": rows,
        "meta": {
            "total": total,
            "limit": body.limit,
            "offset": body.offset,
            "query_hash": key,
            "source": "fresh",
        },
    }
    _cache_set(key, data)
    return MatchesListResponse.model_validate(data)


@router.post("/stats/query", response_model=StatsQueryResponse)
async def query_stats_v3(body: StatsQueryRequest) -> StatsQueryResponse:
    q = _base_query(
        season_id=body.season_id,
        league_id=body.league_id,
        statuses=[body.status] if body.status else [],
    )
    total = await _db.db.matches_v3.count_documents(q)
    advanced = await _db.db.matches_v3.count_documents({**q, "has_advanced_stats": True})
    odds_covered = await _db.db.matches_v3.count_documents(
        {
            **q,
            "odds_meta.summary_1x2.home.avg": {"$exists": True},
            "odds_meta.summary_1x2.draw.avg": {"$exists": True},
            "odds_meta.summary_1x2.away.avg": {"$exists": True},
        }
    )
    xg_pct = round((advanced / total) * 100.0, 2) if total else 0.0
    odds_pct = round((odds_covered / total) * 100.0, 2) if total else 0.0
    return StatsQueryResponse(
        total_matches=total,
        advanced_stats_matches=advanced,
        odds_covered_matches=odds_covered,
        xg_coverage_percent=xg_pct,
        odds_coverage_percent=odds_pct,
    )


@router.post("/qbot/tips/query", response_model=QbotTipsQueryResponse)
async def query_qbot_tips_v3(body: QbotTipsQueryRequest) -> QbotTipsQueryResponse:
    q = _base_query(season_id=body.season_id, league_id=body.league_id)
    q["status"] = "FINISHED"
    rows = await _db.db.matches_v3.find(
        q,
        {
            "_id": 1,
            "season_id": 1,
            "league_id": 1,
            "start_at": 1,
            "status": 1,
            "teams": 1,
            "odds_meta.summary_1x2": 1,
        },
    ).sort("start_at", -1).limit(body.limit).to_list(length=body.limit)
    meta = V3ListMeta(total=len(rows), limit=body.limit, offset=0)
    return QbotTipsQueryResponse(items=rows, meta=meta)


@router.post("/teams/batch")
async def teams_batch_v3(body: BatchIdsRequest, user=Depends(get_current_user)) -> dict[str, list[dict[str, Any]]]:
    _ = user
    ids = sorted(set(int(x) for x in body.ids if isinstance(x, int)))
    max_ids = int(settings.V3_QUERY_MAX_IDS)
    if len(ids) > max_ids:
        raise HTTPException(status_code=422, detail=f"ids length exceeds limit ({max_ids}).")
    rows = await _db.db.teams_v3.find(
        {"_id": {"$in": ids}},
        {"_id": 1, "name": 1, "short_code": 1, "image_path": 1},
    ).to_list(length=max(50, len(ids)))
    items = [{"id": int(r["_id"]), "name": str(r.get("name") or ""), "short_code": r.get("short_code"), "image_path": r.get("image_path")} for r in rows if isinstance(r, dict) and isinstance(r.get("_id"), int)]
    return {"items": items}


@router.post("/persons/batch")
async def persons_batch_v3(body: BatchIdsRequest, user=Depends(get_current_user)) -> dict[str, list[dict[str, Any]]]:
    _ = user
    ids = sorted(set(int(x) for x in body.ids if isinstance(x, int)))
    max_ids = int(settings.V3_QUERY_MAX_IDS)
    if len(ids) > max_ids:
        raise HTTPException(status_code=422, detail=f"ids length exceeds limit ({max_ids}).")
    rows = await _db.db.persons.find(
        {"_id": {"$in": ids}},
        {"_id": 1, "type": 1, "name": 1, "common_name": 1, "image_path": 1},
    ).to_list(length=max(50, len(ids)))
    items = []
    for row in rows:
        pid = row.get("_id")
        if not isinstance(pid, int):
            continue
        items.append(
            {
                "id": pid,
                "type": str(row.get("type") or ""),
                "name": str(row.get("name") or ""),
                "common_name": str(row.get("common_name") or ""),
                "image_path": str(row.get("image_path") or ""),
            }
        )
    return {"items": items}

