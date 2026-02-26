"""
backend/app/services/event_handlers/websocket_handlers.py

Purpose:
    qbus subscribers that fan out selected match/odds events to authenticated
    WebSocket clients through the realtime WebSocket manager.

Dependencies:
    - app.database
    - app.services.event_models
    - app.services.websocket_manager
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from bson import ObjectId

import app.database as _db
from app.config import settings
from app.services.event_models import BaseEvent
from app.services.websocket_manager import websocket_manager
from app.utils import ensure_utc

logger = logging.getLogger("quotico.event_handlers.websocket")


def _to_object_ids(raw_ids: list[str]) -> list[ObjectId]:
    out: list[ObjectId] = []
    for item in raw_ids:
        try:
            out.append(ObjectId(str(item)))
        except Exception:
            continue
    return out


def _extract_league_codes(league_doc: dict[str, Any] | None) -> set[str]:
    if not isinstance(league_doc, dict):
        return set()
    external_ids = league_doc.get("external_ids")
    if not isinstance(external_ids, dict):
        return set()
    out: set[str] = set()
    for value in external_ids.values():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out.add(text)
    return out


async def _load_leagues_by_id(league_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not league_ids:
        return {}
    object_ids = _to_object_ids(sorted(league_ids))
    if not object_ids:
        return {}
    rows = await _db.db.leagues.find(
        {"_id": {"$in": object_ids}},
        {"external_ids": 1},
    ).to_list(length=len(object_ids))
    return {str(row["_id"]): row for row in rows}


async def handle_match_updated_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    match_id = str(getattr(event, "match_id", "") or "")
    if not match_id:
        return
    try:
        match_oid = ObjectId(match_id)
    except Exception:
        return

    match_doc = await _db.db.matches.find_one(
        {"_id": match_oid},
        {
            "_id": 1,
            "league_id": 1,
            "sport_key": 1,
            "status": 1,
            "score": 1,
            "match_date": 1,
            "home_team": 1,
            "away_team": 1,
            "updated_at": 1,
        },
    )
    if not match_doc:
        return

    league_id = str(match_doc.get("league_id") or "")
    league_doc = await _db.db.leagues.find_one({"_id": match_doc.get("league_id")}, {"external_ids": 1}) if match_doc.get("league_id") else None
    league_codes = sorted(_extract_league_codes(league_doc))

    await websocket_manager.broadcast(
        event_type="match.updated",
        data={
            "match_id": str(match_doc["_id"]),
            "league_id": league_id or None,
            "league_codes": league_codes,
            "sport_key": str(match_doc.get("sport_key") or ""),
            "status": str(match_doc.get("status") or ""),
            "score": match_doc.get("score") or {},
            "home_team": str(match_doc.get("home_team") or ""),
            "away_team": str(match_doc.get("away_team") or ""),
            "match_date": ensure_utc(match_doc.get("match_date")).isoformat() if match_doc.get("match_date") else None,
            "updated_at": ensure_utc(match_doc.get("updated_at")).isoformat() if match_doc.get("updated_at") else None,
            "changed_fields": list(getattr(event, "changed_fields", []) or []),
        },
        selectors={
            "match_ids": [str(match_doc["_id"])],
            "league_ids": [league_id] if league_id else [],
            "league_codes": league_codes,
            "sport_keys": [str(match_doc.get("sport_key") or "")],
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )


async def handle_match_finalized_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    match_id = str(getattr(event, "match_id", "") or "")
    if not match_id:
        return
    try:
        match_oid = ObjectId(match_id)
    except Exception:
        return

    match_doc = await _db.db.matches.find_one(
        {"_id": match_oid},
        {
            "_id": 1,
            "league_id": 1,
            "sport_key": 1,
            "status": 1,
            "score": 1,
            "result": 1,
            "updated_at": 1,
        },
    )
    if not match_doc:
        return
    league_id = str(match_doc.get("league_id") or "")
    league_doc = await _db.db.leagues.find_one({"_id": match_doc.get("league_id")}, {"external_ids": 1}) if match_doc.get("league_id") else None
    league_codes = sorted(_extract_league_codes(league_doc))

    await websocket_manager.broadcast(
        event_type="match.finalized",
        data={
            "match_id": str(match_doc["_id"]),
            "league_id": league_id or None,
            "league_codes": league_codes,
            "sport_key": str(match_doc.get("sport_key") or ""),
            "status": str(match_doc.get("status") or "final"),
            "score": match_doc.get("score") or {},
            "result": match_doc.get("result") or {},
            "final_score": dict(getattr(event, "final_score", {}) or {}),
            "updated_at": ensure_utc(match_doc.get("updated_at")).isoformat() if match_doc.get("updated_at") else None,
        },
        selectors={
            "match_ids": [str(match_doc["_id"])],
            "league_ids": [league_id] if league_id else [],
            "league_codes": league_codes,
            "sport_keys": [str(match_doc.get("sport_key") or "")],
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )


async def handle_odds_ingested_ws(event: BaseEvent) -> None:
    if not settings.WS_EVENTS_ENABLED:
        return
    # Single-batch fanout is mandatory to avoid one broadcast per match.
    match_ids = sorted({str(item).strip() for item in (getattr(event, "match_ids", []) or []) if str(item).strip()})
    if not match_ids:
        return
    object_ids = _to_object_ids(match_ids)
    if not object_ids:
        return

    matches = await _db.db.matches.find(
        {"_id": {"$in": object_ids}},
        {
            "_id": 1,
            "league_id": 1,
            "sport_key": 1,
            "home_team": 1,
            "away_team": 1,
            "odds_meta": 1,
            "updated_at": 1,
        },
    ).to_list(length=len(object_ids))
    if not matches:
        return

    league_ids = {str(doc.get("league_id")) for doc in matches if doc.get("league_id")}
    leagues_by_id = await _load_leagues_by_id(league_ids)
    league_codes: set[str] = set()
    sport_keys: set[str] = set()
    payload_matches: list[dict[str, Any]] = []
    for doc in matches:
        league_id = str(doc.get("league_id") or "")
        league_codes.update(_extract_league_codes(leagues_by_id.get(league_id)))
        sport_key = str(doc.get("sport_key") or "")
        if sport_key:
            sport_keys.add(sport_key)
        odds_meta = doc.get("odds_meta") if isinstance(doc.get("odds_meta"), dict) else {}
        payload_matches.append(
            {
                "match_id": str(doc["_id"]),
                "league_id": league_id or None,
                "sport_key": sport_key,
                "home_team": str(doc.get("home_team") or ""),
                "away_team": str(doc.get("away_team") or ""),
                "odds_meta": odds_meta,
                "odds_updated_at": (
                    ensure_utc(odds_meta.get("updated_at")).isoformat()
                    if isinstance(odds_meta.get("updated_at"), datetime)
                    else None
                ),
                "updated_at": ensure_utc(doc.get("updated_at")).isoformat() if doc.get("updated_at") else None,
            }
        )

    await websocket_manager.broadcast(
        event_type="odds.ingested",
        data={
            "provider": str(getattr(event, "provider", "") or ""),
            "inserted": int(getattr(event, "inserted", 0) or 0),
            "deduplicated": int(getattr(event, "deduplicated", 0) or 0),
            "markets_updated": int(getattr(event, "markets_updated", 0) or 0),
            "matches": payload_matches,
        },
        selectors={
            "match_ids": [str(doc["_id"]) for doc in matches],
            "league_ids": sorted(league_ids),
            "league_codes": sorted(league_codes),
            "sport_keys": sorted(sport_keys),
        },
        meta={
            "event_id": str(getattr(event, "event_id", "")),
            "correlation_id": str(getattr(event, "correlation_id", "")),
            "occurred_at": ensure_utc(getattr(event, "occurred_at")).isoformat(),
        },
    )
