"""
backend/app/database.py

Purpose:
    MongoDB connection bootstrap and index management for all collections.
    Ensures canonical indexes for Team-Tower and League-Tower driven domains.

Dependencies:
    - motor.motor_asyncio
    - pymongo
    - app.config
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from app.config import settings

client: AsyncIOMotorClient = None
db: AsyncIOMotorDatabase = None
import logging

logger = logging.getLogger("quotico.database")


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
    """Create indexes on startup. Idempotent — safe to run repeatedly."""

    # ---- Users ----
    await db.users.create_index("email", unique=True)
    await db.users.create_index("alias_slug", unique=True, sparse=True)
    await db.users.create_index("google_sub", sparse=True)
    await db.users.create_index("is_deleted")

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

    # ---- Admin Import Jobs ----

    await db.admin_import_jobs.create_index([("type", 1), ("status", 1), ("updated_at", -1)])
    await db.admin_import_jobs.create_index([("season_id", 1), ("status", 1)])
    await db.admin_import_jobs.create_index(
        [("season_id", 1), ("active_lock", 1)],
        unique=True,
        partialFilterExpression={"active_lock": True, "type": "sportmonks_deep_ingest"},
        name="admin_import_jobs_sportmonks_season_lock",
    )
    await db.admin_import_jobs.create_index(
        [("season_id", 1), ("active_lock", 1)],
        unique=True,
        partialFilterExpression={"active_lock": True, "type": "sportmonks_metrics_sync"},
        name="admin_import_jobs_sportmonks_metrics_lock",
    )

    # ---- V3 Sportmonks Collections ----

    await db.league_registry_v3.create_index([("country", 1), ("name", 1)])
    await db.league_registry_v3.create_index([("last_synced_at", -1)])
    await db.matches_v3.create_index([("league_id", 1), ("start_at", -1)])
    await db.matches_v3.create_index([("league_id", 1), ("start_at", 1)])
    await db.matches_v3.create_index([("referee_id", 1), ("start_at", -1)])
    await db.matches_v3.create_index([("status", 1), ("updated_at", -1)])
    await db.matches_v3.create_index([("has_advanced_stats", 1)])
    await db.matches_v3.create_index([("season_id", 1), ("has_advanced_stats", 1)])
    await db.matches_v3.create_index(
        [
            ("odds_meta.summary_1x2.home.avg", 1),
            ("odds_meta.summary_1x2.draw.avg", 1),
            ("odds_meta.summary_1x2.away.avg", 1),
        ],
        name="matches_v3_odds_summary_avg",
    )
    try:
        await db.matches_v3.create_index(
            [("season_id", 1), ("start_at", 1)],
            partialFilterExpression={"odds_meta.summary_1x2": {"$exists": False}},
            name="matches_v3_missing_odds_summary_scan",
        )
    except OperationFailure as exc:
        # Some MongoDB deployments do not support $exists:false in partial indexes.
        if int(getattr(exc, "code", 0) or 0) != 67 and "Expression not supported in partial index" not in str(exc):
            raise
        logger.warning(
            "Partial repair index unsupported on this MongoDB (%s). "
            "Using non-partial fallback index for season/start scan.",
            exc,
        )
        await db.matches_v3.create_index(
            [("season_id", 1), ("start_at", 1)],
            name="matches_v3_repair_scan_fallback",
        )
    await db.matches_v3.create_index([("season_id", 1), ("odds_meta.updated_at", -1)])
    await db.matches_v3.create_index([("season_id", 1), ("round_id", 1)])
    await db.matches_v3.create_index([("teams.home.sm_id", 1), ("start_at", -1)])
    await db.matches_v3.create_index([("teams.away.sm_id", 1), ("start_at", -1)])
    await db.teams_v3.create_index([("updated_at", -1)])
    await db.teams_v3.create_index([("name", 1)])
    await db.teams_v3.create_index([("aliases.normalized", 1)])
    await db.teams_v3.create_index([("aliases.alias_key", 1)])
    await db.persons.create_index([("type", 1), ("name", 1)])

    # ---- xG raw mirror (Sportmonks expected-fixtures) ----
    await db.xg_raw.create_index([("fixture_id", 1), ("type_id", 1)])
    await db.team_alias_resolution_events.create_index([("alias_key", 1), ("resolved_at", -1)])
    await db.team_alias_resolution_events.create_index([("team_id", 1), ("resolved_at", -1)])
    await db.team_alias_suggestions_v3.create_index([("status", 1), ("last_seen_at", -1)])
    await db.team_alias_suggestions_v3.create_index(
        [("normalized_name", 1), ("source", 1), ("sport_key", 1), ("status", 1)]
    )
    await db.team_alias_suggestions_v3.create_index([("confidence_score", -1), ("status", 1)])
    await db.v3_query_cache.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=0,
        name="v3_query_cache_ttl",
    )
    await db.v3_query_cache.create_index([("kind", 1), ("updated_at", -1)], name="v3_query_cache_kind_updated")
    await db.sportmonks_page_cache.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=0,
        name="sportmonks_page_cache_ttl",
    )
    await db.sportmonks_page_cache.create_index([("endpoint", 1)], name="sportmonks_page_cache_endpoint")

    # ---- Provider Runtime Settings ----

    await db.provider_settings.create_index(
        [("provider", 1), ("scope", 1), ("league_id", 1)],
        unique=True,
    )
    await db.provider_settings.create_index([("provider", 1), ("scope", 1)])
    await db.provider_settings.create_index([("league_id", 1), ("provider", 1)])

    await db.provider_secrets.create_index(
        [("provider", 1), ("scope", 1), ("league_id", 1)],
        unique=True,
    )
    await db.provider_secrets.create_index([("provider", 1), ("scope", 1)])

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

    # ---- Event Bus Monitor ----

    await db.event_bus_stats.create_index(
        "ts", expireAfterSeconds=max(1, int(settings.QBUS_MONITOR_TTL_DAYS)) * 24 * 60 * 60
    )
    await db.event_bus_stats.create_index([("ts", -1)])
    await db.event_bus_stats.create_index([("status_level", 1), ("ts", -1)])

    # ---- Team Merge Archives ----

    archive_ttl_seconds = 60 * 60 * 24 * 90  # 90 days
    await db.archived_matches.create_index("archived_at", expireAfterSeconds=archive_ttl_seconds)
    await db.archived_matches.create_index([("merge_job_id", 1), ("archived_at", -1)])
    await db.archived_matches.create_index([("original_id", 1)])
    await db.archived_matches.create_index([("merged_into_id", 1)])

    await db.archived_quotico_tips.create_index("archived_at", expireAfterSeconds=archive_ttl_seconds)
    await db.archived_quotico_tips.create_index([("merge_job_id", 1), ("archived_at", -1)])
    await db.archived_quotico_tips.create_index([("original_id", 1)])
    await db.archived_quotico_tips.create_index([("merged_into_id", 1)])

    await db.quotico_tips.create_index("match_id", unique=True)
    await db.quotico_tips.create_index("home_team_id")
    await db.quotico_tips.create_index("away_team_id")
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
    await db.engine_time_machine_justice.create_index(
        [("sport_key", 1), ("snapshot_date", 1)],
        unique=True,
    )
    await db.engine_time_machine_justice.create_index([("sport_key", 1), ("snapshot_date", -1)])
    await db.engine_time_machine_justice.create_index([("meta.generated_at", -1)])
