"""
backend/tests/test_provider_audit_logging.py

Purpose:
    Verify provider settings admin actions emit mandatory audit log events.

Dependencies:
    - app.routers.admin
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.routers import admin as admin_router


def _request() -> Request:
    return Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})


@pytest.mark.asyncio
async def test_provider_settings_update_writes_audit(monkeypatch):
    audit_calls: list[dict] = []

    async def _fake_set_settings(*_args, **_kwargs):
        return {"updated_fields": ["rate_limit_rpm"], "scope": "global", "league_id": None}

    async def _fake_log_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        admin_router,
        "provider_settings_service",
        SimpleNamespace(set_settings=_fake_set_settings),
        raising=False,
    )
    monkeypatch.setattr(admin_router, "log_audit", _fake_log_audit)

    body = admin_router.ProviderSettingsPatchBody(rate_limit_rpm=9)
    await admin_router.patch_provider_settings_global(
        provider="football_data",
        body=body,
        request=_request(),
        admin={"_id": "admin-1"},
    )

    assert audit_calls
    assert audit_calls[0]["action"] == "PROVIDER_SETTINGS_UPDATE"


@pytest.mark.asyncio
async def test_provider_secret_set_and_clear_write_audit(monkeypatch):
    audit_calls: list[dict] = []

    async def _fake_set_secret(*_args, **_kwargs):
        return {"scope": "global", "league_id": None, "key_version": 2, "updated_at": None}

    async def _fake_clear_secret(*_args, **_kwargs):
        return {"scope": "global", "league_id": None, "configured": False}

    async def _fake_log_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        admin_router,
        "provider_settings_service",
        SimpleNamespace(
            set_secret=_fake_set_secret,
            clear_secret=_fake_clear_secret,
        ),
        raising=False,
    )
    monkeypatch.setattr(admin_router, "log_audit", _fake_log_audit)

    await admin_router.set_provider_secret(
        provider="football_data",
        body=admin_router.ProviderSecretSetBody(scope="global", api_key="abc"),
        request=_request(),
        admin={"_id": "admin-1"},
    )
    await admin_router.clear_provider_secret(
        provider="football_data",
        body=admin_router.ProviderSecretClearBody(scope="global"),
        request=_request(),
        admin={"_id": "admin-1"},
    )

    actions = [call["action"] for call in audit_calls]
    assert "PROVIDER_SECRET_SET" in actions
    assert "PROVIDER_SECRET_CLEAR" in actions


@pytest.mark.asyncio
async def test_provider_probe_writes_audit(monkeypatch):
    audit_calls: list[dict] = []

    async def _fake_get_effective(*_args, **_kwargs):
        return {
            "effective_config": {
                "enabled": True,
                "base_url": "https://example.test",
                "api_key": "abc",
            },
            "source_map": {"base_url": "db_global"},
        }

    async def _fake_log_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        admin_router,
        "provider_settings_service",
        SimpleNamespace(get_effective=_fake_get_effective),
        raising=False,
    )
    monkeypatch.setattr(admin_router, "log_audit", _fake_log_audit)

    await admin_router.probe_provider_config(
        provider="football_data",
        body=admin_router.ProviderProbeBody(),
        request=_request(),
        admin={"_id": "admin-1"},
    )

    assert audit_calls
    assert audit_calls[0]["action"] == "PROVIDER_CONFIG_PROBE"

