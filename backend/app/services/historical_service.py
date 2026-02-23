"""Historical match context service.

Provides team name resolution (6-step fuzzy matching) and H2H/form context
building. Used by both the historical router and spieltag router.
"""

import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta

import app.database as _db
from app.utils import utcnow

logger = logging.getLogger("quotico.historical_service")

# ---------------------------------------------------------------------------
# In-memory cache for match-context responses (historical data rarely changes)
# ---------------------------------------------------------------------------
_context_cache: dict[str, tuple[float, dict]] = {}  # key → (expires_at, response)
_CACHE_TTL = 3600  # 1 hour


def cache_get(key: str) -> dict | None:
    entry = _context_cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    if entry:
        del _context_cache[key]
    return None


def cache_set(key: str, value: dict) -> None:
    _context_cache[key] = (time.monotonic() + _CACHE_TTL, value)


def clear_context_cache() -> None:
    _context_cache.clear()
    logger.info("Match-context cache cleared")


# ---------------------------------------------------------------------------
# Related sport keys: H2H spans across divisions (e.g. BL1 + BL2)
# ---------------------------------------------------------------------------
_ALL_SOCCER_KEYS = [
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_uefa_champs_league",
]

RELATED_SPORT_KEYS: dict[str, list[str]] = {k: _ALL_SOCCER_KEYS for k in _ALL_SOCCER_KEYS}


def sport_keys_for(sport_key: str) -> list[str]:
    """Return the sport_key(s) to query for H2H/form (spans related leagues)."""
    return RELATED_SPORT_KEYS.get(sport_key, [sport_key])


# ---------------------------------------------------------------------------
# Auto-archive: league codes + season derivation (mirrors tools/scrapper.py)
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


def _derive_season_year(commence_time: datetime) -> int:
    """Derive season start year from a match date.

    All supported sports have seasons starting in the second half of the year
    (Aug for soccer, Sep for NFL, Oct for NBA). Matches Jan-Jun belong to the
    previous year's season.
    """
    return commence_time.year - 1 if commence_time.month <= 6 else commence_time.year


def _season_code(year: int) -> str:
    """Season start year (2025) → code '2526'."""
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def _season_label(year: int) -> str:
    """Season start year (2025) → label '2025/26'."""
    return f"{year}/{(year + 1) % 100:02d}"


