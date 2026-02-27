"""
backend/app/services/provider_settings_service.py

Purpose:
    DB-first runtime provider configuration service with field-level source
    tracking and encrypted secret support (DB > ENV > defaults).

Dependencies:
    - app.database
    - app.config
    - app.services.encryption
    - app.utils.utcnow
"""

from __future__ import annotations

from typing import Any

import app.database as _db
from app.config import settings
from app.services.encryption import CURRENT_KEY_VERSION, decrypt, encrypt
from app.utils import utcnow

Scope = str
ProviderName = str

_SCOPE_GLOBAL = "global"
_SCOPE_LEAGUE = "league"


def _norm_provider(provider: str) -> str:
    return str(provider or "").strip().lower()


def _norm_scope(scope: str) -> str:
    raw = str(scope or "").strip().lower()
    return _SCOPE_LEAGUE if raw == _SCOPE_LEAGUE else _SCOPE_GLOBAL


def _env_key_for_provider(provider: str) -> str:
    mapping = {
        "sportmonks": "SM_API_KEY",
        "understat": "",
    }
    return mapping.get(provider, "")


def _defaults(provider: str) -> dict[str, Any]:
    defaults: dict[str, dict[str, Any]] = {
        "sportmonks": {
            "enabled": True,
            "base_url": str(settings.SPORTMONKS_BASE_URL or ""),
            "timeout_seconds": 90.0,
            "max_retries": 3,
            "base_delay_seconds": 2.0,
            "rate_limit_rpm": 180,
            "poll_interval_seconds": 900,
            "headers_override": {},
            "extra": {},
        }
    }
    return dict(defaults.get(provider, defaults["sportmonks"]))


def _env_fallback(provider: str) -> dict[str, Any]:
    defaults = _defaults(provider)
    base_url_map: dict[str, str] = {
        "sportmonks": str(settings.SPORTMONKS_BASE_URL or "")
    }
    defaults["base_url"] = base_url_map.get(provider, defaults["base_url"])
    defaults["rate_limit_rpm"] = int(rpm_map.get(provider, defaults["rate_limit_rpm"] or 0))
    return defaults


