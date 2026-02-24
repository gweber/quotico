import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson.errors import InvalidId
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)
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

    # Team mappings seeded in database._seed_team_mappings() during connect_db()

    # Start background workers
    from app.workers.odds_poller import poll_odds
    from app.workers.match_resolver import resolve_matches
    from app.workers.leaderboard import materialize_leaderboard
    from app.workers.badge_engine import check_badges
    from app.workers.matchday_sync import sync_matchdays
    from app.workers.matchday_resolver import resolve_matchday_predictions
    from app.workers.matchday_leaderboard import materialize_matchday_leaderboard
    from app.workers.wallet_maintenance import run_wallet_maintenance
    from app.workers.odds_poller import run_qbot_bets

    # ~5 calls per poll (smart sleep), 20k/month budget -> poll every 15min ~ 14k/month
    scheduler.add_job(poll_odds, "interval", minutes=15, id="odds_poller")
    # Universal resolver: handles all slip types (single, parlay, matchday, survivor, fantasy, O/U, bankroll)
    scheduler.add_job(resolve_matches, "interval", minutes=30, id="match_resolver")
    scheduler.add_job(materialize_leaderboard, "interval", minutes=30, id="leaderboard")
    scheduler.add_job(check_badges, "interval", minutes=30, id="badge_engine")
    scheduler.add_job(sync_matchdays, "interval", minutes=30, id="matchday_sync")
    # Auto-bet injection only (scoring handled by universal resolver)
    scheduler.add_job(resolve_matchday_predictions, "interval", minutes=30, id="matchday_resolver")
    scheduler.add_job(materialize_matchday_leaderboard, "interval", minutes=30, id="matchday_leaderboard")

    # Wallet maintenance (daily bonus for bankrupt wallets)
    scheduler.add_job(run_wallet_maintenance, "interval", hours=6, id="wallet_maintenance")

    # Q-Bot: place bets for matches kicking off within 15 min (candidates generated inline by odds_poller)
    scheduler.add_job(run_qbot_bets, "interval", minutes=5, id="qbot_bets")

    # Self-calibration: daily eval, weekly refinement, monthly exploration
    from app.workers.calibration_worker import (
        run_daily_evaluation, run_weekly_refinement, run_monthly_exploration,
        run_reliability_check,
    )
    scheduler.add_job(run_daily_evaluation, "cron", hour=3, minute=0, id="calibration_eval")
    scheduler.add_job(run_weekly_refinement, "cron", day_of_week="mon", hour=4, minute=0, id="calibration_refine")
    scheduler.add_job(run_monthly_exploration, "cron", day=1, hour=5, minute=0, id="calibration_explore")
    # Reliability: meta-learning confidence calibration (Sunday 23:00)
    scheduler.add_job(run_reliability_check, "cron", day_of_week="sun", hour=23, minute=0, id="reliability_check")

    # Initial sync on startup (delayed 5s to let app fully start)
    async def initial_sync():
        await asyncio.sleep(5)
        try:
            await asyncio.gather(sync_matchdays(), poll_odds())
        except Exception:
            logger.exception("Initial sync failed — scheduler will retry on next interval")

    asyncio.create_task(initial_sync())
    scheduler.start()
    logger.info("Background scheduler started")

    yield

    scheduler.shutdown(wait=False)
    await close_db()


app = FastAPI(
    title="Quotico.de",
    description="Odds-based sports prediction platform",
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
from app.routers.matchday import router as matchday_router
from app.routers.legal import router as legal_router
from app.routers.wallet import router as wallet_router
from app.routers.survivor import router as survivor_router
from app.routers.fantasy import router as fantasy_router
from app.routers.parlay import router as parlay_router
from app.routers.historical import router as historical_router
from app.routers.quotico_tips import router as quotico_tips_router
from app.routers.teams import router as teams_router
from app.routers.qbot import router as qbot_router
from app.routers.betting_slips import router as betting_slips_router

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(matches_router)
app.include_router(leaderboard_router)
app.include_router(squads_router)
app.include_router(battles_router)
app.include_router(twofa_router)
app.include_router(gdpr_router)
app.include_router(ws_router)
app.include_router(admin_router)
app.include_router(badges_router)
app.include_router(google_auth_router)
app.include_router(matchday_router)
app.include_router(legal_router)
app.include_router(wallet_router)
app.include_router(survivor_router)
app.include_router(fantasy_router)
app.include_router(parlay_router)
app.include_router(historical_router)
app.include_router(quotico_tips_router)
app.include_router(teams_router)
app.include_router(qbot_router)
app.include_router(betting_slips_router)


@app.exception_handler(InvalidId)
async def invalid_object_id_handler(request: Request, exc: InvalidId):
    return JSONResponse(status_code=400, content={"detail": "Invalid ID."})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Return clean validation errors without leaking internal field paths."""
    errors = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # Strip the "body" / "query" prefix for cleaner messages
        field = ".".join(str(l) for l in loc[1:]) if len(loc) > 1 else str(loc[-1]) if loc else "unknown"
        errors.append({"field": field, "message": err.get("msg", "Invalid value.")})
    return JSONResponse(status_code=422, content={"detail": "Validation error.", "errors": errors})


@app.exception_handler(DuplicateKeyError)
async def duplicate_key_handler(request: Request, exc: DuplicateKeyError):
    return JSONResponse(status_code=409, content={"detail": "Duplicate entry."})


@app.exception_handler(ServerSelectionTimeoutError)
async def db_timeout_handler(request: Request, exc: ServerSelectionTimeoutError):
    logger.error("Database timeout: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable."})


@app.exception_handler(ConnectionFailure)
async def db_connection_handler(request: Request, exc: ConnectionFailure):
    logger.error("Database connection failure: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable."})


@app.exception_handler(OperationFailure)
async def db_operation_handler(request: Request, exc: OperationFailure):
    logger.error("Database operation error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred."})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    logger.warning("ValueError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": "Invalid input."})


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError):
    logger.warning("KeyError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": "Missing required field."})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all: log the real error, return a safe generic message."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred."})


@app.get("/health")
async def health():
    """Health check -- verifies DB connection and provider status."""
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
