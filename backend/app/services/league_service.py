"""
backend/app/services/league_service.py

Purpose:
    In-memory League Tower registry for fast sport_key/provider resolution and
    strict ingest validation based on active flags and league feature controls.

Dependencies:
    - app.database
    - app.utils.utcnow
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from bson import ObjectId

import app.database as _db
from app.utils import utcnow

logger = logging.getLogger("quotico.league_registry")
_NAV_CACHE_TTL = timedelta(hours=1)
_nav_cache_data: list[dict] | None = None
_nav_cache_expires_at: datetime | None = None
_DEFAULT_SOCCER_SEASON_START_MONTH = 7
_SEASON_START_MONTH_OVERRIDES: dict[str, int] = {}

def _tier_from_level(level: int | None) -> str:
    if level is None:
        return "unknown"
    if level <= 1:
        return "tier1"
    if level == 2:
        return "tier2"
    return "tier3"


def _country_code_from_name(country: str | None) -> str | None:
    mapping = {
        "germany": "DE",
        "england": "GB",
        "spain": "ES",
        "italy": "IT",
        "france": "FR",
        "europe": "EU",
    }
    if not country:
        return None
    return mapping.get(country.strip().lower())


def _provider_key(provider_name: str, provider_id: str) -> tuple[str, str]:
    return ((provider_name or "").strip().lower(), str(provider_id).strip())


def default_season_start_month_for_sport(sport_key: str | None) -> int:
    """Return the default season start month for a league key."""
    normalized = (sport_key or "").strip().lower()
    start_month = _SEASON_START_MONTH_OVERRIDES.get(normalized)
    if start_month is not None:
        return start_month
    return _DEFAULT_SOCCER_SEASON_START_MONTH if normalized.startswith("soccer_") else 1


def default_current_season_for_sport(
    sport_key: str | None,
    *,
    season_start_month: int | None = None,
) -> int:
    """Return season start-year for a league key.

    Soccer leagues default to a July->June season; unknown/non-soccer domains
    default to calendar year.
    """
    now = utcnow()
    start_month = int(season_start_month or default_season_start_month_for_sport(sport_key))
    return now.year if now.month >= start_month else now.year - 1


def _default_features() -> dict:
    return {
        "tipping": False,
        "match_load": True,
        "xg_sync": False,
        "odds_sync": False,
    }


def _default_structure_type() -> str:
    return "league"


def _league_features(league: dict) -> dict:
    raw = league.get("features")
    if not isinstance(raw, dict):
        raw = {}
    defaults = _default_features()
    if "tipping" not in raw:
        # Preserve current behavior for pre-feature documents: active leagues
        # remain visible/tippable until explicitly configured otherwise.
        defaults["tipping"] = bool(league.get("is_active", False))
    return {
        "tipping": bool(raw.get("tipping", defaults["tipping"])),
        "match_load": bool(raw.get("match_load", defaults["match_load"])),
        "xg_sync": bool(raw.get("xg_sync", defaults["xg_sync"])),
        "odds_sync": bool(raw.get("odds_sync", defaults["odds_sync"])),
    }


def _league_external_ids(league: dict) -> dict[str, str]:
    raw = league.get("external_ids") or league.get("provider_mappings") or {}
    if not isinstance(raw, dict):
        return {}
    ids: dict[str, str] = {}
    for provider, value in raw.items():
        provider_key = (provider or "").strip().lower()
        provider_value = str(value or "").strip()
        if provider_key and provider_value:
            ids[provider_key] = provider_value
    return ids


async def seed_core_leagues() -> dict[str, int]:
    """Facade to dedicated league seed service."""
    from app.services.league_seed_service import seed_core_leagues as _seed_core_leagues

    return await _seed_core_leagues()


class LeagueService:
    """Service facade for league maintenance operations."""

    @staticmethod
    async def seed_core_leagues() -> dict[str, int]:
        return await seed_core_leagues()


async def invalidate_navigation_cache() -> None:
    global _nav_cache_data, _nav_cache_expires_at
    _nav_cache_data = None
    _nav_cache_expires_at = None


async def get_active_navigation() -> list[dict]:
    """Return cached active leagues for public sidebar navigation."""
    global _nav_cache_data, _nav_cache_expires_at

    now = utcnow()
    if _nav_cache_data is not None and _nav_cache_expires_at is not None and now < _nav_cache_expires_at:
        return _nav_cache_data

    docs = await _db.db.leagues.find(
        {"is_active": True},
        {
            "_id": 1,
            "sport_key": 1,
            "display_name": 1,
            "name": 1,
            "country": 1,
            "country_code": 1,
            "ui_order": 1,
        },
    ).sort([("ui_order", 1), ("display_name", 1), ("name", 1)]).to_list(length=500)

    items = [
        {
            "id": str(doc["_id"]),
            "sport_key": str(doc.get("sport_key") or ""),
            "name": str(doc.get("display_name") or doc.get("name") or doc.get("sport_key") or ""),
            "country": doc.get("country"),
            "country_code": doc.get("country_code"),
            "ui_order": int(doc.get("ui_order", 999)),
        }
        for doc in docs
    ]
    _nav_cache_data = items
    _nav_cache_expires_at = now + _NAV_CACHE_TTL
    return items


async def update_league_order(ordered_ids: list[ObjectId]) -> dict[str, int]:
    """Persist league UI ordering and invalidate navigation cache."""
    now = utcnow()
    updated = 0
    for index, oid in enumerate(ordered_ids):
        result = await _db.db.leagues.update_one(
            {"_id": oid},
            {"$set": {"ui_order": index, "updated_at": now}},
        )
        if result.matched_count:
            updated += 1

    await invalidate_navigation_cache()
    await LeagueRegistry.get().initialize()
    return {"updated": updated, "total": len(ordered_ids)}


def league_feature_enabled(league: dict | None, feature: str, default: bool = False) -> bool:
    if not league:
        return default
    features = _league_features(league)
    return bool(features.get(feature, default))


class LeagueRegistry:
    """In-memory registry for leagues by sport_key and provider mapping."""

    _instance: "LeagueRegistry | None" = None

    def __init__(self):
        self._by_sport_key: dict[str, dict] = {}
        self._provider_map: dict[tuple[str, str], str] = {}
        self._initialized = False
        self._last_refresh: datetime | None = None

    @classmethod
    def get(cls) -> "LeagueRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def initialize(self) -> None:
        docs = await _db.db.leagues.find({}).to_list(length=10_000)
        self._by_sport_key.clear()
        self._provider_map.clear()

        for doc in docs:
            sport_key = doc.get("sport_key")
            if not sport_key:
                continue
            doc.setdefault("ui_order", 999)
            doc.setdefault("season_start_month", default_season_start_month_for_sport(sport_key))
            doc.setdefault(
                "current_season",
                default_current_season_for_sport(
                    sport_key,
                    season_start_month=int(doc.get("season_start_month", default_season_start_month_for_sport(sport_key))),
                ),
            )
            doc.setdefault("structure_type", _default_structure_type())
            doc.setdefault("features", _league_features(doc))
            doc.setdefault("external_ids", _league_external_ids(doc))
            self._by_sport_key[sport_key] = doc
            providers = _league_external_ids(doc)
            if isinstance(providers, dict):
                for provider_name, provider_id in providers.items():
                    if provider_id in (None, ""):
                        continue
                    self._provider_map[_provider_key(provider_name, str(provider_id))] = sport_key

        self._initialized = True
        self._last_refresh = utcnow()
        logger.info(
            "LeagueRegistry initialized: %d leagues, %d provider mappings",
            len(self._by_sport_key), len(self._provider_map),
        )

    async def get_league(self, sport_key: str) -> dict | None:
        if not self._initialized:
            await self.initialize()
        return self._by_sport_key.get(sport_key)

    async def resolve_provider_league(self, provider_id: str, provider_name: str) -> str | None:
        if not self._initialized:
            await self.initialize()
        return self._provider_map.get(_provider_key(provider_name, provider_id))

    async def ensure_for_import(
        self,
        sport_key: str,
        *,
        provider_name: str | None = None,
        provider_id: str | None = None,
        auto_create_inactive: bool = True,
    ) -> dict:
        """Require a known sport_key for ingest or create an inactive review doc."""
        if not self._initialized:
            await self.initialize()

        existing = self._by_sport_key.get(sport_key)
        if existing:
            return existing
        if not auto_create_inactive:
            raise ValueError(f"Unknown sport_key: {sport_key}")

        now = utcnow()
        mappings: dict[str, str] = {}
        if provider_name and provider_id:
            mappings[(provider_name or "").strip().lower()] = str(provider_id).strip()

        doc = {
            "sport_key": sport_key,
            "display_name": sport_key,
            "country_code": None,
            "tier": "unknown",
            "ui_order": 999,
            "season_start_month": default_season_start_month_for_sport(sport_key),
            "current_season": default_current_season_for_sport(
                sport_key,
                season_start_month=default_season_start_month_for_sport(sport_key),
            ),
            "structure_type": _default_structure_type(),
            "is_active": False,
            "needs_review": True,
            "features": _default_features(),
            "external_ids": mappings,
            "created_at": now,
            "updated_at": now,
        }
        result = await _db.db.leagues.update_one(
            {"sport_key": sport_key},
            {"$setOnInsert": doc},
            upsert=True,
        )
        if result.upserted_id is not None:
            doc["_id"] = result.upserted_id
            self._by_sport_key[sport_key] = doc
            for p_name, p_id in mappings.items():
                self._provider_map[_provider_key(p_name, p_id)] = sport_key
            logger.warning(
                "Created inactive review league for unknown sport_key=%s provider=%s provider_id=%s",
                sport_key, provider_name, provider_id,
            )
            return doc

        league = await _db.db.leagues.find_one({"sport_key": sport_key})
        if not league:
            raise RuntimeError(f"Failed to ensure league for sport_key={sport_key}")
        league.setdefault("ui_order", 999)
        league.setdefault("season_start_month", default_season_start_month_for_sport(sport_key))
        league.setdefault(
            "current_season",
            default_current_season_for_sport(
                sport_key,
                season_start_month=int(league.get("season_start_month", default_season_start_month_for_sport(sport_key))),
            ),
        )
        league.setdefault("features", _league_features(league))
        league.setdefault("external_ids", _league_external_ids(league))
        self._by_sport_key[sport_key] = league
        return league


async def require_active_league(sport_key: str) -> dict:
    """Return active league or raise ValueError."""
    registry = LeagueRegistry.get()
    league = await registry.get_league(sport_key)
    if not league:
        raise ValueError(f"Unknown sport_key: {sport_key}")
    if not league.get("is_active", False):
        raise ValueError(f"Inactive sport_key: {sport_key}")
    return league


def extract_league_ids(league: dict | None) -> list[ObjectId]:
    if not league:
        return []
    _id = league.get("_id")
    if isinstance(_id, ObjectId):
        return [_id]
    return []
