"""
backend/app/routers/historical.py

Purpose:
    Historical import and read endpoints for match context/statistics using
    Team Tower identities and canonical match references.

Dependencies:
    - app.database
    - app.services.historical_service
    - app.services.team_registry_service
"""

import logging
import secrets
from datetime import datetime
import re

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.services.historical_service import (
    build_match_context,
    clear_context_cache,
    sport_keys_for,
)
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.utils import utcnow

logger = logging.getLogger("quotico.historical")
router = APIRouter(prefix="/api/historical", tags=["historical"])


def _normalize_match_date(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _serialize_team_ids(rows: list[dict]) -> list[dict]:
    """Convert Team Tower ObjectIds to API-safe strings."""
    normalized: list[dict] = []
    for match in rows:
        row = dict(match)
        home_id = row.get("home_team_id")
        away_id = row.get("away_team_id")
        if isinstance(home_id, ObjectId):
            row["home_team_id"] = str(home_id)
        if isinstance(away_id, ObjectId):
            row["away_team_id"] = str(away_id)
        normalized.append(row)
    return normalized


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
# Request / Response models
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
    sport_key: str
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
    sport_key: str
    team_name: str


class AliasBatch(BaseModel):
    aliases: list[TeamAlias] = Field(..., max_length=2000)


# ---------------------------------------------------------------------------
# Import endpoints (API key auth, called by tools/scrapper.py from home)
# ---------------------------------------------------------------------------

@router.post("/import", response_model=ImportResult)
async def import_matches(batch: ImportBatch, _=Depends(verify_import_key)):
    """Bulk upsert historical matches. Called by the local scraper tool.

    Dedup uses normalized team keys (not raw names) so scraper records
    correctly overwrite auto-archived records from the match resolver
    even when team name spellings differ between providers.
    """
    from pymongo import UpdateOne

    now = utcnow()
    ops = []
    registry = TeamRegistry.get()

    for m in batch.matches:
        doc = m.model_dump(exclude_none=True)

        # --- Transform to unified matches schema ---
        # Convert home_goals/away_goals/result → result.{home_score, away_score, outcome}
        home_goals = doc.pop("home_goals", 0)
        away_goals = doc.pop("away_goals", 0)
        outcome = doc.pop("result", None)
        if outcome is None:
            # Derive outcome from score
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

        # Half-time results stored under result as well
        if "ht_home_goals" in doc or "ht_away_goals" in doc:
            doc["result"]["ht_home_score"] = doc.pop("ht_home_goals", None)
            doc["result"]["ht_away_score"] = doc.pop("ht_away_goals", None)
            doc["result"]["ht_outcome"] = doc.pop("ht_result", None)
        else:
            doc.pop("ht_result", None)

        # All imported historical matches are final
        doc["status"] = "final"
        doc["source"] = "scraper"

        # Flatten nested Pydantic models to dicts for MongoDB
        if doc.get("stats"):
            doc["stats"] = {k: v for k, v in doc["stats"].items() if v is not None}
        if doc.get("odds"):
            doc["odds"] = {bk: dict(v) for bk, v in doc["odds"].items()}
        if doc.get("over_under_odds"):
            doc["over_under_odds"] = {bk: dict(v) for bk, v in doc["over_under_odds"].items()}

        doc["updated_at"] = now

        # match_date_hour: floored to hour for compound unique index dedup
        doc["match_date_hour"] = _normalize_match_date(doc["match_date"])

        # Resolve persistent team identity for match uniqueness and references
        home_team_id = await registry.resolve(doc["home_team"], doc["sport_key"])
        away_team_id = await registry.resolve(doc["away_team"], doc["sport_key"])
        doc["home_team_id"] = home_team_id
        doc["away_team_id"] = away_team_id

        ops.append(UpdateOne(
            {
                "sport_key": doc["sport_key"],
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

    result = await _db.db.matches.bulk_write(ops, ordered=False)
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
    """Bulk upsert team name variants into team_mappings.

    For each alias, finds or creates a Team Tower document and adds the alias.
    """
    from pymongo import UpdateOne

    now = utcnow()
    ops = []

    for alias in batch.aliases:
        normalized_name = normalize_team_name(alias.team_name)
        if not normalized_name:
            continue
        ops.append(UpdateOne(
            {"normalized_name": normalized_name, "sport_key": alias.sport_key},
            {
                "$addToSet": {
                    "aliases": {"name": alias.team_name, "sport_key": alias.sport_key},
                },
                "$setOnInsert": {
                    "normalized_name": normalized_name,
                    "sport_key": alias.sport_key,
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
# Read endpoints (public)
# ---------------------------------------------------------------------------

@router.get("/h2h")
async def head_to_head(
    sport_key: str = Query(...),
    home_team_id: str = Query(...),
    away_team_id: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    skip: int = Query(0, ge=0, le=200),
):
    """Get head-to-head history between two teams (either direction)."""
    try:
        home_oid = ObjectId(home_team_id)
        away_oid = ObjectId(away_team_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid team id.") from None

    related_keys = sport_keys_for(sport_key)
    matches = await _db.db.matches.find(
        {
            "sport_key": {"$in": related_keys},
            "status": "final",
            "$or": [
                {"home_team_id": home_oid, "away_team_id": away_oid},
                {"home_team_id": away_oid, "away_team_id": home_oid},
            ],
        },
        {
            "_id": 0,
            "match_date": 1, "home_team": 1, "away_team": 1,
            "home_team_id": 1, "away_team_id": 1,
            "result.home_score": 1, "result.away_score": 1, "result.outcome": 1,
            "season_label": 1,
        },
    ).sort("match_date", -1).skip(skip).to_list(length=limit)

    normalized_matches = _serialize_team_ids(matches)

    return {"matches": normalized_matches, "count": len(normalized_matches)}


@router.get("/team-form")
async def team_form(
    sport_key: str = Query(...),
    team_id: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
):
    """Get recent form for a team (last N matches)."""
    try:
        team_oid = ObjectId(team_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid team id.") from None

    matches = await _db.db.matches.find(
        {
            "sport_key": sport_key,
            "status": "final",
            "$or": [
                {"home_team_id": team_oid},
                {"away_team_id": team_oid},
            ],
        },
        {
            "_id": 0,
            "match_date": 1, "home_team": 1, "away_team": 1,
            "result.home_score": 1, "result.away_score": 1, "result.outcome": 1,
            "season_label": 1,
        },
    ).sort("match_date", -1).to_list(length=limit)

    normalized_matches = _serialize_team_ids(matches)
    return {"matches": normalized_matches, "count": len(normalized_matches)}


@router.get("/match-context")
async def match_context(
    home_team: str = Query(..., description="Home team name (from provider)"),
    away_team: str = Query(..., description="Away team name (from provider)"),
    sport_key: str = Query(...),
    h2h_limit: int = Query(10, ge=1, le=20),
    form_limit: int = Query(10, ge=1, le=20),
):
    """Combined H2H + form for a single fixture."""
    return await build_match_context(home_team, away_team, sport_key, h2h_limit, form_limit)


class BulkFixture(BaseModel):
    home_team: str
    away_team: str
    sport_key: str


class BulkContextRequest(BaseModel):
    fixtures: list[BulkFixture] = Field(..., max_length=50)
    h2h_limit: int = Field(10, ge=1, le=20)
    form_limit: int = Field(10, ge=1, le=20)


@router.post("/match-context-bulk")
async def match_context_bulk(req: BulkContextRequest):
    """Combined H2H + form for multiple fixtures in a single request."""
    import asyncio

    results = await asyncio.gather(*(
        build_match_context(f.home_team, f.away_team, f.sport_key, req.h2h_limit, req.form_limit)
        for f in req.fixtures
    ))

    return {
        "results": [
            {"home_team": f.home_team, "away_team": f.away_team, "sport_key": f.sport_key, **ctx}
            for f, ctx in zip(req.fixtures, results)
        ]
    }


@router.get("/stats")
async def collection_stats(admin=Depends(get_admin_user)):
    """Admin: overview of historical data in the database."""
    final_filter = {"status": "final"}

    pipeline = [
        {"$match": final_filter},
        {"$group": {
            "_id": {"sport_key": "$sport_key", "season": "$season_label"},
            "count": {"$sum": 1},
            "with_odds": {"$sum": {"$cond": [{"$gt": ["$odds", None]}, 1, 0]}},
            "with_stats": {"$sum": {"$cond": [{"$gt": ["$stats", None]}, 1, 0]}},
        }},
        {"$sort": {"_id.sport_key": 1, "_id.season": 1}},
    ]

    results = await _db.db.matches.aggregate(pipeline).to_list(length=500)
    total = await _db.db.matches.count_documents(final_filter)
    aliases = await _db.db.teams.count_documents({})

    return {
        "total_matches": total,
        "total_aliases": aliases,
        "by_league_season": [
            {
                "sport_key": r["_id"]["sport_key"],
                "season": r["_id"]["season"],
                "matches": r["count"],
                "with_odds": r["with_odds"],
                "with_stats": r["with_stats"],
            }
            for r in results
        ],
    }
