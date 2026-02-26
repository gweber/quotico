"""
backend/tests/test_football_data_uk_season_normalization.py

Purpose:
    Validate football-data.co.uk season input normalization.

Dependencies:
    - app.services.football_data_service
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.football_data_service import _normalize_football_data_uk_season_code


def test_season_start_year_is_converted_to_code() -> None:
    assert _normalize_football_data_uk_season_code("2025", default_start_year=2024) == "2526"


def test_season_code_is_kept() -> None:
    assert _normalize_football_data_uk_season_code("2425", default_start_year=2024) == "2425"


def test_invalid_season_raises() -> None:
    with pytest.raises(HTTPException):
        _normalize_football_data_uk_season_code("25/26", default_start_year=2024)

