import asyncio
import logging
import time
import unicodedata
from datetime import datetime, timedelta

from app.utils import utcnow
from typing import Any, Optional

from app.config import settings
from app.providers.http_client import ResilientClient

logger = logging.getLogger("quotico.football_data")

BASE_URL = "https://api.football-data.org/v4"

# Map our sport keys to football-data.org competition codes
# Free tier only — BL2 (2. Bundesliga) requires paid plan, use OpenLigaDB instead
SPORT_TO_COMPETITION = {
    "soccer_germany_bundesliga": "BL1",
    "soccer_epl": "PL",
    "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA",
    "soccer_uefa_champs_league": "CL",
}


def _normalize_tokens(name: str) -> set[str]:
    """Normalize a team name into lowercase tokens for fuzzy matching."""
    # Remove accents (ü→u, é→e, etc.)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    noise = {"fc", "cf", "sc", "ac", "as", "ss", "us", "afc", "rcd", "1.", "club", "de"}
    return {t for t in name.split() if t not in noise and len(t) >= 3}


def teams_match(name_a: str, name_b: str) -> bool:
    """Check if two team names likely refer to the same team."""
    tokens_a = _normalize_tokens(name_a)
    tokens_b = _normalize_tokens(name_b)

    # Direct token overlap ("bayern" in both)
    if tokens_a & tokens_b:
        return True

    # Prefix matching ("inter" ~ "internazionale", "milan" ~ "milano")
    for ta in tokens_a:
        for tb in tokens_b:
            if len(ta) >= 4 and len(tb) >= 4:
                if ta.startswith(tb[:4]) or tb.startswith(ta[:4]):
                    return True

    return False