class ProviderSettingsService:
    """DB-first provider settings and secret resolution service."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ts: dict[str, float] = {}

    async def get_effective(
        self,
        provider: str,
        *,
        league_id: int | None = None,
        include_secret: bool = True,
    ) -> dict[str, Any]:
        provider_name = _norm_provider(provider)
        lid = await self._resolve_league_id(league_id=league_id)
        cache_key = f"{provider_name}:{lid or 'global'}:{'secret' if include_secret else 'nosecret'}"
        now_ts = utcnow().timestamp()
        ttl = max(0, int(settings.PROVIDER_SETTINGS_CACHE_TTL))
        if ttl > 0 and cache_key in self._cache and (now_ts - self._cache_ts.get(cache_key, 0)) <= ttl:
            return dict(self._cache[cache_key])

        fields = [
            "enabled",
            "base_url",
            "timeout_seconds",
            "max_retries",
            "base_delay_seconds",
            "rate_limit_rpm",
            "poll_interval_seconds",
            "headers_override",
            "extra",
        ]
        source_map: dict[str, str] = {field: "default" for field in fields}
        effective = _defaults(provider_name)

        env_values = _env_fallback(provider_name)
        for field in fields:
            if field in env_values and env_values[field] is not None:
                effective[field] = env_values[field]
                source_map[field] = "env"

        try:
            global_doc = await _db.db.provider_settings.find_one(
                {"provider": provider_name, "scope": _SCOPE_GLOBAL, "league_id": None},
            )
        except Exception:
            global_doc = None
        if global_doc:
            for field in fields:
                if field in global_doc:
                    effective[field] = global_doc.get(field)
                    source_map[field] = "db_global"

        league_doc = None
        if lid:
            try:
                league_doc = await _db.db.provider_settings.find_one(
                    {"provider": provider_name, "scope": _SCOPE_LEAGUE, "league_id": lid},
                )
            except Exception:
                league_doc = None
            if league_doc:
                for field in fields:
                    if field in league_doc:
                        effective[field] = league_doc.get(field)
                        source_map[field] = "db_league"

        secret_val = ""
        secret_source = "none"
        if include_secret:
            env_key_name = _env_key_for_provider(provider_name)
            env_secret = getattr(settings, env_key_name, "") if env_key_name else ""
            if env_secret:
                secret_val = str(env_secret)
                secret_source = "env"

            try:
                secret_global = await _db.db.provider_secrets.find_one(
                    {"provider": provider_name, "scope": _SCOPE_GLOBAL, "league_id": None},
                    {"api_key_enc": 1, "encryption_key_version": 1, "key_version": 1, "updated_at": 1},
                )
            except Exception:
                secret_global = None
            if secret_global and secret_global.get("api_key_enc"):
                secret_val = decrypt(
                    str(secret_global["api_key_enc"]),
                    key_version=int(secret_global.get("encryption_key_version", CURRENT_KEY_VERSION)),
                )
                secret_source = "db_global"

            if lid:
                try:
                    secret_league = await _db.db.provider_secrets.find_one(
                        {"provider": provider_name, "scope": _SCOPE_LEAGUE, "league_id": lid},
                        {"api_key_enc": 1, "encryption_key_version": 1, "key_version": 1, "updated_at": 1},
                    )
                except Exception:
                    secret_league = None
                if secret_league and secret_league.get("api_key_enc"):
                    secret_val = decrypt(
                        str(secret_league["api_key_enc"]),
                        key_version=int(secret_league.get("encryption_key_version", CURRENT_KEY_VERSION)),
                    )
                    secret_source = "db_league"
            effective["api_key"] = secret_val

        payload = {
            "provider": provider_name,
            "league_id": lid,
            "rate_limit_scope": "provider_global",
            "effective_config": effective,
            "source_map": source_map,
            "secret_source": secret_source,
            "configured_secret": bool(secret_val) if include_secret else None,
            "resolved_from": {
                "db_global": bool(global_doc),
                "db_league": bool(league_doc),
            },
        }
        if ttl > 0:
            self._cache[cache_key] = dict(payload)
            self._cache_ts[cache_key] = now_ts
        return payload

    async def get_secret_status(
        self,
        provider: str,
        *,
        scope: Scope = _SCOPE_GLOBAL,
        league_id: int | None = None,
    ) -> dict[str, Any]:
        provider_name = _norm_provider(provider)
        normalized_scope = _norm_scope(scope)
        lid = await self._resolve_league_id(league_id=league_id)
        if normalized_scope == _SCOPE_GLOBAL:
            lid = None
        try:
            doc = await _db.db.provider_secrets.find_one(
                {"provider": provider_name, "scope": normalized_scope, "league_id": lid},
                {"key_version": 1, "updated_at": 1, "updated_by": 1},
            )
        except Exception:
            doc = None
        return {
            "provider": provider_name,
            "scope": normalized_scope,
            "league_id": lid,
            "configured": bool(doc),
            "key_version": int(doc.get("key_version", 0)) if doc else 0,
            "updated_at": doc.get("updated_at") if doc else None,
            "updated_by": doc.get("updated_by") if doc else None,
        }

    async def set_settings(
        self,
        provider: str,
        patch: dict[str, Any],
        *,
        scope: Scope = _SCOPE_GLOBAL,
        league_id: int | None = None,
        actor_id: str,
    ) -> dict[str, Any]:
        provider_name = _norm_provider(provider)
        normalized_scope = _norm_scope(scope)
        lid = await self._resolve_league_id(league_id=league_id)
        if normalized_scope == _SCOPE_GLOBAL:
            lid = None
        now = utcnow()

        allowed = {
            "enabled",
            "base_url",
            "timeout_seconds",
            "max_retries",
            "base_delay_seconds",
            "rate_limit_rpm",
            "poll_interval_seconds",
            "headers_override",
            "extra",
        }
        updates = {k: v for k, v in (patch or {}).items() if k in allowed}
        if "extra" in updates and not isinstance(updates["extra"], dict):
            updates["extra"] = {}
        if "headers_override" in updates and not isinstance(updates["headers_override"], dict):
            updates["headers_override"] = {}
        updates["updated_at"] = now
        updates["updated_by"] = actor_id

        await _db.db.provider_settings.update_one(
            {"provider": provider_name, "scope": normalized_scope, "league_id": lid},
            {
                "$set": updates,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        await self.invalidate(provider=provider_name, league_id=lid)
        return {
            "provider": provider_name,
            "scope": normalized_scope,
            "league_id": lid,
            "updated_fields": sorted([k for k in updates.keys() if k not in {"updated_at", "updated_by"}]),
        }

    async def set_secret(
        self,
        provider: str,
        *,
        api_key: str,
        scope: Scope = _SCOPE_GLOBAL,
        league_id: int | None = None,
        actor_id: str,
    ) -> dict[str, Any]:
        provider_name = _norm_provider(provider)
        normalized_scope = _norm_scope(scope)
        lid = await self._resolve_league_id(league_id=league_id)
        if normalized_scope == _SCOPE_GLOBAL:
            lid = None
        now = utcnow()
        existing = await _db.db.provider_secrets.find_one(
            {"provider": provider_name, "scope": normalized_scope, "league_id": lid},
            {"key_version": 1},
        )
        next_version = int(existing.get("key_version", 0)) + 1 if existing else 1
        encrypted = encrypt(str(api_key), key_version=CURRENT_KEY_VERSION)
        await _db.db.provider_secrets.update_one(
            {"provider": provider_name, "scope": normalized_scope, "league_id": lid},
            {
                "$set": {
                    "api_key_enc": encrypted,
                    "encryption_key_version": CURRENT_KEY_VERSION,
                    "key_version": next_version,
                    "updated_at": now,
                    "updated_by": actor_id,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        await self.invalidate(provider=provider_name, league_id=lid)
        return {
            "provider": provider_name,
            "scope": normalized_scope,
            "league_id": lid,
            "configured": True,
            "key_version": next_version,
            "updated_at": now,
        }

    async def clear_secret(
        self,
        provider: str,
        *,
        scope: Scope = _SCOPE_GLOBAL,
        league_id: int | None = None,
    ) -> dict[str, Any]:
        provider_name = _norm_provider(provider)
        normalized_scope = _norm_scope(scope)
        lid = await self._resolve_league_id(league_id=league_id)
        if normalized_scope == _SCOPE_GLOBAL:
            lid = None
        await _db.db.provider_secrets.delete_one(
            {"provider": provider_name, "scope": normalized_scope, "league_id": lid},
        )
        await self.invalidate(provider=provider_name, league_id=lid)
        return {
            "provider": provider_name,
            "scope": normalized_scope,
            "league_id": lid,
            "configured": False,
        }

    async def invalidate(self, *, provider: str | None = None, league_id: int | None = None) -> None:
        provider_name = _norm_provider(provider or "") if provider else None
        lid = league_id
        keys = list(self._cache.keys())
        for key in keys:
            if provider_name and not key.startswith(f"{provider_name}:"):
                continue
            if lid is not None and f":{lid}:" not in key:
                continue
            self._cache.pop(key, None)
            self._cache_ts.pop(key, None)

    async def _resolve_league_id(
        self,
        *,
        league_id: int | None = None,
    ) -> int | None:
        if league_id is None:
            return None
        return int(league_id)


provider_settings_service = ProviderSettingsService()
