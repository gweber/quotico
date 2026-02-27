"""
backend/app/services/event_models.py

Purpose:
    Domain event contracts for in-process reactive workflows. Defines a compact
    ID-first payload set to decouple publishers from subscribers.

Dependencies:
    - pydantic
    - app.utils
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.utils import ensure_utc, utcnow

EventType = Literal[
    "match.created",
    "match.updated",
    "match.finalized",
    "match.postponed",
    "match.cancelled",
    "odds.ingested",
    "odds.raw_ingested",
    "matchday.started",
    "matchday.completed",
    "metrics.updated",
]


def make_event_id() -> str:
    return str(uuid.uuid4())


def make_correlation_id() -> str:
    return str(uuid.uuid4())


class BaseEvent(BaseModel):
    event_id: str = Field(default_factory=make_event_id)
    event_type: EventType
    occurred_at: datetime = Field(default_factory=utcnow)
    correlation_id: str = Field(default_factory=make_correlation_id)
    source: str


class MatchCreatedEvent(BaseEvent):
    event_type: Literal["match.created"] = "match.created"
    match_id: int
    league_id: int
    season: int
    status: str
    ingest_source: str
    external_id: str


class MatchUpdatedEvent(BaseEvent):
    event_type: Literal["match.updated"] = "match.updated"
    match_id: int
    league_id: int
    season: int
    previous_status: str | None = None
    new_status: str
    ingest_source: str
    external_id: str
    changed_fields: list[str] = Field(default_factory=list)


class MatchFinalizedEvent(BaseEvent):
    event_type: Literal["match.finalized"] = "match.finalized"
    match_id: int
    league_id: int
    season: int
    final_score: dict[str, int | None] = Field(default_factory=dict)


class MatchPostponedEvent(BaseEvent):
    event_type: Literal["match.postponed"] = "match.postponed"
    match_id: int
    league_id: int
    season: int


class MatchCancelledEvent(BaseEvent):
    event_type: Literal["match.cancelled"] = "match.cancelled"
    match_id: int
    league_id: int
    season: int


class OddsIngestedEvent(BaseEvent):
    event_type: Literal["odds.ingested"] = "odds.ingested"
    provider: str
    league_id: int | None = None
    match_ids: list[int] = Field(default_factory=list)
    inserted: int = 0
    deduplicated: int = 0
    markets_updated: int = 0


class OddsRawIngestedEvent(BaseEvent):
    event_type: Literal["odds.raw_ingested"] = "odds.raw_ingested"
    provider: str
    fixture_ids: list[int] = Field(default_factory=list)
    source_method: str = ""  # "sync_fixture" | "sync_round"
    cached_count: int = 0


class MetricsUpdatedEvent(BaseEvent):
    event_type: Literal["metrics.updated"] = "metrics.updated"
    tick_type: str
    matches_synced: int = 0


def normalize_event_time(event: BaseEvent) -> BaseEvent:
    event.occurred_at = ensure_utc(event.occurred_at)
    return event
