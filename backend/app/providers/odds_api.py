import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import settings
from app.providers.base import BaseProvider

logger = logging.getLogger("quotico.odds_api")

BASE_URL = "https://api.the-odds-api.com/v4"

# Sport key mapping: TheOddsAPI sport keys we support
SUPPORTED_SPORTS = [
    "soccer_germany_bundesliga",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_uefa_champs_league",
    "americanfootball_nfl",
    "basketball_nba",
    "tennis_atp_french_open",
]

# 3-way sports (Win/Draw/Loss)
THREE_WAY_SPORTS = {
    "soccer_germany_bundesliga",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_uefa_champs_league",
}


class CircuitBreaker:
    """Simple circuit breaker for external API calls."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.is_open = False

    def record_success(self) -> None:
        self.failure_count = 0
        self.is_open = False

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logger.warning(
                "Circuit breaker OPEN after %d failures", self.failure_count
            )

    def can_attempt(self) -> bool:
        if not self.is_open:
            return True
        # Allow retry after recovery timeout
        if self.last_failure_time and (
            time.time() - self.last_failure_time > self.recovery_timeout
        ):
            logger.info("Circuit breaker half-open, allowing retry")
            return True
        return False


class OddsCache:
    """Stale-while-revalidate in-memory cache with mutex for thundering herd protection."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Optional[list[dict]]:
        entry = self._data.get(key)
        if not entry:
            return None
        return entry["data"]

    def is_fresh(self, key: str) -> bool:
        entry = self._data.get(key)
        if not entry:
            return False
        return (time.time() - entry["timestamp"]) < self.ttl

    def set(self, key: str, data: list[dict]) -> None:
        self._data[key] = {"data": data, "timestamp": time.time()}

    def get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]


class TheOddsAPIProvider(BaseProvider):
    """TheOddsAPI implementation with circuit breaker and stale-while-revalidate cache."""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0)
        self._circuit = CircuitBreaker()
        self._cache = OddsCache(ttl=settings.ODDS_CACHE_TTL_SECONDS)
        self._api_usage = {"requests_used": 0, "requests_remaining": None}

    async def get_odds(self, sport_key: str) -> list[dict[str, Any]]:
        cache_key = f"odds:{sport_key}"

        # Return fresh cache immediately
        if self._cache.is_fresh(cache_key):
            return self._cache.get(cache_key) or []

        # Stale-while-revalidate: try to refresh, but serve stale if refresh fails
        lock = self._cache.get_lock(cache_key)
        if lock.locked():
            # Another request is already refreshing — serve stale
            stale = self._cache.get(cache_key)
            return stale if stale is not None else []

        async with lock:
            # Double-check after acquiring lock
            if self._cache.is_fresh(cache_key):
                return self._cache.get(cache_key) or []

            if not self._circuit.can_attempt():
                logger.warning("Circuit open for odds, serving stale data")
                stale = self._cache.get(cache_key)
                return stale if stale is not None else []

            try:
                is_three_way = sport_key in THREE_WAY_SPORTS

                resp = await self._client.get(
                    f"{BASE_URL}/sports/{sport_key}/odds",
                    params={
                        "apiKey": settings.ODDSAPIKEY,
                        "regions": "eu",
                        "markets": "h2h",
                        "oddsFormat": "decimal",
                    },
                )
                resp.raise_for_status()

                # Track API usage
                self._api_usage["requests_used"] = resp.headers.get(
                    "x-requests-used", self._api_usage["requests_used"]
                )
                self._api_usage["requests_remaining"] = resp.headers.get(
                    "x-requests-remaining"
                )

                raw = resp.json()
                matches = self._parse_odds_response(raw, sport_key, is_three_way)
                self._cache.set(cache_key, matches)
                self._circuit.record_success()
                return matches

            except Exception as e:
                self._circuit.record_failure()
                logger.error("TheOddsAPI error for %s: %s", sport_key, e)
                stale = self._cache.get(cache_key)
                return stale if stale is not None else []

    async def get_scores(self, sport_key: str) -> list[dict[str, Any]]:
        cache_key = f"scores:{sport_key}"

        if self._cache.is_fresh(cache_key):
            return self._cache.get(cache_key) or []

        if not self._circuit.can_attempt():
            stale = self._cache.get(cache_key)
            return stale if stale is not None else []

        try:
            resp = await self._client.get(
                f"{BASE_URL}/sports/{sport_key}/scores",
                params={
                    "apiKey": settings.ODDSAPIKEY,
                    "daysFrom": 3,
                },
            )
            resp.raise_for_status()
            raw = resp.json()
            results = self._parse_scores_response(raw, sport_key)
            self._cache.set(cache_key, results)
            self._circuit.record_success()
            return results
        except Exception as e:
            self._circuit.record_failure()
            logger.error("TheOddsAPI scores error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale if stale is not None else []

    def _parse_odds_response(
        self, raw: list[dict], sport_key: str, is_three_way: bool
    ) -> list[dict[str, Any]]:
        matches = []
        for event in raw:
            # Find the best bookmaker odds (first available)
            odds = {}
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market["key"] == "h2h":
                        outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
                        home_team = event.get("home_team", "")
                        away_team = event.get("away_team", "")

                        if is_three_way:
                            odds = {
                                "1": outcomes.get(home_team, 0),
                                "X": outcomes.get("Draw", 0),
                                "2": outcomes.get(away_team, 0),
                            }
                        else:
                            odds = {
                                "1": outcomes.get(home_team, 0),
                                "2": outcomes.get(away_team, 0),
                            }
                        break
                if odds:
                    break

            if not odds:
                continue

            matches.append({
                "external_id": event["id"],
                "sport_key": sport_key,
                "teams": {
                    "home": event.get("home_team", "Unknown"),
                    "away": event.get("away_team", "Unknown"),
                },
                "commence_time": event["commence_time"],
                "odds": odds,
            })

        return matches

    def _parse_scores_response(
        self, raw: list[dict], sport_key: str
    ) -> list[dict[str, Any]]:
        results = []
        for event in raw:
            if not event.get("completed"):
                continue

            scores = event.get("scores", [])
            if not scores:
                continue

            home_score = None
            away_score = None
            for s in scores:
                if s["name"] == event.get("home_team"):
                    home_score = int(s["score"])
                elif s["name"] == event.get("away_team"):
                    away_score = int(s["score"])

            if home_score is None or away_score is None:
                continue

            is_three_way = sport_key in THREE_WAY_SPORTS
            if is_three_way:
                if home_score > away_score:
                    result = "1"
                elif home_score == away_score:
                    result = "X"
                else:
                    result = "2"
            else:
                result = "1" if home_score > away_score else "2"

            results.append({
                "external_id": event["id"],
                "completed": True,
                "result": result,
                "home_score": home_score,
                "away_score": away_score,
            })

        return results

    @property
    def api_usage(self) -> dict:
        return self._api_usage

    @property
    def circuit_open(self) -> bool:
        return self._circuit.is_open


# Singleton provider instance
odds_provider = TheOddsAPIProvider()
