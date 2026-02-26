import logging
import secrets
from datetime import datetime

from app.utils import utcnow

from argon2 import PasswordHasher

import app.database as _db
from app.config import settings
from app.services.alias_service import generate_default_alias

logger = logging.getLogger("quotico.seed")
ph = PasswordHasher()

SEED_SQUAD_NAME = "Beta-Tester"
SEED_SQUAD_CODE = "QUO-START-2026"

QBOT_EMAIL = "qbot@quotico.de"
QBOT_ALIAS = "Q-Bot"
QBOT_ALIAS_SLUG = "qbot"


async def seed_initial_user() -> None:
    """Create the seed admin user and squad if configured via env."""
    if not settings.SEED_ADMIN_EMAIL or not settings.SEED_ADMIN_PASSWORD:
        logger.debug("SEED_ADMIN_EMAIL not set, skipping seed")
        return
    existing_users = await _db.db.users.count_documents({})
    existing_squads = await _db.db.squads.count_documents({})
    if existing_users > 0 or existing_squads > 0:
        logger.info(
            "Seed bootstrap skipped (existing users=%d, squads=%d)",
            existing_users,
            existing_squads,
        )
        return

    now = utcnow()

    # 1. Create seed user
    existing_user = await _db.db.users.find_one({"email": settings.SEED_ADMIN_EMAIL})
    if existing_user:
        user_id = str(existing_user["_id"])
        if not existing_user.get("is_admin"):
            await _db.db.users.update_one(
                {"_id": existing_user["_id"]},
                {"$set": {"is_admin": True}},
            )
            logger.info("Seed user promoted to admin")
        logger.info("Seed user already exists, skipping")
    else:
        alias, alias_slug = await generate_default_alias(_db.db)
        user_doc = {
            "email": settings.SEED_ADMIN_EMAIL,
            "hashed_password": ph.hash(settings.SEED_ADMIN_PASSWORD),
            "alias": alias,
            "alias_slug": alias_slug,
            "has_custom_alias": False,
            "points": 0.0,
            "is_admin": True,
            "is_banned": False,
            "is_2fa_enabled": False,
            "encrypted_2fa_secret": None,
            "encryption_key_version": 1,
            "is_deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        result = await _db.db.users.insert_one(user_doc)
        user_id = str(result.inserted_id)
        logger.info("Seed user created (admin): %s", user_id)

    # 2. Create seed squad
    existing_squad = await _db.db.squads.find_one({"invite_code": SEED_SQUAD_CODE})
    if existing_squad:
        logger.info("Seed squad already exists, skipping")
    else:
        squad_doc = {
            "name": SEED_SQUAD_NAME,
            "description": "The official beta tester group of Quotico.de",
            "invite_code": SEED_SQUAD_CODE,
            "admin_id": user_id,
            "members": [user_id],
            "created_at": now,
            "updated_at": now,
        }
        await _db.db.squads.insert_one(squad_doc)
        logger.info("Seed squad created: %s", SEED_SQUAD_NAME)


async def seed_qbot_user() -> str:
    """Create the Q-Bot system user if it doesn't exist.

    Returns the Q-Bot user_id (str) for use by workers.
    """
    existing_users = await _db.db.users.count_documents({})
    if existing_users > 0:
        existing = await _db.db.users.find_one({"email": QBOT_EMAIL})
        if existing:
            logger.info("Q-Bot user already exists: %s", existing["_id"])
            return str(existing["_id"])
        logger.info("Q-Bot seed bootstrap skipped (users already present)")
        return ""

    existing = await _db.db.users.find_one({"email": QBOT_EMAIL})
    if existing:
        logger.info("Q-Bot user already exists: %s", existing["_id"])
        return str(existing["_id"])

    now = utcnow()
    user_doc = {
        "email": QBOT_EMAIL,
        "hashed_password": ph.hash(secrets.token_hex(32)),
        "alias": QBOT_ALIAS,
        "alias_slug": QBOT_ALIAS_SLUG,
        "has_custom_alias": True,
        "points": 0.0,
        "is_admin": False,
        "is_banned": False,
        "is_bot": True,
        "is_2fa_enabled": False,
        "encrypted_2fa_secret": None,
        "encryption_key_version": 1,
        "is_deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.users.insert_one(user_doc)
    logger.info("Q-Bot user created: %s", result.inserted_id)
    return str(result.inserted_id)


async def ensure_startup_superadmin() -> None:
    """Ensure a configured superadmin exists at startup.

    Greenfield v3.1 bootstrap behavior:
    - No broad startup seeding of sports data.
    - Optional admin bootstrap only when both env vars are present.
    - Idempotent: promotes existing user and refreshes password hash.
    """
    if not settings.SEED_ADMIN_EMAIL or not settings.SEED_ADMIN_PASSWORD:
        logger.debug("SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD not set, startup admin bootstrap skipped")
        return

    email = settings.SEED_ADMIN_EMAIL.strip().lower()
    if not email:
        logger.warning("Startup admin bootstrap skipped: empty SEED_ADMIN_EMAIL")
        return

    now = utcnow()
    existing = await _db.db.users.find_one({"email": email})
    if existing:
        await _db.db.users.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "is_admin": True,
                    "hashed_password": ph.hash(settings.SEED_ADMIN_PASSWORD),
                    "updated_at": now,
                }
            },
        )
        logger.info("Startup admin bootstrap updated/promoted user: %s", existing["_id"])
        return

    alias, alias_slug = await generate_default_alias(_db.db)
    doc = {
        "email": email,
        "hashed_password": ph.hash(settings.SEED_ADMIN_PASSWORD),
        "alias": alias,
        "alias_slug": alias_slug,
        "has_custom_alias": False,
        "points": 0.0,
        "is_admin": True,
        "is_banned": False,
        "is_2fa_enabled": False,
        "encrypted_2fa_secret": None,
        "encryption_key_version": 1,
        "is_deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    result = await _db.db.users.insert_one(doc)
    logger.info("Startup admin bootstrap created superadmin: %s", result.inserted_id)
