"""
backend/app/services/team_registry_service.py

Purpose:
    Team identity registry (Team Tower) for resolving raw team names to
    canonical ObjectIds with in-memory caching and auto-create fallback.

Dependencies:
    - app.database
    - app.services.league_service
    - app.utils
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

import app.database as _db
from app.services.league_service import LeagueRegistry
from app.utils.team_matching import teams_match
from app.utils import utcnow

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


def create_alias_suggestion(
    provider: str,
    external_team_id: str,
    incoming_name: str,
    matched_team_id: ObjectId,
    matched_team_name: str,
    sport_key: str | None = None,
) -> dict[str, Any]:
    """Create a standardized alias suggestion payload for dry-run admin reviews."""
    return {
        "provider": str(provider or "").strip().lower(),
        "external_team_id": str(external_team_id or "").strip(),
        "incoming_name": str(incoming_name or "").strip(),
        "team_id": str(matched_team_id),
        "team_name": str(matched_team_name or "").strip(),
        "sport_key": str(sport_key or "").strip() or None,
        "confidence": "high",
        "reason": "name_mismatch",
    }


ALIAS_SUGGESTION_SAMPLE_REFS_LIMIT = 10


def _external_id_query(source_key: str, external_id_text: str) -> dict[str, Any]:
    """Build a robust external-id query that matches both string and legacy numeric values."""
    field = f"external_ids.{source_key}"
    values: list[Any] = [external_id_text]
    if external_id_text.isdigit():
        try:
            values.append(int(external_id_text))
        except ValueError:
            pass
    if len(values) == 1:
        return {field: values[0]}
    return {field: {"$in": values}}


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
        self._last_refresh = utcnow()
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

    async def resolve(
        self,
        raw_name: str,
        sport_key: str,
        create_if_missing: bool = True,
    ) -> ObjectId | None:
        """Resolve team name to team identity with cache -> DB fallback -> optional create."""
        if not self._initialized:
            await self.initialize()

        normalized = normalize_team_name(raw_name)
        if not normalized:
            if not create_if_missing:
                return None
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
        if not create_if_missing:
            return None
        reason = "global_ambiguous" if normalized in self._global_ambiguous else "no_match"
        return await self._auto_create(raw_name, normalized, sport_key, reason=reason)

    async def resolve_by_external_id_or_name(
        self,
        source: str,
        external_id: str,
        name: str,
        sport_key: str,
        create_if_missing: bool = True,
    ) -> ObjectId | None:
        """Resolve team by provider external id first, then fallback to Team Tower name logic."""
        source_key = (source or "").strip().lower()
        external_id_text = str(external_id or "").strip()
        if not source_key:
            raise ValueError("source is required")

        if external_id_text:
            id_query = _external_id_query(source_key, external_id_text)
            team_doc = await _db.db.teams.find_one(id_query, {"_id": 1, "display_name": 1, "aliases": 1})
            if team_doc:
                display_name = str(team_doc.get("display_name") or "")
                aliases = [str(alias.get("name") or "") for alias in team_doc.get("aliases", []) if isinstance(alias, dict)]
                names = [display_name, *aliases]
                if name and any(teams_match(name, candidate) for candidate in names if candidate):
                    return team_doc["_id"]
                logger.warning(
                    "Team external-id/name mismatch source=%s external_id=%s input_name=%s team_id=%s",
                    source_key,
                    external_id_text,
                    name,
                    str(team_doc.get("_id")),
                )
                return None

        resolved = await self.resolve(name, sport_key, create_if_missing=create_if_missing)
        if not resolved:
            return None

        if external_id_text and create_if_missing:
            now = utcnow()
            await _db.db.teams.update_one(
                {"_id": resolved, f"external_ids.{source_key}": {"$exists": False}},
                {"$set": {f"external_ids.{source_key}": external_id_text, "updated_at": now}},
            )
        return resolved

    async def add_alias(
        self,
        team_id: ObjectId,
        alias: str,
        *,
        sport_key: str | None = None,
        source: str = "admin_alias_suggestion",
        refresh_cache: bool = True,
    ) -> bool:
        """Add an alias idempotently to a team and refresh in-memory lookup cache."""
        alias_text = str(alias or "").strip()
        if not alias_text:
            raise ValueError("Alias is empty")
        normalized = normalize_team_name(alias_text)
        if not normalized:
            raise ValueError("Alias normalization is empty")

        team = await _db.db.teams.find_one({"_id": team_id})
        if not team:
            raise ValueError("Team not found")

        alias_sport_key = str(sport_key or team.get("sport_key") or "").strip() or None
        for existing in team.get("aliases", []):
            if not isinstance(existing, dict):
                continue
            if (
                str(existing.get("normalized") or "") == normalized
                and (existing.get("sport_key") or None) == alias_sport_key
            ):
                return False

        alias_doc = {
            "name": alias_text,
            "normalized": normalized,
            "sport_key": alias_sport_key,
            "source": source,
        }
        now = utcnow()
        await _db.db.teams.update_one(
            {"_id": team_id},
            {"$addToSet": {"aliases": alias_doc}, "$set": {"updated_at": now}},
        )
        if refresh_cache:
            await self.initialize()
        return True

    async def record_alias_suggestion(
        self,
        *,
        source: str,
        raw_team_name: str,
        sport_key: str | None = None,
        league_id: ObjectId | None = None,
        league_external_id: str | None = None,
        reason: str = "unresolved_team",
        sample_ref: dict[str, Any] | None = None,
        suggested_team_id: ObjectId | None = None,
        suggested_team_name: str | None = None,
        confidence: float | None = None,
    ) -> ObjectId | None:
        """Persist or bump a pending alias suggestion (used by dry-runs and live imports)."""
        source_key = str(source or "").strip().lower()
        incoming_name = str(raw_team_name or "").strip()
        normalized_name = normalize_team_name(incoming_name)
        if not source_key or not incoming_name or not normalized_name:
            return None

        now = utcnow()
        sport_value = str(sport_key or "").strip() or None
        league_external = str(league_external_id or "").strip() or None
        query: dict[str, Any] = {
            "status": "pending",
            "source": source_key,
            "sport_key": sport_value,
            "league_id": league_id,
            "normalized_name": normalized_name,
        }
        set_fields: dict[str, Any] = {
            "raw_team_name": incoming_name,
            "reason": str(reason or "unresolved_team").strip().lower(),
            "last_seen_at": now,
            "updated_at": now,
            "league_external_id": league_external,
        }
        if suggested_team_id is not None:
            set_fields["suggested_team_id"] = suggested_team_id
            set_fields["suggested_team_name"] = str(suggested_team_name or "").strip() or None
        if confidence is not None:
            set_fields["confidence"] = float(confidence)

        update: dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "status": "pending",
                "source": source_key,
                "sport_key": sport_value,
                "league_id": league_id,
                "normalized_name": normalized_name,
                "first_seen_at": now,
                "created_at": now,
                "sample_refs": [],
            },
            "$inc": {"seen_count": 1},
        }
        if isinstance(sample_ref, dict) and sample_ref:
            update["$setOnInsert"].pop("sample_refs", None)
            ref_doc = dict(sample_ref)
            ref_doc["ts"] = now
            update["$push"] = {
                "sample_refs": {
                    "$each": [ref_doc],
                    "$slice": -ALIAS_SUGGESTION_SAMPLE_REFS_LIMIT,
                }
            }

        result = await _db.db.team_alias_suggestions.update_one(query, update, upsert=True)
        if result.upserted_id is not None:
            return result.upserted_id
        doc = await _db.db.team_alias_suggestions.find_one(query, {"_id": 1})
        return doc.get("_id") if doc else None

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
        now = utcnow()

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
        self._last_refresh = utcnow()
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
