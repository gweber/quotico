from pathlib import Path

from pydantic_settings import BaseSettings

# .env is at project root (one level up from backend/)
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


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
    ODDS_STALENESS_MAX_SECONDS: int = 600  # 10 minutes

    # Seed admin user (leave empty to skip seeding)
    SEED_ADMIN_EMAIL: str = ""
    SEED_ADMIN_PASSWORD: str = ""

    # football-data.org (free scores + live data)
    FOOTBALL_DATA_API_KEY: str = ""

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


settings = Settings()
