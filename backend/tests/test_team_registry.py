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

    async def find_one(self, query, projection=None):
        def _get_nested(doc, dotted):
            value = doc
            for part in dotted.split("."):
                if not isinstance(value, dict):
                    return None
                value = value.get(part)
            return value

        for d in self.docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict) and "$exists" in v:
                    exists = _get_nested(d, k) is not None if "." in k else d.get(k) is not None
                    if exists != bool(v.get("$exists")):
                        ok = False
                        break
                elif isinstance(v, dict) and "$in" in v:
                    current = _get_nested(d, k) if "." in k else d.get(k)
                    if current not in list(v.get("$in", [])):
                        ok = False
                        break
                elif "." in k:
                    if _get_nested(d, k) != v:
                        ok = False
                        break
                elif d.get(k) != v:
                    ok = False
                    break
            if ok:
                if projection:
                    return {k: v for k, v in d.items() if k in projection or k == "_id"}
                return dict(d)
        return None

    async def insert_one(self, doc):
        row = dict(doc)
        row["_id"] = row.get("_id", ObjectId())
        self.docs.append(row)
        return _InsertResult(row["_id"])

    async def update_one(self, query, update):
        target = await self.find_one(query)
        if not target:
            return
        for row in self.docs:
            if row.get("_id") == target.get("_id"):
                for key, value in update.get("$addToSet", {}).items():
                    row.setdefault(key, [])
                    if value not in row[key]:
                        row[key].append(value)
                for dotted_key, value in update.get("$set", {}).items():
                    if "." in dotted_key:
                        head, tail = dotted_key.split(".", 1)
                        row.setdefault(head, {})
                        if isinstance(row[head], dict):
                            row[head][tail] = value
                    else:
                        row[dotted_key] = value
                break


class _FakeAliasSuggestionsCollection:
    def __init__(self):
        self.docs = []

    async def update_one(self, query, update, upsert=False):
        for idx, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                merged = dict(doc)
                merged.update(update.get("$set", {}))
                merged["seen_count"] = int(merged.get("seen_count", 0)) + int(update.get("$inc", {}).get("seen_count", 0))
                if "$push" in update:
                    push_def = update["$push"].get("sample_refs", {})
                    each = list(push_def.get("$each", []))
                    merged.setdefault("sample_refs", [])
                    merged["sample_refs"].extend(each)
                    slice_size = int(push_def.get("$slice", 0))
                    if slice_size < 0:
                        merged["sample_refs"] = merged["sample_refs"][slice_size:]
                self.docs[idx] = merged
                return SimpleNamespace(upserted_id=None)

        if upsert:
            new_doc = dict(query)
            new_doc["_id"] = ObjectId()
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            new_doc["seen_count"] = int(new_doc.get("seen_count", 0)) + int(update.get("$inc", {}).get("seen_count", 0))
            if "$push" in update:
                push_def = update["$push"].get("sample_refs", {})
                each = list(push_def.get("$each", []))
                new_doc["sample_refs"] = each
            self.docs.append(new_doc)
            return SimpleNamespace(upserted_id=new_doc["_id"])
        return SimpleNamespace(upserted_id=None)

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                if projection:
                    return {k: v for k, v in doc.items() if k in projection or k == "_id"}
                return dict(doc)
        return None


@pytest.fixture
def registry_factory(monkeypatch):
    def _make(seed_docs):
        async def _fake_get_league(_sport_key):
            return None

        fake_db = SimpleNamespace(teams=_FakeTeamsCollection(seed_docs))
        monkeypatch.setattr(trs._db, "db", fake_db, raising=False)
        monkeypatch.setattr(
            trs.LeagueRegistry,
            "get",
            staticmethod(lambda: SimpleNamespace(get_league=_fake_get_league)),
        )
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
async def test_resolve_without_create_returns_none_on_miss(registry_factory):
    registry, fake_db = registry_factory([])
    await registry.initialize()
    team_id = await registry.resolve("Unknown FC", "soccer_epl", create_if_missing=False)
    assert team_id is None
    assert len(fake_db.teams.docs) == 0


@pytest.mark.asyncio
async def test_auto_create_updates_in_memory_index(registry_factory):
    registry, _ = registry_factory([])
    await registry.initialize()
    team_id = await registry.resolve("Unknown FC", "soccer_epl")
    normalized = normalize_team_name("Unknown FC")
    assert registry.lookup_by_sport[(normalized, "soccer_epl")] == team_id


