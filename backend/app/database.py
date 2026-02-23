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
    await _migrate_existing_users()
    await _migrate_terms_field()
    await _migrate_game_mode_fields()
    await _migrate_league_configs()
    await _migrate_spieltag_prediction_squad_id()
    await _migrate_historical_unique_index()


async def close_db() -> None:
    global client
    if client:
        client.close()


async def get_db() -> AsyncIOMotorDatabase:
    return db


async def _migrate_existing_users() -> None:
    """Backfill is_adult=True for existing users (Bestandsschutz)."""
    import logging
    result = await db.users.update_many(
        {"is_adult": {"$exists": False}},
        {"$set": {"is_adult": True, "birth_date_verified_at": None}},
    )
    if result.modified_count > 0:
        logging.getLogger("quotico.db").info(
            "Migrated %d existing users with is_adult=True", result.modified_count,
        )


async def _migrate_terms_field() -> None:
    """Backfill terms_accepted_version field for existing users."""
    import logging
    result = await db.users.update_many(
        {"terms_accepted_version": {"$exists": False}},
        {"$set": {"terms_accepted_version": None, "terms_accepted_at": None}},
    )
    if result.modified_count > 0:
        logging.getLogger("quotico.db").info(
            "Migrated %d users with terms_accepted_version field", result.modified_count,
        )


async def _migrate_game_mode_fields() -> None:
    """Backfill game_mode fields for existing squads and users."""
    import logging
    log = logging.getLogger("quotico.db")

    result = await db.squads.update_many(
        {"game_mode": {"$exists": False}},
        {"$set": {"game_mode": "classic", "game_mode_config": {}, "game_mode_changed_at": None}},
    )
    if result.modified_count > 0:
        log.info("Migrated %d squads with game_mode fields", result.modified_count)

    result = await db.users.update_many(
        {"wallet_disclaimer_accepted_at": {"$exists": False}},
        {"$set": {"wallet_disclaimer_accepted_at": None, "household_group_id": None}},
    )
    if result.modified_count > 0:
        log.info("Migrated %d users with wallet/household fields", result.modified_count)


async def _migrate_league_configs() -> None:
    """Migrate squads from single game_mode to league_configs array."""
    import logging
    from app.utils import utcnow
    log = logging.getLogger("quotico.db")

    migrated = 0
    async for squad in db.squads.find({"league_configs": {"$exists": False}}):
        old_mode = squad.get("game_mode", "classic")
        old_config = squad.get("game_mode_config", {})
        changed_at = squad.get("game_mode_changed_at") or squad.get("created_at", utcnow())

        # Classic squads get empty league_configs (they use the global tip system)
        if old_mode == "classic":
            league_configs = []
        else:
            # Non-classic: we don't know which sport_key was intended
            league_configs = [{
                "sport_key": "_migrated_unknown",
                "game_mode": old_mode,
                "config": old_config,
                "activated_at": changed_at,
                "deactivated_at": None,
            }]

        await db.squads.update_one(
            {"_id": squad["_id"]},
            {"$set": {"league_configs": league_configs, "updated_at": utcnow()}},
        )
        migrated += 1

    if migrated:
        log.info("Migrated %d squads to league_configs", migrated)

    # Rename "spieltag" game mode value → "classic" (they were duplicates)
    r1 = await db.squads.update_many(
        {"game_mode": "spieltag"},
        {"$set": {"game_mode": "classic"}},
    )
    r2 = await db.squads.update_many(
        {"league_configs.game_mode": "spieltag"},
        {"$set": {"league_configs.$.game_mode": "classic"}},
    )
    if r1.modified_count or r2.modified_count:
        log.info(
            "Renamed spieltag→classic: %d game_mode, %d league_configs",
            r1.modified_count, r2.modified_count,
        )


async def _migrate_spieltag_prediction_squad_id() -> None:
    """Backfill squad_id=None for existing spieltag_predictions."""
    import logging
    log = logging.getLogger("quotico.db")

    result = await db.spieltag_predictions.update_many(
        {"squad_id": {"$exists": False}},
        {"$set": {"squad_id": None}},
    )
    if result.modified_count > 0:
        log.info("Backfilled %d spieltag_predictions with squad_id=None", result.modified_count)

    # Drop old unique indexes and create new ones with squad_id
    try:
        await db.spieltag_predictions.drop_index("user_id_1_matchday_id_1")
    except Exception:
        pass  # Index may not exist or have a different name

    try:
        await db.spieltag_leaderboard.drop_index("sport_key_1_season_1_user_id_1")
    except Exception:
        pass


