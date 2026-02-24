"""Unified team mapping service.

Replaces the dual ``team_aliases`` + ``canonical_map`` system with a single
``team_mappings`` collection.  Each document represents one canonical team
with all known name variants and external provider IDs.

Schema::

    {
      "canonical_id": "bayern_munich",
      "display_name": "Bayern Munich",
      "names": ["FC Bayern München", "Bayern Munich", ...],
      "external_ids": {"openligadb": 40, "theoddsapi": "bayern-munich-fc"},
      "sport_keys": ["soccer_germany_bundesliga"],
      "created_at": ISODate, "updated_at": ISODate,
    }
"""

import logging
import re
import unicodedata
from datetime import datetime

import app.database as _db
from app.utils import utcnow

logger = logging.getLogger("quotico.team_mapping")

# ---------------------------------------------------------------------------
# League codes + season derivation (shared across match_service, matchday_sync)
# ---------------------------------------------------------------------------
SPORT_KEY_TO_LEAGUE_CODE: dict[str, str] = {
    "soccer_germany_bundesliga": "D1",
    "soccer_germany_bundesliga2": "D2",
    "soccer_epl": "E0",
    "soccer_spain_la_liga": "SP1",
    "soccer_italy_serie_a": "I1",
    "soccer_uefa_champs_league": "CL",
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
}


def derive_season_year(match_date: datetime) -> int:
    """Derive season start year from a match date.

    All supported sports have seasons starting in the second half of the year
    (Aug for soccer, Sep for NFL, Oct for NBA).  Matches Jan-Jun belong to
    the previous year's season.
    """
    return match_date.year - 1 if match_date.month <= 6 else match_date.year


def season_code(year: int) -> str:
    """Season start year (2025) → code '2526'."""
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def season_label(year: int) -> str:
    """Season start year (2025) → label '2025/26'."""
    return f"{year}/{(year + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# Name normalization (ported from historical_service)
# ---------------------------------------------------------------------------
_NOISE = {
    "fc", "cf", "sc", "ac", "as", "ss", "us", "afc", "rcd", "1.",
    "club", "de", "sv", "vfb", "vfl", "tsg", "fsv", "bsc", "fk",
}

_SPECIAL_CHARS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
    "ß": "ss", "đ": "d", "ł": "l", "Ł": "L",
})


def team_name_key(name: str) -> str:
    """Normalize a team name to a fuzzy-match key.

    Deterministic: same input always produces the same output.
    Used as the ``home_team_key`` / ``away_team_key`` in match documents
    and as the lookup value in fuzzy resolution.
    """
    name = name.translate(_SPECIAL_CHARS)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.strip().lower()
    tokens = sorted(t for t in name.split() if t not in _NOISE and len(t) >= 3)
    return " ".join(tokens)