async def archive_resolved_match(
    match: dict, result: str, home_score: int, away_score: int,
) -> None:
    """Upsert a resolved match into historical_matches.

    Called from _resolve_match() in match_resolver. Fire-and-forget:
    failures are logged but never raised to the caller.

    Dedup strategy: check for an existing record by normalized team keys
    + date window (±6h). If a scraper record already exists (from
    football-data.co.uk), only merge in the quotico odds — never
    overwrite the richer scraper data (multi-bookmaker odds, stats, etc.).
    """
    sport_key = match.get("sport_key", "")
    league_code = SPORT_KEY_TO_LEAGUE_CODE.get(sport_key)
    if not league_code:
        return  # unsupported sport (e.g. tennis) — skip silently

    teams = match.get("teams", {})
    home_team = teams.get("home", "")
    away_team = teams.get("away", "")
    commence_time = match.get("commence_time")
    if not home_team or not away_team or not commence_time:
        logger.warning("archive_resolved_match: missing data, skipping %s", match.get("_id"))
        return

    season_year = _derive_season_year(commence_time)
    season_code = _season_code(season_year)
    related_keys = sport_keys_for(sport_key)

    # Resolve team names via fuzzy matching (alias table) so provider name
    # differences ("1. FC Heidenheim 1846" vs "Heidenheim") get the same key
    home_team_key = await resolve_team_key(home_team, related_keys) or team_name_key(home_team)
    away_team_key = await resolve_team_key(away_team, related_keys) or team_name_key(away_team)

    # Check if a record already exists (scraper or previous archive) by
    # resolved team keys + date window — handles different team name spellings
    existing = await _db.db.historical_matches.find_one({
        "sport_key": {"$in": related_keys},
        "season": season_code,
        "home_team_key": home_team_key,
        "away_team_key": away_team_key,
        "match_date": {
            "$gte": commence_time - timedelta(hours=6),
            "$lte": commence_time + timedelta(hours=6),
        },
    })

    if existing:
        # Record exists — only merge quotico odds, don't overwrite scraper data
        merge: dict = {"updated_at": utcnow()}

        raw_odds = match.get("current_odds", {})
        if raw_odds:
            entry: dict[str, float] = {}
            if "1" in raw_odds:
                entry["home"] = raw_odds["1"]
            if "X" in raw_odds:
                entry["draw"] = raw_odds["X"]
            if "2" in raw_odds:
                entry["away"] = raw_odds["2"]
            if entry:
                merge["odds.quotico"] = entry

        totals = match.get("totals_odds", {})
        if totals and "over" in totals and "under" in totals:
            merge["over_under_odds.quotico"] = {
                "over": totals["over"],
                "under": totals["under"],
                "line": totals.get("line", 2.5),
            }

        spreads = match.get("spreads_odds", {})
        if spreads and "home_line" in spreads:
            merge["spreads_odds.quotico"] = spreads

        try:
            await _db.db.historical_matches.update_one(
                {"_id": existing["_id"]}, {"$set": merge},
            )
            logger.info(
                "Merged quotico odds into existing archive: %s vs %s",
                existing.get("home_team"), existing.get("away_team"),
            )
        except Exception as e:
            logger.warning("Failed to merge archive: %s", e)
        return

    # No existing record — create a new one (match not covered by scraper)
    # Transform odds: {"1": x, "X": y, "2": z} → {"quotico": {"home": x, ...}}
    odds = None
    raw_odds = match.get("current_odds", {})
    if raw_odds:
        entry2: dict[str, float] = {}
        if "1" in raw_odds:
            entry2["home"] = raw_odds["1"]
        if "X" in raw_odds:
            entry2["draw"] = raw_odds["X"]
        if "2" in raw_odds:
            entry2["away"] = raw_odds["2"]
        if entry2:
            odds = {"quotico": entry2}

    over_under_odds = None
    totals = match.get("totals_odds", {})
    if totals and "over" in totals and "under" in totals:
        over_under_odds = {"quotico": {
            "over": totals["over"],
            "under": totals["under"],
            "line": totals.get("line", 2.5),
        }}

    spreads_odds = None
    spreads = match.get("spreads_odds", {})
    if spreads and "home_line" in spreads:
        spreads_odds = {"quotico": spreads}

    now = utcnow()
    doc: dict = {
        "sport_key": sport_key,
        "league_code": league_code,
        "season": season_code,
        "season_label": _season_label(season_year),
        "match_date": commence_time,
        "home_team": home_team,
        "away_team": away_team,
        "home_team_key": home_team_key,
        "away_team_key": away_team_key,
        "home_goals": home_score,
        "away_goals": away_score,
        "result": result,
        "updated_at": now,
    }
    if odds:
        doc["odds"] = odds
    if over_under_odds:
        doc["over_under_odds"] = over_under_odds
    if spreads_odds:
        doc["spreads_odds"] = spreads_odds

    try:
        await _db.db.historical_matches.update_one(
            {
                "sport_key": sport_key,
                "season": season_code,
                "home_team_key": home_team_key,
                "away_team_key": away_team_key,
                "match_date": commence_time,
            },
            {"$set": doc, "$setOnInsert": {"imported_at": now}},
            upsert=True,
        )
        logger.info("Archived %s vs %s (%s %d-%d)", home_team, away_team, result, home_score, away_score)
    except Exception as e:
        logger.warning("Failed to archive match: %s", e)
        return

    # Register team aliases so future H2H lookups work
    try:
        await _auto_alias(home_team, related_keys, home_team_key)
        await _auto_alias(away_team, related_keys, away_team_key)
    except Exception:
        pass  # non-critical

    clear_context_cache()


