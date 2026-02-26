"""
backend/app/services/admin_service.py

Purpose:
    Administrative merge logic for Team Tower operations.
    Consolidates duplicate teams by rewiring canonical team IDs and resolving
    duplicate matches plus all dependent match references (legs/bets/matchday).

Dependencies:
    - app.database
    - app.services.team_registry_service
    - app.utils
"""

from __future__ import annotations

from bson import ObjectId
from fastapi import HTTPException

import app.database as _db
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.utils import utcnow

_ZERO_OID = ObjectId("000000000000000000000000")


def _alias_key(alias: dict) -> tuple[str, str]:
    return (str(alias.get("normalized", "")), str(alias.get("sport_key", "")))


def _canonical_aliases(team: dict) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for alias in team.get("aliases", []):
        item = {
            "name": alias.get("name", ""),
            "normalized": alias.get("normalized", ""),
            "sport_key": alias.get("sport_key", team.get("sport_key")),
            "source": alias.get("source", team.get("source", "admin")),
        }
        key = _alias_key(item)
        if key in seen or not item["normalized"]:
            continue
        seen.add(key)
        merged.append(item)

    display_name = team.get("display_name", "")
    normalized = normalize_team_name(display_name)
    if normalized:
        display_alias = {
            "name": display_name,
            "normalized": normalized,
            "sport_key": team.get("sport_key"),
            "source": team.get("source", "admin"),
        }
        key = _alias_key(display_alias)
        if key not in seen:
            merged.append(display_alias)
    return merged


def _merged_league_ids(source: dict, target: dict) -> list[ObjectId]:
    seen: set[str] = set()
    merged: list[ObjectId] = []
    for raw in [*(target.get("league_ids") or []), *(source.get("league_ids") or [])]:
        oid: ObjectId | None = None
        if isinstance(raw, ObjectId):
            oid = raw
        elif isinstance(raw, str):
            try:
                oid = ObjectId(raw)
            except Exception:
                oid = None
        if oid is None:
            continue
        sid = str(oid)
        if sid in seen:
            continue
        seen.add(sid)
        merged.append(oid)
    return merged


def _as_id_str(value: ObjectId | str | None) -> str:
    if value is None:
        return ""
    return str(value)


def _as_iso(value) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _status_rank(status: str | None) -> int:
    order = {
        "final": 5,
        "live": 4,
        "scheduled": 3,
        "postponed": 2,
        "canceled": 1,
        "cancelled": 1,
    }
    return order.get(str(status or "").lower(), 0)


def _has_full_time_score(match_doc: dict) -> int:
    score = match_doc.get("score") or {}
    full = score.get("full_time") if isinstance(score, dict) else None
    if not isinstance(full, dict):
        return 0
    return 1 if full.get("home") is not None and full.get("away") is not None else 0


def _match_merge_key(match_doc: dict, source_id: ObjectId, target_id: ObjectId) -> str:
    source_str = str(source_id)
    home_str = _as_id_str(match_doc.get("home_team_id"))
    away_str = _as_id_str(match_doc.get("away_team_id"))
    if home_str == source_str:
        home_str = str(target_id)
    if away_str == source_str:
        away_str = str(target_id)
    match_day = _match_date_bucket(match_doc)
    return "|".join(
        [
            _as_id_str(match_doc.get("league_id")),
            home_str,
            away_str,
            match_day,
        ]
    )


def _match_date_bucket(match_doc: dict) -> str:
    """Group duplicate matches by UTC calendar day to tolerate kickoff time variants."""
    for field in ("match_date", "match_date_hour"):
        value = match_doc.get(field)
        if hasattr(value, "date"):
            try:
                return value.date().isoformat()
            except Exception:
                pass
    return _as_iso(match_doc.get("match_date_hour"))


def _pick_keeper(matches: list[dict]) -> dict:
    # Status-first deterministic keeper selection.
    ordered = sorted(
        matches,
        key=lambda m: (
            _status_rank(m.get("status")),
            _has_full_time_score(m),
            _as_iso(m.get("updated_at")),
            _as_iso(m.get("created_at")),
            str(m.get("_id")),
        ),
    )
    return ordered[-1]