def _strip_accents_lower(s: str) -> str:
    s = s.translate(_SPECIAL_CHARS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def make_canonical_id(display_name: str) -> str:
    """Generate a URL-safe slug from a display name."""
    s = _strip_accents_lower(display_name)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


# ---------------------------------------------------------------------------
# Date normalization (compound-key safety)
# ---------------------------------------------------------------------------

def normalize_match_date(dt: datetime) -> datetime:
    """Floor to nearest hour for the compound unique index key (match_date_hour).

    This value is ONLY used for dedup in the compound unique index. The raw
    accurate time is stored in ``match_date`` for display and countdown logic.
    """
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# In-memory cache (loaded from DB on startup)
# ---------------------------------------------------------------------------
# Maps _strip_accents_lower(name) → (canonical_id, display_name, team_key)
_name_cache: dict[str, tuple[str, str, str]] = {}


async def load_cache() -> int:
    """Populate the in-memory name cache from the DB. Returns entry count."""
    _name_cache.clear()
    docs = await _db.db.team_mappings.find(
        {}, {"canonical_id": 1, "display_name": 1, "names": 1},
    ).to_list(length=10_000)

    for doc in docs:
        cid = doc["canonical_id"]
        display = doc["display_name"]
        key = team_name_key(display)
        for name in doc.get("names", []):
            _name_cache[_strip_accents_lower(name)] = (cid, display, key)

    logger.debug("Team mapping cache loaded: %d entries", len(_name_cache))
    return len(_name_cache)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

async def resolve_team(
    name: str, sport_key: str,
) -> tuple[str, str, str] | None:
    """Resolve a provider team name to ``(canonical_id, display_name, team_key)``.

    Resolution strategy (in order):
      1. In-memory cache (exact name match on ``names`` array)
      2. DB lookup on ``names`` array (covers cache misses)
      3. External ID lookup (if caller provides via kwargs)
      4. Normalized key match via ``team_name_key()``
      5. Fuzzy matching (7-step, ported from historical_service)

    On fuzzy success, auto-registers the new name variant.
    Returns ``None`` if resolution fails.
    """
    # 1. In-memory cache (fast path)
    stripped = _strip_accents_lower(name)
    hit = _name_cache.get(stripped)
    if hit:
        return hit

    # 2. DB exact name lookup (multikey index on names)
    doc = await _db.db.team_mappings.find_one(
        {"names": name},
        {"canonical_id": 1, "display_name": 1},
    )
    if not doc:
        # Also try accent-stripped variant
        doc = await _db.db.team_mappings.find_one(
            {"names": stripped},
            {"canonical_id": 1, "display_name": 1},
        )
    if doc:
        result = (doc["canonical_id"], doc["display_name"], team_name_key(doc["display_name"]))
        _name_cache[stripped] = result
        return result

    # 3. Normalized key match — check if computed key matches any existing team
    computed_key = team_name_key(name)
    if not computed_key:
        logger.warning("resolve_team(%r) → empty after normalization", name)
        return None

    # Try to find a mapping whose display_name normalizes to the same key
    all_mappings = await _db.db.team_mappings.find(
        {}, {"canonical_id": 1, "display_name": 1, "names": 1},
    ).to_list(length=5000)

    for mapping in all_mappings:
        if team_name_key(mapping["display_name"]) == computed_key:
            result = (mapping["canonical_id"], mapping["display_name"], computed_key)
            await register_team_name(mapping["canonical_id"], name)
            _name_cache[stripped] = result
            return result

    # 4. Fuzzy matching (containment, longest-token, suffix, stem)
    best = await _fuzzy_match(name, computed_key, all_mappings)
    if best:
        result = (best["canonical_id"], best["display_name"], team_name_key(best["display_name"]))
        await register_team_name(best["canonical_id"], name)
        _name_cache[stripped] = result
        return result

    logger.warning("resolve_team(%r) → FAILED (computed_key=%r)", name, computed_key)
    return None


async def resolve_or_create_team(
    name: str, sport_key: str,
) -> tuple[str, str]:
    """Resolve a team name, or create a new mapping if resolution fails.

    Returns ``(display_name, team_key)`` — always succeeds.
    """
    resolved = await resolve_team(name, sport_key)
    if resolved:
        _, display_name, key = resolved
        return display_name, key

    # Create a new team mapping for this unresolved name
    key = team_name_key(name)
    canonical_id = make_canonical_id(name)
    display_name = name  # use raw provider name as display until admin fixes it
    now = utcnow()

    try:
        await _db.db.team_mappings.update_one(
            {"canonical_id": canonical_id},
            {
                "$addToSet": {"names": name, "sport_keys": sport_key},
                "$setOnInsert": {
                    "canonical_id": canonical_id,
                    "display_name": display_name,
                    "external_ids": {},
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        stripped = _strip_accents_lower(name)
        _name_cache[stripped] = (canonical_id, display_name, key)
        logger.info("Auto-created team mapping: %r → %s", name, canonical_id)
    except Exception as e:
        logger.warning("Failed to auto-create mapping for %r: %s", name, e)

    return display_name, key


async def resolve_team_key(name: str, sport_keys: list[str] | None = None) -> str | None:
    """Convenience wrapper: resolve a team name to just its team_key.

    ``sport_keys`` is accepted for API compatibility but not used for
    filtering (team_mappings is sport-agnostic).
    """
    result = await resolve_team(name, sport_keys[0] if sport_keys else "")
    if result:
        return result[2]  # team_key
    return None


async def register_team_name(canonical_id: str, new_name: str) -> None:
    """Add a new name variant to an existing team mapping.

    Uses ``$addToSet`` to prevent duplicate names even under concurrency.
    """
    try:
        await _db.db.team_mappings.update_one(
            {"canonical_id": canonical_id},
            {
                "$addToSet": {"names": new_name},
                "$set": {"updated_at": utcnow()},
            },
        )
        stripped = _strip_accents_lower(new_name)
        existing = _name_cache.get(stripped)
        if not existing:
            doc = await _db.db.team_mappings.find_one(
                {"canonical_id": canonical_id},
                {"display_name": 1},
            )
            if doc:
                _name_cache[stripped] = (
                    canonical_id, doc["display_name"],
                    team_name_key(doc["display_name"]),
                )
        logger.debug("Registered name variant %r → %s", new_name, canonical_id)
    except Exception:
        pass  # non-critical


# ---------------------------------------------------------------------------
# Fuzzy matching (ported from historical_service resolve_team_key)
# ---------------------------------------------------------------------------

def _stem_variants(token: str) -> list[str]:
    """Return possible stems for a German city adjective form."""
    stems = []
    if len(token) >= 6:
        if token.endswith("er"):
            stems.append(token[:-2])        # hamburger → hamburg
            stems.append(token[:-1])         # karlsruher → karlsruhe
        elif token.endswith("en"):
            stems.append(token[:-2])
    return stems


async def _fuzzy_match(
    name: str, computed_key: str, all_mappings: list[dict],
) -> dict | None:
    """Multi-strategy fuzzy match against existing team mappings."""
    tokens = computed_key.split()
    if not tokens:
        return None

    # Step 1: Containment — stored key ⊂ computed key or vice versa
    last_token = tokens[-1]
    for mapping in all_mappings:
        stored_key = team_name_key(mapping["display_name"])
        if not stored_key:
            continue
        stored_tokens = set(stored_key.split())
        computed_tokens = set(tokens)
        if last_token in stored_key and (
            stored_tokens <= computed_tokens or computed_tokens <= stored_tokens
        ):
            logger.debug(
                "fuzzy(%r) → %s via containment [%s ↔ %s]",
                name, mapping["canonical_id"], computed_key, stored_key,
            )
            return mapping

    # Step 2: Longest token fallback
    longest = max(tokens, key=len) if tokens else ""
    if len(longest) >= 5:
        computed_tokens_set = set(tokens)
        best_candidate = None
        best_overlap = 0

        for mapping in all_mappings:
            stored_key = team_name_key(mapping["display_name"])
            if longest not in stored_key:
                continue
            stored_tokens_set = set(stored_key.split())
            overlap = len(stored_tokens_set & computed_tokens_set)
            if len(stored_tokens_set) == 1 and overlap == 1:
                if best_overlap < 1:
                    best_candidate = mapping
                    best_overlap = 1
            elif overlap >= 2 and overlap > best_overlap:
                best_candidate = mapping
                best_overlap = overlap

        if best_candidate:
            logger.debug(
                "fuzzy(%r) → %s via longest-token [%s, overlap=%d]",
                name, best_candidate["canonical_id"], longest, best_overlap,
            )
            return best_candidate

    # Step 3: Suffix match
    computed_alpha = re.sub(r"[^a-z]", "", computed_key)
    best_match = None
    best_suffix = 0

    for mapping in all_mappings:
        stored_key = team_name_key(mapping["display_name"])
        stored_alpha = re.sub(r"[^a-z]", "", stored_key)
        max_check = min(len(computed_alpha), len(stored_alpha))
        suffix_len = 0
        for i in range(1, max_check + 1):
            if computed_alpha[-i] == stored_alpha[-i]:
                suffix_len = i
            else:
                break
        if suffix_len >= 7 and suffix_len > best_suffix:
            shorter_len = min(len(computed_alpha), len(stored_alpha))
            if shorter_len > 0 and suffix_len / shorter_len >= 0.70:
                best_suffix = suffix_len
                best_match = mapping

    if best_match:
        logger.debug(
            "fuzzy(%r) → %s via suffix [%d common trailing chars]",
            name, best_match["canonical_id"], best_suffix,
        )
        return best_match

    # Step 4: Stem match (German -er/-en suffixes)
    for t in tokens:
        for stem in _stem_variants(t):
            stemmed = [stem if tok == t else tok for tok in tokens]
            stemmed_key = " ".join(sorted(stemmed))
            for mapping in all_mappings:
                if team_name_key(mapping["display_name"]) == stemmed_key:
                    logger.debug(
                        "fuzzy(%r) → %s via stem [%s → %s]",
                        name, mapping["canonical_id"], computed_key, stemmed_key,
                    )
                    return mapping

    return None


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

# Default seed: maps _strip_accents_lower(provider_name) → display_name.
# Grouped by canonical team — multiple provider names per display_name.
_CANONICAL_SEED: dict[str, list[str]] = {
    # --- EPL ---
    "Man City": ["manchester city", "manchester city fc"],
    "Man United": ["manchester united", "manchester united fc"],
    "Tottenham": ["tottenham hotspur", "tottenham hotspur fc"],
    "Brighton": ["brighton and hove albion", "brighton & hove albion", "brighton & hove albion fc"],
    "Nott'm Forest": ["nottingham forest", "nottingham forest fc"],
    "Wolves": ["wolverhampton wanderers", "wolverhampton wanderers fc"],
    "West Ham": ["west ham united", "west ham united fc"],
    "Newcastle": ["newcastle united", "newcastle united fc"],
    "Crystal Palace": ["crystal palace", "crystal palace fc"],
    "Sheffield United": ["sheffield united", "sheffield united fc"],
    "Leeds": ["leeds united", "leeds united fc"],
    "Leicester": ["leicester city", "leicester city fc"],
    "Aston Villa": ["aston villa", "aston villa fc"],
    "Ipswich": ["ipswich town", "ipswich town fc"],
    "Southampton": ["southampton fc"],
    "Bournemouth": ["afc bournemouth"],
    # --- Bundesliga ---
    "Bayern Munich": ["fc bayern munchen", "bayern munchen"],
    "Dortmund": ["borussia dortmund", "bvb dortmund"],
    "Leverkusen": ["bayer leverkusen", "bayer 04 leverkusen"],
    "M'gladbach": ["borussia monchengladbach", "vfl borussia monchengladbach"],
    "Ein Frankfurt": ["eintracht frankfurt"],
    "Freiburg": ["sc freiburg"],
    "Wolfsburg": ["vfl wolfsburg"],
    "Hoffenheim": ["tsg hoffenheim", "tsg 1899 hoffenheim"],
    "Union Berlin": ["1. fc union berlin", "union berlin"],
    "Mainz": ["1. fsv mainz 05", "fsv mainz 05", "mainz 05"],
    "Augsburg": ["fc augsburg"],
    "Werder Bremen": ["sv werder bremen", "werder bremen"],
    "Bochum": ["vfl bochum", "vfl bochum 1848"],
    "Heidenheim": ["1. fc heidenheim 1846", "1. fc heidenheim"],
    "Darmstadt": ["sv darmstadt 98", "darmstadt 98"],
    "Koln": ["1. fc koln", "fc koln", "fc cologne"],
    "Stuttgart": ["vfb stuttgart"],
    "St Pauli": ["fc st. pauli", "fc st pauli", "st. pauli"],
    "Holstein Kiel": ["holstein kiel"],
    "Munster": ["sc preussen munster", "preussen munster"],
    "Hamburg": ["hamburger sv"],
    "Hertha": ["hertha bsc", "hertha berlin"],
    "Schalke 04": ["schalke 04", "fc schalke 04"],
    "RB Leipzig": ["rb leipzig", "rasenballsport leipzig"],
    "Greuther Furth": ["spvgg greuther furth", "greuther furth"],
    "Karlsruhe": ["karlsruher sc"],
    "Elversberg": ["sv elversberg"],
    "Kaiserslautern": ["1. fc kaiserslautern"],
    "Paderborn": ["sc paderborn", "sc paderborn 07"],
    "Magdeburg": ["1. fc magdeburg"],
    "Hannover": ["hannover 96"],
    "Braunschweig": ["eintracht braunschweig"],
    "Fortuna Dusseldorf": ["fortuna dusseldorf"],
    "Nurnberg": ["1. fc nurnberg"],
    "Wehen": ["sv wehen wiesbaden", "wehen wiesbaden"],
    "Regensburg": ["ssv jahn regensburg", "jahn regensburg"],
    # --- La Liga ---
    "Ath Madrid": ["atletico madrid", "club atletico de madrid", "atletico de madrid"],
    "Ath Bilbao": ["athletic bilbao", "athletic club"],
    "Betis": ["real betis", "real betis balompie"],
    "Celta": ["celta vigo", "rc celta de vigo"],
    "Vallecano": ["rayo vallecano", "rayo vallecano de madrid"],
    "Sociedad": ["real sociedad", "real sociedad de futbol"],
    "Alaves": ["deportivo alaves"],
    "Deportivo La Coruna": ["deportivo la coruna", "deportivo", "rc deportivo"],
    "Espanyol": ["rcd espanyol", "rcd espanyol de barcelona"],
    "Leganes": ["cd leganes"],
    "Valladolid": ["real valladolid"],
    "Oviedo": ["real oviedo"],
    # --- Premier League (historical) ---
    "Queens Park Rangers": ["queens park rangers", "qpr"],
    "West Bromwich Albion": ["west bromwich albion", "west brom"],
    # --- Bundesliga (historical) ---
    "Nurnberg": ["nuernberg", "1. fc nurnberg", "fc nurnberg"],
    # --- Serie A ---
    "Inter": ["inter milan", "fc internazionale milano", "internazionale"],
    "AC Milan": ["ac milan"],
    "Atalanta": ["atalanta bc"],
    "Fiorentina": ["acf fiorentina"],
    "Bologna": ["bologna fc 1909"],
    "Cagliari": ["cagliari calcio"],
    "Verona": ["hellas verona", "hellas verona fc"],
    "Sassuolo": ["us sassuolo calcio"],
    "Napoli": ["ssc napoli"],
    "Genoa": ["genoa cfc"],
    "Udinese": ["udinese calcio"],
    "Parma": ["parma calcio 1913"],
    "Como": ["como 1907"],
    "Lecce": ["us lecce"],
    "Torino": ["torino fc"],
    "Empoli": ["empoli fc"],
    "Salernitana": ["us salernitana 1919"],
    "Frosinone": ["frosinone calcio"],
    "Monza": ["ac monza"],
    "Pisa": ["ac pisa 1909"],
    "Venezia": ["venezia fc"],
    # --- Champions League / European ---
    "Paris SG": ["paris saint germain", "paris saint-germain"],
    "Monaco": ["as monaco"],
    "Benfica": ["sl benfica"],
    "Club Brugge": ["club brugge"],
    "Olympiakos": ["olympiakos piraeus"],
    "Bodo Glimt": ["bodo/glimt"],
    "Qarabag": ["qarabag fk"],
    "Red Star": ["red star belgrade"],
    "Dynamo Kyiv": ["dynamo kyiv"],
    "Salzburg": ["fc salzburg", "red bull salzburg"],
    "Sporting CP": ["sporting cp", "sporting lisbon"],
}


async def seed_team_mappings() -> int:
    """Seed the ``team_mappings`` collection from defaults.

    Uses ``$addToSet`` so repeated runs never create duplicates and
    ``$setOnInsert`` so admin edits to display_name are preserved.
    Returns number of new documents created.
    """
    from pymongo import UpdateOne

    now = utcnow()
    ops = []
    for display_name, provider_names in _CANONICAL_SEED.items():
        canonical_id = make_canonical_id(display_name)
        # Include the display_name itself in the names array
        all_names = [display_name] + provider_names
        ops.append(
            UpdateOne(
                {"canonical_id": canonical_id},
                {
                    "$addToSet": {"names": {"$each": all_names}},
                    "$setOnInsert": {
                        "canonical_id": canonical_id,
                        "display_name": display_name,
                        "external_ids": {},
                        "sport_keys": [],
                        "created_at": now,
                    },
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )
        )

    if not ops:
        return 0

    result = await _db.db.team_mappings.bulk_write(ops, ordered=False)
    inserted = result.upserted_count
    if inserted:
        logger.info("Seeded %d new team mappings", inserted)
    return inserted
