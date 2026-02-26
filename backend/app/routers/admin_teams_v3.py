"""
backend/app/routers/admin_teams_v3.py

Purpose:
    Admin Team Tower v3 endpoints for alias-only operations on Sportmonks
    canonical teams (`teams_v3`).

Dependencies:
    - app.services.auth_service
    - app.services.team_alias_normalizer
    - app.utils
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import app.database as _db
from app.config import settings
from app.services.auth_service import get_admin_user
from app.utils import ensure_utc, utcnow
from app.services.team_alias_normalizer import normalize_team_alias

router = APIRouter(prefix="/api/admin/teams-v3", tags=["admin-teams-v3"])

_LOCKED_FIELDS = ["name", "short_code", "image_path"]
_SUGGESTION_COLL = "team_alias_suggestions_v3"
_EVENT_COLL = "team_alias_resolution_events"
_ALLOWED_SOURCES = tuple(
    sorted(
        {
            s.strip()
            for s in (
                getattr(settings, "ALIAS_SOURCES_ALLOWED", "manual,provider_x,crawler,provider_unknown")
            ).split(",")
            if s.strip()
        }
    )
) or ("manual", "provider_x", "crawler", "provider_unknown")


class AliasSource(str, Enum):
    manual = "manual"
    provider_x = "provider_x"
    crawler = "crawler"
    provider_unknown = "provider_unknown"


def _source_allowed(value: str) -> str:
    source = str(value or "").strip()
    if source not in _ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail=f"Invalid source. Allowed: {', '.join(_ALLOWED_SOURCES)}")
    return source


def _alias_key(normalized: str, sport_key: str | None, source: str) -> str:
    return f"{normalized}|{sport_key or '*'}|{source}"


def _default_alias_for_team(team: dict[str, Any]) -> dict[str, Any] | None:
    name = str((team or {}).get("name") or "").strip()
    if not name:
        return None
    normalized = normalize_team_alias(name)
    if not normalized:
        return None
    now = utcnow()
    source = "provider_unknown"
    return {
        "name": name,
        "normalized": normalized,
        "source": source,
        "sport_key": None,
        "alias_key": _alias_key(normalized, None, source),
        "is_default": True,
        "created_at": now,
        "updated_at": now,
    }


async def _ensure_default_alias(team_id: int, team: dict[str, Any]) -> None:
    aliases = list((team or {}).get("aliases") or [])
    if any(bool((row or {}).get("is_default")) for row in aliases):
        return
    alias = _default_alias_for_team(team)
    if not alias:
        return
    await _db.db.teams_v3.update_one(
        {"_id": int(team_id), "aliases.alias_key": {"$ne": alias["alias_key"]}},
        {"$addToSet": {"aliases": alias}, "$set": {"updated_at": utcnow()}},
    )


def _serialize_alias(alias: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str((alias or {}).get("name") or ""),
        "normalized": str((alias or {}).get("normalized") or ""),
        "source": str((alias or {}).get("source") or ""),
        "sport_key": (alias or {}).get("sport_key"),
        "alias_key": str((alias or {}).get("alias_key") or ""),
        "is_default": bool((alias or {}).get("is_default", False)),
        "created_at": ensure_utc((alias or {}).get("created_at")).isoformat() if (alias or {}).get("created_at") else None,
        "updated_at": ensure_utc((alias or {}).get("updated_at")).isoformat() if (alias or {}).get("updated_at") else None,
    }


def _serialize_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int((team or {}).get("_id")),
        "name": str((team or {}).get("name") or ""),
        "short_code": (team or {}).get("short_code"),
        "image_path": (team or {}).get("image_path"),
        "locked_fields": list(_LOCKED_FIELDS),
        "aliases": [_serialize_alias(a) for a in (team or {}).get("aliases") or []],
        "updated_at": ensure_utc((team or {}).get("updated_at")).isoformat() if (team or {}).get("updated_at") else None,
    }


class AliasInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: AliasSource
    sport_key: str | None = None

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("name is required")
        return name

    @field_validator("sport_key")
    @classmethod
    def _sport_trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None


class AliasDeleteInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: str | None = None
    sport_key: str | None = None
    dry_run: bool = False

    @field_validator("source")
    @classmethod
    def _source_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _source_allowed(value)


class SuggestionApplyItem(BaseModel):
    id: str
    team_id: int | None = None


class SuggestionApplyBody(BaseModel):
    items: list[SuggestionApplyItem]


class RejectSuggestionBody(BaseModel):
    reason: str | None = None


@router.get("")
async def list_teams_v3(
    search: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin=Depends(get_admin_user),
):
    _ = admin
    query: dict[str, Any] = {}
    if isinstance(search, str) and search.strip():
        escaped = re.escape(search.strip())
        normalized = normalize_team_alias(search.strip())
        ors: list[dict[str, Any]] = [
            {"name": {"$regex": escaped, "$options": "i"}},
            {"aliases.name": {"$regex": escaped, "$options": "i"}},
        ]
        if normalized:
            ors.append({"aliases.normalized": normalized})
        query["$or"] = ors

    total = int(await _db.db.teams_v3.count_documents(query))
    rows = (
        await _db.db.teams_v3.find(query)
        .sort("updated_at", -1)
        .skip(int(offset))
        .limit(int(limit))
        .to_list(length=int(limit))
    )
    for row in rows:
        team_id = int(row.get("_id"))
        await _ensure_default_alias(team_id, row)
        if not row.get("aliases"):
            row["aliases"] = [_default_alias_for_team(row)] if _default_alias_for_team(row) else []

    return {
        "total": total,
        "offset": int(offset),
        "limit": int(limit),
        "items": [_serialize_team(r) for r in rows],
    }


async def _compute_alias_impact(team_id: int, alias_keys: list[str]) -> dict[str, Any]:
    since = utcnow() - timedelta(days=30)
    if not alias_keys:
        return {"usage_30d": 0, "last_seen_at": None}
    q = {
        "team_id": int(team_id),
        "alias_key": {"$in": alias_keys},
        "resolved_at": {"$gte": since},
    }
    usage = int(await _db.db[_EVENT_COLL].count_documents(q))
    last = await _db.db[_EVENT_COLL].find_one(
        q,
        {"resolved_at": 1},
        sort=[("resolved_at", -1)],
    )
    last_seen = (
        ensure_utc(last.get("resolved_at")).isoformat()
        if isinstance(last, dict) and last.get("resolved_at") is not None
        else None
    )
    return {"usage_30d": usage, "last_seen_at": last_seen}


def _orphan_warning(team: dict[str, Any], aliases_to_remove: list[dict[str, Any]]) -> dict[str, Any] | None:
    remaining = [a for a in (team.get("aliases") or []) if a not in aliases_to_remove]
    remove_sources = {str((a or {}).get("source") or "") for a in aliases_to_remove if str((a or {}).get("source") or "")}
    affected: list[str] = []
    for src in remove_sources:
        if not any(str((a or {}).get("source") or "") == src for a in remaining):
            affected.append(src)
    if not affected:
        return None
    return {
        "code": "orphan_risk",
        "affected_sources": sorted(set(affected)),
        "message": "Deleting this alias removes the last mapping for one or more sources.",
    }


@router.post("/{team_id}/aliases")
async def add_alias_v3(
    team_id: int,
    body: AliasInput,
    admin=Depends(get_admin_user),
):
    _ = admin
    team = await _db.db.teams_v3.find_one({"_id": int(team_id)})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")
    source = _source_allowed(body.source.value if isinstance(body.source, AliasSource) else str(body.source))
    normalized = normalize_team_alias(body.name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Alias normalization is empty.")
    now = utcnow()
    key = _alias_key(normalized, body.sport_key, source)
    alias = {
        "name": body.name.strip(),
        "normalized": normalized,
        "source": source,
        "sport_key": body.sport_key,
        "alias_key": key,
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.teams_v3.update_one(
        {"_id": int(team_id), "aliases.alias_key": {"$ne": key}},
        {"$addToSet": {"aliases": alias}, "$set": {"updated_at": now}},
    )
    return {"team_id": int(team_id), "inserted": bool(result.modified_count > 0), "alias_key": key}


async def _find_alias_candidates(team: dict[str, Any], normalized: str, source: str | None, sport_key: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for alias in (team.get("aliases") or []):
        if str((alias or {}).get("normalized") or "") != normalized:
            continue
        alias_source = str((alias or {}).get("source") or "")
        alias_sport = (alias or {}).get("sport_key")
        if source is not None and alias_source != source:
            continue
        if sport_key is not None and str(alias_sport or "") != str(sport_key or ""):
            continue
        out.append(alias)
    return out


@router.post("/{team_id}/aliases/impact")
async def alias_impact_v3(
    team_id: int,
    body: AliasDeleteInput,
    admin=Depends(get_admin_user),
):
    _ = admin
    team = await _db.db.teams_v3.find_one({"_id": int(team_id)}, {"name": 1, "aliases": 1})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")
    normalized = normalize_team_alias(body.name)
    candidates = await _find_alias_candidates(team, normalized, body.source, body.sport_key)
    keys = [str((a or {}).get("alias_key") or "") for a in candidates if str((a or {}).get("alias_key") or "")]
    impact = await _compute_alias_impact(int(team_id), keys)
    warning = _orphan_warning(team, candidates)
    return {"usage_30d": impact["usage_30d"], "last_seen_at": impact["last_seen_at"], "orphan_risk": bool(warning), "affected_sources": (warning or {}).get("affected_sources", [])}


@router.delete("/{team_id}/aliases")
async def delete_alias_v3(
    team_id: int,
    body: AliasDeleteInput,
    admin=Depends(get_admin_user),
):
    _ = admin
    team = await _db.db.teams_v3.find_one({"_id": int(team_id)}, {"name": 1, "aliases": 1})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found.")
    normalized = normalize_team_alias(body.name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Alias normalization is empty.")
    candidates = await _find_alias_candidates(team, normalized, body.source, body.sport_key)
    if not candidates:
        return {"removed": False, "impact": {"usage_30d": 0, "last_seen_at": None}}

    canonical_norm = normalize_team_alias(str(team.get("name") or ""))
    for alias in candidates:
        if bool((alias or {}).get("is_default")) or str((alias or {}).get("normalized") or "") == canonical_norm:
            return {
                "removed": False,
                "blocked": {
                    "code": "canonical_alias_protected",
                    "message": "Canonical/default alias cannot be deleted.",
                },
            }

    keys = [str((a or {}).get("alias_key") or "") for a in candidates if str((a or {}).get("alias_key") or "")]
    impact = await _compute_alias_impact(int(team_id), keys)
    warning = _orphan_warning(team, candidates)
    if bool(body.dry_run):
        return {"removed": False, "impact": impact, "warning": warning}

    result = await _db.db.teams_v3.update_one(
        {"_id": int(team_id)},
        {"$pull": {"aliases": {"alias_key": {"$in": keys}}}, "$set": {"updated_at": utcnow()}},
    )
    return {"removed": bool(result.modified_count > 0), "impact": impact, "warning": warning}


@router.get("/alias-suggestions")
async def list_alias_suggestions_v3(
    status: str = Query("pending"),
    source: str | None = Query(None),
    sport_key: str | None = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    q: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    admin=Depends(get_admin_user),
):
    _ = admin
    query: dict[str, Any] = {"status": str(status or "pending").strip().lower() or "pending"}
    if source:
        query["source"] = _source_allowed(source)
    if sport_key:
        query["sport_key"] = str(sport_key).strip()
    if min_confidence > 0.0:
        query["confidence_score"] = {"$gte": float(min_confidence)}
    if q and q.strip():
        escaped = re.escape(q.strip())
        query["$or"] = [
            {"raw_team_name": {"$regex": escaped, "$options": "i"}},
            {"normalized_name": {"$regex": escaped, "$options": "i"}},
        ]
    rows = (
        await _db.db[_SUGGESTION_COLL]
        .find(query)
        .sort([("confidence_score", -1), ("last_seen_at", -1)])
        .limit(int(limit))
        .to_list(length=int(limit))
    )
    team_ids = [int(r["candidate_team_id"]) for r in rows if isinstance(r.get("candidate_team_id"), int)]
    teams = await _db.db.teams_v3.find({"_id": {"$in": team_ids}}, {"name": 1}).to_list(length=max(len(team_ids), 1))
    team_map = {int(t["_id"]): str(t.get("name") or "") for t in teams}
    items: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = row.get("candidate_team_id")
        items.append(
            {
                "id": str(row.get("_id")),
                "status": str(row.get("status") or "pending"),
                "source": str(row.get("source") or ""),
                "sport_key": row.get("sport_key"),
                "raw_team_name": str(row.get("raw_team_name") or ""),
                "normalized_name": str(row.get("normalized_name") or ""),
                "reason": str(row.get("reason") or "unresolved_team"),
                "confidence_score": float(row.get("confidence_score") or 0.0),
                "match_basis": str(row.get("match_basis") or "name"),
                "scoring_version": str(row.get("scoring_version") or "v1"),
                "seen_count": int(row.get("seen_count") or 0),
                "first_seen_at": ensure_utc(row.get("first_seen_at")).isoformat() if row.get("first_seen_at") else None,
                "last_seen_at": ensure_utc(row.get("last_seen_at")).isoformat() if row.get("last_seen_at") else None,
                "suggested_team_id": int(candidate_id) if isinstance(candidate_id, int) else None,
                "suggested_team_name": team_map.get(int(candidate_id), "") if isinstance(candidate_id, int) else "",
                "sample_refs": row.get("sample_refs") or [],
            }
        )
    return {"total": len(items), "items": items}


@router.post("/alias-suggestions/apply")
async def apply_alias_suggestions_v3(
    body: SuggestionApplyBody,
    admin=Depends(get_admin_user),
):
    _ = admin
    if not body.items:
        raise HTTPException(status_code=400, detail="Provide at least one suggestion id.")
    applied = 0
    failed: list[dict[str, Any]] = []
    for item in body.items:
        doc = await _db.db[_SUGGESTION_COLL].find_one({"_id": item.id} if isinstance(item.id, int) else {"_id": item.id})
        if not doc:
            # try ObjectId fallback
            from bson import ObjectId

            try:
                doc = await _db.db[_SUGGESTION_COLL].find_one({"_id": ObjectId(item.id)})
            except Exception:
                doc = None
        if not doc:
            failed.append({"id": item.id, "code": "not_found", "message": "Suggestion not found"})
            continue
        if str(doc.get("status") or "pending") != "pending":
            failed.append({"id": item.id, "code": "invalid_status", "message": "Suggestion is not pending"})
            continue
        team_id = int(item.team_id) if item.team_id is not None else doc.get("candidate_team_id")
        if not isinstance(team_id, int):
            failed.append({"id": item.id, "code": "missing_target", "message": "No target team_id provided"})
            continue
        source = _source_allowed(str(doc.get("source") or "provider_unknown"))
        name = str(doc.get("raw_team_name") or "").strip()
        normalized = normalize_team_alias(name)
        if not normalized:
            failed.append({"id": item.id, "code": "invalid_alias", "message": "Suggestion has empty alias"})
            continue
        key = _alias_key(normalized, doc.get("sport_key"), source)
        now = utcnow()
        alias = {
            "name": name,
            "normalized": normalized,
            "source": source,
            "sport_key": doc.get("sport_key"),
            "alias_key": key,
            "is_default": False,
            "created_at": now,
            "updated_at": now,
        }
        await _db.db.teams_v3.update_one(
            {"_id": int(team_id), "aliases.alias_key": {"$ne": key}},
            {"$addToSet": {"aliases": alias}, "$set": {"updated_at": now}},
        )
        await _db.db[_SUGGESTION_COLL].update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "applied", "applied_at": now, "applied_to_team_id": int(team_id), "updated_at": now}},
        )
        applied += 1
    return {"applied": applied, "failed": failed}


@router.post("/alias-suggestions/{suggestion_id}/reject")
async def reject_alias_suggestion_v3(
    suggestion_id: str,
    body: RejectSuggestionBody,
    admin=Depends(get_admin_user),
):
    _ = admin
    from bson import ObjectId

    doc = None
    try:
        doc = await _db.db[_SUGGESTION_COLL].find_one({"_id": ObjectId(suggestion_id)})
    except Exception:
        doc = await _db.db[_SUGGESTION_COLL].find_one({"_id": suggestion_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    if str(doc.get("status") or "pending") != "pending":
        raise HTTPException(status_code=400, detail="Suggestion is not pending.")
    now = utcnow()
    await _db.db[_SUGGESTION_COLL].update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "rejected", "rejected_reason": str(body.reason or "").strip() or None, "rejected_at": now, "updated_at": now}},
    )
    return {"ok": True, "id": str(doc["_id"])}

