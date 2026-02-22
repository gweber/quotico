import logging
from datetime import datetime, timezone

from argon2 import PasswordHasher

import app.database as _db
from app.config import settings
from app.services.alias_service import generate_default_alias

logger = logging.getLogger("quotico.seed")
ph = PasswordHasher()

SEED_SQUAD_NAME = "Beta-Tester"
SEED_SQUAD_CODE = "QUO-START-2026"


async def seed_initial_user() -> None:
    """Create the seed admin user and squad if configured via env."""
    if not settings.SEED_ADMIN_EMAIL or not settings.SEED_ADMIN_PASSWORD:
        logger.debug("SEED_ADMIN_EMAIL not set, skipping seed")
        return

    now = datetime.now(timezone.utc)

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
            "description": "Die offizielle Beta-Tester-Gruppe von Quotico.de",
            "invite_code": SEED_SQUAD_CODE,
            "admin_id": user_id,
            "members": [user_id],
            "created_at": now,
            "updated_at": now,
        }
        await _db.db.squads.insert_one(squad_doc)
        logger.info("Seed squad created: %s", SEED_SQUAD_NAME)
