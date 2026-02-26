"""
backend/app/providers/football_data_uk.py

Purpose:
    Adapter for football-data.co.uk CSV downloads via the shared resilient
    HTTP client, used by admin-triggered historical stats imports.

Dependencies:
    - app.providers.http_client.ResilientClient
    - app.services.provider_settings_service
    - app.services.provider_rate_limiter
"""

from app.providers.http_client import ResilientClient
from app.services.provider_rate_limiter import provider_rate_limiter
from app.services.provider_settings_service import provider_settings_service

PROVIDER_NAME = "football_data_uk"


class FootballDataCoUkProvider:
    """Provider wrapper for football-data.co.uk CSV files."""

    def __init__(self):
        self._client = ResilientClient("football_data_co_uk", timeout=30.0, max_retries=3, base_delay=5.0)
        self._runtime_fingerprint: tuple[float, int, float] | None = None

    async def _runtime(self) -> dict:
        payload = await provider_settings_service.get_effective(
            PROVIDER_NAME,
            include_secret=False,
        )
        effective = dict(payload.get("effective_config") or {})
        timeout = float(effective.get("timeout_seconds") or 30.0)
        retries = int(effective.get("max_retries") or 3)
        base_delay = float(effective.get("base_delay_seconds") or 5.0)
        fp = (timeout, retries, base_delay)
        if fp != self._runtime_fingerprint:
            if hasattr(self._client, "_max_retries"):
                self._client._max_retries = retries
            if hasattr(self._client, "_base_delay"):
                self._client._base_delay = base_delay
            if hasattr(self._client, "_client"):
                self._client._client.timeout = timeout
            self._runtime_fingerprint = fp
        return effective

    async def fetch_season_csv(self, season_code: str, division_code: str) -> str:
        """Fetch a season CSV and return its text content."""
        season = (season_code or "").strip()
        division = (division_code or "").strip().upper()
        if not season or not division:
            raise ValueError("season_code and division_code are required.")

        runtime = await self._runtime()
        if not bool(runtime.get("enabled", True)):
            raise ValueError("football_data_uk provider is disabled.")
        base_url = str(runtime.get("base_url") or "")
        if not base_url:
            raise ValueError("football_data_uk base_url is missing.")
        await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
        url = f"{base_url}/{season}/{division}.csv"
        response = await self._client.get(url)
        response.raise_for_status()
        return response.text


football_data_uk_provider = FootballDataCoUkProvider()
