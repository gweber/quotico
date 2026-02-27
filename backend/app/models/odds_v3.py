"""
backend/app/models/odds_v3.py

Purpose:
    Canonical v3 odds pipeline model: raw API response cache document.
    Phase 2 will add OddsTimelineV3 and OddsMetaV3 for the transformer.

Dependencies:
    - pydantic
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OddsRawV3(BaseModel):
    """Raw Sportmonks API odds response, cached for deferred transformation."""

    fixture_id: int
    fetched_at: datetime
    source: str = "sportmonks"
    raw_response: dict[str, Any] = Field(default_factory=dict)
    transformed_at: datetime | None = None
