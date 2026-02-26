"""
backend/tests/test_quotico_tips_backfill_admin_guard.py

Purpose:
    Validate admin backfill guardrails and response metrics for quotico_tips router.
"""

from types import SimpleNamespace
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, "backend")

from app.routers import quotico_tips as router


class _FindCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs[:length])


class _Matches:
    def __init__(self, docs, count):
        self.docs = docs
        self.count = count

    async def count_documents(self, _query):
        return self.count

    def find(self, *_args, **_kwargs):
        return _FindCursor(self.docs)


class _Tips:
    def __init__(self):
        self.inserted = []

    def find(self, *_args, **_kwargs):
        return _FindCursor([])

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))


@pytest.mark.asyncio
async def test_admin_backfill_rejects_oversize_scope(monkeypatch):
    fake_db = SimpleNamespace(matches=_Matches([], count=100), quotico_tips=_Tips())
    monkeypatch.setattr(router._db, "db", fake_db, raising=False)
    monkeypatch.setattr(router.settings, "QTIP_BACKFILL_ADMIN_MAX_MATCHES", 10, raising=False)

    with pytest.raises(HTTPException) as exc:
        await router.backfill_quotico_tips(
            sport_key="soccer_germany_bundesliga",
            batch_size=10,
            skip=0,
            dry_run=True,
            admin={"_id": "admin"},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_admin_backfill_returns_new_metrics(monkeypatch):
    matches = [
        {
            "_id": "m1",
            "sport_key": "soccer_germany_bundesliga",
            "home_team": "A",
            "away_team": "B",
            "match_date": "2025-02-01T00:00:00",
            "odds_meta": {"markets": {"h2h": {"current": {"1": 2.1, "X": 3.2, "2": 3.8}}}},
            "result": {"outcome": "1"},
        },
        {
            "_id": "m2",
            "sport_key": "soccer_germany_bundesliga",
            "home_team": "C",
            "away_team": "D",
            "match_date": "2025-02-02T00:00:00",
            "odds_meta": {"markets": {"h2h": {"current": {"1": 2.0, "X": 3.1, "2": 4.1}}}},
            "result": {"outcome": "2"},
        },
    ]
    fake_db = SimpleNamespace(matches=_Matches(matches, count=2), quotico_tips=_Tips())
    monkeypatch.setattr(router._db, "db", fake_db, raising=False)
    monkeypatch.setattr(router.settings, "QTIP_BACKFILL_ADMIN_MAX_MATCHES", 10, raising=False)

    async def _fake_generate(match, before_date=None):
        assert before_date == match["match_date"]
        return {"match_id": str(match["_id"]), "status": "active", "was_correct": None}

    async def _fake_enrich(tip, match=None):
        if str(match["_id"]) == "m1":
            tip["qbot_logic"] = {
                "archetype": "value_oracle",
                "post_match_reasoning": {"xg_home": 1.1, "xg_away": 0.9},
            }
            tip["status"] = "resolved"
            tip["was_correct"] = True
        else:
            tip["qbot_logic"] = {"archetype": "steady_hand"}
            tip["status"] = "no_signal"
        return tip

    def _fake_resolve(tip, _match):
        return tip

    monkeypatch.setattr(router, "generate_quotico_tip", _fake_generate)
    monkeypatch.setattr(router, "enrich_tip", _fake_enrich)
    monkeypatch.setattr(router, "resolve_tip", _fake_resolve)

    result = await router.backfill_quotico_tips(
        sport_key="soccer_germany_bundesliga",
        batch_size=10,
        skip=0,
        dry_run=True,
        admin={"_id": "admin"},
    )

    assert "xg_coverage_pct" in result
    assert "archetype_distribution" in result
    assert "no_signal_rate_pct" in result
    assert "skipped_missing_odds_meta" in result
    assert result["generated"] == 1
    assert result["no_signal"] == 1
    assert result["no_signal_rate_pct"] == 50.0
    assert result["xg_coverage_pct"] == 50.0