@pytest.mark.asyncio
async def test_resolve_by_external_id_hit(registry_factory):
    team_id = ObjectId()
    registry, _ = registry_factory([
        {
            "_id": team_id,
            "normalized_name": normalize_team_name("Bayern Munich"),
            "display_name": "Bayern Munich",
            "sport_key": "soccer_germany_bundesliga",
            "aliases": [],
            "external_ids": {"openligadb": "40"},
        }
    ])
    await registry.initialize()
    resolved = await registry.resolve_by_external_id_or_name(
        source="openligadb",
        external_id="40",
        name="FC Bayern München",
        sport_key="soccer_germany_bundesliga",
    )
    assert resolved == team_id


@pytest.mark.asyncio
async def test_resolve_by_external_id_conflict_returns_none(registry_factory):
    team_id = ObjectId()
    registry, _ = registry_factory([
        {
            "_id": team_id,
            "normalized_name": normalize_team_name("Borussia Dortmund"),
            "display_name": "Borussia Dortmund",
            "sport_key": "soccer_germany_bundesliga",
            "aliases": [],
            "external_ids": {"openligadb": "7"},
        }
    ])
    await registry.initialize()
    resolved = await registry.resolve_by_external_id_or_name(
        source="openligadb",
        external_id="7",
        name="Bayern Munich",
        sport_key="soccer_germany_bundesliga",
        create_if_missing=True,
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_by_external_id_name_fallback_backfills_id(registry_factory):
    team_id = ObjectId()
    registry, fake_db = registry_factory([
        {
            "_id": team_id,
            "normalized_name": normalize_team_name("Bayern Munich"),
            "display_name": "Bayern Munich",
            "sport_key": "soccer_germany_bundesliga",
            "aliases": [],
        }
    ])
    await registry.initialize()
    resolved = await registry.resolve_by_external_id_or_name(
        source="openligadb",
        external_id="40",
        name="Bayern Munich",
        sport_key="soccer_germany_bundesliga",
        create_if_missing=True,
    )
    assert resolved == team_id
    updated = await fake_db.teams.find_one({"_id": team_id})
    assert updated is not None
    assert updated.get("external_ids", {}).get("openligadb") == "40"


@pytest.mark.asyncio
async def test_add_alias_adds_and_is_idempotent(registry_factory):
    team_id = ObjectId()
    registry, fake_db = registry_factory([
        {
            "_id": team_id,
            "normalized_name": normalize_team_name("Arsenal"),
            "display_name": "Arsenal",
            "sport_key": "soccer_epl",
            "aliases": [],
        }
    ])
    await registry.initialize()
    created = await registry.add_alias(team_id, "Arsenal FC", sport_key="soccer_epl", refresh_cache=False)
    assert created is True
    created_again = await registry.add_alias(team_id, "Arsenal FC", sport_key="soccer_epl", refresh_cache=False)
    assert created_again is False
    updated = await fake_db.teams.find_one({"_id": team_id})
    assert updated is not None
    assert any(alias.get("name") == "Arsenal FC" for alias in updated.get("aliases", []))


@pytest.mark.asyncio
async def test_add_alias_missing_team_raises(registry_factory):
    registry, _ = registry_factory([])
    await registry.initialize()
    with pytest.raises(ValueError):
        await registry.add_alias(ObjectId(), "Ghost Team", refresh_cache=False)


@pytest.mark.asyncio
async def test_record_alias_suggestion_dedupes_and_increments(monkeypatch, registry_factory):
    registry, fake_db = registry_factory([])
    fake_aliases = _FakeAliasSuggestionsCollection()
    monkeypatch.setattr(trs._db, "db", SimpleNamespace(teams=fake_db.teams, team_alias_suggestions=fake_aliases), raising=False)
    await registry.initialize()

    league_id = ObjectId()
    first_id = await registry.record_alias_suggestion(
        source="openligadb",
        raw_team_name="FC Pauli",
        sport_key="soccer_germany_bundesliga",
        league_id=league_id,
        reason="unresolved_team",
        sample_ref={"match_external_id": "123", "side": "home"},
    )
    second_id = await registry.record_alias_suggestion(
        source="openligadb",
        raw_team_name="FC Pauli",
        sport_key="soccer_germany_bundesliga",
        league_id=league_id,
        reason="unresolved_team",
        sample_ref={"match_external_id": "456", "side": "away"},
    )

    assert first_id is not None
    assert second_id == first_id
    doc = await fake_aliases.find_one({"_id": first_id})
    assert doc is not None
    assert doc["seen_count"] == 2
    assert len(doc.get("sample_refs", [])) == 2
