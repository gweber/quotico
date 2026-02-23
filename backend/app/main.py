import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson.errors import InvalidId
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings

# Derive a separate secret for sessions (key separation from JWT)
_SESSION_SECRET = hashlib.sha256(b"session:" + settings.JWT_SECRET.encode()).hexdigest()
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
    from app.seed import seed_initial_user, seed_qbot_user
    await seed_initial_user()
    await seed_qbot_user()

    # Seed canonical team name map + load into memory
    from app.services.historical_service import seed_canonical_map, reload_canonical_cache
    await seed_canonical_map()
    await reload_canonical_cache()

    # Start background workers
    from app.workers.odds_poller import poll_odds
    from app.workers.match_resolver import resolve_matches
    from app.workers.leaderboard import materialize_leaderboard
    from app.workers.badge_engine import check_badges
    from app.workers.matchday_sync import sync_matchdays
    from app.workers.spieltag_resolver import resolve_spieltag_predictions
    from app.workers.spieltag_leaderboard import materialize_spieltag_leaderboard
    from app.workers.bankroll_resolver import resolve_bankroll_bets
    from app.workers.survivor_resolver import resolve_survivor_picks
    from app.workers.over_under_resolver import resolve_over_under_bets
    from app.workers.fantasy_resolver import resolve_fantasy_picks
    from app.workers.parlay_resolver import resolve_parlays
    from app.workers.wallet_maintenance import run_wallet_maintenance
    from app.workers.quotico_tip_worker import generate_quotico_tips
    from app.workers.qbot_worker import run_qbot

    # ~8 calls per poll, 500/day budget → poll every 30min = ~384 calls/day
    scheduler.add_job(poll_odds, "interval", minutes=30, id="odds_poller")
    scheduler.add_job(resolve_matches, "interval", minutes=30, id="match_resolver")
    scheduler.add_job(materialize_leaderboard, "interval", minutes=30, id="leaderboard")
    scheduler.add_job(check_badges, "interval", minutes=30, id="badge_engine")
    scheduler.add_job(sync_matchdays, "interval", minutes=30, id="matchday_sync")
    scheduler.add_job(resolve_spieltag_predictions, "interval", minutes=30, id="spieltag_resolver")
    scheduler.add_job(materialize_spieltag_leaderboard, "interval", minutes=30, id="spieltag_leaderboard")

    # Game mode resolvers (30min)
    scheduler.add_job(resolve_bankroll_bets, "interval", minutes=30, id="bankroll_resolver")
    scheduler.add_job(resolve_survivor_picks, "interval", minutes=30, id="survivor_resolver")
    scheduler.add_job(resolve_over_under_bets, "interval", minutes=30, id="over_under_resolver")
    scheduler.add_job(resolve_fantasy_picks, "interval", minutes=30, id="fantasy_resolver")
    scheduler.add_job(resolve_parlays, "interval", minutes=30, id="parlay_resolver")

    # Wallet maintenance (daily bonus for bankrupt wallets)
    scheduler.add_job(run_wallet_maintenance, "interval", hours=6, id="wallet_maintenance")

    # QuoticoTip EV engine (generates value-bet recommendations)
    scheduler.add_job(generate_quotico_tips, "interval", minutes=30, id="quotico_tip_worker")

    # Q-Bot auto-tipper (places tips from QuoticoTip recommendations)
    scheduler.add_job(run_qbot, "interval", minutes=30, id="qbot_worker")

    # Initial sync on startup (delayed 5s to let app fully start)
    async def initial_sync():
        await asyncio.sleep(5)
        await sync_matchdays()
        await poll_odds()

    asyncio.create_task(initial_sync())
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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Session middleware (used by Google OAuth for CSRF state)
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET, max_age=300)

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
from app.routers.spieltag import router as spieltag_router
from app.routers.legal import router as legal_router
from app.routers.wallet import router as wallet_router
from app.routers.survivor import router as survivor_router
from app.routers.fantasy import router as fantasy_router
from app.routers.parlay import router as parlay_router
from app.routers.historical import router as historical_router
from app.routers.quotico_tips import router as quotico_tips_router
from app.routers.teams import router as teams_router

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
app.include_router(spieltag_router)
app.include_router(legal_router)
app.include_router(wallet_router)
app.include_router(survivor_router)
app.include_router(fantasy_router)
app.include_router(parlay_router)
app.include_router(historical_router)
app.include_router(quotico_tips_router)
app.include_router(teams_router)


@app.exception_handler(InvalidId)
async def invalid_object_id_handler(request: Request, exc: InvalidId):
    return JSONResponse(status_code=400, content={"detail": "Ungültige ID."})


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
