"""
backend/app/providers/sportmonks.py

Purpose:
    Sportmonks v3 provider wrapper for league discovery and season ingest
    endpoints with rate-limit header capture.

Dependencies:
    - app.config
    - app.providers.http_client
"""

from __future__ import annotations

import time
from typing import Any

from app.config import settings
from app.providers.http_client import ResilientClient


class SportmonksProvider:
    """HTTP adapter for Sportmonks API with rate-limit header extraction."""

    def __init__(self) -> None:
        self._client = ResilientClient("sportmonks", timeout=90.0, max_retries=3, base_delay=2.0)

    def _build_url(self, path: str) -> str:
        base = str(settings.SPORTMONKS_BASE_URL or "").rstrip("/")
        suffix = str(path or "").lstrip("/")
        if not base:
            raise ValueError("SPORTMONKS_BASE_URL is missing.")
        return f"{base}/{suffix}"

    def _auth_token(self) -> str:
        api_key = str(settings.SM_API_KEY or "").strip()
        if not api_key:
            raise ValueError("SM_API_KEY is missing.")
        return api_key

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _extract_rate_limit(self, *, payload: dict[str, Any], headers: Any) -> tuple[int | None, int | None]:
        remaining = self._to_int(headers.get("X-RateLimit-Remaining"))
        reset_at = self._to_int(headers.get("X-RateLimit-Reset"))
        rate_limit = (payload or {}).get("rate_limit") or {}
        if remaining is None:
            remaining = self._to_int(rate_limit.get("remaining"))
        if reset_at is None:
            reset_in = self._to_int(rate_limit.get("resets_in_seconds"))
            if reset_in is not None:
                reset_at = int(time.time()) + max(0, int(reset_in))
        return remaining, reset_at

    async def _get(self, path_or_url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query: dict[str, Any] = dict(params or {})
        if str(path_or_url).startswith("http://") or str(path_or_url).startswith("https://"):
            url = str(path_or_url)
        else:
            url = self._build_url(path_or_url)
        response = await self._client.get(
            url,
            params=query,
            headers={"Authorization": self._auth_token()},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
        remaining, reset_at = self._extract_rate_limit(payload=payload, headers=response.headers)
        return {
            "payload": payload,
            "remaining": remaining,
            "reset_at": reset_at,
        }

    async def _get_paginated(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        next_url: str | None = None
        has_more = True
        remaining: int | None = None
        reset_at: int | None = None
        page_guard = 0
        while has_more:
            page_guard += 1
            if page_guard > 500:
                break
            response = await self._get(next_url or path, params=None if next_url else params)
            payload = response.get("payload") or {}
            rows = payload.get("data") or []
            if isinstance(rows, list):
                merged.extend(rows)
            remaining = response.get("remaining")
            reset_at = response.get("reset_at")
            pagination = payload.get("pagination") or {}
            has_more = bool(pagination.get("has_more"))
            next_page = pagination.get("next_page")
            next_url = str(next_page).strip() if has_more and next_page else None
            if has_more and not next_url:
                break
        return {
            "payload": {"data": merged},
            "remaining": remaining,
            "reset_at": reset_at,
        }

    async def get_leagues_with_seasons_country(self) -> dict[str, Any]:
        return await self._get_paginated("football/leagues", params={"include": "seasons;country"})

    async def get_season_rounds(self, season_id: int) -> dict[str, Any]:
        return await self._get(f"football/rounds/seasons/{int(season_id)}")

    async def get_expected_fixtures_by_season(self, season_id: int) -> dict[str, Any]:
        return await self._get_paginated(
            "football/expected/fixtures",
            params={
                "filters": f"fixtureSeasons:{int(season_id)}",
                "per_page": 100,
            },
        )

    async def get_expected_fixtures_page(
        self,
        *,
        season_id: int,
        next_page_url: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one expected-fixtures page for fine-grained progress updates."""
        return await self._get(
            next_page_url or "football/expected/fixtures",
            params=(
                None
                if next_page_url
                else {"filters": f"fixtureSeasons:{int(season_id)}", "per_page": 100}
            ),
        )

    async def get_prematch_odds_by_fixture(self, fixture_id: int) -> dict[str, Any]:
        return await self._get(f"football/odds/pre-match/fixtures/{int(fixture_id)}")

    async def get_round_fixtures(self, round_id: int) -> dict[str, Any]:
        response = await self._get(
            f"football/rounds/{int(round_id)}",
            params={
                "include": (
                    "fixtures;"
                    "fixtures.participants;"
                    "fixtures.referees;"
                    "fixtures.referees.referee;"
                    "fixtures.events;"
                    "fixtures.statistics;"
                    "fixtures.lineups;"
                    "fixtures.lineups.player;"
                    "fixtures.odds"
                ),
            },
        )
        data = (response.get("payload") or {}).get("data") or {}
        fixtures = (data or {}).get("fixtures") or []
        response["payload"] = {"data": fixtures}
        return response


sportmonks_provider = SportmonksProvider()