async def _migrate_historical_unique_index() -> None:
    """Drop old unique indexes that don't include match_date."""
    import logging
    log = logging.getLogger("quotico.db")
    old_indexes = [
        "sport_key_1_season_1_match_date_1_home_team_1_away_team_1",  # raw-name index
        "sport_key_1_season_1_home_team_key_1_away_team_key_1",       # key-only (no date)
    ]
    for idx in old_indexes:
        try:
            await db.historical_matches.drop_index(idx)
            log.info("Dropped old historical_matches index: %s", idx)
        except Exception:
            pass  # Already dropped or never existed


async def _ensure_indexes() -> None:
    """Create indexes and schema validation on startup."""
    # Users
    await db.users.create_index("email", unique=True)
    await db.users.create_index("alias_slug", unique=True, sparse=True)
    await db.users.create_index("google_sub", sparse=True)
    await db.users.create_index("is_deleted")  # auth checks, badge engine, leaderboard

    # Tips
    await db.tips.create_index(
        [("user_id", 1), ("match_id", 1)], unique=True
    )
    await db.tips.create_index([("match_id", 1), ("status", 1)])
    await db.tips.create_index([("user_id", 1), ("status", 1)])

    # Matches
    await db.matches.create_index([("commence_time", 1), ("status", 1)])
    await db.matches.create_index([("sport_key", 1), ("status", 1)])
    await db.matches.create_index([("sport_key", 1), ("commence_time", 1)])  # matchday sync team matching

    # Points transactions
    await db.points_transactions.create_index("user_id")
    await db.points_transactions.create_index("tip_id")

    # Leaderboard (materialized, sorted descending)
    await db.leaderboard.create_index([("points", -1)])

    # Squads
    await db.squads.create_index("invite_code", unique=True)
    await db.squads.create_index("members")
    await db.squads.create_index("admin_id")
    await db.squads.create_index(
        [("league_configs.sport_key", 1), ("league_configs.game_mode", 1)]
    )

    # Join requests
    await db.join_requests.create_index([("squad_id", 1), ("status", 1)])
    await db.join_requests.create_index([("squad_id", 1), ("user_id", 1), ("status", 1)])

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
    await db.battle_participations.create_index("user_id")  # user commitment lookup

    # Badges
    await db.badges.create_index(
        [("user_id", 1), ("badge_key", 1)], unique=True
    )

    # Matchdays (Spieltag-Modus)
    await db.matchdays.create_index(
        [("sport_key", 1), ("season", 1), ("matchday_number", 1)], unique=True
    )
    await db.matchdays.create_index([("status", 1)])
    await db.matchdays.create_index([("sport_key", 1), ("status", 1)])  # smart sleep per-sport
    await db.matchdays.create_index([("sport_key", 1), ("first_kickoff", 1)])  # upcoming matchday check

    # Spieltag predictions (squad-scoped: one prediction per user per matchday per squad)
    await db.spieltag_predictions.create_index(
        [("user_id", 1), ("matchday_id", 1), ("squad_id", 1)], unique=True
    )
    await db.spieltag_predictions.create_index(
        [("matchday_id", 1), ("status", 1)]
    )
    await db.spieltag_predictions.create_index(
        [("status", 1), ("updated_at", 1)]  # smart sleep: recent resolutions check
    )
    await db.spieltag_predictions.create_index(
        [("squad_id", 1), ("matchday_id", 1), ("status", 1)]  # squad-scoped leaderboard
    )

    # Spieltag leaderboard (materialized, squad-scoped)
    await db.spieltag_leaderboard.create_index(
        [("sport_key", 1), ("season", 1), ("total_points", -1)]
    )
    await db.spieltag_leaderboard.create_index(
        [("sport_key", 1), ("season", 1), ("user_id", 1), ("squad_id", 1)], unique=True
    )
    await db.spieltag_leaderboard.create_index(
        [("squad_id", 1), ("sport_key", 1), ("season", 1), ("total_points", -1)]
    )

    # Matches: smart sleep (started-but-unresolved lookup)
    await db.matches.create_index([("status", 1), ("commence_time", 1)])

    # Matchdays: smart sleep (recency check)
    await db.matchdays.create_index("updated_at")

    # Points transactions: smart sleep (leaderboard activity check)
    await db.points_transactions.create_index("created_at")

    # Tips: smart sleep (badge engine activity check)
    await db.tips.create_index("resolved_at", sparse=True)

    # Matches: Spieltag lookup
    await db.matches.create_index(
        [("sport_key", 1), ("matchday_season", 1), ("matchday_number", 1)]
    )

    # Admin audit log (legacy — kept for historical data)
    await db.admin_audit_log.create_index("timestamp")
    await db.admin_audit_log.create_index("admin_id")

    # Unified audit logs (compliance & regulatory)
    await db.audit_logs.create_index([("action", 1), ("timestamp", -1)])
    await db.audit_logs.create_index([("actor_id", 1), ("timestamp", -1)])
    await db.audit_logs.create_index([("target_id", 1), ("timestamp", -1)])
    await db.audit_logs.create_index("timestamp")

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

    # ---- Game Modes: Wallets, Bets, Survivors, Fantasy, Parlays ----

    # Wallets (per-user per-squad per-season)
    await db.wallets.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1)],
        unique=True,
    )
    await db.wallets.create_index("status")

    # Wallet transactions (immutable audit trail)
    await db.wallet_transactions.create_index([("wallet_id", 1), ("created_at", -1)])
    await db.wallet_transactions.create_index([("user_id", 1), ("created_at", -1)])
    await db.wallet_transactions.create_index("created_at")

    # Bankroll bets
    await db.bankroll_bets.create_index(
        [("user_id", 1), ("squad_id", 1), ("match_id", 1)], unique=True,
    )
    await db.bankroll_bets.create_index([("match_id", 1), ("status", 1)])
    await db.bankroll_bets.create_index([("squad_id", 1), ("matchday_id", 1)])

    # Over/Under bets
    await db.over_under_bets.create_index(
        [("user_id", 1), ("squad_id", 1), ("match_id", 1)], unique=True,
    )
    await db.over_under_bets.create_index([("match_id", 1), ("status", 1)])

    # Survivor entries
    await db.survivor_entries.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1)],
        unique=True,
    )
    await db.survivor_entries.create_index([("squad_id", 1), ("sport_key", 1), ("season", 1)])

    # Fantasy picks
    await db.fantasy_picks.create_index(
        [("user_id", 1), ("squad_id", 1), ("sport_key", 1), ("season", 1), ("matchday_number", 1)],
        unique=True,
    )
    await db.fantasy_picks.create_index([("squad_id", 1), ("sport_key", 1), ("season", 1)])
    await db.fantasy_picks.create_index([("match_id", 1), ("status", 1)])

    # Parlays (one per user per squad per matchday)
    await db.parlays.create_index(
        [("user_id", 1), ("squad_id", 1), ("matchday_id", 1)], unique=True,
    )
    await db.parlays.create_index([("status", 1)])

    # Device fingerprints (DSGVO: hash-only, no raw components)
    await db.device_fingerprints.create_index(
        [("user_id", 1), ("fingerprint_hash", 1)], unique=True,
    )
    await db.device_fingerprints.create_index("ip_truncated")
    await db.device_fingerprints.create_index("fingerprint_hash")

    # Historical matches (imported from football-data.co.uk via tools/scrapper.py)
    # Unique by team keys + match_date — handles NBA playoffs (same home/away, different dates)
    await db.historical_matches.create_index(
        [("sport_key", 1), ("season", 1), ("home_team_key", 1), ("away_team_key", 1), ("match_date", 1)],
        unique=True,
    )
    await db.historical_matches.create_index([("sport_key", 1), ("match_date", -1)])
    await db.historical_matches.create_index([("sport_key", 1), ("season", 1)])
    # Team form queries: filter by team_key + sport_key, sort by match_date desc
    await db.historical_matches.create_index([("home_team_key", 1), ("sport_key", 1), ("match_date", -1)])
    await db.historical_matches.create_index([("away_team_key", 1), ("sport_key", 1), ("match_date", -1)])
    # H2H lookups for match card enrichment
    await db.historical_matches.create_index(
        [("home_team_key", 1), ("away_team_key", 1), ("sport_key", 1), ("match_date", -1)]
    )

    # Team aliases (maps historical names to canonical live-provider names)
    await db.team_aliases.create_index([("sport_key", 1), ("team_name", 1)], unique=True)
    await db.team_aliases.create_index([("sport_key", 1), ("team_key", 1)])
    await db.team_aliases.create_index("canonical_name", sparse=True)

    # ---- QuoticoTip EV Engine ----

    # Odds snapshots (line movement tracking for sharp money detection)
    await db.odds_snapshots.create_index([("match_id", 1), ("snapshot_at", 1)])
    await db.odds_snapshots.create_index(
        "snapshot_at", expireAfterSeconds=60 * 60 * 24 * 14  # TTL: 14 days
    )

    # QuoticoTips (pre-computed value bet recommendations)
    await db.quotico_tips.create_index("match_id", unique=True)
    await db.quotico_tips.create_index(
        [("sport_key", 1), ("status", 1), ("confidence", -1)]
    )
