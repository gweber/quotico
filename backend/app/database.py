"""MongoDB connection and index setup.

Green-field setup — no migrations. Collections and indexes are created
on first startup via ``_ensure_indexes()``.
"""

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
    await _migrate_match_date_hour()
    await _ensure_indexes()
    await _seed_team_mappings()


async def close_db() -> None:
    global client
    if client:
        client.close()


async def get_db() -> AsyncIOMotorDatabase:
    return db


async def _seed_team_mappings() -> None:
    """Seed team_mappings collection and load in-memory cache."""
    from app.services.team_mapping_service import seed_team_mappings, load_cache
    await seed_team_mappings()
    await load_cache()


async def _migrate_match_date_hour() -> None:
    """One-time migration: add match_date_hour field and drop old compound index.

    Old schema stored normalized (hour-floored) time in match_date.
    New schema: match_date = raw accurate time, match_date_hour = floored (index only).
    For existing docs, match_date was already floored, so copy it to match_date_hour.
    """
    import logging
    logger = logging.getLogger("quotico.database")

    # Backfill match_date_hour from match_date for docs that lack it
    result = await db.matches.update_many(
        {"match_date_hour": {"$exists": False}, "match_date": {"$exists": True}},
        [{"$set": {"match_date_hour": "$match_date"}}],
    )
    if result.modified_count:
        logger.info("Backfilled match_date_hour for %d matches", result.modified_count)

    # Drop old compound unique index (uses match_date) if it exists
    try:
        existing_indexes = await db.matches.index_information()
        for name, info in existing_indexes.items():
            keys = [k for k, _ in info["key"]]
            if (
                info.get("unique")
                and "match_date" in keys
                and "match_date_hour" not in keys
                and "home_team_key" in keys
            ):
                await db.matches.drop_index(name)
                logger.info("Dropped old compound index %s (used match_date)", name)
                break
    except Exception as e:
        logger.debug("Index migration check: %s", e)


