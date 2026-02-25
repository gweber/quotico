from __future__ import annotations

from bson import ObjectId
from fastapi import HTTPException

import app.database as _db
from app.services.team_registry_service import TeamRegistry, normalize_team_name
from app.utils import utcnow


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

    matches_home = await _db.db.matches.update_many(
        {"home_team_id": {"$in": source_values}},
        {"$set": {"home_team_id": target_id, "updated_at": now}},
    )
    matches_away = await _db.db.matches.update_many(
        {"away_team_id": {"$in": source_values}},
        {"$set": {"away_team_id": target_id, "updated_at": now}},
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
        "aliases_transferred": len(merged_aliases) - len(target_aliases),
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
