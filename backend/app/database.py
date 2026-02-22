from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

client: AsyncIOMotorClient = None
db: AsyncIOMotorDatabase = None


async def connect_db() -> None:
    global client, db
    client = AsyncIOMotorClient(
        settings.MONGO_URI,
        maxPoolSize=25,
        minPoolSize=5,
    )
    db = client[settings.MONGO_DB]
    await _ensure_indexes()


async def close_db() -> None:
    global client
    if client:
        client.close()


async def get_db() -> AsyncIOMotorDatabase:
    return db


async def _ensure_indexes() -> None:
    """Create indexes and schema validation on startup."""
    # Users
    await db.users.create_index("email", unique=True)
    await db.users.create_index("alias_slug", unique=True, sparse=True)
    await db.users.create_index("google_sub", sparse=True)

    # Tips
    await db.tips.create_index(
        [("user_id", 1), ("match_id", 1)], unique=True
    )
    await db.tips.create_index([("match_id", 1), ("status", 1)])
    await db.tips.create_index([("user_id", 1), ("status", 1)])

    # Matches
    await db.matches.create_index([("commence_time", 1), ("status", 1)])
    await db.matches.create_index([("sport_key", 1), ("status", 1)])

    # Points transactions
    await db.points_transactions.create_index("user_id")
    await db.points_transactions.create_index("tip_id")

    # Leaderboard (materialized)
    await db.leaderboard.create_index("points", unique=False)

    # Squads
    await db.squads.create_index("invite_code", unique=True)
    await db.squads.create_index("members")
    await db.squads.create_index("admin_id")

    # Matches - external ID for upsert
    await db.matches.create_index("external_id", unique=True)

    # Battles
    await db.battles.create_index([("status", 1), ("start_time", 1)])
    await db.battles.create_index("squad_a_id")
    await db.battles.create_index("squad_b_id")

    # Battle participations
    await db.battle_participations.create_index(
        [("battle_id", 1), ("user_id", 1)], unique=True
    )
    await db.battle_participations.create_index("squad_id")

    # Badges
    await db.badges.create_index(
        [("user_id", 1), ("badge_key", 1)], unique=True
    )

    # Admin audit log
    await db.admin_audit_log.create_index("timestamp")
    await db.admin_audit_log.create_index("admin_id")

    # Refresh tokens (TTL: auto-delete after expiry)
    await db.refresh_tokens.create_index("jti", unique=True)
    await db.refresh_tokens.create_index("user_id")
    await db.refresh_tokens.create_index("family")
    await db.refresh_tokens.create_index(
        "expires_at", expireAfterSeconds=0
    )

    # Access token blocklist (TTL: auto-delete after expiry)
    await db.access_blocklist.create_index("jti", unique=True)
    await db.access_blocklist.create_index(
        "expires_at", expireAfterSeconds=0
    )
