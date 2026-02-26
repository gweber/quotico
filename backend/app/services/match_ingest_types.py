"""
backend/app/services/match_ingest_types.py

Purpose:
    Shared type contracts for the unified match ingest pipeline. Defines the
    normalized provider payload format and structured ingest result counters.

Dependencies:
    - typing
    - bson.ObjectId
    - datetime
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict

from bson import ObjectId


MatchIngestSource = Literal["football_data", "openligadb", "football_data_uk", "theoddsapi"]


class TeamRef(TypedDict):
    external_id: str | None
    name: str


class MatchData(TypedDict):
    external_id: str
    source: MatchIngestSource
    league_external_id: str
    season: int
    sport_key: str
    match_date: datetime
    home_team: TeamRef
    away_team: TeamRef
    status: str | None
    matchday: int | None
    score: dict[str, Any] | None
    metadata: dict[str, Any]
    correlation_id: NotRequired[str]


class IngestConflict(TypedDict):
    code: Literal["unresolved_league", "unresolved_team", "team_name_conflict", "other_conflict"]
    source: str
    external_id: str
    message: str
    detail: dict[str, Any]


class IngestPreviewItem(TypedDict):
    action: Literal["create", "update", "skip"]
    source: str
    external_id: str
    match_id: str | None
    code: str | None


class MatchIngestContext(TypedDict):
    source: str
    external_id: str
    league_id: ObjectId
    season: int
    match_data: MatchData


class IngestResult(TypedDict):
    processed: int
    created: int
    updated: int
    skipped: int
    conflicts: int
    unresolved_league: int
    unresolved_team: int
    team_name_conflict: int
    other_conflicts: int
    matched_by_external_id: int
    matched_by_identity_window: int
    dry_run: bool
    items_preview: list[IngestPreviewItem]
    conflicts_preview: list[IngestConflict]
