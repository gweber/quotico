import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
import app.database as _db
from app.database import connect_db, close_db
from app.middleware.logging import StructuredLoggingMiddleware, setup_logging

logger = logging.getLogger("quotico")
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await connect_db()

    # Seed on startup
    from app.seed import seed_initial_user
    await seed_initial_user()

    # Start background workers
    from app.workers.odds_poller import poll_odds
    from app.workers.match_resolver import resolve_matches
    from app.workers.leaderboard import materialize_leaderboard
    from app.workers.badge_engine import check_badges

    # ~8 calls per poll, 500/day budget → poll every 30min = ~384 calls/day
    scheduler.add_job(poll_odds, "interval", minutes=30, id="odds_poller")
    scheduler.add_job(resolve_matches, "interval", minutes=30, id="match_resolver")
    scheduler.add_job(materialize_leaderboard, "interval", minutes=30, id="leaderboard")
    scheduler.add_job(check_badges, "interval", minutes=30, id="badge_engine")

    # Initial poll on startup (delayed 5s to let app fully start)
    async def initial_poll():
        await asyncio.sleep(5)
        await poll_odds()

    asyncio.create_task(initial_poll())
    scheduler.start()
    logger.info("Background scheduler started")

    yield

    scheduler.shutdown(wait=False)
    await close_db()


app = FastAPI(
    title="Quotico.de",
    description="Quoten-basierte Competition-Plattform",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Session middleware (used by Google OAuth for CSRF state)
app.add_middleware(SessionMiddleware, secret_key=settings.JWT_SECRET, max_age=300)

# Structured logging
app.add_middleware(StructuredLoggingMiddleware)

# Routers
from app.routers.auth import router as auth_router
from app.routers.matches import router as matches_router
from app.routers.tips import router as tips_router
from app.routers.leaderboard import router as leaderboard_router
from app.routers.squads import router as squads_router
from app.routers.battles import router as battles_router
from app.routers.twofa import router as twofa_router
from app.routers.gdpr import router as gdpr_router
from app.routers.ws import router as ws_router
from app.routers.admin import router as admin_router
from app.routers.badges import router as badges_router
from app.routers.google_auth import router as google_auth_router
from app.routers.user import router as user_router

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(matches_router)
app.include_router(tips_router)
app.include_router(leaderboard_router)
app.include_router(squads_router)
app.include_router(battles_router)
app.include_router(twofa_router)
app.include_router(gdpr_router)
app.include_router(ws_router)
app.include_router(admin_router)
app.include_router(badges_router)
app.include_router(google_auth_router)


@app.get("/health")
async def health():
    """Health check — verifies DB connection and provider status."""
    from app.providers.odds_api import odds_provider

    try:
        result = await _db.db.command("ping")
        db_ok = result.get("ok") == 1.0
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "db": "connected" if db_ok else "disconnected",
        "odds_provider": {
            "circuit_open": odds_provider.circuit_open,
        },
    }
