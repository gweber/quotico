import logging
import time
from datetime import datetime

from app.utils import parse_utc, utcnow
from typing import Any, Optional

from app.providers.http_client import ResilientClient

logger = logging.getLogger("quotico.openligadb")

BASE_URL = "https://api.openligadb.de"

# German football leagues supported
SPORT_TO_LEAGUE = {
    "soccer_germany_bundesliga": "bl1",
    "soccer_germany_bundesliga2": "bl2",
}


def _current_season() -> int:
    """Bundesliga season year: 2025 for the 2025/26 season (starts July)."""
    now = utcnow()
    return now.year if now.month >= 7 else now.year - 1


class OpenLigaDBProvider:
    """OpenLigaDB — free, no API key, Bundesliga scores + live data."""

    def __init__(self):
        self._client = ResilientClient("openligadb")
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = 300  # 5 min for finished, overridden for live

    def _get_cached(self, key: str, ttl: Optional[int] = None) -> Optional[list[dict]]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry["ts"]) < (ttl or self._cache_ttl):
            return entry["data"]
        return None

    def _set_cache(self, key: str, data: list[dict]) -> None:
        self._cache[key] = {"data": data, "ts": time.time()}

    async def get_finished_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch completed Bundesliga match results."""
        league = SPORT_TO_LEAGUE.get(sport_key)
        if not league:
            return []

        cache_key = f"finished:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            season = _current_season()

            # Current matchday
            resp = await self._client.get(f"{BASE_URL}/getmatchdata/{league}")
            resp.raise_for_status()
            current_matches = resp.json()

            # Previous matchday (for recently finished matches)
            all_matches = list(current_matches)
            if current_matches:
                group_id = current_matches[0].get("group", {}).get("groupOrderID")
                if group_id and group_id > 1:
                    resp2 = await self._client.get(
                        f"{BASE_URL}/getmatchdata/{league}/{season}/{group_id - 1}"
                    )
                    if resp2.status_code == 200:
                        all_matches.extend(resp2.json())

            results = []
            for match in all_matches:
                if not match.get("matchIsFinished"):
                    continue

                # resultTypeID 2 = Endergebnis (final result)
                home_score = None
                away_score = None
                for r in match.get("matchResults", []):
                    if r.get("resultTypeID") == 2:
                        home_score = r["pointsTeam1"]
                        away_score = r["pointsTeam2"]
                        break

                if home_score is None or away_score is None:
                    continue

                if home_score > away_score:
                    result = "1"
                elif home_score == away_score:
                    result = "X"
                else:
                    result = "2"

                results.append({
                    "home_team": match.get("team1", {}).get("teamName", ""),
                    "away_team": match.get("team2", {}).get("teamName", ""),
                    "utc_date": match.get("matchDateTimeUTC", match.get("matchDateTime", "")),
                    "completed": True,
                    "result": result,
                    "home_score": home_score,
                    "away_score": away_score,
                })

            self._set_cache(cache_key, results)
            logger.info("OpenLigaDB: %d finished matches for %s", len(results), sport_key)
            return results

        except Exception as e:
            logger.error("OpenLigaDB finished error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []

    async def get_live_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Fetch live Bundesliga scores."""
        league = SPORT_TO_LEAGUE.get(sport_key)
        if not league:
            return []

        cache_key = f"live:{sport_key}"
        cached = self._get_cached(cache_key, ttl=60)
        if cached is not None:
            return cached

        try:
            resp = await self._client.get(f"{BASE_URL}/getmatchdata/{league}")
            resp.raise_for_status()
            matches = resp.json()

            now = utcnow()
            live = []

            for match in matches:
                if match.get("matchIsFinished"):
                    continue

                # Parse match time
                match_dt = match.get("matchDateTimeUTC", match.get("matchDateTime", ""))
                if not match_dt:
                    continue
                try:
                    mt = parse_utc(match_dt)
                except (ValueError, TypeError):
                    continue

                if mt > now:
                    continue  # Not started yet

                # Get current score
                home_score = 0
                away_score = 0
                ht_home = 0
                ht_away = 0
                for r in match.get("matchResults", []):
                    if r.get("resultTypeID") == 2:
                        home_score = r["pointsTeam1"]
                        away_score = r["pointsTeam2"]
                    elif r.get("resultTypeID") == 1:
                        ht_home = r["pointsTeam1"]
                        ht_away = r["pointsTeam2"]

                # Approximate minute from kickoff time
                elapsed = (now - mt).total_seconds() / 60
                minute = min(int(elapsed), 90) if elapsed > 0 else None
                # Rough half-time correction
                if minute and 45 < minute < 60:
                    minute = 45

                live.append({
                    "home_team": match.get("team1", {}).get("teamName", ""),
                    "away_team": match.get("team2", {}).get("teamName", ""),
                    "utc_date": match_dt,
                    "home_score": home_score,
                    "away_score": away_score,
                    "half_time": {"home": ht_home, "away": ht_away},
                    "minute": minute,
                    "status": "IN_PLAY",
                })

            self._set_cache(cache_key, live)
            return live

        except Exception as e:
            logger.error("OpenLigaDB live error for %s: %s", sport_key, e)
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


    async def get_current_matchday_number(self, sport_key: str) -> int | None:
        """Get the current matchday number for a sport."""
        league = SPORT_TO_LEAGUE.get(sport_key)
        if not league:
            return None

        cache_key = f"current_md:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached[0]["matchday_number"] if cached else None

        try:
            resp = await self._client.get(f"{BASE_URL}/getmatchdata/{league}")
            resp.raise_for_status()
            matches = resp.json()
            if not matches:
                return None
            md = matches[0].get("group", {}).get("groupOrderID")
            if md:
                self._set_cache(cache_key, [{"matchday_number": md}])
            return md
        except Exception as e:
            logger.error("OpenLigaDB current matchday error: %s", e)
            return None

    async def get_matchday_data(
        self, sport_key: str, season: int, matchday_number: int
    ) -> list[dict[str, Any]]:
        """Fetch all matches for a specific matchday."""
        league = SPORT_TO_LEAGUE.get(sport_key)
        if not league:
            return []

        cache_key = f"matchday:{sport_key}:{season}:{matchday_number}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = await self._client.get(
                f"{BASE_URL}/getmatchdata/{league}/{season}/{matchday_number}"
            )
            resp.raise_for_status()
            raw = resp.json()

            matches = []
            for m in raw:
                home_score = None
                away_score = None
                is_finished = m.get("matchIsFinished", False)

                if is_finished:
                    for r in m.get("matchResults", []):
                        if r.get("resultTypeID") == 2:
                            home_score = r["pointsTeam1"]
                            away_score = r["pointsTeam2"]
                            break

                match_dt = m.get("matchDateTimeUTC", m.get("matchDateTime", ""))

                matches.append({
                    "home_team": m.get("team1", {}).get("teamName", ""),
                    "away_team": m.get("team2", {}).get("teamName", ""),
                    "utc_date": match_dt,
                    "is_finished": is_finished,
                    "home_score": home_score,
                    "away_score": away_score,
                    "matchday_number": matchday_number,
                    "season": season,
                })

            self._set_cache(cache_key, matches)
            logger.info(
                "OpenLigaDB: %d matches for %s matchday %d",
                len(matches), sport_key, matchday_number,
            )
            return matches

        except Exception as e:
            logger.error(
                "OpenLigaDB matchday error for %s/%d/%d: %s",
                sport_key, season, matchday_number, e,
            )
            stale = self._cache.get(cache_key)
            return stale["data"] if stale else []


# Singleton
openligadb_provider = OpenLigaDBProvider()