# ---------------------------------------------------------------------------
# Team name normalization (mirrors tools/scrapper.py team_name_key)
# ---------------------------------------------------------------------------
_NOISE = {"fc", "cf", "sc", "ac", "as", "ss", "us", "afc", "rcd", "1.",
          "club", "de", "sv", "vfb", "vfl", "tsg", "fsv", "bsc", "fk"}


# Special character replacements not handled by NFKD decomposition
_SPECIAL_CHARS = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
                                 "ß": "ss", "đ": "d", "ł": "l", "Ł": "L"})


def team_name_key(name: str) -> str:
    """Normalize a team name to a fuzzy-match key (same logic as scrapper.py)."""
    name = name.translate(_SPECIAL_CHARS)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.strip().lower()
    tokens = sorted(t for t in name.split() if t not in _NOISE and len(t) >= 3)
    return " ".join(tokens)


def _strip_accents_lower(s: str) -> str:
    """Normalize name for static alias lookup (matches team_name_key char handling)."""
    s = s.translate(_SPECIAL_CHARS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


# ---------------------------------------------------------------------------
# Canonical team name mapping (DB-backed, seeded from defaults)
# ---------------------------------------------------------------------------
# Maps _strip_accents_lower(provider_name) → football-data.co.uk short name.
# team_name_key(canonical) produces the DB-stored key.
# Loaded from the `canonical_map` collection; seeded on startup.

# In-memory cache (loaded from DB)
_canonical_cache: dict[str, str] = {}

# Default seed data — inserted into DB on first startup, never overwrites admin edits.
_CANONICAL_SEED: dict[str, str] = {
    # --- EPL ---
    "manchester city": "Man City",
    "manchester city fc": "Man City",
    "manchester united": "Man United",
    "manchester united fc": "Man United",
    "tottenham hotspur": "Tottenham",
    "tottenham hotspur fc": "Tottenham",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "brighton & hove albion fc": "Brighton",
    "nottingham forest": "Nott'm Forest",
    "nottingham forest fc": "Nott'm Forest",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton wanderers fc": "Wolves",
    "west ham united": "West Ham",
    "west ham united fc": "West Ham",
    "newcastle united": "Newcastle",
    "newcastle united fc": "Newcastle",
    "crystal palace": "Crystal Palace",
    "crystal palace fc": "Crystal Palace",
    "sheffield united": "Sheffield United",
    "sheffield united fc": "Sheffield United",
    "leeds united": "Leeds",
    "leeds united fc": "Leeds",
    "leicester city": "Leicester",
    "leicester city fc": "Leicester",
    "aston villa": "Aston Villa",
    "aston villa fc": "Aston Villa",
    "ipswich town": "Ipswich",
    "ipswich town fc": "Ipswich",
    "southampton fc": "Southampton",
    "afc bournemouth": "Bournemouth",
    # --- Bundesliga ---
    "fc bayern munchen": "Bayern Munich",
    "bayern munchen": "Bayern Munich",
    "borussia dortmund": "Dortmund",
    "bvb dortmund": "Dortmund",
    "bayer leverkusen": "Leverkusen",
    "bayer 04 leverkusen": "Leverkusen",
    "borussia monchengladbach": "M'gladbach",
    "vfl borussia monchengladbach": "M'gladbach",
    "eintracht frankfurt": "Ein Frankfurt",
    "sc freiburg": "Freiburg",
    "vfl wolfsburg": "Wolfsburg",
    "tsg hoffenheim": "Hoffenheim",
    "tsg 1899 hoffenheim": "Hoffenheim",
    "1. fc union berlin": "Union Berlin",
    "union berlin": "Union Berlin",
    "1. fsv mainz 05": "Mainz",
    "fsv mainz 05": "Mainz",
    "mainz 05": "Mainz",
    "fc augsburg": "Augsburg",
    "sv werder bremen": "Werder Bremen",
    "werder bremen": "Werder Bremen",
    "vfl bochum": "Bochum",
    "vfl bochum 1848": "Bochum",
    "1. fc heidenheim 1846": "Heidenheim",
    "1. fc heidenheim": "Heidenheim",
    "sv darmstadt 98": "Darmstadt",
    "darmstadt 98": "Darmstadt",
    "1. fc koln": "Koln",
    "fc koln": "Koln",
    "vfb stuttgart": "Stuttgart",
    "fc st. pauli": "St Pauli",
    "fc st pauli": "St Pauli",
    "st. pauli": "St Pauli",
    "holstein kiel": "Holstein Kiel",
    "sc preussen munster": "Munster",
    "preussen munster": "Munster",
    "hamburger sv": "Hamburg",
    "hertha bsc": "Hertha",
    "hertha berlin": "Hertha",
    "schalke 04": "Schalke 04",
    "fc schalke 04": "Schalke 04",
    "rb leipzig": "RB Leipzig",
    "rasenballsport leipzig": "RB Leipzig",
    "spvgg greuther furth": "Greuther Furth",
    "greuther furth": "Greuther Furth",
    "karlsruher sc": "Karlsruhe",
    "sv elversberg": "Elversberg",
    "1. fc kaiserslautern": "Kaiserslautern",
    "sc paderborn": "Paderborn",
    "sc paderborn 07": "Paderborn",
    "1. fc magdeburg": "Magdeburg",
    "hannover 96": "Hannover",
    "eintracht braunschweig": "Braunschweig",
    "fortuna dusseldorf": "Fortuna Dusseldorf",
    "1. fc nurnberg": "Nurnberg",
    "sv wehen wiesbaden": "Wehen",
    "wehen wiesbaden": "Wehen",
    "ssv jahn regensburg": "Regensburg",
    "jahn regensburg": "Regensburg",
    # --- La Liga ---
    "atletico madrid": "Ath Madrid",
    "club atletico de madrid": "Ath Madrid",
    "atletico de madrid": "Ath Madrid",
    "athletic bilbao": "Ath Bilbao",
    "athletic club": "Ath Bilbao",
    "real betis": "Betis",
    "real betis balompie": "Betis",
    "celta vigo": "Celta",
    "rc celta de vigo": "Celta",
    "rayo vallecano": "Vallecano",
    "rayo vallecano de madrid": "Vallecano",
    "real sociedad": "Sociedad",
    "real sociedad de futbol": "Sociedad",
    "deportivo alaves": "Alaves",
    "rcd espanyol": "Espanyol",
    "rcd espanyol de barcelona": "Espanyol",
    "cd leganes": "Leganes",
    "real valladolid": "Valladolid",
    "real oviedo": "Oviedo",
    # --- Serie A ---
    "inter milan": "Inter",
    "fc internazionale milano": "Inter",
    "internazionale": "Inter",
    "ac milan": "AC Milan",
    "atalanta bc": "Atalanta",
    "acf fiorentina": "Fiorentina",
    "bologna fc 1909": "Bologna",
    "cagliari calcio": "Cagliari",
    "hellas verona": "Verona",
    "hellas verona fc": "Verona",
    "us sassuolo calcio": "Sassuolo",
    "ssc napoli": "Napoli",
    "genoa cfc": "Genoa",
    "udinese calcio": "Udinese",
    "parma calcio 1913": "Parma",
    "como 1907": "Como",
    "us lecce": "Lecce",
    "torino fc": "Torino",
    "empoli fc": "Empoli",
    "us salernitana 1919": "Salernitana",
    "frosinone calcio": "Frosinone",
    "ac monza": "Monza",
    "ac pisa 1909": "Pisa",
    "venezia fc": "Venezia",
    # --- Champions League / European ---
    "paris saint germain": "Paris SG",
    "paris saint-germain": "Paris SG",
    "as monaco": "Monaco",
    "sl benfica": "Benfica",
    "club brugge": "Club Brugge",
    "olympiakos piraeus": "Olympiakos",
    "bodo/glimt": "Bodo Glimt",
    "qarabag fk": "Qarabag",
    "red star belgrade": "Red Star",
    "dynamo kyiv": "Dynamo Kyiv",
    "fc salzburg": "Salzburg",
    "red bull salzburg": "Salzburg",
    "sporting cp": "Sporting CP",
    "sporting lisbon": "Sporting CP",
}


async def seed_canonical_map() -> int:
    """Seed the canonical_map collection from defaults. Runs on startup.

    Uses $setOnInsert so admin edits are never overwritten.
    Returns number of new entries inserted.
    """
    from pymongo import UpdateOne

    ops = [
        UpdateOne(
            {"provider_name": pn},
            {
                "$setOnInsert": {
                    "provider_name": pn,
                    "canonical_name": cn,
                    "source": "seed",
                    "imported_at": utcnow(),
                },
            },
            upsert=True,
        )
        for pn, cn in _CANONICAL_SEED.items()
    ]
    if not ops:
        return 0
    result = await _db.db.canonical_map.bulk_write(ops, ordered=False)
    inserted = result.upserted_count
    if inserted:
        logger.info("Seeded %d new canonical map entries", inserted)
    return inserted


async def reload_canonical_cache() -> int:
    """Reload the in-memory canonical map from the DB. Returns entry count."""
    docs = await _db.db.canonical_map.find(
        {}, {"_id": 0, "provider_name": 1, "canonical_name": 1},
    ).to_list(length=5000)
    _canonical_cache.clear()
    for doc in docs:
        _canonical_cache[doc["provider_name"]] = doc["canonical_name"]
    logger.debug("Canonical map cache loaded: %d entries", len(_canonical_cache))
    return len(_canonical_cache)


def get_canonical_cache() -> dict[str, str]:
    """Return the current canonical map (read-only reference)."""
    return _canonical_cache


# ---------------------------------------------------------------------------
# Team name resolution (7-step fuzzy matching, step 0 = static map)
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


async def resolve_team_key(name: str, related_keys: list[str]) -> str | None:
    """Multi-strategy team name resolution.

    Provider names (e.g. "Bayer 04 Leverkusen" from OpenLigaDB) often differ
    from CSV names (e.g. "Leverkusen" from football-data.co.uk). We try:
    1. Exact match on team_name in aliases
    2. Exact match on team_key (normalized incoming name)
    3. Containment: stored key is a substring of incoming key or vice versa
    4. Longest-token regex match on team_key
    5. Suffix match: common trailing chars (handles M'gladbach ↔ Mönchengladbach)
    6. Stem match: strip -er/-en suffixes (handles Hamburger→Hamburg)

    On fuzzy success (steps 3+), auto-registers the provider name as a new alias.
    """
    sport_filter = {"sport_key": {"$in": related_keys}}

    # 0. Canonical map (DB-backed, covers cross-provider name discrepancies)
    stripped = _strip_accents_lower(name)
    canonical = _canonical_cache.get(stripped)
    if canonical:
        key = team_name_key(canonical)
        logger.debug("resolve_key(%r) → %r via canonical map (%s)", name, key, canonical)
        await _auto_alias(name, related_keys, key)
        return key

    # 1. Exact alias name match
    alias = await _db.db.team_aliases.find_one(
        {**sport_filter, "team_name": name}, {"team_key": 1},
    )
    if alias:
        logger.debug("resolve_key(%r) → %r via exact name", name, alias["team_key"])
        return alias["team_key"]

    # Compute normalized key from the incoming provider name
    computed_key = team_name_key(name)
    if not computed_key:
        logger.warning("resolve_key(%r) → empty after normalization", name)
        return None

    # 2. Exact team_key match
    alias = await _db.db.team_aliases.find_one(
        {**sport_filter, "team_key": computed_key}, {"team_key": 1},
    )
    if alias:
        logger.debug("resolve_key(%r) → %r via exact key [%s]", name, alias["team_key"], computed_key)
        return alias["team_key"]

    # 3. Containment: find aliases where stored key ⊂ computed key
    last_token = computed_key.split()[-1]
    alias = await _db.db.team_aliases.find_one(
        {**sport_filter, "team_key": {"$regex": re.escape(last_token), "$options": "i"}},
        {"team_key": 1},
    )
    if alias:
        stored_tokens = set(alias["team_key"].split())
        computed_tokens = set(computed_key.split())
        if stored_tokens <= computed_tokens or computed_tokens <= stored_tokens:
            logger.debug("resolve_key(%r) → %r via containment [%s ↔ %s]",
                         name, alias["team_key"], computed_key, alias["team_key"])
            await _auto_alias(name, related_keys, alias["team_key"])
            return alias["team_key"]

    # 4. Longest token fallback (require 2+ shared tokens for multi-token keys)
    tokens = computed_key.split()
    longest = max(tokens, key=len) if tokens else ""
    if len(longest) >= 5:
        computed_tokens_set = set(tokens)
        candidates = await _db.db.team_aliases.find(
            {**sport_filter, "team_key": {"$regex": re.escape(longest), "$options": "i"}},
            {"team_key": 1},
        ).to_list(length=20)

        best_candidate = None
        best_overlap = 0
        for cand in candidates:
            stored_tokens_set = set(cand["team_key"].split())
            overlap = len(stored_tokens_set & computed_tokens_set)
            # Single-token stored keys: full match on that token is enough
            # Multi-token stored keys: require at least 2 shared tokens
            if len(stored_tokens_set) == 1 and overlap == 1:
                if best_overlap < 1:
                    best_candidate = cand
                    best_overlap = 1
            elif overlap >= 2 and overlap > best_overlap:
                best_candidate = cand
                best_overlap = overlap

        if best_candidate:
            logger.debug("resolve_key(%r) → %r via longest-token [%s, overlap=%d]",
                         name, best_candidate["team_key"], longest, best_overlap)
            await _auto_alias(name, related_keys, best_candidate["team_key"])
            return best_candidate["team_key"]

    # 5. Suffix match: common trailing characters
    all_aliases = await _db.db.team_aliases.find(
        sport_filter, {"team_key": 1},
    ).to_list(length=200)

    computed_alpha = re.sub(r"[^a-z]", "", computed_key)
    best_match = None
    best_suffix = 0

    for candidate in all_aliases:
        stored_alpha = re.sub(r"[^a-z]", "", candidate["team_key"])
        max_check = min(len(computed_alpha), len(stored_alpha))
        suffix_len = 0
        for i in range(1, max_check + 1):
            if computed_alpha[-i] == stored_alpha[-i]:
                suffix_len = i
            else:
                break
        if suffix_len >= 7 and suffix_len > best_suffix:
            # Require suffix to cover at least 70% of the shorter string
            # (prevents "wolverhampton" matching "southampton" via "hampton")
            shorter_len = min(len(computed_alpha), len(stored_alpha))
            if shorter_len > 0 and suffix_len / shorter_len >= 0.70:
                best_suffix = suffix_len
                best_match = candidate

    if best_match:
        logger.debug(
            "resolve_key(%r) → %r via suffix [%d common trailing chars]",
            name, best_match["team_key"], best_suffix,
        )
        await _auto_alias(name, related_keys, best_match["team_key"])
        return best_match["team_key"]

    # 6. Stem match: strip German -er/-en suffixes
    for t in tokens:
        for stem in _stem_variants(t):
            stemmed = [stem if tok == t else tok for tok in tokens]
            stemmed_key = " ".join(sorted(stemmed))
            alias = await _db.db.team_aliases.find_one(
                {**sport_filter, "team_key": stemmed_key}, {"team_key": 1},
            )
            if alias:
                logger.debug("resolve_key(%r) → %r via stem [%s → %s]",
                             name, alias["team_key"], computed_key, stemmed_key)
                await _auto_alias(name, related_keys, alias["team_key"])
                return alias["team_key"]

    logger.warning("resolve_key(%r) → FAILED (computed_key=%r)", name, computed_key)
    return None


async def _auto_alias(provider_name: str, related_keys: list[str], team_key: str) -> None:
    """Register a provider name as a new alias so future lookups are instant."""
    try:
        await _db.db.team_aliases.update_one(
            {"sport_key": related_keys[0], "team_name": provider_name},
            {
                "$set": {"team_key": team_key, "updated_at": utcnow()},
                "$setOnInsert": {
                    "sport_key": related_keys[0],
                    "team_name": provider_name,
                    "canonical_name": None,
                    "imported_at": utcnow(),
                },
            },
            upsert=True,
        )
        logger.info("Auto-aliased %r → %r", provider_name, team_key)
    except Exception:
        pass  # duplicate key or other — non-critical


# ---------------------------------------------------------------------------
# Match context builder (H2H + form)
# ---------------------------------------------------------------------------

async def build_match_context(
    home_team: str,
    away_team: str,
    sport_key: str,
    h2h_limit: int = 10,
    form_limit: int = 10,
) -> dict:
    """Core logic: resolve team names, fetch H2H + form, return context dict."""
    cache_key = f"{home_team}|{away_team}|{sport_key}|{h2h_limit}|{form_limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    related_keys = sport_keys_for(sport_key)

    home_key = await resolve_team_key(home_team, related_keys)
    away_key = await resolve_team_key(away_team, related_keys)

    if not home_key or not away_key:
        result: dict = {"h2h": None, "home_form": None, "away_form": None}
        cache_set(cache_key, result)
        return result

    proj = {
        "_id": 0,
        "match_date": 1, "home_team": 1, "away_team": 1,
        "home_team_key": 1, "away_team_key": 1,
        "home_goals": 1, "away_goals": 1, "result": 1,
        "season_label": 1, "sport_key": 1,
    }

    # H2H query: all matches between these two teams across related leagues
    h2h_query = {
        "sport_key": {"$in": related_keys},
        "$or": [
            {"home_team_key": home_key, "away_team_key": away_key},
            {"home_team_key": away_key, "away_team_key": home_key},
        ],
    }

    # Fetch recent H2H matches
    h2h_matches = await _db.db.historical_matches.find(
        h2h_query, proj,
    ).sort("match_date", -1).to_list(length=h2h_limit)

    # Compute H2H summary from ALL matches (not just the limited ones)
    h2h_summary = None
    h2h_all = await _db.db.historical_matches.find(
        h2h_query,
        {"_id": 0, "home_team_key": 1, "home_goals": 1, "away_goals": 1},
    ).to_list(length=500)

    if h2h_all:
        total = len(h2h_all)
        home_wins = 0
        away_wins = 0
        draws = 0
        total_goals = 0
        over_2_5 = 0
        btts = 0

        for m in h2h_all:
            hg = m["home_goals"]
            ag = m["away_goals"]
            total_goals += hg + ag
            if hg + ag > 2:
                over_2_5 += 1
            if hg > 0 and ag > 0:
                btts += 1

            if m["home_team_key"] == home_key:
                if hg > ag:
                    home_wins += 1
                elif hg < ag:
                    away_wins += 1
                else:
                    draws += 1
            else:
                if ag > hg:
                    home_wins += 1
                elif ag < hg:
                    away_wins += 1
                else:
                    draws += 1

        h2h_summary = {
            "total": total,
            "home_wins": home_wins,
            "away_wins": away_wins,
            "draws": draws,
            "avg_goals": round(total_goals / total, 1),
            "over_2_5_pct": round(over_2_5 / total, 2),
            "btts_pct": round(btts / total, 2),
        }

    # Form: last N matches for each team
    async def get_form(team_key: str) -> list[dict]:
        return await _db.db.historical_matches.find(
            {
                "sport_key": {"$in": related_keys},
                "$or": [
                    {"home_team_key": team_key},
                    {"away_team_key": team_key},
                ],
            },
            proj,
        ).sort("match_date", -1).to_list(length=form_limit)

    home_form = await get_form(home_key)
    away_form = await get_form(away_key)

    response = {
        "h2h": {
            "summary": h2h_summary,
            "matches": h2h_matches,
        } if h2h_summary else None,
        "home_form": home_form,
        "away_form": away_form,
        "home_team_key": home_key,
        "away_team_key": away_key,
    }
    cache_set(cache_key, response)
    return response
