from types import SimpleNamespace
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import team_registry_service as trs
from app.services.team_registry_service import normalize_team_name


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _InsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class _FakeTeamsCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query):
        if not query:
            return _FakeCursor(self.docs)
        if "_id" in query:
            _id = query["_id"]
            return _FakeCursor([d for d in self.docs if d.get("_id") == _id])
        return _FakeCursor(self.docs)

    async def find_one(self, query):
        for d in self.docs:
            ok = True
            for k, v in query.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                return dict(d)
        return None

    async def insert_one(self, doc):
        row = dict(doc)
        row["_id"] = row.get("_id", ObjectId())
        self.docs.append(row)
        return _InsertResult(row["_id"])


@pytest.fixture
def registry_factory(monkeypatch):
    def _make(seed_docs):
        fake_db = SimpleNamespace(teams=_FakeTeamsCollection(seed_docs))
        monkeypatch.setattr(trs._db, "db", fake_db, raising=False)
        trs.TeamRegistry._instance = None
        return trs.TeamRegistry.get(), fake_db

    return _make


# --- Normalization ---


def test_normalize_1_fc_koln():
    assert normalize_team_name("1. FC Köln") == "koln"


def test_normalize_fc_koln_same():
    assert normalize_team_name("FC Köln") == "koln"


def test_normalize_borussia_dortmund():
    assert normalize_team_name("Borussia Dortmund") == "borussia dortmund"


def test_normalize_fc_bayern_munchen():
    assert normalize_team_name("FC Bayern München") == "bayern munchen"


def test_normalize_real_madrid():
    assert normalize_team_name("Real Madrid") == "madrid real"


def test_normalize_real_madrid_vs_sociedad():
    assert normalize_team_name("Real Madrid") != normalize_team_name("Real Sociedad")
    assert normalize_team_name("Real Sociedad") == "real sociedad"


def test_normalize_spanish_noise():
    assert normalize_team_name("CD Leganés") == "leganes"
    assert normalize_team_name("UD Almería") == "almeria"


def test_normalize_italian_noise():
    assert normalize_team_name("SSC Napoli") == "napoli"


def test_normalize_empty_string():
    assert normalize_team_name("") == ""


def test_normalize_empty_after_noise():
    assert normalize_team_name("1. FC") == ""


def test_normalize_isolated_1_removed():
    assert "1" not in normalize_team_name("1. FC Köln").split()


# --- Registry ---


@pytest.mark.asyncio
async def test_sport_scoped_hit(registry_factory):
    arsenal_id = ObjectId()
    registry, _ = registry_factory([
        {
            "_id": arsenal_id,
            "normalized_name": normalize_team_name("Arsenal"),
            "display_name": "Arsenal",
            "sport_key": "soccer_epl",
            "aliases": [
                {
                    "name": "Arsenal FC",
                    "normalized": normalize_team_name("Arsenal FC"),
                    "sport_key": "soccer_epl",
                    "source": "seed",
                }
            ],
        }
    ])
    await registry.initialize()
    team_id = await registry.resolve("Arsenal FC", "soccer_epl")
    assert team_id == arsenal_id


@pytest.mark.asyncio
async def test_global_unambiguous_hit(registry_factory):
    arsenal_id = ObjectId()
    registry, _ = registry_factory([
        {
            "_id": arsenal_id,
            "normalized_name": normalize_team_name("Arsenal"),
            "display_name": "Arsenal",
            "sport_key": "soccer_epl",
            "aliases": [
                {
                    "name": "Arsenal FC",
                    "normalized": normalize_team_name("Arsenal FC"),
                    "sport_key": "soccer_epl",
                    "source": "seed",
                }
            ],
        }
    ])
    await registry.initialize()
    team_id = await registry.resolve("Arsenal FC", "soccer_unknown_league")
    assert team_id == arsenal_id


@pytest.mark.asyncio
async def test_global_ambiguous_creates_review(registry_factory):
    madrid_id = ObjectId()
    sociedad_id = ObjectId()
    registry, fake_db = registry_factory([
        {
            "_id": madrid_id,
            "normalized_name": normalize_team_name("Real Madrid"),
            "display_name": "Real Madrid",
            "sport_key": "soccer_spain_la_liga",
            "aliases": [
                {"name": "Real", "normalized": "real", "sport_key": "soccer_spain_la_liga", "source": "seed"}
            ],
        },
        {
            "_id": sociedad_id,
            "normalized_name": normalize_team_name("Real Sociedad"),
            "display_name": "Real Sociedad",
            "sport_key": "soccer_spain_la_liga",
            "aliases": [
                {"name": "Real", "normalized": "real", "sport_key": "soccer_spain_la_liga", "source": "seed"}
            ],
        },
    ])
    await registry.initialize()
    team_id = await registry.resolve("Real", "soccer_epl")
    assert team_id not in (madrid_id, sociedad_id)

    team = await fake_db.teams.find_one({"_id": team_id})
    assert team is not None
    assert team["needs_review"] is True
    assert team["source"] == "auto"
    assert team["auto_reason"] == "global_ambiguous"


@pytest.mark.asyncio
async def test_auto_create_idempotent(registry_factory):
    registry, _ = registry_factory([])
    await registry.initialize()
    id1 = await registry.resolve("Unknown FC", "soccer_epl")
    id2 = await registry.resolve("Unknown FC", "soccer_epl")
    assert id1 == id2


@pytest.mark.asyncio
async def test_auto_create_updates_in_memory_index(registry_factory):
    registry, _ = registry_factory([])
    await registry.initialize()
    team_id = await registry.resolve("Unknown FC", "soccer_epl")
    normalized = normalize_team_name("Unknown FC")
    assert registry.lookup_by_sport[(normalized, "soccer_epl")] == team_id