def _global_match_duplicate_key(match_doc: dict) -> str:
    return "|".join(
        [
            _as_id_str(match_doc.get("league_id")),
            _as_id_str(match_doc.get("home_team_id")),
            _as_id_str(match_doc.get("away_team_id")),
            _match_date_bucket(match_doc),
        ]
    )


async def _archive_documents(
    *,
    collection_name: str,
    docs: list[dict],
    merge_job_id: str,
    source_id: ObjectId,
    target_id: ObjectId,
    merged_into_lookup: dict[str, str] | None = None,
    reason: str = "team_merge_duplicate_match",
    session=None,
) -> int:
    if not docs:
        return 0
    archived_at = utcnow()
    rows: list[dict] = []
    for doc in docs:
        original_id = str(doc.get("_id") or "")
        rows.append(
            {
                "merge_job_id": merge_job_id,
                "original_id": original_id,
                "merged_into_id": (merged_into_lookup or {}).get(original_id),
                "source_team_id": source_id,
                "target_team_id": target_id,
                "archived_at": archived_at,
                "reason": reason,
                "full_document": doc,
            }
        )
    await _db.db[collection_name].insert_many(rows, session=session)
    return len(rows)


async def _rewrite_match_references(
    loser_to_keeper: dict[str, str],
    *,
    now,
    session=None,
) -> dict[str, int]:
    if not loser_to_keeper:
        return {}

    rewrite_stats = {
        "betting_slips_selections": 0,
        "betting_slips_admin_unlocked": 0,
        "matchday_predictions_predictions": 0,
        "matchday_predictions_admin_unlocked": 0,
        "matchdays_match_ids": 0,
        "parlays_legs": 0,
        "bankroll_bets": 0,
        "over_under_bets": 0,
        "fantasy_picks": 0,
        "survivor_entries_picks": 0,
    }

    for loser_id, keeper_id in loser_to_keeper.items():
        slips_sel = await _db.db.betting_slips.update_many(
            {"selections.match_id": loser_id},
            {"$set": {"selections.$[sel].match_id": keeper_id, "updated_at": now}},
            array_filters=[{"sel.match_id": loser_id}],
            session=session,
        )
        rewrite_stats["betting_slips_selections"] += slips_sel.modified_count

        slips_unlock = await _db.db.betting_slips.update_many(
            {"admin_unlocked_matches": loser_id},
            [
                {
                    "$set": {
                        "admin_unlocked_matches": {
                            "$setUnion": [
                                {
                                    "$map": {
                                        "input": {"$ifNull": ["$admin_unlocked_matches", []]},
                                        "as": "mid",
                                        "in": {
                                            "$cond": [
                                                {"$in": [{"$toString": "$$mid"}, [loser_id, keeper_id]]},
                                                keeper_id,
                                                "$$mid",
                                            ]
                                        },
                                    }
                                },
                                [],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            session=session,
        )
        rewrite_stats["betting_slips_admin_unlocked"] += slips_unlock.modified_count

        md_preds = await _db.db.matchday_predictions.update_many(
            {"predictions.match_id": loser_id},
            {"$set": {"predictions.$[pred].match_id": keeper_id, "updated_at": now}},
            array_filters=[{"pred.match_id": loser_id}],
            session=session,
        )
        rewrite_stats["matchday_predictions_predictions"] += md_preds.modified_count

        md_unlock = await _db.db.matchday_predictions.update_many(
            {"admin_unlocked_matches": loser_id},
            [
                {
                    "$set": {
                        "admin_unlocked_matches": {
                            "$setUnion": [
                                {
                                    "$map": {
                                        "input": {"$ifNull": ["$admin_unlocked_matches", []]},
                                        "as": "mid",
                                        "in": {
                                            "$cond": [
                                                {"$in": [{"$toString": "$$mid"}, [loser_id, keeper_id]]},
                                                keeper_id,
                                                "$$mid",
                                            ]
                                        },
                                    }
                                },
                                [],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            session=session,
        )
        rewrite_stats["matchday_predictions_admin_unlocked"] += md_unlock.modified_count

        md_rows = await _db.db.matchdays.update_many(
            {"match_ids": loser_id},
            [
                {
                    "$set": {
                        "match_ids": {
                            "$setUnion": [
                                {
                                    "$map": {
                                        "input": {"$ifNull": ["$match_ids", []]},
                                        "as": "mid",
                                        "in": {
                                            "$cond": [
                                                {"$in": [{"$toString": "$$mid"}, [loser_id, keeper_id]]},
                                                keeper_id,
                                                "$$mid",
                                            ]
                                        },
                                    }
                                },
                                [],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            session=session,
        )
        rewrite_stats["matchdays_match_ids"] += md_rows.modified_count

        parlay_rows = await _db.db.parlays.update_many(
            {"legs.match_id": loser_id},
            {"$set": {"legs.$[leg].match_id": keeper_id}},
            array_filters=[{"leg.match_id": loser_id}],
            session=session,
        )
        rewrite_stats["parlays_legs"] += parlay_rows.modified_count

        bankroll_rows = await _db.db.bankroll_bets.update_many(
            {"match_id": loser_id},
            {"$set": {"match_id": keeper_id}},
            session=session,
        )
        rewrite_stats["bankroll_bets"] += bankroll_rows.modified_count

        ou_rows = await _db.db.over_under_bets.update_many(
            {"match_id": loser_id},
            {"$set": {"match_id": keeper_id}},
            session=session,
        )
        rewrite_stats["over_under_bets"] += ou_rows.modified_count

        fantasy_rows = await _db.db.fantasy_picks.update_many(
            {"match_id": loser_id},
            {"$set": {"match_id": keeper_id, "updated_at": now}},
            session=session,
        )
        rewrite_stats["fantasy_picks"] += fantasy_rows.modified_count

        survivor_rows = await _db.db.survivor_entries.update_many(
            {"picks.match_id": loser_id},
            {"$set": {"picks.$[pick].match_id": keeper_id, "updated_at": now}},
            array_filters=[{"pick.match_id": loser_id}],
            session=session,
        )
        rewrite_stats["survivor_entries_picks"] += survivor_rows.modified_count

    return rewrite_stats


async def _dedupe_quotico_tips_for_matches(
    keeper_match_ids: list[str],
    *,
    merge_job_id: str,
    source_id: ObjectId,
    target_id: ObjectId,
    session=None,
) -> int:
    if not keeper_match_ids:
        return 0

    pipeline = [
        {"$match": {"match_id": {"$in": keeper_match_ids}}},
        {"$sort": {"updated_at": -1, "generated_at": -1, "created_at": -1, "_id": -1}},
        {
            "$group": {
                "_id": "$match_id",
                "keeper": {"$first": "$$ROOT"},
                "docs": {"$push": "$$ROOT"},
                "count": {"$sum": 1},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    groups = await _db.db.quotico_tips.aggregate(pipeline, session=session).to_list(length=10_000)
    archived = 0

    for group in groups:
        keeper_doc = group.get("keeper") or {}
        docs = group.get("docs") or []
        losers = [doc for doc in docs if str(doc.get("_id")) != str(keeper_doc.get("_id"))]
        if not losers:
            continue

        merged_into_lookup = {
            str(doc.get("_id")): str(keeper_doc.get("_id"))
            for doc in losers
            if doc.get("_id")
        }
        archived += await _archive_documents(
            collection_name="archived_quotico_tips",
            docs=losers,
            merge_job_id=merge_job_id,
            source_id=source_id,
            target_id=target_id,
            merged_into_lookup=merged_into_lookup,
            reason="team_merge_tip_dedupe",
            session=session,
        )
        await _db.db.quotico_tips.delete_many(
            {"_id": {"$in": [doc["_id"] for doc in losers if doc.get("_id")]}},
            session=session,
        )

    return archived


async def _dedupe_odds_events_for_matches(
    keeper_match_ids: list[ObjectId],
    *,
    merge_job_id: str,
    source_id: ObjectId,
    target_id: ObjectId,
    session=None,
) -> int:
    if not keeper_match_ids:
        return 0

    archived = 0
    keeper_filter = {"match_id": {"$in": keeper_match_ids}}

    # Pass 1: duplicates with event_hash present.
    hash_pipeline = [
        {"$match": {**keeper_filter, "event_hash": {"$exists": True, "$nin": [None, ""]}}},
        {"$sort": {"snapshot_at": -1, "ingested_at": -1, "_id": -1}},
        {
            "$group": {
                "_id": {"match_id": "$match_id", "event_hash": "$event_hash"},
                "keeper": {"$first": "$$ROOT"},
                "docs": {"$push": "$$ROOT"},
                "count": {"$sum": 1},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    hash_groups = await _db.db.odds_events.aggregate(hash_pipeline, session=session).to_list(length=200_000)
    hash_losers: list[dict] = []
    hash_delete_ids: list[ObjectId] = []
    hash_lookup: dict[str, str] = {}

    for group in hash_groups:
        keeper_doc = group.get("keeper") or {}
        docs = group.get("docs") or []
        losers = [doc for doc in docs if str(doc.get("_id")) != str(keeper_doc.get("_id"))]
        for doc in losers:
            if not doc.get("_id"):
                continue
            hash_losers.append(doc)
            hash_delete_ids.append(doc["_id"])
            hash_lookup[str(doc["_id"])] = str(keeper_doc.get("_id"))

    if hash_losers:
        archived += await _archive_documents(
            collection_name="archived_odds_events",
            docs=hash_losers,
            merge_job_id=merge_job_id,
            source_id=source_id,
            target_id=target_id,
            merged_into_lookup=hash_lookup,
            reason="team_merge_odds_event_dedupe_hash",
            session=session,
        )
        await _db.db.odds_events.delete_many({"_id": {"$in": hash_delete_ids}}, session=session)

    # Pass 2: fallback duplicates where event_hash is missing.
    fallback_pipeline = [
        {"$match": {**keeper_filter, "$or": [{"event_hash": {"$exists": False}}, {"event_hash": {"$in": [None, ""]}}]}},
        {"$sort": {"snapshot_at": -1, "ingested_at": -1, "_id": -1}},
        {
            "$group": {
                "_id": {
                    "match_id": "$match_id",
                    "provider": "$provider",
                    "market": "$market",
                    "selection_key": "$selection_key",
                    "line": "$line",
                    "snapshot_at": "$snapshot_at",
                    "price": "$price",
                },
                "keeper": {"$first": "$$ROOT"},
                "docs": {"$push": "$$ROOT"},
                "count": {"$sum": 1},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    fallback_groups = await _db.db.odds_events.aggregate(fallback_pipeline, session=session).to_list(length=200_000)
    fallback_losers: list[dict] = []
    fallback_delete_ids: list[ObjectId] = []
    fallback_lookup: dict[str, str] = {}

    for group in fallback_groups:
        keeper_doc = group.get("keeper") or {}
        docs = group.get("docs") or []
        losers = [doc for doc in docs if str(doc.get("_id")) != str(keeper_doc.get("_id"))]
        for doc in losers:
            if not doc.get("_id"):
                continue
            fallback_losers.append(doc)
            fallback_delete_ids.append(doc["_id"])
            fallback_lookup[str(doc["_id"])] = str(keeper_doc.get("_id"))

    if fallback_losers:
        archived += await _archive_documents(
            collection_name="archived_odds_events",
            docs=fallback_losers,
            merge_job_id=merge_job_id,
            source_id=source_id,
            target_id=target_id,
            merged_into_lookup=fallback_lookup,
            reason="team_merge_odds_event_dedupe_fallback",
            session=session,
        )
        await _db.db.odds_events.delete_many({"_id": {"$in": fallback_delete_ids}}, session=session)

    return archived


async def _collapse_duplicate_matches_for_team_merge(
    source_id: ObjectId,
    target_id: ObjectId,
    now,
    *,
    merge_job_id: str,
    session=None,
) -> dict[str, int]:
    source_values = [source_id, str(source_id)]
    target_values = [target_id, str(target_id)]
    match_docs = await _db.db.matches.find(
        {
            "$or": [
                {"home_team_id": {"$in": [*source_values, *target_values]}},
                {"away_team_id": {"$in": [*source_values, *target_values]}},
            ]
        },
        {
            "_id": 1,
            "league_id": 1,
            "home_team_id": 1,
            "away_team_id": 1,
            "match_date_hour": 1,
            "created_at": 1,
            "updated_at": 1,
            "status": 1,
            "score": 1,
        },
        session=session,
    ).to_list(length=10_000)

    grouped: dict[str, list[dict]] = {}
    for match_doc in match_docs:
        key = _match_merge_key(match_doc, source_id, target_id)
        grouped.setdefault(key, []).append(match_doc)

    losers: list[dict] = []
    loser_to_keeper: dict[str, str] = {}
    collisions = 0
    for docs in grouped.values():
        if len(docs) <= 1:
            continue
        collisions += 1
        keeper = _pick_keeper(docs)
        keeper_id = str(keeper["_id"])
        for doc in docs:
            doc_id = str(doc["_id"])
            if doc_id == keeper_id:
                continue
            loser_to_keeper[doc_id] = keeper_id
            losers.append(doc)

    if not losers:
        return {
            "collisions_detected": 0,
            "matches_deduped": 0,
            "refs_rewritten": 0,
            "archived_matches": 0,
            "archived_tips": 0,
            "archived_odds_events": 0,
            "deduped_quotico_tips": 0,
            "deduped_odds_events": 0,
        }

    # Rewire all dependent references first (legs/bets/etc.).
    rewrite_stats = await _rewrite_match_references(loser_to_keeper, now=now, session=session)
    rewritten_total = sum(rewrite_stats.values())

    loser_ids = [ObjectId(mid) for mid in loser_to_keeper.keys()]
    loser_id_strings = list(loser_to_keeper.keys())

    # Archive loser matches.
    loser_match_docs = await _db.db.matches.find({"_id": {"$in": loser_ids}}, session=session).to_list(length=len(loser_ids))
    archived_matches = await _archive_documents(
        collection_name="archived_matches",
        docs=loser_match_docs,
        merge_job_id=merge_job_id,
        source_id=source_id,
        target_id=target_id,
        merged_into_lookup=loser_to_keeper,
        session=session,
    )

    # Handle quotico_tips carefully because match_id is unique.
    archived_tips = 0
    for loser_id, keeper_id in loser_to_keeper.items():
        keeper_tip = await _db.db.quotico_tips.find_one({"match_id": keeper_id}, session=session)
        loser_tip = await _db.db.quotico_tips.find_one({"match_id": loser_id}, session=session)
        if not loser_tip:
            continue
        if keeper_tip:
            archived_tips += await _archive_documents(
                collection_name="archived_quotico_tips",
                docs=[loser_tip],
                merge_job_id=merge_job_id,
                source_id=source_id,
                target_id=target_id,
                merged_into_lookup={str(loser_tip["_id"]): str(keeper_tip.get("_id", ""))},
                session=session,
            )
            await _db.db.quotico_tips.delete_one({"_id": loser_tip["_id"]}, session=session)
        else:
            await _db.db.quotico_tips.update_one(
                {"_id": loser_tip["_id"]},
                {"$set": {"match_id": keeper_id, "updated_at": now}},
                session=session,
            )

    loser_odds_docs = await _db.db.odds_events.find({"match_id": {"$in": loser_ids}}, session=session).to_list(length=200_000)
    archived_odds = await _archive_documents(
        collection_name="archived_odds_events",
        docs=loser_odds_docs,
        merge_job_id=merge_job_id,
        source_id=source_id,
        target_id=target_id,
        merged_into_lookup=loser_to_keeper,
        session=session,
    )

    # Keep odds history on keeper match id.
    for loser_id, keeper_id in loser_to_keeper.items():
        await _db.db.odds_events.update_many(
            {"match_id": ObjectId(loser_id)},
            {"$set": {"match_id": ObjectId(keeper_id)}},
            session=session,
        )

    keeper_match_ids_str = sorted(set(loser_to_keeper.values()))
    keeper_match_ids_oid = [ObjectId(mid) for mid in keeper_match_ids_str]
    deduped_tips = await _dedupe_quotico_tips_for_matches(
        keeper_match_ids_str,
        merge_job_id=merge_job_id,
        source_id=source_id,
        target_id=target_id,
        session=session,
    )
    deduped_odds = await _dedupe_odds_events_for_matches(
        keeper_match_ids_oid,
        merge_job_id=merge_job_id,
        source_id=source_id,
        target_id=target_id,
        session=session,
    )

    await _db.db.matches.delete_many({"_id": {"$in": loser_ids}}, session=session)

    return {
        "collisions_detected": collisions,
        "matches_deduped": len(loser_ids),
        "refs_rewritten": rewritten_total,
        "archived_matches": archived_matches,
        "archived_tips": archived_tips,
        "archived_odds_events": archived_odds,
        "deduped_quotico_tips": deduped_tips,
        "deduped_odds_events": deduped_odds,
        **rewrite_stats,
    }


async def _collect_same_day_duplicate_groups(
    *,
    league_id: ObjectId | None = None,
    sport_key: str | None = None,
    limit_groups: int = 200,
) -> list[dict]:
    query: dict = {}
    if league_id is not None:
        query["league_id"] = league_id
    if sport_key:
        query["sport_key"] = str(sport_key).strip()

    docs = await _db.db.matches.find(
        query,
        {
            "_id": 1,
            "league_id": 1,
            "sport_key": 1,
            "home_team_id": 1,
            "away_team_id": 1,
            "home_team": 1,
            "away_team": 1,
            "match_date": 1,
            "match_date_hour": 1,
            "status": 1,
            "score": 1,
            "result": 1,
            "created_at": 1,
            "updated_at": 1,
        },
    ).to_list(length=200_000)

    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        key = _global_match_duplicate_key(doc)
        grouped.setdefault(key, []).append(doc)

    out: list[dict] = []
    for key, items in grouped.items():
        if len(items) <= 1:
            continue
        keeper = _pick_keeper(items)
        match_day = _match_date_bucket(keeper)
        out.append(
            {
                "key": key,
                "league_id": str(keeper.get("league_id") or ""),
                "sport_key": str(keeper.get("sport_key") or ""),
                "home_team": str(keeper.get("home_team") or ""),
                "away_team": str(keeper.get("away_team") or ""),
                "match_day": match_day,
                "count": len(items),
                "keeper_id": str(keeper.get("_id")),
                "matches": sorted(
                    [
                        {
                            "id": str(m.get("_id")),
                            "status": str(m.get("status") or ""),
                            "match_date": _as_iso(m.get("match_date")),
                            "match_date_hour": _as_iso(m.get("match_date_hour")),
                            "result": m.get("result") or {},
                            "score": m.get("score") or {},
                            "is_keeper": str(m.get("_id")) == str(keeper.get("_id")),
                        }
                        for m in items
                    ],
                    key=lambda row: (not row["is_keeper"], row.get("match_date") or "", row.get("id") or ""),
                ),
            }
        )

    out.sort(key=lambda row: (int(row.get("count") or 0), str(row.get("match_day") or "")), reverse=True)
    return out[: max(0, int(limit_groups))]


async def list_same_day_duplicate_matches(
    *,
    league_id: ObjectId | None = None,
    sport_key: str | None = None,
    limit_groups: int = 200,
) -> dict:
    groups = await _collect_same_day_duplicate_groups(
        league_id=league_id,
        sport_key=sport_key,
        limit_groups=limit_groups,
    )
    return {
        "total_groups": len(groups),
        "total_matches": sum(int(g.get("count") or 0) for g in groups),
        "groups": groups,
    }


async def cleanup_same_day_duplicate_matches(
    *,
    league_id: ObjectId | None = None,
    sport_key: str | None = None,
    limit_groups: int = 500,
    dry_run: bool = False,
) -> dict:
    groups = await _collect_same_day_duplicate_groups(
        league_id=league_id,
        sport_key=sport_key,
        limit_groups=limit_groups,
    )
    loser_to_keeper: dict[str, str] = {}
    losers: list[ObjectId] = []
    for group in groups:
        keeper_id = str(group.get("keeper_id") or "")
        for row in group.get("matches") or []:
            rid = str((row or {}).get("id") or "")
            if not rid or rid == keeper_id:
                continue
            try:
                losers.append(ObjectId(rid))
                loser_to_keeper[rid] = keeper_id
            except Exception:
                continue

    if dry_run or not losers:
        return {
            "dry_run": bool(dry_run),
            "groups": len(groups),
            "would_delete": len(losers),
            "deleted": 0,
            "reference_rewrite": not bool(dry_run),
        }

    now = utcnow()
    rewrite_stats = await _rewrite_match_references(loser_to_keeper, now=now, session=None)
    rewritten_total = sum(int(v or 0) for v in rewrite_stats.values())

    # quotico_tips uses unique match_id; handle collisions and re-point survivors.
    archived_tips = 0
    for loser_id, keeper_id in loser_to_keeper.items():
        keeper_tip = await _db.db.quotico_tips.find_one({"match_id": keeper_id})
        loser_tip = await _db.db.quotico_tips.find_one({"match_id": loser_id})
        if not loser_tip:
            continue
        if keeper_tip:
            archived_tips += await _archive_documents(
                collection_name="archived_quotico_tips",
                docs=[loser_tip],
                merge_job_id=str(ObjectId()),
                source_id=_ZERO_OID,
                target_id=_ZERO_OID,
                merged_into_lookup={str(loser_tip["_id"]): str(keeper_tip.get("_id", ""))},
                reason="same_day_duplicate_tip_dedupe",
            )
            await _db.db.quotico_tips.delete_one({"_id": loser_tip["_id"]})
        else:
            await _db.db.quotico_tips.update_one(
                {"_id": loser_tip["_id"]},
                {"$set": {"match_id": keeper_id, "updated_at": now}},
            )

    # Move odds to keeper match ids, then dedupe per keeper.
    for loser_id, keeper_id in loser_to_keeper.items():
        await _db.db.odds_events.update_many(
            {"match_id": ObjectId(loser_id)},
            {"$set": {"match_id": ObjectId(keeper_id)}},
        )

    keeper_match_ids_str = sorted(set(loser_to_keeper.values()))
    keeper_match_ids_oid = [ObjectId(mid) for mid in keeper_match_ids_str]
    deduped_tips = await _dedupe_quotico_tips_for_matches(
        keeper_match_ids_str,
        merge_job_id=str(ObjectId()),
        source_id=_ZERO_OID,
        target_id=_ZERO_OID,
        session=None,
    )
    deduped_odds = await _dedupe_odds_events_for_matches(
        keeper_match_ids_oid,
        merge_job_id=str(ObjectId()),
        source_id=_ZERO_OID,
        target_id=_ZERO_OID,
        session=None,
    )

    loser_docs = await _db.db.matches.find({"_id": {"$in": losers}}).to_list(length=len(losers))
    merge_job_id = str(ObjectId())
    archived = await _archive_documents(
        collection_name="archived_matches",
        docs=loser_docs,
        merge_job_id=merge_job_id,
        source_id=_ZERO_OID,
        target_id=_ZERO_OID,
        merged_into_lookup=loser_to_keeper,
        reason="same_day_duplicate_cleanup",
        session=None,
    )
    delete_result = await _db.db.matches.delete_many({"_id": {"$in": losers}})
    return {
        "dry_run": False,
        "groups": len(groups),
        "archived": int(archived),
        "deleted": int(delete_result.deleted_count),
        "merge_job_id": merge_job_id,
        "reference_rewrite": True,
        "refs_rewritten": rewritten_total,
        "archived_quotico_tips": int(archived_tips),
        "deduped_quotico_tips": int(deduped_tips),
        "deduped_odds_events": int(deduped_odds),
        **rewrite_stats,
    }


async def merge_teams(source_id: ObjectId, target_id: ObjectId) -> dict:
    source = await _db.db.teams.find_one({"_id": source_id})
    target = await _db.db.teams.find_one({"_id": target_id})
    if not source or not target:
        raise HTTPException(status_code=404, detail="Source or target team not found.")
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ.")

    source_aliases = _canonical_aliases(source)
    target_aliases = _canonical_aliases(target)
    alias_seen = {_alias_key(a) for a in target_aliases}
    merged_aliases = list(target_aliases)
    for alias in source_aliases:
        key = _alias_key(alias)
        if key in alias_seen:
            continue
        alias_seen.add(key)
        merged_aliases.append(alias)

    now = utcnow()
    merge_job_id = str(ObjectId())

    dedupe_stats = await _collapse_duplicate_matches_for_team_merge(
        source_id,
        target_id,
        now,
        merge_job_id=merge_job_id,
    )

    await _db.db.teams.update_one(
        {"_id": target_id},
        {
            "$set": {
                "aliases": merged_aliases,
                "league_ids": _merged_league_ids(source, target),
                "updated_at": now,
                "needs_review": False,
            }
        },
    )

    source_values = [source_id, str(source_id)]
    target_str = str(target_id)
    target_display_name = str(target.get("display_name") or "")

    matches_home = await _db.db.matches.update_many(
        {"home_team_id": {"$in": source_values}},
        {"$set": {"home_team_id": target_id, "home_team": target_display_name, "updated_at": now}},
    )
    matches_away = await _db.db.matches.update_many(
        {"away_team_id": {"$in": source_values}},
        {"$set": {"away_team_id": target_id, "away_team": target_display_name, "updated_at": now}},
    )
    tips_home = await _db.db.quotico_tips.update_many(
        {"home_team_id": {"$in": source_values}},
        {"$set": {"home_team_id": target_id, "updated_at": now}},
    )
    tips_away = await _db.db.quotico_tips.update_many(
        {"away_team_id": {"$in": source_values}},
        {"$set": {"away_team_id": target_id, "updated_at": now}},
    )

    slips_sel = await _db.db.betting_slips.update_many(
        {"selections.team_id": {"$in": source_values}},
        {"$set": {"selections.$[sel].team_id": target_id, "updated_at": now}},
        array_filters=[{"sel.team_id": {"$in": source_values}}],
    )
    slips_used = await _db.db.betting_slips.update_many(
        {"used_team_ids": {"$in": [source_id, str(source_id), target_str]}},
        [
            {
                "$set": {
                    "used_team_ids": {
                        "$map": {
                            "input": {"$ifNull": ["$used_team_ids", []]},
                            "as": "tid",
                            "in": {
                                "$cond": [
                                    {
                                        "$in": [
                                            {"$toString": "$$tid"},
                                            [str(source_id), target_str],
                                        ]
                                    },
                                    target_id,
                                    "$$tid",
                                ]
                            },
                        }
                    }
                }
            },
            {"$set": {"used_team_ids": {"$setUnion": ["$used_team_ids", []]}, "updated_at": now}},
        ],
    )

    survivor_picks = await _db.db.survivor_entries.update_many(
        {"picks.team_id": {"$in": source_values}},
        {"$set": {"picks.$[pick].team_id": target_id, "updated_at": now}},
        array_filters=[{"pick.team_id": {"$in": source_values}}],
    )
    survivor_used = await _db.db.survivor_entries.update_many(
        {"used_team_ids": {"$in": [source_id, str(source_id), target_str]}},
        [
            {
                "$set": {
                    "used_team_ids": {
                        "$map": {
                            "input": {"$ifNull": ["$used_team_ids", []]},
                            "as": "tid",
                            "in": {
                                "$cond": [
                                    {
                                        "$in": [
                                            {"$toString": "$$tid"},
                                            [str(source_id), target_str],
                                        ]
                                    },
                                    target_id,
                                    "$$tid",
                                ]
                            },
                        }
                    }
                }
            },
            {"$set": {"used_team_ids": {"$setUnion": ["$used_team_ids", []]}, "updated_at": now}},
        ],
    )

    fantasy = await _db.db.fantasy_picks.update_many(
        {"team_id": {"$in": source_values}},
        {"$set": {"team_id": target_id, "updated_at": now}},
    )

    delete_source = await _db.db.teams.delete_one({"_id": source_id})
    if delete_source.deleted_count != 1:
        raise HTTPException(status_code=500, detail="Failed to delete source team after merge.")

    await TeamRegistry.get().initialize()

    return {
        "merge_job_id": merge_job_id,
        "aliases_transferred": len(merged_aliases) - len(target_aliases),
        "match_collisions_detected": dedupe_stats["collisions_detected"],
        "duplicate_matches_deleted": dedupe_stats["matches_deduped"],
        "match_refs_rewritten": dedupe_stats["refs_rewritten"],
        "archived_matches": dedupe_stats["archived_matches"],
        "archived_quotico_tips": dedupe_stats["archived_tips"],
        "archived_odds_events": dedupe_stats["archived_odds_events"],
        "deduped_quotico_tips": dedupe_stats["deduped_quotico_tips"],
        "deduped_odds_events": dedupe_stats["deduped_odds_events"],
        "matches_home_updated": matches_home.modified_count,
        "matches_away_updated": matches_away.modified_count,
        "quotico_tips_home_updated": tips_home.modified_count,
        "quotico_tips_away_updated": tips_away.modified_count,
        "betting_slips_selections_updated": slips_sel.modified_count,
        "betting_slips_used_team_ids_updated": slips_used.modified_count,
        "survivor_entries_picks_updated": survivor_picks.modified_count,
        "survivor_entries_used_team_ids_updated": survivor_used.modified_count,
        "fantasy_picks_updated": fantasy.modified_count,
        "source_deleted": delete_source.deleted_count,
    }
