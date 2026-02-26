"""
backend/app/providers/football_data.py

Purpose:
    Adapter for football-data.org competitions with normalized score/status
    payloads for league, cup, and tournament ingest.

Dependencies:
    - app.providers.http_client
    - app.services.provider_settings_service
    - app.services.provider_rate_limiter
    - app.utils.utcnow
"""

import logging
import time
from datetime import timedelta
from typing import Any, Optional

from app.config import settings  # kept for test monkeypatch compatibility
from app.providers.http_client import ResilientClient
from app.services.provider_rate_limiter import provider_rate_limiter
from app.services.provider_settings_service import provider_settings_service
from app.utils import utcnow

logger = logging.getLogger("quotico.football_data")

PROVIDER_NAME = "football_data"

# Map our sport keys to football-data.org competition codes
# Free tier only — BL2 (2. Bundesliga) requires paid plan, use OpenLigaDB instead
SPORT_TO_COMPETITION = {
    "soccer_germany_bundesliga": "BL1",
    "soccer_epl": "PL",
    "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA",
    "soccer_france_ligue_one": "FL1",
    "soccer_netherlands_eredivisie": "DED",
    "soccer_portugal_primeira_liga": "PPL",
}


class FootballDataProvider:
    """football-data.org provider for free match scores and live data."""

    def __init__(self):
        self._client = ResilientClient("football_data")
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._runtime_fingerprint: tuple[float, int, float] | None = None

    async def _runtime(self, sport_key: str | None = None) -> dict[str, Any]:
        payload = await provider_settings_service.get_effective(
            PROVIDER_NAME,
            sport_key=sport_key,
            include_secret=True,
        )
        effective = dict(payload.get("effective_config") or {})
        timeout = float(effective.get("timeout_seconds") or 15.0)
        retries = int(effective.get("max_retries") or 3)
        base_delay = float(effective.get("base_delay_seconds") or 10.0)
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

    def _get_cached(self, key: str, ttl_seconds: int | None = None) -> Optional[list[dict]]:
        entry = self._cache.get(key)
        ttl = ttl_seconds if ttl_seconds is not None else self._cache_ttl
        if entry and (time.time() - entry["ts"]) < ttl:
            return entry["data"]
        return None

    def _set_cache(self, key: str, data: list[dict]) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    async def _fetch_matches(
        self,
        competition: str,
        status: str,
        days_back: int = 3,
        sport_key: str | None = None,
    ) -> list[dict]:
        """Fetch matches from football-data.org for a competition."""
        runtime = await self._runtime(sport_key=sport_key)
        if not bool(runtime.get("enabled", True)):
            return []
        api_key = str(runtime.get("api_key") or "")
        if not api_key:
            return []
        base_url = str(runtime.get("base_url") or "")
        if not base_url:
            return []

        now = utcnow()
        date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
        resp = await self._client.get(
            f"{base_url}/competitions/{competition}/matches",
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
            raw = await self._fetch_matches(competition, "FINISHED", days_back=3, sport_key=sport_key)
            results = []

            for match in raw:
                score_data = match.get("score", {})
                ft = score_data.get("fullTime", {})
                et = score_data.get("extraTime", {})
                pens = score_data.get("penalties", {})
                stage = match.get("stage")
                group_raw = match.get("group")
                group = group_raw.get("name") if isinstance(group_raw, dict) else group_raw
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
                    "round_name": stage,
                    "group_name": group,
                    "score_extra_time": {
                        "home": et.get("home"),
                        "away": et.get("away"),
                    },
                    "score_penalties": {
                        "home": pens.get("home"),
                        "away": pens.get("away"),
                    },
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
            runtime = await self._runtime(sport_key=sport_key)
            if not bool(runtime.get("enabled", True)):
                return []
            api_key = str(runtime.get("api_key") or "")
            if not api_key:
                return []
            base_url = str(runtime.get("base_url") or "")
            if not base_url:
                return []

            await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
            resp = await self._client.get(
                f"{base_url}/competitions/{competition}/matches",
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
            runtime = await self._runtime(sport_key=sport_key)
            if not bool(runtime.get("enabled", True)):
                return None
            api_key = str(runtime.get("api_key") or "")
            if not api_key:
                return None
            base_url = str(runtime.get("base_url") or "")
            if not base_url:
                return None

            await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
            resp = await self._client.get(
                f"{base_url}/competitions/{competition}",
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
            runtime = await self._runtime(sport_key=sport_key)
            if not bool(runtime.get("enabled", True)):
                return []
            api_key = str(runtime.get("api_key") or "")
            if not api_key:
                return []
            base_url = str(runtime.get("base_url") or "")
            if not base_url:
                return []

            await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
            resp = await self._client.get(
                f"{base_url}/competitions/{competition}/matches",
                params={"matchday": str(matchday_number)},
                headers={"X-Auth-Token": api_key},
            )
            resp.raise_for_status()
            raw = resp.json().get("matches", [])

            matches = []
            for m in raw:
                score_data = m.get("score", {})
                ft = score_data.get("fullTime", {})
                et = score_data.get("extraTime", {})
                pens = score_data.get("penalties", {})
                stage = m.get("stage")
                group_raw = m.get("group")
                group = group_raw.get("name") if isinstance(group_raw, dict) else group_raw
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
                    "round_name": stage,
                    "group_name": group,
                    "score_extra_time": {
                        "home": et.get("home"),
                        "away": et.get("away"),
                    },
                    "score_penalties": {
                        "home": pens.get("home"),
                        "away": pens.get("away"),
                    },
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

    async def get_season_matches(self, competition: str, season_year: int) -> list[dict[str, Any]]:
        """Fetch all matches for one competition season and return normalized rows."""
        cache_key = f"season:{competition}:{season_year}"
        cached = self._get_cached(cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        runtime = await self._runtime()
        if not bool(runtime.get("enabled", True)):
            return []
        api_key = str(runtime.get("api_key") or "")
        if not api_key:
            return []
        base_url = str(runtime.get("base_url") or "")
        if not base_url:
            return []

        try:
            await provider_rate_limiter.acquire(PROVIDER_NAME, runtime.get("rate_limit_rpm"))
            resp = await self._client.get(
                f"{base_url}/competitions/{competition}/matches",
                params={"season": int(season_year)},
                headers={"X-Auth-Token": api_key},
            )
            resp.raise_for_status()
            raw = resp.json().get("matches", [])

            normalized: list[dict[str, Any]] = []
            for match in raw:
                score_data = match.get("score", {}) if isinstance(match.get("score"), dict) else {}
                ft = score_data.get("fullTime", {}) if isinstance(score_data.get("fullTime"), dict) else {}
                ht = score_data.get("halfTime", {}) if isinstance(score_data.get("halfTime"), dict) else {}
                et = score_data.get("extraTime", {}) if isinstance(score_data.get("extraTime"), dict) else {}
                pens = score_data.get("penalties", {}) if isinstance(score_data.get("penalties"), dict) else {}
                group = match.get("group")
                group_name = group.get("name") if isinstance(group, dict) else group
                normalized.append(
                    {
                        "match_id": str(match.get("id")) if match.get("id") is not None else "",
                        "utc_date": str(match.get("utcDate") or ""),
                        "status_raw": str(match.get("status") or ""),
                        "matchday": match.get("matchday"),
                        "season": int(season_year),
                        "stage": match.get("stage"),
                        "group": group_name,
                        "home_team_id": str(match.get("homeTeam", {}).get("id") or ""),
                        "home_team_name": str(match.get("homeTeam", {}).get("name") or ""),
                        "away_team_id": str(match.get("awayTeam", {}).get("id") or ""),
                        "away_team_name": str(match.get("awayTeam", {}).get("name") or ""),
                        "score": {
                            "full_time": {"home": ft.get("home"), "away": ft.get("away")},
                            "half_time": {"home": ht.get("home"), "away": ht.get("away")},
                            "extra_time": {"home": et.get("home"), "away": et.get("away")},
                            "penalties": {"home": pens.get("home"), "away": pens.get("away")},
                        },
                    }
                )

            self._set_cache(cache_key, normalized)
            return normalized
        except Exception as exc:
            logger.error(
                "football-data.org season error competition=%s season=%s: %s",
                competition,
                season_year,
                exc,
            )
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


# Singleton
football_data_provider = FootballDataProvider()
