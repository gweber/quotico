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
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

import app.database as _db
from app.config import settings
from app.models.v3_query_models import (
    BatchIdsRequest,
    JusticeMetrics,
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
from app.services.justice_service import JusticeService
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.v3_query")

router = APIRouter(prefix="/api/v3", tags=["v3"])

_QUERY_CACHE: dict[str, tuple[Any, Any]] = {}
_ANALYSIS_CACHE: dict[str, tuple[Any, Any]] = {}
_JUSTICE_SERVICE = JusticeService()


def _compute_justice(doc: dict) -> dict | None:
    """Compute justice metrics for a single match document.

    Returns a JusticeMetrics-compatible dict or None if data is insufficient.
    """
    if doc.get("status") != "FINISHED" or not doc.get("has_advanced_stats"):
        return None
    teams = doc.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    xg_home = home.get("xg")
    xg_away = away.get("xg")
    if not isinstance(xg_home, (int, float)) or not isinstance(xg_away, (int, float)):
        return None
    total_xg = xg_home + xg_away
    if total_xg == 0:
        return None
    odds_meta = doc.get("odds_meta") or {}
    summary = odds_meta.get("summary_1x2") or {}
    avg_home = (summary.get("home") or {}).get("avg")
    if not isinstance(avg_home, (int, float)) or avg_home <= 0:
        return None
    xg_share_home = xg_home / total_xg
    implied_prob_home = 1.0 / avg_home
    justice_diff = xg_share_home - implied_prob_home
    return {
        "xg_share_home": round(xg_share_home, 4),
        "implied_prob_home": round(implied_prob_home, 4),
        "justice_diff": round(justice_diff, 4),
    }


def _enrich_with_justice(rows: list[dict]) -> list[dict]:
    """Inject justice metrics into each qualifying match document."""
    for row in rows:
        j = _compute_justice(row)
        if j is not None:
            row["justice"] = j
    return rows


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

    justice_mode = body.min_justice_diff is not None

    statuses = body.statuses
    q_extra: dict[str, Any] = {}
    if justice_mode:
        statuses = ["FINISHED"]
        q_extra["has_advanced_stats"] = True

    q = _base_query(
        ids=ids,
        season_id=body.season_id,
        league_id=body.league_id,
        team_id=body.team_id,
        statuses=statuses,
        date_from=body.date_from,
        date_to=body.date_to,
    )
    q.update(q_extra)
    sort_field = body.sort_by
    sort_dir = 1 if body.sort_dir == "asc" else -1

    if justice_mode:
        # Fetch all qualifying matches, compute justice, filter, sort, paginate in Python
        all_rows = await _db.db.matches_v3.find(q).sort(sort_field, sort_dir).to_list(length=5000)
        _enrich_with_justice(all_rows)
        threshold = body.min_justice_diff or 0.0
        filtered = [r for r in all_rows if (r.get("justice") or {}).get("justice_diff", 0) >= threshold]
        # Sort by absolute justice_diff descending
        filtered.sort(key=lambda r: abs((r.get("justice") or {}).get("justice_diff", 0)), reverse=True)
        total = len(filtered)
        page = filtered[body.offset : body.offset + body.limit]
        data = {
            "items": page,
            "meta": {"total": total, "limit": body.limit, "offset": body.offset, "query_hash": key, "source": "fresh"},
        }
    else:
        total = await _db.db.matches_v3.count_documents(q)
        rows = await _db.db.matches_v3.find(q).sort(sort_field, sort_dir).skip(body.offset).limit(body.limit).to_list(length=body.limit)
        _enrich_with_justice(rows)
        data = {
            "items": rows,
            "meta": {"total": total, "limit": body.limit, "offset": body.offset, "query_hash": key, "source": "fresh"},
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


# ---------------------------------------------------------------------------
# Public Analysis Endpoints
# ---------------------------------------------------------------------------

_ANALYSIS_CACHE_TTL = timedelta(minutes=10)


def _analysis_cache_get(key: str) -> Any | None:
    row = _ANALYSIS_CACHE.get(key)
    if not row:
        return None
    expires_at, data = row
    if ensure_utc(expires_at) < utcnow():
        _ANALYSIS_CACHE.pop(key, None)
        return None
    return data


def _analysis_cache_set(key: str, value: Any) -> None:
    _ANALYSIS_CACHE[key] = (utcnow() + _ANALYSIS_CACHE_TTL, value)


@router.get("/analysis/leagues")
async def analysis_leagues() -> dict[str, Any]:
    """Return leagues with sufficient xG data for justice table analysis."""
    cached = _analysis_cache_get("analysis:leagues")
    if cached is not None:
        return cached

    leagues = await _db.db.league_registry_v3.find(
        {},
        {"_id": 1, "name": 1, "country": 1, "available_seasons": 1},
    ).to_list(length=200)

    items: list[dict[str, Any]] = []
    for lg in leagues:
        lid = lg.get("_id")
        if not isinstance(lid, int):
            continue
        name = lg.get("name") or ""
        if not name:
            continue

        seasons = lg.get("available_seasons") or []
        if not seasons:
            continue

        # Pick the most recent season (highest ID)
        current = max(seasons, key=lambda s: s.get("id") or 0)
        season_id = current.get("id")
        if not isinstance(season_id, int):
            continue

        xg_count = await _db.db.matches_v3.count_documents(
            {"league_id": lid, "season_id": season_id, "status": "FINISHED", "has_advanced_stats": True}
        )
        if xg_count < 10:
            continue

        items.append({
            "league_id": lid,
            "league_name": name,
            "country": lg.get("country") or "",
            "current_season_id": season_id,
            "season_name": current.get("name") or "",
            "xg_match_count": xg_count,
        })

    result = {"items": items}
    _analysis_cache_set("analysis:leagues", result)
    return result


@router.get("/analysis/unjust-table/{league_id}")
async def analysis_unjust_table(
    league_id: int,
    season_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Compute unjust table for a league/season using the justice service."""
    try:
        return await _JUSTICE_SERVICE.get_unjust_table(league_id=int(league_id), season_id=season_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
