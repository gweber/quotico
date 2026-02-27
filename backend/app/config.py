"""
backend/app/config.py

Purpose:
    Central settings loading for backend services.

Dependencies:
    - pydantic-settings
    - pathlib
"""

from pathlib import Path

from pydantic_settings import BaseSettings

# Prefer backend/.env, fallback to project-root .env.
_BACKEND_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_ROOT_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    ODDSAPIKEY: str
    MONGO_URI: str
    MONGO_DB: str = "quotico"
    JWT_SECRET: str
    JWT_SECRET_OLD: str = ""  # Set during rotation; cleared after 7 days
    ENCRYPTION_KEY: str
    ENCRYPTION_KEY_OLD: str = ""  # Set during rotation; cleared after re-encryption
    BACKEND_CORS_ORIGINS: str = "http://localhost:5173"
    COOKIE_SECURE: bool = False  # Set True in production (HTTPS)

    # JWT settings
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Odds provider settings
    ODDS_CACHE_TTL_SECONDS: int = 300  # 5 minutes
    ODDS_STALENESS_MAX_SECONDS: int = 1500  # 25 minutes (must exceed polling interval of 15 min)

    # Seed admin user (leave empty to skip seeding)
    SEED_ADMIN_EMAIL: str = ""
    SEED_ADMIN_PASSWORD: str = ""

    # football-data.org (free scores + live data)
    FOOTBALL_DATA_ORG_API_KEY: str = ""
    FOOTBALL_DATA_ORG_BASE_URL: str = "https://api.football-data.org/v4"

    # API key for local tools (scraper import etc.)
    IMPORT_API_KEY: str = ""
    SM_API_KEY: str = ""

    # Provider runtime settings fallback (DB-first: DB > ENV > defaults)
    THEODDSAPI_BASE_URL: str = "https://api.the-odds-api.com/v4"
    OPENLIGADB_BASE_URL: str = "https://api.openligadb.de"
    FOOTBALL_DATA_UK_BASE_URL: str = "https://www.football-data.co.uk/mmz4281"
    UNDERSTAT_BASE_URL: str = "https://understat.com"
    SPORTMONKS_BASE_URL: str = "https://api.sportmonks.com/v3"
    PROVIDER_SETTINGS_CACHE_TTL: int = 15
    THEODDSAPI_RATE_LIMIT_RPM: int = 30
    FOOTBALL_DATA_RATE_LIMIT_RPM: int = 10
    OPENLIGADB_RATE_LIMIT_RPM: int = 120
    FOOTBALL_DATA_UK_RATE_LIMIT_RPM: int = 30
    UNDERSTAT_RATE_LIMIT_RPM: int = 30
    SPORTMONKS_RESERVE_CREDITS: int = 50
    SPORTMONKS_DISCOVERY_TTL_MINUTES: int = 30
    SPORTMONKS_STALE_JOB_MINUTES: int = 5
    SPORTMONKS_PAGE_CACHE_ENABLED: bool = True
    SPORTMONKS_PAGE_CACHE_TTL_MINUTES: int = 20
    SPORTMONKS_MAX_RUNTIME_DEEP_MINUTES: int = 45
    SPORTMONKS_MAX_RUNTIME_METRICS_MINUTES: int = 90
    SPORTMONKS_MAX_PAGE_REQUESTS_PER_PHASE: int = 500
    SPORTMONKS_MAX_PAGE_REQUESTS_TOTAL: int = 2000
    SPORTMONKS_DUPLICATE_PAGE_WINDOW_SECONDS: int = 300
    SPORTMONKS_DUPLICATE_PAGE_MAX_HITS: int = 2
    SPORTMONKS_STARTUP_DISCOVERY_ENABLED: bool = True
    SPORTMONKS_DEEP_INGEST_SYNC_ODDS: bool = False
    SPORTMONKS_ODDS_TIMELINE_MIN_DELTA: float = 0.02
    SPORTMONKS_ODDS_TIMELINE_MINUTES: int = 60
    ALIAS_SOURCES_ALLOWED: str = "manual,provider_x,crawler,provider_unknown"

    # V3 query transport
    V3_QUERY_CACHE_TTL_SECONDS: int = 120
    V3_QUERY_MAX_IDS: int = 500
    MATCHDAY_V3_CACHE_TTL_SECONDS: int = 900
    JUSTICE_CACHE_TTL_SECONDS: int = 3600

    # Metrics heartbeat (automated odds + xG sync)
    METRICS_HEARTBEAT_ENABLED: bool = False
    METRICS_HEARTBEAT_POST_MATCH_DELAY_HOURS: int = 2
    XG_CRAWLER_TICK_SECONDS: int = 2  # env default; runtime override via admin API

    # Tiered odds scheduler (replaces flat pre-match loop)
    ODDS_SCHEDULER_TICK_SECONDS: int = 600
    ODDS_SCHEDULER_LOOKAHEAD_DAYS: int = 7
    ODDS_SCHEDULER_MAX_TICK_SECONDS: int = 300
    ODDS_SCHEDULER_TIER_APPROACHING_HOURS: int = 6
    ODDS_SCHEDULER_TIER_IMMINENT_MINUTES: int = 30
    ODDS_SCHEDULER_TIER_CLOSING_MINUTES: int = 10

    # Q-Bot: minimum QuoticoTip confidence to auto-bet
    QBOT_MIN_CONFIDENCE: float = 0.55

    # Event bus (in-process, V1)
    EVENT_BUS_ENABLED: bool = True
    EVENT_BUS_INGRESS_QUEUE_MAXSIZE: int = 10000
    EVENT_BUS_HANDLER_QUEUE_MAXSIZE: int = 2000
    EVENT_BUS_HANDLER_DEFAULT_CONCURRENCY: int = 1
    EVENT_BUS_ERROR_BUFFER_SIZE: int = 200
    EVENT_PUBLISH_FOOTBALL_DATA: bool = True
    EVENT_PUBLISH_OPENLIGADB: bool = True
    EVENT_PUBLISH_FOOTBALL_DATA_UK: bool = True
    EVENT_PUBLISH_THEODDSAPI: bool = True
    EVENT_PUBLISH_ODDS_INGESTED: bool = True
    EVENT_HANDLER_MATCH_FINALIZED_ENABLED: bool = True
    EVENT_HANDLER_MATCH_UPDATED_ENABLED: bool = True
    EVENT_HANDLER_ODDS_INGESTED_ENABLED: bool = True
    EVENT_HANDLER_WS_BROADCAST_ENABLED: bool = True

    # WebSocket realtime stream
    WS_EVENTS_ENABLED: bool = True
    WS_HEARTBEAT_SECONDS: int = 30
    WS_MAX_CONNECTIONS: int = 500

    # QBus monitor
    QBUS_MONITOR_ENABLED: bool = True
    QBUS_MONITOR_SAMPLING_SECONDS: int = 10
    QBUS_MONITOR_TTL_DAYS: int = 7
    QBUS_ALERT_QUEUE_WARN_PCT: float = 80.0
    QBUS_ALERT_QUEUE_CRIT_PCT: float = 95.0
    QBUS_ALERT_FAILED_RATE_WARN: float = 0.05
    QBUS_ALERT_FAILED_RATE_CRIT: float = 0.10
    QBUS_ALERT_DROPPED_WARN_PER_MIN: int = 1
    QBUS_ALERT_DROPPED_CRIT_PER_MIN: int = 5
    QBUS_ALERT_LATENCY_P95_WARN_MS: int = 500
    QBUS_ALERT_LATENCY_P95_CRIT_MS: int = 1500
    QTIP_BACKFILL_NO_SIGNAL_WARN_PCT: float = 30.0
    QTIP_BACKFILL_ADMIN_MAX_MATCHES: int = 2000

    model_config = {
        "env_file": (str(_BACKEND_ENV_FILE), str(_ROOT_ENV_FILE)),
        "extra": "ignore",
    }


settings = Settings()
