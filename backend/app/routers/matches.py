import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger("quotico.matches")

import app.database as _db
from app.models.match import LiveScoreResponse, MatchResponse, db_to_response
from app.services.match_service import get_matches, get_match_by_id
from app.services.auth_service import get_admin_user
from app.utils import parse_utc
from app.providers.odds_api import odds_provider
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
    teams_match,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.providers.espn import espn_provider, SPORT_TO_ESPN

router = APIRouter(prefix="/api/matches", tags=["matches"])


@router.get("/", response_model=list[MatchResponse])
async def list_matches(
    sport: Optional[str] = Query(None, description="Filter by sport key"),
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by match status"
    ),
    limit: int = Query(50, ge=1, le=200),
):
    """Get matches with optional sport and status filters."""
    matches = await get_matches(
        sport_key=sport, status=status_filter, limit=limit
    )
    return [db_to_response(m) for m in matches]


@router.get("/live-scores", response_model=list[LiveScoreResponse])
async def live_scores(
    sport: Optional[str] = Query(None, description="Filter by sport key"),
):
    """Get live scores — only polls providers for sports with active matches.

    Uses DB as gatekeeper: if no matches have kicked off for a sport,
    zero external API calls are made.
    """
    from app.services.match_service import sports_with_live_action

    # Smart gate: only poll sports that actually have matches in progress
    active_sports = await sports_with_live_action()

    if sport:
        sport_keys = [sport] if sport in active_sports else []
    else:
        sport_keys = list(active_sports)

    results: list[LiveScoreResponse] = []
    seen_match_ids: set[str] = set()

    for sport_key in sport_keys:
        live_data: list[dict] = []

        try:
            if sport_key in SPORT_TO_LEAGUE:
                live_data = await openligadb_provider.get_live_scores(sport_key)
                if not live_data:
                    live_data = await football_data_provider.get_live_scores(sport_key)
            elif sport_key in SPORT_TO_COMPETITION:
                live_data = await football_data_provider.get_live_scores(sport_key)
            elif sport_key in SPORT_TO_ESPN:
                live_data = await espn_provider.get_live_scores(sport_key)
        except Exception:
            logger.warning("Live score provider failed for %s", sport_key, exc_info=True)
            continue

        if not live_data:
            continue

        for score in live_data:
            matched = await _match_live_score(sport_key, score)
            if matched and matched["match_id"] not in seen_match_ids:
                seen_match_ids.add(matched["match_id"])
                results.append(LiveScoreResponse(**matched))

    return results


async def _match_live_score(sport_key: str, score: dict) -> Optional[dict]:
    """Match a live score from any provider to our DB match."""
    utc_date = score.get("utc_date", "")
    if isinstance(utc_date, str) and utc_date:
        try:
            match_time = parse_utc(utc_date)
        except ValueError:
            return None
    else:
        return None

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "match_date": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=50)

    for candidate in candidates:
        home = candidate.get("home_team", "")
        if teams_match(home, score.get("home_team", "")):
            return {
                "match_id": str(candidate["_id"]),
                "home_score": score["home_score"],
                "away_score": score["away_score"],
                "minute": score.get("minute"),
                "half_time_home": score.get("half_time", {}).get("home"),
                "half_time_away": score.get("half_time", {}).get("away"),
            }

    return None


@router.get("/{match_id}/odds-timeline")
async def match_odds_timeline(match_id: str):
    """Odds snapshots for a single match, sorted chronologically."""
    raw = await _db.db.odds_snapshots.find(
        {"match_id": match_id},
        {"_id": 0, "snapshot_at": 1, "odds": 1, "totals": 1, "totals_odds": 1},
    ).sort("snapshot_at", 1).to_list(length=500)

    # Normalize: old docs have totals_odds, new docs have totals
    snapshots = []
    for s in raw:
        # Ensure snapshot_at is a clean ISO string (no microseconds — JS compat)
        snap_at = s.get("snapshot_at")
        if isinstance(snap_at, datetime):
            snap_at = snap_at.replace(microsecond=0, tzinfo=snap_at.tzinfo or timezone.utc).isoformat()
        entry: dict = {"snapshot_at": snap_at, "odds": s.get("odds", {})}
        totals = s.get("totals") or s.get("totals_odds")
        if totals:
            entry["totals"] = totals
        snapshots.append(entry)

    return {
        "match_id": match_id,
        "snapshots": snapshots,
        "snapshot_count": len(snapshots),
    }


@router.get("/{match_id}", response_model=MatchResponse)
async def get_match(match_id: str):
    """Get a single match by ID."""
    match = await get_match_by_id(match_id)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Match not found.",
        )
    return db_to_response(match)


@router.get("/status/provider")
async def provider_status(admin=Depends(get_admin_user)):
    """Check odds provider health (circuit breaker state, API usage). Admin only."""
    return {
        "circuit_open": odds_provider.circuit_open,
        "api_usage": await odds_provider.load_usage(),
    }
