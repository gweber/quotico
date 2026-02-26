"""
backend/app/main.py

Purpose:
    FastAPI application bootstrap, middleware/router wiring, scheduler lifecycle,
    and startup initialization for core registries and seed data.

Dependencies:
    - app.database
    - app.services.league_service
    - app.services.team_registry_service
"""

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
from app.utils import utcnow

logger = logging.getLogger("quotico")
scheduler = AsyncIOScheduler()
_AUTOMATION_META_ID = "automation_settings"
_AUTOMATED_JOB_IDS = {
    "odds_poller",
    "match_resolver",
    "leaderboard",
    "badge_engine",
    "matchday_sync",
    "matchday_resolver",
    "matchday_leaderboard",
    "wallet_maintenance",
    "qbot_bets",
    "calibration_eval",
    "calibration_refine",
    "calibration_explore",
    "reliability_check",
}
_automation_enabled = False


def _build_automated_job_specs() -> list[dict]:
    from app.workers.odds_poller import poll_odds, run_qbot_bets
    from app.workers.match_resolver import resolve_matches
    from app.workers.leaderboard import materialize_leaderboard
    from app.workers.badge_engine import check_badges
    from app.workers.matchday_sync import sync_matchdays
    from app.workers.matchday_resolver import resolve_matchday_predictions
    from app.workers.matchday_leaderboard import materialize_matchday_leaderboard
    from app.workers.wallet_maintenance import run_wallet_maintenance
    from app.workers.calibration_worker import (
        run_daily_evaluation, run_weekly_refinement, run_monthly_exploration,
        run_reliability_check,
    )
    return [
        {"id": "odds_poller", "func": poll_odds, "trigger": "interval", "trigger_kwargs": {"minutes": 15}},
        {"id": "match_resolver", "func": resolve_matches, "trigger": "interval", "trigger_kwargs": {"hours": 3}},
        {"id": "leaderboard", "func": materialize_leaderboard, "trigger": "interval", "trigger_kwargs": {"hours": 3}},
        {"id": "badge_engine", "func": check_badges, "trigger": "interval", "trigger_kwargs": {"minutes": 30}},
        {"id": "matchday_sync", "func": sync_matchdays, "trigger": "interval", "trigger_kwargs": {"minutes": 30}},
        {"id": "matchday_resolver", "func": resolve_matchday_predictions, "trigger": "interval", "trigger_kwargs": {"minutes": 30}},
        {"id": "matchday_leaderboard", "func": materialize_matchday_leaderboard, "trigger": "interval", "trigger_kwargs": {"hours": 6}},
        {"id": "wallet_maintenance", "func": run_wallet_maintenance, "trigger": "interval", "trigger_kwargs": {"hours": 6}},
        {"id": "qbot_bets", "func": run_qbot_bets, "trigger": "interval", "trigger_kwargs": {"minutes": 5}},
        {"id": "calibration_eval", "func": run_daily_evaluation, "trigger": "cron", "trigger_kwargs": {"hour": 3, "minute": 0}},
        {"id": "calibration_refine", "func": run_weekly_refinement, "trigger": "cron", "trigger_kwargs": {"day_of_week": "mon", "hour": 4, "minute": 0}},
        {"id": "calibration_explore", "func": run_monthly_exploration, "trigger": "cron", "trigger_kwargs": {"day": 1, "hour": 5, "minute": 0}},
        {"id": "reliability_check", "func": run_reliability_check, "trigger": "cron", "trigger_kwargs": {"day_of_week": "sun", "hour": 23, "minute": 0}},
    ]


def _register_automated_jobs() -> int:
    added = 0
    for spec in _build_automated_job_specs():
        if scheduler.get_job(spec["id"]):
            continue
        scheduler.add_job(
            spec["func"],
            spec["trigger"],
            id=spec["id"],
            replace_existing=True,
            **spec["trigger_kwargs"],
        )
        added += 1
    return added


