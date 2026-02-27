"""
backend/app/routers/historical.py

Purpose:
    Historical read endpoints for H2H and form data using Sportmonks sm_id
    team identity (matches_v3).  Legacy scraper import endpoints kept for
    backward compatibility with tools/scrapper.py.

Dependencies:
    - app.database
    - app.services.historical_service
    - app.services.team_registry_service  (import endpoints only)
"""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.services.historical_service import (
    build_h2h,
    build_form,
    build_match_context,
    clear_context_cache,
)
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.utils import utcnow

logger = logging.getLogger("quotico.historical")
router = APIRouter(prefix="/api/historical", tags=["historical"])


def _normalize_match_date(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# API key auth for scraper tool (no login needed)
# ---------------------------------------------------------------------------

async def verify_import_key(x_import_key: str = Header(...)):
    """Verify the import API key sent by the local scraper."""
    if not settings.IMPORT_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Import API key not configured on server.",
        )
    if not secrets.compare_digest(x_import_key, settings.IMPORT_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid import API key.",
        )


# ---------------------------------------------------------------------------
# Request / Response models (import)
# ---------------------------------------------------------------------------

class OddsEntry(BaseModel):
    home: float
    draw: float
    away: float


class OverUnderEntry(BaseModel):
    over: float
    under: float
    line: float = 2.5


class MatchStats(BaseModel):
    shots_home: int | None = None
    shots_away: int | None = None
    shots_on_target_home: int | None = None
    shots_on_target_away: int | None = None
    corners_home: int | None = None
    corners_away: int | None = None
    fouls_home: int | None = None
    fouls_away: int | None = None
    yellow_cards_home: int | None = None
    yellow_cards_away: int | None = None
    red_cards_home: int | None = None
    red_cards_away: int | None = None


class HistoricalMatch(BaseModel):
    league_id: int
    league_code: str
    season: str
    season_label: str
    match_date: datetime
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    result: str | None = None
    ht_home_goals: int | None = None
    ht_away_goals: int | None = None
    ht_result: str | None = None
    stats: MatchStats | None = None
    odds: dict[str, OddsEntry] | None = None
    over_under_odds: dict[str, OverUnderEntry] | None = None
    referee: str | None = None


class ImportBatch(BaseModel):
    matches: list[HistoricalMatch] = Field(..., max_length=500)


class ImportResult(BaseModel):
    received: int
    upserted: int
    modified: int


class TeamAlias(BaseModel):
    league_id: int
    team_name: str


class AliasBatch(BaseModel):
    aliases: list[TeamAlias] = Field(..., max_length=2000)


# ---------------------------------------------------------------------------
# Import endpoints (API key auth, called by tools/scrapper.py from home)
# ---------------------------------------------------------------------------

@router.post("/import", response_model=ImportResult)
async def import_matches(batch: ImportBatch, _=Depends(verify_import_key)):
    """Bulk upsert historical matches. Called by the local scraper tool."""
    from pymongo import UpdateOne

    now = utcnow()
    ops = []
    registry = TeamRegistry.get()

    for m in batch.matches:
        doc = m.model_dump(exclude_none=True)
        if not isinstance(doc.get("league_id"), int):
            logger.warning("Historical import rejected non-int league_id: value=%r", doc.get("league_id"))
            raise HTTPException(status_code=400, detail="league_id must be int.")

        home_goals = doc.pop("home_goals", 0)
        away_goals = doc.pop("away_goals", 0)
        outcome = doc.pop("result", None)
        if outcome is None:
            if home_goals > away_goals:
                outcome = "1"
            elif home_goals < away_goals:
                outcome = "2"
            else:
                outcome = "X"
        doc["result"] = {
            "home_score": home_goals,
            "away_score": away_goals,
            "outcome": outcome,
        }

        if "ht_home_goals" in doc or "ht_away_goals" in doc:
            doc["result"]["ht_home_score"] = doc.pop("ht_home_goals", None)
            doc["result"]["ht_away_score"] = doc.pop("ht_away_goals", None)
            doc["result"]["ht_outcome"] = doc.pop("ht_result", None)
        else:
            doc.pop("ht_result", None)

        doc["status"] = "final"
        doc["source"] = "scraper"

        if doc.get("stats"):
            doc["stats"] = {k: v for k, v in doc["stats"].items() if v is not None}
        if doc.get("odds"):
            doc["odds"] = {bk: dict(v) for bk, v in doc["odds"].items()}
        if doc.get("over_under_odds"):
            doc["over_under_odds"] = {bk: dict(v) for bk, v in doc["over_under_odds"].items()}

        doc["updated_at"] = now
        doc["match_date_hour"] = _normalize_match_date(doc["start_at"])

        home_team_id = await registry.resolve(doc["home_team"], doc["league_id"])
        away_team_id = await registry.resolve(doc["away_team"], doc["league_id"])
        doc["home_team_id"] = home_team_id
        doc["away_team_id"] = away_team_id

        ops.append(UpdateOne(
            {
                "league_id": doc["league_id"],
                "season": doc["season"],
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "match_date_hour": doc["match_date_hour"],
            },
            {
                "$set": doc,
                "$setOnInsert": {"imported_at": now},
            },
            upsert=True,
        ))

    if not ops:
        return ImportResult(received=0, upserted=0, modified=0)

    result = await _db.db.matches_v3.bulk_write(ops, ordered=False)
    logger.info(
        "Historical import: %d received, %d upserted, %d modified",
        len(batch.matches), result.upserted_count, result.modified_count,
    )

    clear_context_cache()

    return ImportResult(
        received=len(batch.matches),
        upserted=result.upserted_count,
        modified=result.modified_count,
    )


