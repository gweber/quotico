"""
backend/app/services/match_ingest_adapters/base.py

Purpose:
    Adapter interface for provider-specific match payload transformation into
    the unified MatchData contract used by MatchIngestService.

Dependencies:
    - typing
    - app.services.match_ingest_types
"""

from __future__ import annotations

from typing import Protocol

from app.services.match_ingest_types import MatchData


class MatchIngestAdapter(Protocol):
    async def build_matches(self, *args, **kwargs) -> tuple[list[MatchData], int]:
        """Return transformed match payloads and skipped season mismatch count."""
        ...
