"""Team identity registry service (Team-Tower)."""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.services.league_service import LeagueRegistry

logger = logging.getLogger("quotico.team_registry")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes


_CHAR_MAP = str.maketrans({
    "ß": "ss", "ø": "o", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe",
    "ł": "l", "đ": "d", "ı": "i",
})

_NOISE_TOKENS = frozenset({
    "fc", "sv", "club", "vfl", "vfb", "tsg", "fsv", "bsc", "bvb",
    "as", "sc", "rcd", "afc",
    "cf", "cd", "ud", "sd", "rc", "ca",
    "ssc", "us", "ac", "uc",
    "og", "ogc", "losc", "es", "se",
    "az", "psv", "nac",
    "1",
})


def normalize_team_name(raw: str) -> str:
    """Deterministic team name normalization."""
    s = (raw or "").lower().strip()
    s = s.translate(_CHAR_MAP)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]", " ", s)
    tokens = s.split()
    tokens = [t for t in tokens if t not in _NOISE_TOKENS]
    tokens.sort()
    return " ".join(tokens)


class TeamRegistry:
    """In-memory team lookup, built from DB at startup."""

    _instance: "TeamRegistry | None" = None

    def __init__(self):
        self.lookup_by_sport: dict[tuple[str, str], ObjectId] = {}
        self.lookup_global: dict[str, ObjectId] = {}
        self._global_candidates: dict[str, set[ObjectId]] = {}
        self._global_ambiguous: set[str] = set()
        self._initialized = False
        self._last_refresh: datetime | None = None
        self._refresh_task: asyncio.Task | None = None
        self._stats = {
            "cache_hits_sport": 0,
            "cache_hits_global": 0,
            "db_fallback_hits": 0,
            "auto_creates": 0,
            "race_duplicates": 0,
            "refreshes": 0,
            "refresh_teams_loaded": 0,
        }

    @classmethod
    def get(cls) -> "TeamRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def initialize(self) -> None:
        """Full DB load. Call once at startup."""
        teams = await _db.db.teams.find({}).to_list(length=50_000)

        self.lookup_by_sport.clear()
        self._global_candidates.clear()
        for team in teams:
            team_id = team["_id"]
            norm = team.get("normalized_name", "")
            sport = team.get("sport_key", "")
            if norm:
                self.lookup_by_sport[(norm, sport)] = team_id
                self._global_candidates.setdefault(norm, set()).add(team_id)

            for alias in team.get("aliases", []):
                a_norm = alias.get("normalized", "")
                a_sport = alias.get("sport_key", sport)
                if a_norm:
                    self.lookup_by_sport[(a_norm, a_sport)] = team_id
                    self._global_candidates.setdefault(a_norm, set()).add(team_id)

        self._rebuild_global_lookup()
        self._last_refresh = datetime.now(timezone.utc)
        self._initialized = True
        logger.info(
            "TeamRegistry initialized: %d sport entries, %d global, %d ambiguous",
            len(self.lookup_by_sport), len(self.lookup_global), len(self._global_ambiguous),
        )

    async def start_background_refresh(self) -> None:
        """Start periodic incremental refresh."""
        if self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "TeamRegistry background refresh started (interval=%ds)",
            REFRESH_INTERVAL_SECONDS,
        )

    async def stop_background_refresh(self) -> None:
        """Stop periodic incremental refresh."""
        if not self._refresh_task:
            return
        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except asyncio.CancelledError:
            pass
        self._refresh_task = None
        logger.info("TeamRegistry background refresh stopped")

    async def resolve(self, raw_name: str, sport_key: str) -> ObjectId:
        """Resolve team name to team identity with cache -> DB fallback -> create."""
        if not self._initialized:
            await self.initialize()

        normalized = normalize_team_name(raw_name)
        if not normalized:
            return await self._auto_create(raw_name, normalized, sport_key, reason="empty_normalization")

        # 1) Sport-scoped cache
        team_id = self.lookup_by_sport.get((normalized, sport_key))
        if team_id:
            self._stats["cache_hits_sport"] += 1
            return team_id

        # 2) Global cache (only unambiguous entries)
        team_id = self.lookup_global.get(normalized)
        if team_id:
            self._stats["cache_hits_global"] += 1
            return team_id

        # 3) DB fallback (another worker may have created it)
        team_id = await self._db_lookup(normalized, sport_key)
        if team_id:
            return team_id

        # 4) Auto-create (true miss)
        reason = "global_ambiguous" if normalized in self._global_ambiguous else "no_match"
        return await self._auto_create(raw_name, normalized, sport_key, reason=reason)

    async def _db_lookup(self, normalized: str, sport_key: str) -> ObjectId | None:
        """Check DB for team created by another worker since cache load."""
        doc = await _db.db.teams.find_one(
            {
                "$or": [
                    {"normalized_name": normalized, "sport_key": sport_key},
                    {"aliases": {"$elemMatch": {"normalized": normalized, "sport_key": sport_key}}},
                ]
            }
        )
        if not doc:
            return None

        team_id = doc["_id"]
        self.lookup_by_sport[(normalized, sport_key)] = team_id
        self._global_candidates.setdefault(normalized, set()).add(team_id)
        self._rebuild_global_lookup()
        self._stats["db_fallback_hits"] += 1
        logger.debug("DB fallback hit: '%s' (%s) -> %s", normalized, sport_key, team_id)
        return team_id

    async def _auto_create(
        self, raw_name: str, normalized: str, sport_key: str, *, reason: str
    ) -> ObjectId:
        """Create team doc with race-safe duplicate handling across workers."""
        now = datetime.now(timezone.utc)

        existing = self.lookup_by_sport.get((normalized, sport_key))
        if existing:
            return existing

        league = await LeagueRegistry.get().get_league(sport_key)
        league_ids: list[ObjectId] = []
        if league and isinstance(league.get("_id"), ObjectId):
            league_ids = [league["_id"]]

        doc = {
            "normalized_name": normalized,
            "display_name": raw_name,
            "sport_key": sport_key,
            "league_ids": league_ids,
            "aliases": [{
                "name": raw_name,
                "normalized": normalized,
                "sport_key": sport_key,
                "source": "auto",
            }],
            "needs_review": True,
            "source": "auto",
            "auto_reason": reason,
            "created_at": now,
            "updated_at": now,
        }

        created = False
        try:
            result = await _db.db.teams.insert_one(doc)
            team_id = result.inserted_id
            created = True
            self._stats["auto_creates"] += 1
        except DuplicateKeyError:
            self._stats["race_duplicates"] += 1
            existing_doc = await _db.db.teams.find_one(
                {"normalized_name": normalized, "sport_key": sport_key}
            )
            if not existing_doc:
                raise
            team_id = existing_doc["_id"]
            logger.debug(
                "Race resolved: '%s' (%s) -> %s (created by other worker)",
                normalized, sport_key, team_id,
            )
        except Exception as exc:
            # Defensive fallback for wrapped duplicate errors.
            if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
                self._stats["race_duplicates"] += 1
                existing_doc = await _db.db.teams.find_one(
                    {"normalized_name": normalized, "sport_key": sport_key}
                )
                if not existing_doc:
                    raise
                team_id = existing_doc["_id"]
                logger.debug(
                    "Race resolved: '%s' (%s) -> %s (created by other worker)",
                    normalized, sport_key, team_id,
                )
            else:
                raise

        self.lookup_by_sport[(normalized, sport_key)] = team_id
        self._global_candidates.setdefault(normalized, set()).add(team_id)
        self._rebuild_global_lookup()

        if created:
            logger.info(
                "Auto-created team: '%s' -> %s (sport=%s, reason=%s, needs_review=True)",
                raw_name, team_id, sport_key, reason,
            )
        return team_id

    async def _refresh_loop(self) -> None:
        """Periodically load teams updated since last refresh."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            try:
                await self._incremental_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TeamRegistry refresh failed — will retry next cycle")

    async def _incremental_refresh(self) -> None:
        """Incremental merge of teams changed since the last refresh timestamp."""
        if not self._last_refresh:
            return

        new_teams = await _db.db.teams.find(
            {"updated_at": {"$gt": self._last_refresh}},
        ).to_list(length=10_000)

        added = 0
        for team in new_teams:
            team_id = team["_id"]
            norm = team.get("normalized_name", "")
            sport = team.get("sport_key", "")

            if norm:
                self.lookup_by_sport[(norm, sport)] = team_id
                self._global_candidates.setdefault(norm, set()).add(team_id)

            for alias in team.get("aliases", []):
                a_norm = alias.get("normalized", "")
                a_sport = alias.get("sport_key", sport)
                if a_norm:
                    self.lookup_by_sport[(a_norm, a_sport)] = team_id
                    self._global_candidates.setdefault(a_norm, set()).add(team_id)
            added += 1

        self._rebuild_global_lookup()
        self._last_refresh = datetime.now(timezone.utc)
        self._stats["refreshes"] += 1
        self._stats["refresh_teams_loaded"] += added

        if added:
            logger.info("TeamRegistry refreshed: %d teams updated", added)

    def _rebuild_global_lookup(self) -> None:
        """Rebuild unambiguous global lookup from sport-scoped entries."""
        self.lookup_global.clear()
        self._global_ambiguous.clear()
        for norm, ids in self._global_candidates.items():
            if len(ids) == 1:
                self.lookup_global[norm] = next(iter(ids))
            else:
                self._global_ambiguous.add(norm)

    def stats(self) -> dict:
        """Return cache health metrics."""
        return {
            **self._stats,
            "sport_entries": len(self.lookup_by_sport),
            "global_entries": len(self.lookup_global),
            "ambiguous_keys": len(self._global_ambiguous),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "initialized": self._initialized,
        }