class FootballDataProvider:
    """football-data.org provider for free match scores and live data."""

    # Free tier: 10 requests/minute → 1 request per 7 seconds (with margin)
    _MIN_INTERVAL = 7.0

    def __init__(self):
        self._client = ResilientClient("football_data")
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._last_request_at: float = 0.0

    async def _throttle(self) -> None:
        """Enforce minimum interval between API calls (free tier rate limit)."""
        elapsed = time.time() - self._last_request_at
        if elapsed < self._MIN_INTERVAL:
            await asyncio.sleep(self._MIN_INTERVAL - elapsed)
        self._last_request_at = time.time()

    def _get_cached(self, key: str) -> Optional[list[dict]]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry["ts"]) < self._cache_ttl:
            return entry["data"]
        return None

    def _set_cache(self, key: str, data: list[dict]) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    async def _fetch_matches(
        self, competition: str, status: str, days_back: int = 3
    ) -> list[dict]:
        """Fetch matches from football-data.org for a competition."""
        api_key = getattr(settings, "FOOTBALL_DATA_API_KEY", "")
        if not api_key:
            return []

        now = utcnow()
        date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        await self._throttle()
        resp = await self._client.get(
            f"{BASE_URL}/competitions/{competition}/matches",
            params={
                "status": status,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
            headers={"X-Auth-Token": api_key},
        )
        resp.raise_for_status()
        return resp.json().get("matches", [])

    async def get_finished_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch completed match results for tip resolution."""
        competition = SPORT_TO_COMPETITION.get(sport_key)
        if not competition:
            return []

        cache_key = f"finished:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            raw = await self._fetch_matches(competition, "FINISHED", days_back=3)
            results = []

            for match in raw:
                ft = match.get("score", {}).get("fullTime", {})
                home_score = ft.get("home")
                away_score = ft.get("away")
                if home_score is None or away_score is None:
                    continue

                if home_score > away_score:
                    result = "1"
                elif home_score == away_score:
                    result = "X"
                else:
                    result = "2"

                results.append({
                    "home_team": match.get("homeTeam", {}).get("name", ""),
                    "away_team": match.get("awayTeam", {}).get("name", ""),
                    "utc_date": match.get("utcDate", ""),
                    "completed": True,
                    "result": result,
                    "home_score": home_score,
                    "away_score": away_score,
                })

            self._set_cache(cache_key, results)
            logger.info(
                "football-data.org: %d finished matches for %s",
                len(results), sport_key,
            )
            return results

        except Exception as e:
            logger.error("football-data.org finished error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []

    async def get_live_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch live match scores for display."""
        competition = SPORT_TO_COMPETITION.get(sport_key)
        if not competition:
            return []

        cache_key = f"live:{sport_key}"
        # Shorter TTL for live data (60s)
        entry = self._cache.get(cache_key)
        if entry and (time.time() - entry["ts"]) < 60:
            return entry["data"]

        try:
            api_key = getattr(settings, "FOOTBALL_DATA_API_KEY", "")
            if not api_key:
                return []

            await self._throttle()
            resp = await self._client.get(
                f"{BASE_URL}/competitions/{competition}/matches",
                params={"status": "IN_PLAY"},
                headers={"X-Auth-Token": api_key},
            )
            resp.raise_for_status()
            raw = resp.json().get("matches", [])

            live = []
            for match in raw:
                ft = match.get("score", {}).get("fullTime", {})
                ht = match.get("score", {}).get("halfTime", {})
                home_score = ft.get("home", 0) or 0
                away_score = ft.get("away", 0) or 0
                minute = match.get("minute")

                live.append({
                    "home_team": match.get("homeTeam", {}).get("name", ""),
                    "away_team": match.get("awayTeam", {}).get("name", ""),
                    "utc_date": match.get("utcDate", ""),
                    "home_score": home_score,
                    "away_score": away_score,
                    "half_time": {
                        "home": ht.get("home", 0) or 0,
                        "away": ht.get("away", 0) or 0,
                    },
                    "minute": minute,
                    "status": match.get("status", "IN_PLAY"),
                })

            self._cache[cache_key] = {"data": live, "ts": time.time()}
            return live

        except Exception as e:
            logger.error("football-data.org live error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


    async def get_current_matchday_number(self, sport_key: str) -> int | None:
        """Get the current matchday number for a competition."""
        competition = SPORT_TO_COMPETITION.get(sport_key)
        if not competition:
            return None

        cache_key = f"current_md:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached[0]["matchday_number"] if cached else None

        try:
            api_key = getattr(settings, "FOOTBALL_DATA_API_KEY", "")
            if not api_key:
                return None

            await self._throttle()
            resp = await self._client.get(
                f"{BASE_URL}/competitions/{competition}",
                headers={"X-Auth-Token": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            md = data.get("currentSeason", {}).get("currentMatchday")
            if md:
                self._set_cache(cache_key, [{"matchday_number": md}])
            return md
        except Exception as e:
            logger.error("football-data.org current matchday error for %s: %s", sport_key, e)
            return None

    async def get_matchday_data(
        self, sport_key: str, season: int, matchday_number: int
    ) -> list[dict[str, Any]]:
        """Fetch all matches for a specific matchday of a competition."""
        competition = SPORT_TO_COMPETITION.get(sport_key)
        if not competition:
            return []

        cache_key = f"matchday:{sport_key}:{season}:{matchday_number}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            api_key = getattr(settings, "FOOTBALL_DATA_API_KEY", "")
            if not api_key:
                return []

            await self._throttle()
            resp = await self._client.get(
                f"{BASE_URL}/competitions/{competition}/matches",
                params={"matchday": str(matchday_number)},
                headers={"X-Auth-Token": api_key},
            )
            resp.raise_for_status()
            raw = resp.json().get("matches", [])

            matches = []
            for m in raw:
                ft = m.get("score", {}).get("fullTime", {})
                is_finished = m.get("status") == "FINISHED"
                home_score = ft.get("home") if is_finished else None
                away_score = ft.get("away") if is_finished else None

                matches.append({
                    "home_team": m.get("homeTeam", {}).get("name", ""),
                    "away_team": m.get("awayTeam", {}).get("name", ""),
                    "utc_date": m.get("utcDate", ""),
                    "is_finished": is_finished,
                    "home_score": home_score,
                    "away_score": away_score,
                    "matchday_number": matchday_number,
                    "season": season,
                })

            self._set_cache(cache_key, matches)
            logger.info(
                "football-data.org: %d matches for %s matchday %d",
                len(matches), sport_key, matchday_number,
            )
            return matches

        except Exception as e:
            logger.error(
                "football-data.org matchday error for %s/%d/%d: %s",
                sport_key, season, matchday_number, e,
            )
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


# Singleton
football_data_provider = FootballDataProvider()
