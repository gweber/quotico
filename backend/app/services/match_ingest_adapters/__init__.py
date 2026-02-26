"""
backend/app/services/match_ingest_adapters/__init__.py

Purpose:
    Adapter exports for unified match ingest transformations.
"""

from app.services.match_ingest_adapters.football_data_org_adapter import build_football_data_org_matches
from app.services.match_ingest_adapters.openligadb_adapter import build_openligadb_matches
from app.services.match_ingest_adapters.football_data_uk_adapter import (
    build_football_data_uk_external_id,
    build_football_data_uk_match_data,
)

__all__ = [
    "build_football_data_org_matches",
    "build_openligadb_matches",
    "build_football_data_uk_external_id",
    "build_football_data_uk_match_data",
]
