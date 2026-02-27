"""
backend/app/main.py

Purpose:
    FastAPI application bootstrap, middleware/router wiring, and passive runtime
    startup for Greenfield v3.1.

Dependencies:
    - app.database
    - app.services.event_bus
    - app.services.websocket_manager
"""

import hashlib
import logging
from contextlib import asynccontextmanager

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
class _PassiveScheduler:
    """Compatibility shim for endpoints/tests expecting a scheduler object."""

    running = False

    def get_jobs(self) -> list:
        return []


scheduler = _PassiveScheduler()


async def set_automation_enabled(
    enabled: bool,
    *,
    run_initial_sync: bool = False,
    persist: bool = True,
) -> dict:
    """Legacy automation is disabled in v3.1."""
    _ = enabled, run_initial_sync, persist
    return {
        "enabled": False,
        "changed": False,
        "added_jobs": 0,
        "removed_jobs": 0,
        "scheduled_jobs": 0,
    }


def automation_enabled() -> bool:
    return False


def automated_job_count() -> int:
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await connect_db()
    from app.seed import ensure_startup_superadmin
    from app.services.event_bus import event_bus
    from app.services.event_bus_monitor import event_bus_monitor
    from app.services.event_handlers import register_event_handlers
    from app.services.websocket_manager import websocket_manager
    await ensure_startup_superadmin()
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
    if settings.METRICS_HEARTBEAT_ENABLED:
        from app.services.metrics_heartbeat import metrics_heartbeat
        await metrics_heartbeat.start()
        logger.info("Metrics heartbeat enabled")
    else:
        logger.info("Metrics heartbeat disabled via config")
    logger.info("Legacy automation disabled: passive startup mode active.")

    yield

    if settings.METRICS_HEARTBEAT_ENABLED:
        from app.services.metrics_heartbeat import metrics_heartbeat
        await metrics_heartbeat.stop()
    if settings.EVENT_BUS_ENABLED:
        if settings.QBUS_MONITOR_ENABLED:
            await event_bus_monitor.stop()
        await event_bus.stop()
    if settings.WS_EVENTS_ENABLED:
        await websocket_manager.stop()
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
from app.routers.leaderboard import router as leaderboard_router
from app.routers.squads import router as squads_router
from app.routers.battles import router as battles_router
from app.routers.twofa import router as twofa_router
from app.routers.gdpr import router as gdpr_router
from app.routers.ws import router as ws_router
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
from app.routers.qbot import router as qbot_router
from app.routers.betting_slips import router as betting_slips_router
from app.routers.leagues import router as leagues_router
from app.routers.admin import router as admin_router
from app.routers.admin_ingest import router as admin_ingest_router
from app.routers.admin_teams_v3 import router as admin_teams_v3_router
from app.routers.v3_query import router as v3_query_router

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(leaderboard_router)
app.include_router(squads_router)
app.include_router(battles_router)
app.include_router(twofa_router)
app.include_router(gdpr_router)
app.include_router(ws_router)
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
app.include_router(qbot_router)
app.include_router(betting_slips_router)
app.include_router(leagues_router)
app.include_router(admin_router)
app.include_router(admin_ingest_router)
app.include_router(admin_teams_v3_router)
app.include_router(v3_query_router)


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
