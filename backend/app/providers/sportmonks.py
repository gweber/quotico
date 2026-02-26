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

import hashlib
import json
import logging
import time
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit
from typing import Any

import app.database as _db
from app.config import settings
from app.providers.http_client import ResilientClient
from app.utils import ensure_utc, utcnow

logger = logging.getLogger("quotico.sportmonks")


class SportmonksProvider:
    """HTTP adapter for Sportmonks API with rate-limit header extraction."""

    def __init__(self) -> None:
        self._client = ResilientClient("sportmonks", timeout=90.0, max_retries=3, base_delay=2.0)
        self._cache_metrics_meta_id = "sportmonks_page_cache_metrics"

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
        base = str(settings.SPORTMONKS_BASE_URL or "").rstrip("/")
        if str(path_or_url).startswith("http://") or str(path_or_url).startswith("https://"):
            url = str(path_or_url)
            endpoint = url[len(base) + 1 :] if base and url.startswith(f"{base}/") else url
        else:
            url = self._build_url(path_or_url)
            endpoint = str(path_or_url).lstrip("/")
        log_endpoint = self._format_endpoint_for_log(endpoint=endpoint, query=query if query else None)
        db = _db.db
        cache_result: dict[str, Any] | None = None
        cache_enabled = bool(settings.SPORTMONKS_PAGE_CACHE_ENABLED)
        endpoint_path, merged_query = self._split_endpoint_and_query(endpoint=endpoint, query=query)
        cache_params = self._normalize_cache_params(merged_query)
        cache_allowed = cache_enabled and self._cache_allowed(endpoint=endpoint_path, params_norm=cache_params)
        if cache_allowed and db is not None:
            cache_result = await self._cache_read(endpoint=endpoint_path, params_norm=cache_params)
            if isinstance(cache_result, dict):
                logger.info("Cache HIT for /%s", log_endpoint)
                await self._cache_metrics_inc(hits=1, misses=0)
                remaining, reset_at = self._extract_rate_limit(
                    payload=cache_result.get("payload") or {},
                    headers={},
                )
                return {
                    "payload": cache_result.get("payload") or {},
                    "remaining": remaining,
                    "reset_at": reset_at,
                    "from_cache": True,
                }
            logger.info("Cache MISS for /%s", log_endpoint)
            await self._cache_metrics_inc(hits=0, misses=1)
        logger.info("Sportmonks API call: GET /%s", log_endpoint)
        response = await self._client.get(
            url,
            params=query,
            headers={"Authorization": self._auth_token()},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
        remaining, reset_at = self._extract_rate_limit(payload=payload, headers=response.headers)
        if cache_allowed and db is not None and int(getattr(response, "status_code", 0) or 0) == 200:
            await self._cache_write(
                endpoint=endpoint_path,
                params_norm=cache_params,
                status_code=200,
                payload=payload if isinstance(payload, dict) else {},
            )
        return {
            "payload": payload,
            "remaining": remaining,
            "reset_at": reset_at,
            "from_cache": False,
        }

    @staticmethod
    def _split_endpoint_and_query(*, endpoint: str, query: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        split = urlsplit(endpoint if endpoint.startswith("http") else f"https://_/{endpoint.lstrip('/')}")
        endpoint_path = split.path.lstrip("/")
        merged_query = {key: value for key, value in parse_qsl(split.query, keep_blank_values=False)}
        merged_query.update(query or {})
        return endpoint_path, merged_query

    @staticmethod
    def _normalize_cache_params(params: dict[str, Any]) -> dict[str, str]:
        allowed = ("include", "filters", "page", "per_page", "order")
        normalized: dict[str, str] = {}
        for key in allowed:
            value = params.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            normalized[key] = text
        return dict(sorted(normalized.items(), key=lambda kv: kv[0]))

    @staticmethod
    def _cache_allowed(*, endpoint: str, params_norm: dict[str, str]) -> bool:
        path = str(endpoint or "").lstrip("/")
        if path.startswith("football/odds/pre-match/fixtures/"):
            return False
        if path.startswith("football/leagues"):
            return True
        if path.startswith("football/expected/fixtures"):
            return True
        if path.startswith("football/rounds/"):
            include = str(params_norm.get("include") or "")
            return "fixtures.odds" not in include
        return False

    @staticmethod
    def _cache_doc_id(*, endpoint: str, params_norm: dict[str, str]) -> str:
        material = {
            "method": "GET",
            "endpoint": str(endpoint or "").lstrip("/"),
            "params": params_norm,
        }
        payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _cache_read(self, *, endpoint: str, params_norm: dict[str, str]) -> dict[str, Any] | None:
        db = _db.db
        if db is None:
            return None
        doc = await db.sportmonks_page_cache.find_one({"_id": self._cache_doc_id(endpoint=endpoint, params_norm=params_norm)})
        if not isinstance(doc, dict):
            return None
        expires_at = doc.get("expires_at")
        if expires_at is None:
            return None
        if ensure_utc(expires_at) <= utcnow():
            return None
        return {"payload": doc.get("payload") or {}}

    async def _cache_write(
        self,
        *,
        endpoint: str,
        params_norm: dict[str, str],
        status_code: int,
        payload: dict[str, Any],
    ) -> None:
        db = _db.db
        if db is None or int(status_code) != 200:
            return
        now = utcnow()
        ttl_minutes = max(1, int(settings.SPORTMONKS_PAGE_CACHE_TTL_MINUTES))
        await db.sportmonks_page_cache.update_one(
            {"_id": self._cache_doc_id(endpoint=endpoint, params_norm=params_norm)},
            {
                "$set": {
                    "endpoint": endpoint,
                    "params_norm": params_norm,
                    "status_code": int(status_code),
                    "payload": payload,
                    "expires_at": now + timedelta(minutes=ttl_minutes),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def _cache_metrics_inc(self, *, hits: int, misses: int) -> None:
        db = _db.db
        if db is None:
            return
        await db.meta.update_one(
            {"_id": self._cache_metrics_meta_id},
            {
                "$inc": {"hits": int(hits), "misses": int(misses)},
                "$set": {"updated_at": utcnow()},
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )

    @staticmethod
    def _format_endpoint_for_log(*, endpoint: str, query: dict[str, Any] | None) -> str:
        """Log endpoint with a safe query subset (never auth/secrets)."""
        safe_keys = {"filters", "page", "per_page", "order", "include"}
        split = urlsplit(endpoint if endpoint.startswith("http") else f"https://_/{endpoint.lstrip('/')}")
        path = split.path.lstrip("/")
        merged_items: list[tuple[str, str]] = []
        for key, value in parse_qsl(split.query, keep_blank_values=False):
            if key in safe_keys:
                merged_items.append((key, value))
        for key, value in (query or {}).items():
            if key not in safe_keys:
                continue
            if value is None:
                continue
            merged_items.append((key, str(value)))
        if not merged_items:
            return path
        return f"{path}?{urlencode(merged_items)}"

    async def _get_paginated(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        has_more = True
        remaining: int | None = None
        reset_at: int | None = None
        page_guard = 0
        base_params: dict[str, Any] = dict(params or {})
        current_page = self._to_int(base_params.get("page")) or 1
        seen_pages: set[int] = set()
        while has_more:
            page_guard += 1
            if page_guard > 500:
                logger.warning("Sportmonks pagination guard hit for %s", path)
                break
            if current_page in seen_pages:
                logger.warning(
                    "Sportmonks pagination repeated page=%s for %s; stopping to prevent loop",
                    current_page,
                    path,
                )
                break
            seen_pages.add(current_page)
            request_params = dict(base_params)
            request_params["page"] = current_page
            response = await self._get(path, params=request_params)
            payload = response.get("payload") or {}
            rows = payload.get("data") or []
            if isinstance(rows, list):
                merged.extend(rows)
            remaining = response.get("remaining")
            reset_at = response.get("reset_at")
            pagination = payload.get("pagination") or {}
            has_more = bool(pagination.get("has_more"))
            if not has_more:
                break
            next_page = str(pagination.get("next_page") or "").strip()
            next_page_num = self._extract_page_number(next_page)
            if next_page_num is None:
                next_page_num = current_page + 1
            if next_page_num <= current_page:
                next_page_num = current_page + 1
            current_page = next_page_num
        return {
            "payload": {"data": merged},
            "remaining": remaining,
            "reset_at": reset_at,
        }

    @staticmethod
    def _extract_page_number(next_page_url: str | None) -> int | None:
        if not next_page_url:
            return None
        try:
            query = dict(parse_qsl(urlsplit(next_page_url).query, keep_blank_values=False))
            value = query.get("page")
            return int(value) if value is not None else None
        except Exception:
            return None

    async def get_leagues_with_seasons_country(self) -> dict[str, Any]:
        return await self._get_paginated("football/leagues", params={"include": "seasons;country"})

    async def get_season_rounds(self, season_id: int) -> dict[str, Any]:
        return await self._get(f"football/rounds/seasons/{int(season_id)}")

    async def get_expected_fixtures_page(self, *, page: int = 1) -> dict[str, Any]:
        """Fetch one page of expected-fixtures (xG), ordered newest-first.

        Note: this endpoint returns xG for ALL seasons (fixtureSeasons filter
        is not supported). Client-side filtering by season fixture IDs is required.
        The API also ignores per_page and always returns 25 rows.
        """
        return await self._get(
            "football/expected/fixtures",
            params={"order": "desc", "page": int(page)},
        )

    async def get_prematch_odds_by_fixture(self, fixture_id: int) -> dict[str, Any]:
        return await self._get(f"football/odds/pre-match/fixtures/{int(fixture_id)}")

    async def get_round_fixtures(self, round_id: int, *, include_odds: bool = True) -> dict[str, Any]:
        include_parts = [
            "fixtures",
            "fixtures.state",
            "fixtures.scores",
            "fixtures.participants",
            "fixtures.referees",
            "fixtures.referees.referee",
            "fixtures.events",
            "fixtures.statistics",
            "fixtures.lineups",
            "fixtures.lineups.player",
        ]
        if include_odds:
            include_parts.append("fixtures.odds")
        response = await self._get(
            f"football/rounds/{int(round_id)}",
            params={
                "include": ";".join(include_parts),
            },
        )
        data = (response.get("payload") or {}).get("data") or {}
        fixtures = (data or {}).get("fixtures") or []
        response["payload"] = {"data": fixtures}
        return response


sportmonks_provider = SportmonksProvider()