@router.post("/aliases", response_model=dict)
async def import_aliases(batch: AliasBatch, _=Depends(verify_import_key)):
    """Bulk upsert team name variants into team_mappings."""
    from pymongo import UpdateOne

    now = utcnow()
    ops = []

    for alias in batch.aliases:
        normalized_name = normalize_team_name(alias.team_name)
        if not normalized_name:
            continue
        ops.append(UpdateOne(
            {"normalized_name": normalized_name, "league_id": alias.league_id},
            {
                "$addToSet": {
                    "aliases": {"name": alias.team_name, "league_id": alias.league_id},
                },
                "$setOnInsert": {
                    "normalized_name": normalized_name,
                    "league_id": alias.league_id,
                    "display_name": alias.team_name,
                    "needs_review": False,
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        ))

    if not ops:
        return {"received": 0, "upserted": 0}

    result = await _db.db.teams.bulk_write(ops, ordered=False)
    return {
        "received": len(batch.aliases),
        "upserted": result.upserted_count,
        "modified": result.modified_count,
    }


# ---------------------------------------------------------------------------
# v3 read endpoints — all queries use Sportmonks sm_id (int)
# ---------------------------------------------------------------------------

@router.get("/h2h")
async def head_to_head(
    home_sm_id: int = Query(..., description="Sportmonks team ID (home)"),
    away_sm_id: int = Query(..., description="Sportmonks team ID (away)"),
    limit: int = Query(10, ge=1, le=50),
    skip: int = Query(0, ge=0, le=200),
):
    """Paginated H2H history between two teams (either direction)."""
    result = await build_h2h(home_sm_id, away_sm_id, limit=limit, skip=skip)
    if result is None:
        return {"matches": [], "summary": None}
    return result


@router.get("/team-form")
async def team_form(
    team_sm_id: int = Query(..., description="Sportmonks team ID"),
    limit: int = Query(5, ge=1, le=20),
):
    """Recent form for a team (last N finished matches)."""
    matches = await build_form(team_sm_id, limit=limit)
    return {"matches": matches}


@router.get("/match-context")
async def match_context(
    home_sm_id: int = Query(..., description="Sportmonks team ID (home)"),
    away_sm_id: int = Query(..., description="Sportmonks team ID (away)"),
    h2h_limit: int = Query(10, ge=1, le=20),
    form_limit: int = Query(5, ge=1, le=20),
):
    """Combined H2H + form for a single fixture."""
    return await build_match_context(
        home_sm_id, away_sm_id,
        h2h_limit=h2h_limit,
        form_limit=form_limit,
    )


class BulkFixtureV3(BaseModel):
    home_sm_id: int
    away_sm_id: int


class BulkContextRequestV3(BaseModel):
    fixtures: list[BulkFixtureV3] = Field(..., max_length=50)
    h2h_limit: int = Field(10, ge=1, le=20)
    form_limit: int = Field(5, ge=1, le=20)


@router.post("/match-context-bulk")
async def match_context_bulk(req: BulkContextRequestV3):
    """Combined H2H + form for multiple fixtures in a single request."""
    import asyncio

    results = await asyncio.gather(*(
        build_match_context(
            f.home_sm_id, f.away_sm_id,
            h2h_limit=req.h2h_limit,
            form_limit=req.form_limit,
        )
        for f in req.fixtures
    ))

    return {
        "results": [
            {**ctx, "home_sm_id": f.home_sm_id, "away_sm_id": f.away_sm_id}
            for f, ctx in zip(req.fixtures, results)
        ]
    }


@router.get("/stats")
async def collection_stats(admin=Depends(get_admin_user)):
    """Admin: overview of historical data (v3)."""
    pipeline = [
        {"$match": {"status": "FINISHED"}},
        {"$group": {
            "_id": {"league_id": "$league_id", "season_id": "$season_id"},
            "count": {"$sum": 1},
            "with_xg": {"$sum": {"$cond": [{"$gt": ["$teams.home.xg", None]}, 1, 0]}},
        }},
        {"$sort": {"_id.league_id": 1, "_id.season_id": 1}},
    ]
    results = await _db.db.matches_v3.aggregate(pipeline).to_list(length=500)
    total = await _db.db.matches_v3.count_documents({"status": "FINISHED"})

    return {
        "total_matches": total,
        "by_league_season": [
            {
                "league_id": r["_id"]["league_id"],
                "season_id": r["_id"]["season_id"],
                "matches": r["count"],
                "with_xg": r["with_xg"],
            }
            for r in results
        ],
    }
