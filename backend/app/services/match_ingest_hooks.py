"""
backend/app/services/match_ingest_hooks.py

Purpose:
    Hook protocol for side effects after unified match ingest create/update
    events and batch completion.

Dependencies:
    - typing
    - app.services.match_ingest_types
"""

from __future__ import annotations

from typing import Any, Protocol

from app.services.match_ingest_types import IngestResult, MatchIngestContext


class MatchIngestHooks(Protocol):
    async def on_match_created(self, match_doc: dict[str, Any], context: MatchIngestContext) -> None:
        ...

    async def on_match_updated(self, match_doc: dict[str, Any], context: MatchIngestContext) -> None:
        ...

    async def on_batch_completed(self, result: IngestResult, context: dict[str, Any]) -> None:
        ...
