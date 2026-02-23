import re
import secrets

from motor.motor_asyncio import AsyncIOMotorDatabase

ALIAS_MIN_LENGTH = 3
ALIAS_MAX_LENGTH = 20
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DEFAULT_ALIAS_PATTERN = re.compile(r"^user\d+$", re.IGNORECASE)

ALIAS_BLACKLIST = frozenset({
    "admin", "administrator", "support", "quotico", "moderator", "mod",
    "system", "root", "superuser", "helpdesk", "official", "staff",
    "team", "bot", "null", "undefined", "anonymous", "deleted",
    "qbot",
})


def normalize_slug(alias: str) -> str:
    """Create a URL-safe slug from an alias: lowercase, alphanumeric + underscore only."""
    return re.sub(r"[^a-z0-9_]", "", alias.lower())


def validate_alias(alias: str) -> str | None:
    """Validate an alias. Returns an error message string, or None if valid."""
    if len(alias) < ALIAS_MIN_LENGTH:
        return f"Alias muss mindestens {ALIAS_MIN_LENGTH} Zeichen lang sein."

    if len(alias) > ALIAS_MAX_LENGTH:
        return f"Alias darf maximal {ALIAS_MAX_LENGTH} Zeichen lang sein."

    if not ALIAS_PATTERN.match(alias):
        return "Alias darf nur Buchstaben (A-Z), Ziffern (0-9) und Unterstriche (_) enthalten."

    slug = normalize_slug(alias)

    if slug in ALIAS_BLACKLIST:
        return "Dieser Name ist reserviert."

    if DEFAULT_ALIAS_PATTERN.match(slug):
        return "Dieser Name ist reserviert."

    return None


async def generate_default_alias(db: AsyncIOMotorDatabase) -> tuple[str, str]:
    """Generate a unique default alias like User#123456.

    Returns (alias, alias_slug) tuple.
    Retries up to 20 times on collision.
    """
    for _ in range(20):
        digits = f"{secrets.randbelow(1_000_000):06d}"
        alias = f"User#{digits}"
        slug = f"user{digits}"
        existing = await db.users.find_one({"alias_slug": slug}, {"_id": 1})
        if not existing:
            return alias, slug

    raise RuntimeError("Could not generate a unique default alias after 20 attempts.")
