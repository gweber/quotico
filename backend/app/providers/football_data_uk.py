"""
backend/app/providers/football_data_uk.py

Purpose:
    Adapter for football-data.co.uk CSV downloads via the shared resilient
    HTTP client, used by admin-triggered historical stats imports.

Dependencies:
    - app.providers.http_client.ResilientClient
"""

from app.providers.http_client import ResilientClient

BASE_URL = "https://www.football-data.co.uk/mmz4281"


class FootballDataCoUkProvider:
    """Provider wrapper for football-data.co.uk CSV files."""

    def __init__(self):
        self._client = ResilientClient("football_data_co_uk", timeout=30.0, max_retries=3, base_delay=5.0)

    async def fetch_season_csv(self, season_code: str, division_code: str) -> str:
        """Fetch a season CSV and return its text content."""
        season = (season_code or "").strip()
        division = (division_code or "").strip().upper()
        if not season or not division:
            raise ValueError("season_code and division_code are required.")

        url = f"{BASE_URL}/{season}/{division}.csv"
        response = await self._client.get(url)
        response.raise_for_status()
        return response.text


football_data_uk_provider = FootballDataCoUkProvider()
