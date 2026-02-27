"""
backend/app/services/referee_dna_service.py

Purpose:
    Shared referee DNA aggregation service for admin endpoints. Computes
    discipline averages, strictness indices, and season-vs-career trends from
    matches_v3 (Sportmonks-native integer referee IDs only).

Dependencies:
    - app.database
    - app.utils.ensure_utc
"""

from __future__ import annotations

from typing import Any

import app.database as _db


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


def discipline_points(yellow: int, red: int, penalty: bool) -> float:
    return float(yellow) + (2.0 * float(red)) + (0.5 if penalty else 0.0)


def strictness_band(index: float) -> str:
    if index < 90.0:
        return "loose"
    if index >= 125.0:
        return "extreme_strict"
    if index >= 110.0:
        return "strict"
    return "normal"


def strictness_trend(season_index: float, career_index: float) -> str:
    delta = float(season_index) - float(career_index)
    if delta >= 5.0:
        return "stricter"
    if delta <= -5.0:
        return "looser"
    return "flat"


def _aggregate_docs(
    docs: list[dict[str, Any]],
    *,
    referee_ids: set[int] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, float]]:
    per_ref: dict[int, dict[str, Any]] = {}
    total_matches = 0
    total_yellow = 0
    total_red = 0
    total_penalty = 0
    total_points = 0.0

    for doc in docs:
        rid = doc.get("referee_id")
        if not isinstance(rid, int):
            continue
        if referee_ids is not None and int(rid) not in referee_ids:
            continue
        events = doc.get("events") if isinstance(doc.get("events"), list) else []
        clean_events = [event for event in events if isinstance(event, dict)]
        yellow, red = _count_cards(clean_events)
        penalty = _penalty_occurred(doc, clean_events)
        points = discipline_points(yellow, red, penalty)

        total_matches += 1
        total_yellow += int(yellow)
        total_red += int(red)
        total_penalty += 1 if penalty else 0
        total_points += float(points)

        bucket = per_ref.setdefault(
            int(rid),
            {
                "name": str(doc.get("referee_name") or ""),
                "matches": 0,
                "yellow": 0,
                "red": 0,
                "penalty_matches": 0,
                "points": 0.0,
            },
        )
        if not str(bucket.get("name") or "").strip() and str(doc.get("referee_name") or "").strip():
            bucket["name"] = str(doc.get("referee_name") or "")
        bucket["matches"] += 1
        bucket["yellow"] += int(yellow)
        bucket["red"] += int(red)
        bucket["penalty_matches"] += 1 if penalty else 0
        bucket["points"] += float(points)

    baseline = {
        "matches": float(total_matches),
        "yellow": float(total_yellow),
        "red": float(total_red),
        "penalty_matches": float(total_penalty),
        "points": float(total_points),
    }
    return per_ref, baseline


def _avg_block(stats: dict[str, Any], baseline_points_per_match: float) -> dict[str, Any]:
    matches = int(stats.get("matches") or 0)
    if matches <= 0:
        return {
            "yellow": 0.0,
            "red": 0.0,
            "penalty_pct": 0.0,
            "discipline_points": 0.0,
            "strictness_index": 100.0,
        }
    yellow_avg = float(stats.get("yellow") or 0.0) / float(matches)
    red_avg = float(stats.get("red") or 0.0) / float(matches)
    penalty_pct = (float(stats.get("penalty_matches") or 0.0) / float(matches)) * 100.0
    points_avg = float(stats.get("points") or 0.0) / float(matches)
    strictness_index = (points_avg / float(baseline_points_per_match) * 100.0) if baseline_points_per_match > 0 else 100.0
    return {
        "yellow": round(yellow_avg, 3),
        "red": round(red_avg, 3),
        "penalty_pct": round(penalty_pct, 3),
        "discipline_points": round(points_avg, 4),
        "strictness_index": round(strictness_index, 2),
    }


async def build_referee_profiles(
    referee_ids: list[int],
    *,
    season_id: int | None = None,
    league_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    ids = sorted({int(value) for value in referee_ids if isinstance(value, int)})
    if not ids:
        return {}
    id_set = set(ids)

    base_query: dict[str, Any] = {"status": "FINISHED"}
    if league_id is not None:
        base_query["league_id"] = int(league_id)

    projection = {
        "_id": 1,
        "referee_id": 1,
        "referee_name": 1,
        "events": 1,
        "penalty_info": 1,
    }
    career_docs = await _db.db.matches_v3.find(
        {**base_query, "referee_id": {"$in": ids}},
        projection,
    ).to_list(length=250_000)
    if not career_docs:
        return {}
    baseline_docs = await _db.db.matches_v3.find(base_query, projection).to_list(length=500_000)

    career_per_ref, career_baseline = _aggregate_docs(career_docs, referee_ids=id_set)
    baseline_points = (
        float(career_baseline["points"]) / float(career_baseline["matches"])
        if float(career_baseline["matches"]) > 0
        else 0.0
    )

    season_per_ref: dict[int, dict[str, Any]] = {}
    season_points_baseline = baseline_points
    if season_id is not None:
        season_query = {**base_query, "season_id": int(season_id)}
        season_ref_docs = await _db.db.matches_v3.find(
            {**season_query, "referee_id": {"$in": ids}},
            projection,
        ).to_list(length=250_000)
        season_baseline_docs = await _db.db.matches_v3.find(season_query, projection).to_list(length=500_000)
        season_per_ref, season_baseline = _aggregate_docs(season_ref_docs, referee_ids=id_set)
        season_points_baseline = (
            float(season_baseline["points"]) / float(season_baseline["matches"])
            if float(season_baseline["matches"]) > 0
            else baseline_points
        )

    out: dict[int, dict[str, Any]] = {}
    for rid in ids:
        career_stats = career_per_ref.get(int(rid))
        if not isinstance(career_stats, dict):
            continue
        career_avg = _avg_block(career_stats, baseline_points)
        season_stats = season_per_ref.get(int(rid)) if season_id is not None else None
        if isinstance(season_stats, dict):
            season_avg = _avg_block(season_stats, season_points_baseline)
        else:
            season_avg = dict(career_avg)
        trend = strictness_trend(
            float(season_avg.get("strictness_index") or 100.0),
            float(career_avg.get("strictness_index") or 100.0),
        )
        out[int(rid)] = {
            "id": int(rid),
            "name": str(career_stats.get("name") or ""),
            "strictness_index": float(career_avg["strictness_index"]),
            "strictness_band": strictness_band(float(career_avg["strictness_index"])),
            "avg_yellow": float(career_avg["yellow"]),
            "avg_red": float(career_avg["red"]),
            "penalty_pct": float(career_avg["penalty_pct"]),
            "career_avg": career_avg,
            "season_avg": season_avg,
            "trend": trend,
        }
    return out
