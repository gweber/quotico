# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Quotico.de is a competitive sports prediction platform. Users predict football match outcomes, earn points based on locked odds, and compete in squad-based battles. The app covers Bundesliga, Premier League, La Liga, and other leagues.

## Development Commands

```bash
# Start both backend + frontend for local dev
./dev.sh

# Backend only (from backend/)
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 4201

# Frontend only (from frontend/)
pnpm dev          # Vite dev server on http://localhost:5173
pnpm build        # TypeScript check + Vite production build

# MongoDB (local, no auth needed — dev.sh overrides MONGO_URI)
docker compose up mongodb

# Production deploy (on server only)
./deploy.sh
```

No test suite exists yet. The frontend uses TypeScript strict mode as its primary type checking.

## Architecture

### Backend — FastAPI (Python 3.13)
- **Entry:** `backend/app/main.py` — FastAPI app, scheduler startup, middleware registration
- **Config:** `backend/app/config.py` — Pydantic Settings, reads `.env` from project root
- **Database:** `backend/app/database.py` — Motor async MongoDB client, index creation, migrations on startup
- **Routers:** `backend/app/routers/` — one file per domain (auth, matches, bets, squads, battles, matchday, etc.)
- **Services:** `backend/app/services/` — business logic separated from HTTP layer
- **Providers:** `backend/app/providers/` — external API integrations (TheOddsAPI, football-data.org, OpenLigaDB, ESPN)
- **Workers:** `backend/app/workers/` — APScheduler background jobs (odds polling, match resolution, leaderboard materialization, badge engine) running every 30 minutes

### Frontend — Vue 3 + TypeScript
- **Build:** Vite with `@vitejs/plugin-vue`
- **State:** Pinia stores in `frontend/src/stores/`
- **Routing:** Vue Router in `frontend/src/router/index.ts`
- **API calls:** `frontend/src/composables/useApi.ts` — fetch wrapper with auth
- **i18n:** vue-i18n with locale files in `frontend/src/locales/` (`de.ts`, `en.ts`). Default locale: `de`. Config in `frontend/src/i18n.ts`. Use `$t('key')` in templates, `t('key')` in script setup via `useI18n()`.
- **Styling:** Tailwind CSS
- **Path alias:** `@/*` maps to `frontend/src/*`
- **Dev proxy:** Vite proxies `/api` and `/ws` to backend at localhost:4201

### Database — MongoDB 7
No ORM. Direct Motor async driver with `app.database.db` module-level instance. Collections: `users`, `matches`, `bets`, `squads`, `battles`, `battle_participations`, `leaderboard`, `points_transactions`, `badges`, `audit_logs`, `refresh_tokens`, `access_blocklist`, `matchday_predictions`, `matchday_leaderboard`, `quotico_tips`, `join_requests`, `wallet_transactions`. Indexes are created on startup in `database._ensure_indexes()`. Naming migrations run on startup in `database._migrate_naming_refactor()`.

**Datetime convention:** MongoDB returns naive datetimes (no tzinfo). All Python-side datetime arithmetic must use the helpers in `backend/app/utils.py`:
- `utcnow()` — tz-aware "now" (use instead of `datetime.now()`/`datetime.utcnow()`)
- `ensure_utc(dt)` — wrap any datetime read from MongoDB before comparing/subtracting with `utcnow()`
- `parse_utc(value)` — parse a string or datetime into tz-aware UTC (use for provider API responses)

### Auth Flow
- JWT access tokens (15 min, httpOnly cookie) + refresh tokens (7 days, httpOnly cookie)
- Refresh token family tracking for reuse detection
- Token blocklists with TTL auto-deletion
- Google OAuth as alternative login
- Optional TOTP 2FA with Fernet-encrypted secrets (supports key rotation via `ENCRYPTION_KEY_OLD`)
- JWT secret rotation supported via `JWT_SECRET_OLD`
- Passwords hashed with Argon2

### Key Business Logic
- **Bets:** User selects match outcome (1/X/2), odds locked server-side at creation. On match completion, worker resolves bets: win = `locked_odds × 10` points, loss = 0.
- **Squads:** User groups with invite codes (QUO-XXXX-XX). One admin per squad.
- **Battles:** Squad vs squad over a date range, resolved by weighted average points.
- **Aliases:** Auto-generated on registration (e.g. "BrilliantFlamingo"), customizable. Slugified for uniqueness.

## Environment Variables

Defined in `.env` at project root (see `.env.example`). Key vars:
- `MONGO_URI`, `MONGO_DB` — database connection
- `JWT_SECRET`, `JWT_SECRET_OLD` — token signing (OLD for rotation)
- `ENCRYPTION_KEY`, `ENCRYPTION_KEY_OLD` — Fernet encryption for 2FA secrets
- `ODDSAPIKEY`, `FOOTBALL_DATA_API_KEY` — external data providers
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — OAuth
- `BACKEND_CORS_ORIGINS`, `COOKIE_SECURE` — security settings (dev.sh overrides these locally)

## Production

- Nginx reverse proxy with TLS (Let's Encrypt), rate limiting (5 req/min auth, 30 req/s API)
- Systemd service (`quotico.service`) runs uvicorn on 127.0.0.1:4201
- Frontend built to static files served by Nginx with SPA fallback
- WebSocket endpoint at `/ws` for live scores
