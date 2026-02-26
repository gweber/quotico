"""
backend/tests/test_sportmonks_provider.py

Purpose:
    Validate Sportmonks provider compliance with official v3 API behavior:
    header auth usage, paginated discovery merge, and rate-limit parsing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sys

import pytest

sys.path.insert(0, "backend")

from app.providers.sportmonks import SportmonksProvider


class _FakeResponse:
    def __init__(self, payload: dict, headers: dict[str, str] | None = None, status_code: int = 200) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_round_fixtures_uses_plural_referees_and_flattens_payload(monkeypatch):
    provider = SportmonksProvider()
    fake_client = _FakeClient(
        [
            _FakeResponse(
                payload={"data": {"id": 1, "fixtures": [{"id": 10}, {"id": 11}]}},
                headers={"X-RateLimit-Remaining": "123", "X-RateLimit-Reset": "999999"},
            )
        ]
    )
    monkeypatch.setattr(provider, "_client", fake_client)
    monkeypatch.setattr(provider, "_auth_token", lambda: "secret")

    result = await provider.get_round_fixtures(77)

    include = fake_client.calls[0]["params"]["include"]
    include_tokens = set(str(include).split(";"))
    assert "fixtures.participants" in include_tokens
    assert "fixtures.referees" in include_tokens
    assert "fixtures.referees.referee" in include_tokens
    assert "fixtures.referee" not in include_tokens
    assert result["payload"]["data"] == [{"id": 10}, {"id": 11}]
    assert result["remaining"] == 123
    assert result["reset_at"] == 999999


@pytest.mark.asyncio
async def test_discovery_pagination_merges_data_and_uses_rate_limit_fallback(monkeypatch):
    provider = SportmonksProvider()
    fake_client = _FakeClient(
        [
            _FakeResponse(
                payload={
                    "data": [{"id": 1}],
                    "pagination": {"has_more": True, "next_page": "https://api.sportmonks.com/v3/football/leagues?page=2"},
                    "rate_limit": {"remaining": 88, "resets_in_seconds": 9},
                },
                headers={},
            ),
            _FakeResponse(
                payload={
                    "data": [{"id": 2}],
                    "pagination": {"has_more": False, "next_page": None},
                    "rate_limit": {"remaining": 77, "resets_in_seconds": 20},
                },
                headers={},
            ),
        ]
    )
    monkeypatch.setattr(provider, "_client", fake_client)
    monkeypatch.setattr(provider, "_auth_token", lambda: "secret")

    before = int(datetime.now(timezone.utc).timestamp())
    result = await provider.get_leagues_with_seasons_country()
    after = int(datetime.now(timezone.utc).timestamp())

    assert result["payload"]["data"] == [{"id": 1}, {"id": 2}]
    assert result["remaining"] == 77
    assert before + 20 <= int(result["reset_at"]) <= after + 20
    assert fake_client.calls[0]["params"]["include"] == "seasons;country"
    assert fake_client.calls[1]["params"] == {}
