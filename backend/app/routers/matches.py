"""
backend/app/routers/matches.py

Purpose:
    Match read API including live scores and odds timeline views backed by the
    greenfield odds architecture (`odds_events` + `matches.odds_meta`).

Dependencies:
    - app.services.match_service
    - app.models.match
    - app.database
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger("quotico.matches")

import app.database as _db
from app.models.match import LiveScoreResponse, MatchResponse, db_to_response
from app.services.match_service import get_matches, get_match_by_id
from app.services.odds_meta_service import build_legacy_like_odds
from app.services.auth_service import get_admin_user
from app.utils import parse_utc
from app.providers.odds_api import odds_provider
from app.providers.football_data import (
    SPORT_TO_COMPETITION,
    football_data_provider,
)
from app.providers.openligadb import openligadb_provider, SPORT_TO_LEAGUE
from app.utils.team_matching import teams_match

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
    """Odds snapshots for a single match, sorted chronologically from odds_events."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(match_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match id.") from None
    raw = await _db.db.odds_events.find(
        {"match_id": oid},
        {"_id": 0, "snapshot_at": 1, "provider": 1, "market": 1, "selection_key": 1, "price": 1, "line": 1},
    ).sort("snapshot_at", 1).to_list(length=5000)

    grouped: dict[datetime, dict] = {}
    for ev in raw:
        ts = ev["snapshot_at"]
        group = grouped.setdefault(
            ts,
            {"providers": defaultdict(lambda: {"odds": {}, "totals": {}, "spreads": {}})},
        )
        p = str(ev.get("provider") or "unknown")
        m = str(ev.get("market") or "")
        k = str(ev.get("selection_key") or "")
        v = ev.get("price")
        if not isinstance(v, (int, float)):
            continue
        line = ev.get("line")
        node = group["providers"][p]
        if m == "h2h":
            node["odds"][k] = float(v)
        elif m == "totals":
            node["totals"][k] = float(v)
            if isinstance(line, (int, float)):
                node["totals"]["line"] = float(line)
        elif m == "spreads":
            node["spreads"][k] = float(v)
            if isinstance(line, (int, float)):
                node["spreads"]["line"] = float(line)

    snapshots = []
    for ts in sorted(grouped.keys()):
        providers = grouped[ts]["providers"]
        odds_vals = [pv for pnode in providers.values() for pv in [pnode["odds"]] if pnode["odds"]]
        totals_vals = [pv for pnode in providers.values() for pv in [pnode["totals"]] if pnode["totals"]]

        avg_odds: dict[str, float] = {}
        for key in ("1", "X", "2"):
            vals = [o[key] for o in odds_vals if key in o]
            if vals:
                avg_odds[key] = round(sum(vals) / len(vals), 4)

        avg_totals: dict[str, float] = {}
        for key in ("over", "under", "line"):
            vals = [o[key] for o in totals_vals if key in o]
            if vals:
                avg_totals[key] = round(sum(vals) / len(vals), 4)

        snapshots.append(
            {
                "snapshot_at": ts.replace(microsecond=0, tzinfo=ts.tzinfo or timezone.utc).isoformat(),
                "odds": avg_odds,
                "totals": avg_totals,
                "providers": {
                    pname: pvals for pname, pvals in providers.items()
                },
            }
        )

    return {
        "match_id": match_id,
        "snapshots": snapshots,
        "snapshot_count": len(snapshots),
    }


@router.get("/{match_id}/odds")
async def get_match_odds(match_id: str):
    """Get current aggregated odds meta for one match."""
    match = await get_match_by_id(match_id)
    if not match:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
    return {"match_id": match_id, "odds_meta": match.get("odds_meta", {}), "odds": build_legacy_like_odds(match)}


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
