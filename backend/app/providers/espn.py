import logging
import time
from datetime import datetime

from app.utils import utcnow
from typing import Any, Optional

from app.providers.http_client import ResilientClient

logger = logging.getLogger("quotico.espn")

# ESPN public API — no auth needed
SPORT_TO_ESPN = {
    "americanfootball_nfl": "football/nfl",
    "basketball_nba": "basketball/nba",
}

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"


class ESPNProvider:
    """ESPN public scoreboard API — free, no key, NFL + NBA scores."""

    def __init__(self):
        self._client = ResilientClient("espn")
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = 300

    def _get_cached(self, key: str, ttl: Optional[int] = None) -> Optional[list[dict]]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry["ts"]) < (ttl or self._cache_ttl):
            return entry["data"]
        return None

    def _set_cache(self, key: str, data: list[dict]) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    async def _fetch_scoreboard(self, sport_key: str, dates: Optional[str] = None) -> list[dict]:
        """Fetch ESPN scoreboard for a sport."""
        espn_path = SPORT_TO_ESPN.get(sport_key)
        if not espn_path:
            return []

        params: dict[str, str] = {}
        if dates:
            params["dates"] = dates  # Format: YYYYMMDD

        resp = await self._client.get(
            f"{BASE_URL}/{espn_path}/scoreboard",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("events", [])

    async def get_finished_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch completed NFL/NBA match results."""
        if sport_key not in SPORT_TO_ESPN:
            return []

        cache_key = f"finished:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            # Fetch recent scoreboard (today and recent days)
            now = utcnow()
            events = await self._fetch_scoreboard(sport_key)

            # Also fetch yesterday and day before for recently completed
            for days_ago in [1, 2]:
                dt = now.replace(hour=0, minute=0, second=0)
                from datetime import timedelta
                past = dt - timedelta(days=days_ago)
                past_events = await self._fetch_scoreboard(
                    sport_key, dates=past.strftime("%Y%m%d")
                )
                events.extend(past_events)

            results = []
            for event in events:
                competitions = event.get("competitions", [])
                if not competitions:
                    continue
                comp = competitions[0]

                status_type = comp.get("status", {}).get("type", {})
                if not status_type.get("completed"):
                    continue

                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                # ESPN: competitors[0] is usually home
                home = None
                away = None
                for c in competitors:
                    if c.get("homeAway") == "home":
                        home = c
                    else:
                        away = c

                if not home or not away:
                    home, away = competitors[0], competitors[1]

                home_score = int(home.get("score", "0"))
                away_score = int(away.get("score", "0"))
                home_name = home.get("team", {}).get("displayName", "")
                away_name = away.get("team", {}).get("displayName", "")

                # NFL/NBA are 2-way (no draw)
                result = "1" if home_score > away_score else "2"

                results.append({
                    "home_team": home_name,
                    "away_team": away_name,
                    "utc_date": event.get("date", ""),
                    "completed": True,
                    "result": result,
                    "home_score": home_score,
                    "away_score": away_score,
                })

            self._set_cache(cache_key, results)
            logger.info("ESPN: %d finished matches for %s", len(results), sport_key)
            return results

        except Exception as e:
            logger.error("ESPN finished error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []

    async def get_live_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch live NFL/NBA scores."""
        if sport_key not in SPORT_TO_ESPN:
            return []

        cache_key = f"live:{sport_key}"
        cached = self._get_cached(cache_key, ttl=60)
        if cached is not None:
            return cached

        try:
            events = await self._fetch_scoreboard(sport_key)
            live = []

            for event in events:
                competitions = event.get("competitions", [])
                if not competitions:
                    continue
                comp = competitions[0]

                status_obj = comp.get("status", {})
                status_type = status_obj.get("type", {})

                # Only in-progress games
                if status_type.get("completed") or status_type.get("state") != "in":
                    continue

                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = None
                away = None
                for c in competitors:
                    if c.get("homeAway") == "home":
                        home = c
                    else:
                        away = c

                if not home or not away:
                    home, away = competitors[0], competitors[1]

                home_score = int(home.get("score", "0"))
                away_score = int(away.get("score", "0"))

                # Clock / period info
                clock = status_obj.get("displayClock", "")
                period = status_obj.get("period", 0)
                detail = status_type.get("shortDetail", "")

                live.append({
                    "home_team": home.get("team", {}).get("displayName", ""),
                    "away_team": away.get("team", {}).get("displayName", ""),
                    "utc_date": event.get("date", ""),
                    "home_score": home_score,
                    "away_score": away_score,
                    "half_time": {"home": 0, "away": 0},
                    "minute": None,
                    "clock": clock,
                    "period": period,
                    "detail": detail,
                    "status": "IN_PLAY",
                })

            self._set_cache(cache_key, live)
            return live

        except Exception as e:
            logger.error("ESPN live error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


# Singleton
espn_provider = ESPNProvider()
