import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.providers.base import BaseProvider
from app.providers.http_client import ResilientClient

logger = logging.getLogger("quotico.odds_api")

BASE_URL = "https://api.the-odds-api.com/v4"

# Sport key mapping: TheOddsAPI sport keys we support
SUPPORTED_SPORTS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
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
    "soccer_germany_bundesliga2",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_uefa_champs_league",
}

# Sports that carry totals (over/under) odds
TOTALS_SPORTS = THREE_WAY_SPORTS | {"basketball_nba", "americanfootball_nfl"}

# Sports that carry spread (handicap) odds
SPREADS_SPORTS = {"basketball_nba", "americanfootball_nfl"}


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
        self._cleanup()

    def _cleanup(self) -> None:
        """Remove expired entries to prevent unbounded memory growth."""
        now = time.time()
        expired = [k for k, v in self._data.items() if (now - v["timestamp"]) > self.ttl * 10]
        for k in expired:
            del self._data[k]
            self._locks.pop(k, None)

    def get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]


class TheOddsAPIProvider(BaseProvider):
    """TheOddsAPI implementation with circuit breaker and stale-while-revalidate cache."""

    def __init__(self):
        self._client = ResilientClient("odds_api")
        self._cache = OddsCache(ttl=settings.ODDS_CACHE_TTL_SECONDS)
        self._api_usage = {"requests_used": 0, "requests_remaining": None}
        self._usage_loaded = False

    async def _load_persisted_usage(self) -> None:
        """Load API usage from DB on first access."""
        if self._usage_loaded:
            return
        self._usage_loaded = True
        try:
            import app.database as _db
            doc = await _db.db.meta.find_one({"_id": "odds_api_usage"})
            if doc:
                self._api_usage["requests_used"] = doc.get("requests_used", 0)
                self._api_usage["requests_remaining"] = doc.get("requests_remaining")
        except Exception:
            logger.debug("Failed to load persisted API usage from DB", exc_info=True)

    def _track_usage_headers(self, resp) -> None:
        """Extract and store API usage from response headers."""
        used = resp.headers.get("x-requests-used")
        remaining = resp.headers.get("x-requests-remaining")
        if used is not None:
            self._api_usage["requests_used"] = int(used)
        if remaining is not None:
            self._api_usage["requests_remaining"] = int(remaining)

    async def _persist_usage(self) -> None:
        """Persist API usage to DB so it survives restarts."""
        try:
            import app.database as _db
            await _db.db.meta.update_one(
                {"_id": "odds_api_usage"},
                {"$set": {
                    "requests_used": self._api_usage["requests_used"],
                    "requests_remaining": self._api_usage["requests_remaining"],
                }},
                upsert=True,
            )
        except Exception:
            logger.warning("Failed to persist API usage to DB", exc_info=True)

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

            if not self._client.circuit.can_attempt():
                logger.warning("Circuit open for odds, serving stale data")
                stale = self._cache.get(cache_key)
                return stale if stale is not None else []

            try:
                is_three_way = sport_key in THREE_WAY_SPORTS

                # Build market list: h2h always, totals/spreads per sport
                market_parts = ["h2h"]
                if sport_key in TOTALS_SPORTS:
                    market_parts.append("totals")
                if sport_key in SPREADS_SPORTS:
                    market_parts.append("spreads")
                markets = ",".join(market_parts)

                resp = await self._client.get(
                    f"{BASE_URL}/sports/{sport_key}/odds",
                    params={
                        "apiKey": settings.ODDSAPIKEY,
                        "regions": "eu",
                        "markets": markets,
                        "oddsFormat": "decimal",
                    },
                )
                resp.raise_for_status()

                # Track API usage
                self._track_usage_headers(resp)
                await self._persist_usage()

                raw = resp.json()
                matches = self._parse_odds_response(raw, sport_key, is_three_way)
                self._cache.set(cache_key, matches)
                self._client.circuit.record_success()
                return matches

            except Exception as e:
                self._client.circuit.record_failure()
                logger.error("TheOddsAPI error for %s: %s", sport_key, e)
                stale = self._cache.get(cache_key)
                return stale if stale is not None else []

    async def get_scores(self, sport_key: str) -> list[dict[str, Any]]:
        cache_key = f"scores:{sport_key}"

        if self._cache.is_fresh(cache_key):
            return self._cache.get(cache_key) or []

        if not self._client.circuit.can_attempt():
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
            self._track_usage_headers(resp)
            await self._persist_usage()
            raw = resp.json()
            results = self._parse_scores_response(raw, sport_key)
            self._cache.set(cache_key, results)
            self._client.circuit.record_success()
            return results
        except Exception as e:
            self._client.circuit.record_failure()
            logger.error("TheOddsAPI scores error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale if stale is not None else []

    def _parse_odds_response(
        self, raw: list[dict], sport_key: str, is_three_way: bool
    ) -> list[dict[str, Any]]:
        want_totals = sport_key in TOTALS_SPORTS
        want_spreads = sport_key in SPREADS_SPORTS

        matches = []
        for event in raw:
            # Find the best bookmaker odds (first available)
            odds = {}
            totals_odds = {}
            spreads_odds = {}
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")

            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market["key"] == "h2h" and not odds:
                        outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
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

                    elif market["key"] == "totals" and not totals_odds:
                        for outcome in market["outcomes"]:
                            if outcome["name"] == "Over":
                                totals_odds["over"] = outcome["price"]
                                totals_odds["line"] = outcome.get("point", 2.5)
                            elif outcome["name"] == "Under":
                                totals_odds["under"] = outcome["price"]

                    elif market["key"] == "spreads" and not spreads_odds:
                        for outcome in market["outcomes"]:
                            point = outcome.get("point", 0)
                            if outcome["name"] == home_team:
                                spreads_odds["home_line"] = point
                                spreads_odds["home_odds"] = outcome["price"]
                            elif outcome["name"] == away_team:
                                spreads_odds["away_line"] = point
                                spreads_odds["away_odds"] = outcome["price"]

                # Break once we have all requested markets from one bookmaker
                has_all = odds
                if want_totals and not totals_odds:
                    has_all = False
                if want_spreads and not spreads_odds:
                    has_all = False
                if has_all:
                    break

            if not odds:
                continue

            match_data = {
                "external_id": event["id"],
                "sport_key": sport_key,
                "teams": {
                    "home": home_team or "Unknown",
                    "away": away_team or "Unknown",
                },
                "commence_time": event["commence_time"],
                "odds": odds,
            }
            if totals_odds:
                match_data["totals_odds"] = totals_odds
            if spreads_odds:
                match_data["spreads_odds"] = spreads_odds

            matches.append(match_data)

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

    async def load_usage(self) -> dict:
        """Return API usage, loading from DB if needed."""
        await self._load_persisted_usage()
        return self._api_usage

    @property
    def api_usage(self) -> dict:
        return self._api_usage

    @property
    def circuit_open(self) -> bool:
        return self._client.circuit.is_open


# Singleton provider instance
odds_provider = TheOddsAPIProvider()