def _remove_automated_jobs() -> int:
    removed = 0
    for job_id in _AUTOMATED_JOB_IDS:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            removed += 1
    return removed


async def _initial_sync_after_enable() -> None:
    from app.workers.matchday_sync import sync_matchdays
    from app.workers.odds_poller import poll_odds

    await asyncio.sleep(5)
    try:
        await asyncio.gather(sync_matchdays(), poll_odds())
    except Exception:
        logger.exception("Initial sync after automation enable failed")


async def set_automation_enabled(
    enabled: bool,
    *,
    run_initial_sync: bool = False,
    persist: bool = True,
) -> dict:
    global _automation_enabled

    changed = enabled != _automation_enabled
    added = 0
    removed = 0

    if enabled:
        added = _register_automated_jobs()
        _automation_enabled = True
        if run_initial_sync:
            asyncio.create_task(_initial_sync_after_enable())
    else:
        removed = _remove_automated_jobs()
        _automation_enabled = False

    if persist:
        await _db.db.meta.update_one(
            {"_id": _AUTOMATION_META_ID},
            {"$set": {"enabled": _automation_enabled, "updated_at": utcnow()}},
            upsert=True,
        )

    return {
        "enabled": _automation_enabled,
        "changed": changed,
        "added_jobs": added,
        "removed_jobs": removed,
        "scheduled_jobs": automated_job_count(),
    }


def automation_enabled() -> bool:
    return _automation_enabled


def automated_job_count() -> int:
    return sum(1 for job in scheduler.get_jobs() if job.id in _AUTOMATED_JOB_IDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await connect_db()
    from app.services.league_service import seed_core_leagues
    from app.services.provider_settings_seed_service import seed_provider_settings_defaults
    from app.services.team_seed_service import seed_core_teams
    from app.services.team_registry_service import TeamRegistry
    from app.services.event_bus import event_bus
    from app.services.event_bus_monitor import event_bus_monitor
    from app.services.event_handlers import register_event_handlers
    from app.services.websocket_manager import websocket_manager

    seed_result = await seed_core_leagues()
    logger.info("Core leagues seeded on startup: %s", seed_result)
    provider_settings_seed = await seed_provider_settings_defaults()
    logger.info("Provider settings seeded on startup: %s", provider_settings_seed)
    team_seed_result = await seed_core_teams()
    logger.info("Core teams seeded on startup: %s", team_seed_result)

    registry = TeamRegistry.get()
    await registry.initialize()
    await registry.start_background_refresh()

    # Seed on startup
    from app.seed import seed_initial_user, seed_qbot_user
    await seed_initial_user()
    await seed_qbot_user()

    scheduler.start()
    await set_automation_enabled(False, run_initial_sync=False, persist=False)
    if settings.WS_EVENTS_ENABLED:
        await websocket_manager.start()
        logger.info("WebSocket realtime manager enabled")
    else:
        logger.info("WebSocket realtime manager disabled via config")
    if settings.EVENT_BUS_ENABLED:
        register_event_handlers(event_bus)
        await event_bus.start()
        logger.info("Event bus enabled")
        if settings.QBUS_MONITOR_ENABLED:
            await event_bus_monitor.start()
            logger.info("Event bus monitor enabled")
        else:
            logger.info("Event bus monitor disabled via config")
    else:
        logger.info("Event bus disabled via config")
    logger.info("Automated workers disabled on startup. Use Admin to activate.")
    logger.info("Background scheduler started")

    yield

    if settings.EVENT_BUS_ENABLED:
        if settings.QBUS_MONITOR_ENABLED:
            await event_bus_monitor.stop()
        await event_bus.stop()
    if settings.WS_EVENTS_ENABLED:
        await websocket_manager.stop()
    await registry.stop_background_refresh()
    if scheduler.running:
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
from app.routers.leagues import router as leagues_router
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
app.include_router(leagues_router)
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
