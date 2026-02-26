"""
backend/tests/test_qtip_backfill_v3.py

Purpose:
    Validate qtip_backfill v3 behavior:
    - v3 snapshot metadata loading
    - enrich_tip receives explicit match object
    - rerun-failed deletes only failed tips
"""

from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, "backend")

import app.database as db_module
import app.services.qbot_intelligence_service as qi_module
import app.services.quotico_tip_service as tip_module
from tools import qtip_backfill


class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._idx = 0

    def sort(self, *_args, **_kwargs):
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs[:length])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._docs):
            raise StopAsyncIteration
        item = self._docs[self._idx]
        self._idx += 1
        return item


class _FakeEngineConfigHistory:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *_args, **_kwargs):
        return _AsyncCursor(self.docs)


class _FakeMatchesCollection:
    def __init__(self, docs, count=None):
        self.docs = docs
        self.count = len(docs) if count is None else count

    async def count_documents(self, _query):
        return self.count

    def find(self, *_args, **_kwargs):
        return _AsyncCursor(self.docs)


class _FakeQuoticoTipsCollection:
    def __init__(self):
        self.deleted_queries = []

    async def delete_many(self, query):
        self.deleted_queries.append(dict(query))
        return SimpleNamespace(deleted_count=1)

    def find(self, *_args, **_kwargs):
        return _AsyncCursor([])

    async def insert_one(self, _doc):
        return None

    async def replace_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_load_history_snapshots_includes_v3_fields(monkeypatch):
    docs = [
        {
            "sport_key": "soccer_germany_bundesliga",
            "snapshot_date": qtip_backfill.datetime(2025, 2, 1),
            "params": {"rho": -0.08, "alpha": 0.005, "floor": 0.05},
            "reliability": {"cap": 0.9},
            "market_performance": {"avg_clv": 0.1},
            "statistical_integrity": {"avg_xg_delta": 0.2},
            "meta": {"source": "time_machine", "schema_version": "v3"},
        }
    ]
    fake_db = SimpleNamespace(engine_config_history=_FakeEngineConfigHistory(docs))
    monkeypatch.setattr(db_module, "db", fake_db, raising=False)

    grouped = await qtip_backfill.load_history_snapshots("soccer_germany_bundesliga")
    assert "soccer_germany_bundesliga" in grouped
    _ts, cache_entry = grouped["soccer_germany_bundesliga"][0]
    assert cache_entry["schema_version"] == "v3"
    assert cache_entry["market_performance"]["avg_clv"] == 0.1
    assert cache_entry["statistical_integrity"]["avg_xg_delta"] == 0.2


@pytest.mark.asyncio
async def test_run_backfill_calls_enrich_tip_with_match(monkeypatch):
    seen = {"match_id": None}
    match = {
        "_id": "m1",
        "sport_key": "soccer_germany_bundesliga",
        "home_team": "A",
        "away_team": "B",
        "home_team_id": "h1",
        "away_team_id": "a1",
        "match_date": qtip_backfill.datetime(2025, 2, 1),
        "status": "final",
        "result": {"outcome": "1"},
        "odds_meta": {"markets": {"h2h": {"current": {"1": 2.1, "X": 3.4, "2": 3.2}}}},
    }
    fake_db = SimpleNamespace(
        name="testdb",
        matches=_FakeMatchesCollection([match]),
        quotico_tips=_FakeQuoticoTipsCollection(),
    )
    monkeypatch.setattr(db_module, "db", fake_db, raising=False)

    async def _fake_connect_db():
        return None

    monkeypatch.setattr(db_module, "connect_db", _fake_connect_db, raising=False)
    async def _fake_load_history(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(qtip_backfill, "load_history_snapshots", _fake_load_history)

    async def _fake_generate(match_doc, before_date=None):
        assert before_date == match_doc["match_date"]
        return {"match_id": str(match_doc["_id"]), "status": "resolved", "was_correct": True}

    async def _fake_enrich(tip, match=None):
        seen["match_id"] = str(match["_id"]) if match else None
        tip["qbot_logic"] = {"archetype": "value_oracle", "post_match_reasoning": {"xg_home": 1.2}}
        return tip

    def _fake_resolve(tip, _match_doc):
        return tip

    monkeypatch.setattr(tip_module, "generate_quotico_tip", _fake_generate)
    monkeypatch.setattr(qi_module, "enrich_tip", _fake_enrich)
    monkeypatch.setattr(tip_module, "resolve_tip", _fake_resolve)

    await qtip_backfill.run_backfill(
        sport_key="soccer_germany_bundesliga",
        batch_size=10,
        skip=0,
        dry_run=True,
        max_batches=1,
        rerun=False,
        rerun_failed=False,
        calibrate=False,
        calibrate_only=False,
    )
    assert seen["match_id"] == "m1"


@pytest.mark.asyncio
async def test_run_backfill_rerun_failed_deletes_error_only(monkeypatch):
    fake_tips = _FakeQuoticoTipsCollection()
    fake_db = SimpleNamespace(
        name="testdb",
        matches=_FakeMatchesCollection([], count=0),
        quotico_tips=fake_tips,
    )
    monkeypatch.setattr(db_module, "db", fake_db, raising=False)

    async def _fake_connect_db():
        return None

    monkeypatch.setattr(db_module, "connect_db", _fake_connect_db, raising=False)
    async def _fake_load_history(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(qtip_backfill, "load_history_snapshots", _fake_load_history)

    await qtip_backfill.run_backfill(
        sport_key="soccer_germany_bundesliga",
        batch_size=50,
        skip=0,
        dry_run=False,
        max_batches=0,
        rerun=False,
        rerun_failed=True,
        calibrate=False,
        calibrate_only=False,
    )
    assert fake_tips.deleted_queries
    assert fake_tips.deleted_queries[0]["status"] == "error"
    assert fake_tips.deleted_queries[0]["sport_key"] == "soccer_germany_bundesliga"


@pytest.mark.asyncio
async def test_run_backfill_rejects_rerun_and_rerun_failed(monkeypatch):
    fake_db = SimpleNamespace(
        name="testdb",
        matches=_FakeMatchesCollection([], count=0),
        quotico_tips=_FakeQuoticoTipsCollection(),
    )
    monkeypatch.setattr(db_module, "db", fake_db, raising=False)

    async def _fake_connect_db():
        return None

    async def _fake_load_history(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(db_module, "connect_db", _fake_connect_db, raising=False)
    monkeypatch.setattr(qtip_backfill, "load_history_snapshots", _fake_load_history)

    with pytest.raises(ValueError):
        await qtip_backfill.run_backfill(
            sport_key=None,
            batch_size=10,
            skip=0,
            dry_run=True,
            max_batches=0,
            rerun=True,
            rerun_failed=True,
            calibrate=False,
            calibrate_only=False,
        )
