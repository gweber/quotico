from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

import app.database as _db
from app.models.match import LiveScoreResponse, MatchResponse
from app.services.match_service import get_matches, get_match_by_id
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
    return [
        MatchResponse(
            id=str(m["_id"]),
            sport_key=m["sport_key"],
            teams=m["teams"],
            commence_time=m["commence_time"],
            status=m["status"],
            current_odds=m["current_odds"],
            odds_updated_at=m["odds_updated_at"],
            result=m.get("result"),
            home_score=m.get("home_score"),
            away_score=m.get("away_score"),
        )
        for m in matches
    ]


@router.get("/live-scores", response_model=list[LiveScoreResponse])
async def live_scores(
    sport: Optional[str] = Query(None, description="Filter by sport key"),
):
    """Get live scores from all free providers.

    - Bundesliga: OpenLigaDB (priority) + football-data.org fallback
    - Other soccer: football-data.org
    - NFL/NBA: ESPN
    """
    from app.providers.odds_api import SUPPORTED_SPORTS

    sport_keys = [sport] if sport else list(SUPPORTED_SPORTS)
    results: list[LiveScoreResponse] = []
    seen_match_ids: set[str] = set()

    for sport_key in sport_keys:
        live_data: list[dict] = []

        # Route to the right provider
        if sport_key in SPORT_TO_LEAGUE:
            # Bundesliga: prefer OpenLigaDB
            live_data = await openligadb_provider.get_live_scores(sport_key)
            if not live_data:
                live_data = await football_data_provider.get_live_scores(sport_key)
        elif sport_key in SPORT_TO_COMPETITION:
            live_data = await football_data_provider.get_live_scores(sport_key)
        elif sport_key in SPORT_TO_ESPN:
            live_data = await espn_provider.get_live_scores(sport_key)

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
            match_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    candidates = await _db.db.matches.find({
        "sport_key": sport_key,
        "commence_time": {
            "$gte": match_time - timedelta(hours=6),
            "$lte": match_time + timedelta(hours=6),
        },
    }).to_list(length=50)

    for candidate in candidates:
        home = candidate.get("teams", {}).get("home", "")
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


@router.get("/{match_id}", response_model=MatchResponse)
async def get_match(match_id: str):
    """Get a single match by ID."""
    match = await get_match_by_id(match_id)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spiel nicht gefunden.",
        )
    return MatchResponse(
        id=str(match["_id"]),
        sport_key=match["sport_key"],
        teams=match["teams"],
        commence_time=match["commence_time"],
        status=match["status"],
        current_odds=match["current_odds"],
        odds_updated_at=match["odds_updated_at"],
        result=match.get("result"),
        home_score=match.get("home_score"),
        away_score=match.get("away_score"),
    )


@router.get("/status/provider")
async def provider_status():
    """Check odds provider health (circuit breaker state, API usage)."""
    return {
        "circuit_open": odds_provider.circuit_open,
        "api_usage": odds_provider.api_usage,
    }
