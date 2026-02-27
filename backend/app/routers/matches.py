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
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger("quotico.matches")

import app.database as _db
from app.models.match import LiveScoreResponse, MatchResponse, db_to_response
from app.services.match_service import get_matches, get_match_by_id
from app.services.odds_meta_service import build_legacy_like_odds
from app.services.auth_service import get_admin_user

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
        league_id=sport, status=status_filter, limit=limit
    )
    return [db_to_response(m) for m in matches]


@router.get("/live-scores", response_model=list[LiveScoreResponse])
async def live_scores(
    sport: Optional[str] = Query(None, description="Filter by sport key"),
):
    """Get live scores — stub pending Sportmonks livescore integration."""
    logger.debug("Live scores endpoint called — legacy providers removed, returning empty")
    return []


# FIXME: ODDS_V3_BREAK — reads odds_events collection which is no longer populated by connector
@router.get("/{match_id}/odds-timeline")
async def match_odds_timeline(match_id: str):
    """Odds snapshots for a single match, sorted chronologically from odds_events."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = int(match_id)
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
    # FIXME: ODDS_V3_BREAK — returns odds_meta no longer produced by connector
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
    """Provider health — Sportmonks only."""
    return {
        "provider": "sportmonks",
        "status": "ok",
    }
