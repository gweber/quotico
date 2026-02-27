"""
backend/app/routers/admin_persons.py

Purpose:
    Admin Referee Tower API for Sportmonks-native person IDs and matches_v3 data.
    Provides referee list metrics, strictness DNA profiling, and recent match feed.

Dependencies:
    - app.database
    - app.services.auth_service
    - app.utils
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

import app.database as _db
from app.services.auth_service import get_admin_user
from app.services.referee_dna_service import strictness_band
from app.utils import ensure_utc, utcnow

router = APIRouter(prefix="/api/admin", tags=["admin-referees"])


def _count_cards(events: list[dict[str, Any]]) -> tuple[int, int]:
    yellow = 0
    red = 0
    for event in events:
        if str(event.get("type") or "") != "card":
            continue
        detail = str(event.get("detail") or "").lower()
        if detail in {"yellow", "yellow_red"}:
            yellow += 1
        if detail in {"red", "yellow_red"}:
            red += 1
    return yellow, red


def _penalty_occurred(doc: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    penalty_info = doc.get("penalty_info") if isinstance(doc.get("penalty_info"), dict) else {}
    if bool(penalty_info.get("occurred")):
        return True
    for event in events:
        event_type = str(event.get("type") or "")
        detail = str(event.get("detail") or "").lower()
        if event_type == "missed_penalty":
            return True
        if event_type == "goal" and detail == "penalty":
            return True
    return False


def _discipline_points(yellow: int, red: int, penalty: bool) -> float:
    return float(yellow) + (2.0 * float(red)) + (0.5 if penalty else 0.0)


def _justice_labels(doc: dict[str, Any]) -> dict[str, str]:
    teams = doc.get("teams") if isinstance(doc.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    scores = doc.get("scores") if isinstance(doc.get("scores"), dict) else {}
    full_time = scores.get("full_time") if isinstance(scores.get("full_time"), dict) else {}
    hs = full_time.get("home")
    aw = full_time.get("away")
    hxg = home.get("xg")
    axg = away.get("xg")
    if not all(isinstance(v, (int, float)) for v in (hs, aw, hxg, axg)):
        return {"home": "none", "away": "none"}

    xg_gap_home = float(hxg) - float(axg)
    home_result = "none"
    away_result = "none"
    if xg_gap_home > 0.5 and float(hs) <= float(aw):
        home_result = "unlucky"
    elif xg_gap_home < -0.5 and float(hs) > float(aw):
        home_result = "overperformed"
    if xg_gap_home < -0.5 and float(aw) <= float(hs):
        away_result = "unlucky"
    elif xg_gap_home > 0.5 and float(aw) > float(hs):
        away_result = "overperformed"
    return {"home": home_result, "away": away_result}


async def _league_name_map() -> dict[int, str]:
    rows = await _db.db.league_registry_v3.find({}, {"_id": 1, "name": 1}).to_list(length=1000)
    out: dict[int, str] = {}
    for row in rows:
        lid = row.get("_id")
        if not isinstance(lid, int):
            continue
        out[int(lid)] = str(row.get("name") or "")
    return out


@router.get("/referees")
async def list_referees(
    search: str | None = Query(None),
    league_id: int | None = Query(None),
    strictness: str | None = Query(None, pattern="^(loose|normal|extreme_strict)$"),
    since_days: int = Query(730, ge=30, le=3650),
    limit: int = Query(250, ge=1, le=1000),
    admin=Depends(get_admin_user),
):
    _ = admin
    now = utcnow()
    query: dict[str, Any] = {
        "status": "FINISHED",
        "referee_id": {"$type": "int"},
        "start_at": {"$gte": now - timedelta(days=int(since_days))},
    }
    if league_id is not None:
        query["league_id"] = int(league_id)

    docs = await _db.db.matches_v3.find(
        query,
        {
            "_id": 1,
            "league_id": 1,
            "referee_id": 1,
            "referee_name": 1,
            "events": 1,
            "penalty_info": 1,
            "start_at": 1,
        },
    ).to_list(length=200_000)

    league_map = await _league_name_map()
    grouped: dict[int, dict[str, Any]] = {}
    global_points = 0.0
    global_matches = 0

    for doc in docs:
        rid = doc.get("referee_id")
        if not isinstance(rid, int):
            continue
        events = doc.get("events") if isinstance(doc.get("events"), list) else []
        yellow, red = _count_cards([row for row in events if isinstance(row, dict)])
        penalty = _penalty_occurred(doc, [row for row in events if isinstance(row, dict)])
        points = _discipline_points(yellow, red, penalty)
        global_points += points
        global_matches += 1

        bucket = grouped.setdefault(
            int(rid),
            {
                "referee_id": int(rid),
                "referee_name": str(doc.get("referee_name") or ""),
                "matches_officiated": 0,
                "yellow_total": 0,
                "red_total": 0,
                "penalty_matches": 0,
                "discipline_points_total": 0.0,
                "league_ids": set(),
                "last_seen": None,
            },
        )
        bucket["matches_officiated"] += 1
        bucket["yellow_total"] += int(yellow)
        bucket["red_total"] += int(red)
        bucket["penalty_matches"] += 1 if penalty else 0
        bucket["discipline_points_total"] += float(points)
        lid = doc.get("league_id")
        if isinstance(lid, int):
            bucket["league_ids"].add(int(lid))
        started = ensure_utc(doc.get("start_at")) if doc.get("start_at") else None
        if started and (bucket["last_seen"] is None or started > bucket["last_seen"]):
            bucket["last_seen"] = started

    baseline_points = (global_points / float(global_matches)) if global_matches > 0 else 0.0
    q = str(search or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for value in grouped.values():
        matches_count = int(value["matches_officiated"])
        if matches_count <= 0:
            continue
        avg_yellow = float(value["yellow_total"]) / float(matches_count)
        avg_red = float(value["red_total"]) / float(matches_count)
        penalty_pct = (float(value["penalty_matches"]) / float(matches_count)) * 100.0
        avg_points = float(value["discipline_points_total"]) / float(matches_count)
        strictness_index = (avg_points / baseline_points * 100.0) if baseline_points > 0 else 100.0
        band = strictness_band(strictness_index)
        name = str(value.get("referee_name") or "")
        if q and q not in name.lower() and q not in str(value["referee_id"]):
            continue
        if strictness and band != strictness:
            continue
        league_ids = sorted([int(x) for x in value["league_ids"]])
        rows.append(
            {
                "referee_id": int(value["referee_id"]),
                "referee_name": name,
                "matches_officiated": matches_count,
                "avg_yellow": round(avg_yellow, 3),
                "avg_red": round(avg_red, 3),
                "penalty_pct": round(penalty_pct, 3),
                "strictness_points_per_match": round(avg_points, 3),
                "strictness_index": round(strictness_index, 2),
                "strictness_band": band,
                "league_ids": league_ids,
                "league_names": [league_map.get(lid, str(lid)) for lid in league_ids],
                "last_seen_at": value["last_seen"].isoformat() if value["last_seen"] else None,
            }
        )

    rows.sort(
        key=lambda row: (
            float(row.get("strictness_index") or 0.0),
            int(row.get("matches_officiated") or 0),
        ),
        reverse=True,
    )
    items = rows[: int(limit)]
    league_options = [
        {"league_id": int(lid), "league_name": str(name)}
        for lid, name in sorted(league_map.items(), key=lambda kv: kv[1].lower())
    ]
    return {
        "items": items,
        "count": len(items),
        "baseline_points_per_match": round(float(baseline_points), 4),
        "league_options": league_options,
        "generated_at": now.isoformat(),
    }


@router.get("/referees/{referee_id}/dna")
async def referee_dna(
    referee_id: int,
    since_days: int = Query(730, ge=30, le=3650),
    admin=Depends(get_admin_user),
):
    _ = admin
    now = utcnow()
    base_query: dict[str, Any] = {
        "status": "FINISHED",
        "start_at": {"$gte": now - timedelta(days=int(since_days))},
    }
    ref_query = {**base_query, "referee_id": int(referee_id)}
    ref_docs = await _db.db.matches_v3.find(
        ref_query,
        {
            "_id": 1,
            "league_id": 1,
            "referee_id": 1,
            "referee_name": 1,
            "events": 1,
            "penalty_info": 1,
            "start_at": 1,
        },
    ).to_list(length=50_000)
    if not ref_docs:
        raise HTTPException(status_code=404, detail="Referee not found.")

    league_ids = sorted({int(doc.get("league_id")) for doc in ref_docs if isinstance(doc.get("league_id"), int)})
    league_map = await _league_name_map()
    league_docs = await _db.db.matches_v3.find(
        {**base_query, "league_id": {"$in": league_ids}},
        {"events": 1, "penalty_info": 1},
    ).to_list(length=250_000)

    def _aggregate_points(docs: list[dict[str, Any]]) -> tuple[int, int, int, int, float]:
        matches = 0
        yellow_sum = 0
        red_sum = 0
        penalty_matches = 0
        points_sum = 0.0
        for row in docs:
            events = row.get("events") if isinstance(row.get("events"), list) else []
            clean_events = [e for e in events if isinstance(e, dict)]
            yellow, red = _count_cards(clean_events)
            penalty = _penalty_occurred(row, clean_events)
            matches += 1
            yellow_sum += int(yellow)
            red_sum += int(red)
            penalty_matches += 1 if penalty else 0
            points_sum += _discipline_points(yellow, red, penalty)
        return matches, yellow_sum, red_sum, penalty_matches, points_sum

    ref_matches, ref_yellow, ref_red, ref_penalties, ref_points = _aggregate_points(ref_docs)
    lg_matches, lg_yellow, lg_red, lg_penalties, lg_points = _aggregate_points(league_docs)
    if ref_matches <= 0 or lg_matches <= 0:
        raise HTTPException(status_code=404, detail="Referee DNA basis not available.")

    ref_points_avg = ref_points / float(ref_matches)
    lg_points_avg = lg_points / float(lg_matches)
    strictness_index = (ref_points_avg / lg_points_avg * 100.0) if lg_points_avg > 0 else 100.0
    strictness_bucket = strictness_band(strictness_index)

    name = str(ref_docs[0].get("referee_name") or "")
    return {
        "referee_id": int(referee_id),
        "referee_name": name,
        "sample_size": int(ref_matches),
        "league_sample_size": int(lg_matches),
        "league_ids": league_ids,
        "league_names": [league_map.get(lid, str(lid)) for lid in league_ids],
        "strictness_index": round(strictness_index, 2),
        "strictness_band": strictness_bucket,
        "referee_avg": {
            "yellow": round(ref_yellow / float(ref_matches), 3),
            "red": round(ref_red / float(ref_matches), 3),
            "penalty_pct": round((ref_penalties / float(ref_matches)) * 100.0, 3),
            "discipline_points": round(ref_points_avg, 4),
        },
        "league_avg": {
            "yellow": round(lg_yellow / float(lg_matches), 3),
            "red": round(lg_red / float(lg_matches), 3),
            "penalty_pct": round((lg_penalties / float(lg_matches)) * 100.0, 3),
            "discipline_points": round(lg_points_avg, 4),
        },
        "generated_at": now.isoformat(),
    }


@router.get("/referees/{referee_id}/matches")
async def referee_matches(
    referee_id: int,
    limit: int = Query(25, ge=1, le=200),
    admin=Depends(get_admin_user),
):
    _ = admin
    docs = await _db.db.matches_v3.find(
        {"referee_id": int(referee_id)},
        {
            "_id": 1,
            "league_id": 1,
            "season_id": 1,
            "round_id": 1,
            "referee_id": 1,
            "referee_name": 1,
            "teams": 1,
            "scores.full_time": 1,
            "events": 1,
            "penalty_info": 1,
            "start_at": 1,
            "status": 1,
        },
    ).sort("start_at", -1).limit(int(limit)).to_list(length=int(limit))
    if not docs:
        raise HTTPException(status_code=404, detail="Referee not found.")

    league_map = await _league_name_map()
    items: list[dict[str, Any]] = []
    for row in docs:
        events = row.get("events") if isinstance(row.get("events"), list) else []
        clean_events = [event for event in events if isinstance(event, dict)]
        yellow, red = _count_cards(clean_events)
        penalty = _penalty_occurred(row, clean_events)
        justice = _justice_labels(row)
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        full_time = ((row.get("scores") or {}).get("full_time") or {}) if isinstance(row.get("scores"), dict) else {}
        start_at = ensure_utc(row.get("start_at")) if row.get("start_at") else None
        lid = row.get("league_id") if isinstance(row.get("league_id"), int) else 0
        items.append(
            {
                "match_id": int(row.get("_id")),
                "league_id": int(lid),
                "league_name": league_map.get(int(lid), ""),
                "season_id": row.get("season_id"),
                "round_id": row.get("round_id"),
                "start_at": start_at.isoformat() if start_at else None,
                "status": str(row.get("status") or ""),
                "home_team": str(home.get("name") or ""),
                "away_team": str(away.get("name") or ""),
                "home_xg": home.get("xg"),
                "away_xg": away.get("xg"),
                "home_score": full_time.get("home"),
                "away_score": full_time.get("away"),
                "yellow_cards": int(yellow),
                "red_cards": int(red),
                "penalty_occurred": bool(penalty),
                "discipline_points": round(_discipline_points(yellow, red, penalty), 3),
                "justice": justice,
            }
        )

    name = str(docs[0].get("referee_name") or "")
    return {
        "referee_id": int(referee_id),
        "referee_name": name,
        "items": items,
        "count": len(items),
    }