async def _ensure_indexes() -> None:
    """Create indexes on startup. Idempotent — safe to run repeatedly."""

    # ---- Users ----
    await db.users.create_index("email", unique=True)
    await db.users.create_index("alias_slug", unique=True, sparse=True)
    await db.users.create_index("google_sub", sparse=True)
    await db.users.create_index("is_deleted")

    # ---- Matches (lifecycle: scheduled → live → final) ----

    # Compound unique dedup key (sport + season + canonical team keys + hour-floored date)
    # match_date_hour is floored to the hour for dedup safety; match_date is the raw accurate time
    await db.matches.create_index(
        [("sport_key", 1), ("season", 1), ("home_team_key", 1),
         ("away_team_key", 1), ("match_date_hour", 1)],
        unique=True,
    )
    # TheOddsAPI fast-path lookup (sparse: matchday-only matches lack this)
    await db.matches.create_index(
        "metadata.theoddsapi_id", unique=True, sparse=True,
    )
    # Dashboard / odds polling
    await db.matches.create_index([("sport_key", 1), ("status", 1)])
    await db.matches.create_index([("match_date", 1), ("status", 1)])
    await db.matches.create_index([("sport_key", 1), ("match_date", 1)])
    # Smart sleep (resolver: started-but-unresolved)
    await db.matches.create_index([("status", 1), ("match_date", 1)])
    # Matchday lookup
    await db.matches.create_index(
        [("sport_key", 1), ("matchday_season", 1), ("matchday_number", 1)]
    )
    # H2H queries
    await db.matches.create_index(
        [("home_team_key", 1), ("away_team_key", 1), ("sport_key", 1), ("match_date", -1)]
    )
    # Form queries
    await db.matches.create_index(
        [("home_team_key", 1), ("sport_key", 1), ("match_date", -1)]
    )
    await db.matches.create_index(
        [("away_team_key", 1), ("sport_key", 1), ("match_date", -1)]
    )
    # Season-scoped (EVD, league averages)
    await db.matches.create_index([("sport_key", 1), ("match_date", -1)])

    # ---- Betting Slips ----

    await db.betting_slips.create_index([("user_id", 1), ("submitted_at", -1)])
    await db.betting_slips.create_index(
        [("status", 1), ("selections.match_id", 1)]
    )
    await db.betting_slips.create_index([("selections.match_id", 1), ("status", 1)])
    await db.betting_slips.create_index([("user_id", 1), ("selections.match_id", 1)])
    await db.betting_slips.create_index([("squad_id", 1), ("submitted_at", -1)])
    await db.betting_slips.create_index("resolved_at", sparse=True)

    # Matchday round dedup (one prediction set per user/matchday/squad)
    await db.betting_slips.create_index(
        [("user_id", 1), ("matchday_id", 1), ("squad_id", 1)],
        unique=True,
        partialFilterExpression={"type": "matchday_round"},
    )
    # Survivor dedup (one season-long entry per user/squad/sport/season)
    await db.betting_slips.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1)],
        unique=True,
        partialFilterExpression={"type": "survivor"},
    )
    # Fantasy dedup (one pick per user/squad/sport/season/matchday)
    await db.betting_slips.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1), ("matchday_number", 1)],
        unique=True,
        partialFilterExpression={"type": "fantasy"},
    )
    # Parlay dedup (one per user per squad per matchday)
    await db.betting_slips.create_index(
        [("user_id", 1), ("squad_id", 1), ("matchday_id", 1)],
        unique=True,
        partialFilterExpression={"type": "parlay"},
    )
    # Matchday resolver + leaderboard queries
    await db.betting_slips.create_index(
        [("matchday_id", 1), ("type", 1), ("status", 1)]
    )
    # Survivor/fantasy standings + type-scoped season queries
    await db.betting_slips.create_index(
        [("type", 1), ("sport_key", 1), ("season", 1), ("status", 1)]
    )
    await db.betting_slips.create_index(
        [("squad_id", 1), ("type", 1), ("sport_key", 1), ("season", 1)]
    )
    # Wallet-funded slip lookup for resolver
    await db.betting_slips.create_index(
        [("wallet_id", 1), ("status", 1)], sparse=True,
    )
    # Draft lookup (active draft per user per type)
    await db.betting_slips.create_index(
        [("user_id", 1), ("type", 1)],
        partialFilterExpression={"status": "draft"},
    )
    # Draft cleanup (stale draft reaping by updated_at)
    await db.betting_slips.create_index(
        [("status", 1), ("updated_at", 1)],
        partialFilterExpression={"status": "draft"},
    )
    # Single-bet dedup (one active bet per user per match)
    # Note: $ne is not supported in partialFilterExpression, use $in instead.
    # Drop stale non-unique version of this index if it exists (key conflict).
    try:
        await db.betting_slips.drop_index("user_id_1_selections.match_id_1")
    except Exception:
        pass  # Index doesn't exist or already correct — fine
    await db.betting_slips.create_index(
        [("user_id", 1), ("selections.match_id", 1)],
        unique=True,
        name="betting_slips_single_dedup",
        partialFilterExpression={
            "type": "single",
            "status": {"$in": ["pending", "won", "lost", "partial", "resolved"]},
        },
    )

    # ---- Team Mappings ----

    await db.team_mappings.create_index("canonical_id", unique=True)
    await db.team_mappings.create_index("names")  # multikey (array index)
    await db.team_mappings.create_index("sport_keys")
    await db.team_mappings.create_index(
        "external_ids.theoddsapi", unique=True,
        partialFilterExpression={"external_ids.theoddsapi": {"$exists": True}},
    )
    await db.team_mappings.create_index(
        "external_ids.openligadb", unique=True,
        partialFilterExpression={"external_ids.openligadb": {"$exists": True}},
    )

    # ---- Points / Leaderboard ----

    await db.points_transactions.create_index("user_id")
    await db.points_transactions.create_index("bet_id")
    await db.points_transactions.create_index("created_at")

    await db.leaderboard.create_index([("points", -1)])

    # ---- Squads ----

    await db.squads.create_index("invite_code", unique=True)
    await db.squads.create_index("members")
    await db.squads.create_index("admin_id")
    await db.squads.create_index(
        [("league_configs.sport_key", 1), ("league_configs.game_mode", 1)]
    )

    # ---- Join Requests ----

    await db.join_requests.create_index([("squad_id", 1), ("status", 1)])
    await db.join_requests.create_index([("squad_id", 1), ("user_id", 1), ("status", 1)])

    # ---- Battles ----

    await db.battles.create_index([("status", 1), ("start_time", 1)])
    await db.battles.create_index("squad_a_id")
    await db.battles.create_index("squad_b_id")

    await db.battle_participations.create_index(
        [("battle_id", 1), ("user_id", 1)], unique=True
    )
    await db.battle_participations.create_index("squad_id")
    await db.battle_participations.create_index("user_id")

    # ---- Badges ----

    await db.badges.create_index(
        [("user_id", 1), ("badge_key", 1)], unique=True
    )

    # ---- Matchdays ----

    await db.matchdays.create_index(
        [("sport_key", 1), ("season", 1), ("matchday_number", 1)], unique=True
    )
    await db.matchdays.create_index([("status", 1)])
    await db.matchdays.create_index([("sport_key", 1), ("status", 1)])
    await db.matchdays.create_index([("sport_key", 1), ("first_kickoff", 1)])
    await db.matchdays.create_index("updated_at")

    # ---- Matchday Predictions (squad-scoped) ----

    await db.matchday_predictions.create_index(
        [("user_id", 1), ("matchday_id", 1), ("squad_id", 1)], unique=True
    )
    await db.matchday_predictions.create_index([("matchday_id", 1), ("status", 1)])
    await db.matchday_predictions.create_index([("status", 1), ("updated_at", 1)])
    await db.matchday_predictions.create_index(
        [("squad_id", 1), ("matchday_id", 1), ("status", 1)]
    )

    # ---- Matchday Leaderboard (materialized, squad-scoped) ----

    await db.matchday_leaderboard.create_index(
        [("sport_key", 1), ("season", 1), ("total_points", -1)]
    )
    await db.matchday_leaderboard.create_index(
        [("sport_key", 1), ("season", 1), ("user_id", 1), ("squad_id", 1)], unique=True
    )
    await db.matchday_leaderboard.create_index(
        [("squad_id", 1), ("sport_key", 1), ("season", 1), ("total_points", -1)]
    )

    # ---- Audit Logs ----

    await db.admin_audit_log.create_index("timestamp")
    await db.admin_audit_log.create_index("admin_id")

    await db.audit_logs.create_index([("action", 1), ("timestamp", -1)])
    await db.audit_logs.create_index([("actor_id", 1), ("timestamp", -1)])
    await db.audit_logs.create_index([("target_id", 1), ("timestamp", -1)])
    await db.audit_logs.create_index("timestamp")

    # ---- Auth Tokens (TTL auto-delete) ----

    await db.refresh_tokens.create_index("jti", unique=True)
    await db.refresh_tokens.create_index("user_id")
    await db.refresh_tokens.create_index("family")
    await db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)

    await db.access_blocklist.create_index("jti", unique=True)
    await db.access_blocklist.create_index("expires_at", expireAfterSeconds=0)

    # ---- Game Modes: Wallets ----

    await db.wallets.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1)],
        unique=True,
    )
    await db.wallets.create_index("status")

    await db.wallet_transactions.create_index([("wallet_id", 1), ("created_at", -1)])
    await db.wallet_transactions.create_index([("user_id", 1), ("created_at", -1)])
    await db.wallet_transactions.create_index("created_at")

    # ---- Device Fingerprints (GDPR: hash-only) ----

    await db.device_fingerprints.create_index(
        [("user_id", 1), ("fingerprint_hash", 1)], unique=True,
    )
    await db.device_fingerprints.create_index("ip_truncated")
    await db.device_fingerprints.create_index("fingerprint_hash")

    # ---- QuoticoTip EV Engine ----

    await db.odds_snapshots.create_index([("match_id", 1), ("snapshot_at", 1)])
    await db.odds_snapshots.create_index(
        "snapshot_at", expireAfterSeconds=60 * 60 * 24 * 14  # TTL: 14 days
    )

    await db.quotico_tips.create_index("match_id", unique=True)
    await db.quotico_tips.create_index(
        [("sport_key", 1), ("status", 1), ("confidence", -1)]
    )
    await db.quotico_tips.create_index(
        [("status", 1), ("was_correct", 1), ("match_date", -1)]
    )

    # ---- Qbot Intelligence ----

    await db.qbot_strategies.create_index(
        [("is_active", 1), ("sport_key", 1)],
        unique=True,
        partialFilterExpression={"is_active": True},
    )
    await db.qbot_strategies.create_index("created_at")
    await db.qbot_strategies.create_index(
        [("sport_key", 1), ("is_active", 1), ("is_shadow", 1), ("created_at", -1)]
    )
    await db.qbot_strategies.create_index(
        [("optimization_notes.stage_info.stage_used", 1), ("created_at", -1)]
    )

    await db.qbot_cluster_stats.create_index("sport_key")
    # _id = cluster_key string, no additional unique index needed

    # ---- Engine Config (calibration) ----
    # _id = sport_key, no indexes needed (6 docs max, all lookups by _id)

    # ---- Engine Config History (time machine snapshots) ----
    await db.engine_config_history.create_index(
        [("sport_key", 1), ("snapshot_date", 1)],
        unique=True,
    )
