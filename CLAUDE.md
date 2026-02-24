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
- **Calibration prep:** scheduled calibration jobs normalize legacy bookmaker odds into canonical `odds.h2h` via `backend/app/services/odds_normalization_service.py` before running optimizer calibration.
- **Qbot evolution:** `tools/qbot_evolution_arena.py` uses soft bet-count penalties (no hard kill), two-stage search relaxation, Pareto compromise selection, and shadow-mode persistence with structured `optimization_notes`.
- **Modern-era fitness:** Arena tip loading defaults to an 8-year lookback (`--lookback-years` override), applies linear time-decay weights (floor 0.20) to ROI/Sharpe/Drawdown fitness terms, and persists `optimization_notes.lookback_years`.
- **Engine time-machine snapshots:** `tools/engine_time_machine.py` now writes calendar-month snapshots (month-start anchors), supports `--mode auto` (monthly refinement, quarterly exploration), and ensures gap-free history via carry-forward snapshots with `meta.source` (`time_machine` / `time_machine_carry_forward`) and audit metadata (`window_start/end`, `script_version`).
- **Engine time-machine parallelism:** per-league execution uses `ProcessPoolExecutor` (true multi-core parallelism for CPU-heavy calibration). `--concurrency 1` keeps sequential mode for debugging; `--rerun` cleanup still completes before parallel workers start.
- **Engine time-machine rerun:** `tools/engine_time_machine.py --rerun` clears only retro snapshots for target leagues (`meta.source=time_machine*` or `meta.is_retroactive=true`) before rebuilding; live worker snapshots remain untouched.
- **Qbot deep/robustness updates:** deep mode now uses a consistent CV objective (`mean - 0.5*std`) in-loop and for final selection; candidate pools are mode-adaptive (quick=12, deep=20), stress testing is two-stage (bootstrap/MC prefilter then finalist pass), and Monte-Carlo path shuffling is vectorized for speed.
- **Qbot stress acceleration:** bootstrap sampling is vectorized in NumPy batches (no per-sample Python loop), and Monte-Carlo prefilter supports fail-fast cutoffs (early abort on clearly excessive ruin-rate trajectories).
- **Qbot candidate parallelism:** per-league candidate stress evaluation supports threaded execution via `--candidate-workers N` (default `1`), with deterministic per-candidate RNG seeds.
- **Qbot pipeline reuse:** single-bot confidence/stake/profit logic is centralized in `_single_bot_pipeline()` and reused by detailed metrics, bootstrap, and Monte-Carlo to avoid drift across duplicated implementations.
- **Qbot stress profiling:** `_stress_test_with_rescue()` emits per-candidate timing (`bs/mc prefilter`, `rescue`, `bs/mc final`) and stage-level aggregate summaries with cache-hit rates via `Stress timing` / `Stress timing summary` logs.
- **Qbot ensemble mining:** `tools/qbot_ensemble_miner.py` runs deterministic multi-seed GA batches (`--runs`, `--base-seed`) per league, computes per-gene mean/std/CV robustness classes, prints a consensus report, and can persist `is_ensemble: true` shadow strategies using robust-gene consensus DNA.
- **Qbot ensemble identities:** the ensemble miner optionally persists archetype identities via `--with-archetypes` (`consensus`, `profit_hunter`, `volume_grinder`) as separate shadow strategy docs for league-level comparison and activation decisions.
- **Admin Qbot Lab API:** `GET /api/admin/qbot/strategies` groups strategies by league, deduplicates to one representative per `sport_key` (active-first), classifies into `active`/`shadow`/`failed` (`archived` alias), and exposes portfolio summary + optimization metadata labels for tabbed admin UX.
- **Shadow visibility + comparison:** Qbot Lab API additionally surfaces shadow identity entries (e.g. consensus/profit_hunter/volume_grinder) even when a league has an active representative, including `active_comparison` deltas (ROI/Bets/Sharpe vs active).
- **Admin strategy backtest:** `GET /api/admin/qbot/strategies/{id}/backtest` simulates a 1000 EUR bankroll over resolved league tips using the strategy DNA and returns an equity-curve point series for chart rendering.
- **Admin bet ledger:** `GET /api/admin/qbot/strategies/{id}/backtest/ledger` returns per-bet journal rows (date/match, edge, odds, stake, win/loss, net P/L, bankroll before/after) with optional `limit` (`0` = all).
- **Backtest window controls:** `/backtest` and `/backtest/ledger` accept `since_date` (ISO). Without `since_date`, service prioritizes strategy `optimization_notes.validation_window` (or reconstructed 80/20 pre-creation validation window), and only falls back to default lookback limits (last 3 years / max 1000 tips).
- **Engine snapshot source separation:** live calibration writes `engine_config_history.meta.source = \"live_worker\"`; qtip backfill prioritizes retro time-machine snapshots when present to avoid mixing live-now parameters into historical replay.
- **Backtest stake sizing fix:** admin backtests now size stakes from current bankroll (`stake = bankroll * KellyFraction`), capped by DNA `max_stake` and a hard risk brake `bankroll * 0.05`, then resolve odds from historical match `odds.h2h` first (implied-probability fallback only), preventing micro-stakes and frequent synthetic `odds=10` artifacts.
- **Backtest consistency metrics:** backtest responses include time-weighted metrics (`weighted_roi`, `weighted_profit`, `weighted_staked`) using the same linear decay scheme as arena fitness; ledger rows are returned newest-first.
- **Admin Qbot Lab UI:** strategy rows now surface identity cards (`consensus`, `profit_hunter`, `volume_grinder`), include an inline equity-curve chart in detail view, and support manual identity activation via `POST /api/admin/qbot/strategies/{id}/activate`.
- **Admin strategy detail route:** `/admin/qbot-lab/:strategyId` provides a dedicated strategy screen with identity switching/activation, full backtest chart, ledger table, and CSV export based on `/api/admin/qbot/strategies/{id}` + `/backtest` + `/backtest/ledger`.
- **Decision traceability:** QTip enrichment now persists a `decision_trace` object per tip (engine metrics, matched strategy metadata, DNA filter checks, risk-stage calculations, and explicit kill-point), and tip detail UI renders this as a step-by-step decision journey.
- **Admin trace deep-link:** Dedicated admin route `/admin/qtips/:matchId` renders a linkable QTip Decision Trace detail view (`frontend/src/views/admin/AdminQtipTraceView.vue`) using the shared `DecisionJourney` component.

### Frontend — Vue 3 + TypeScript
- **Build:** Vite with `@vitejs/plugin-vue`
- **State:** Pinia stores in `frontend/src/stores/`
- **Routing:** Vue Router in `frontend/src/router/index.ts`
- **API calls:** `frontend/src/composables/useApi.ts` — fetch wrapper with auth
- **i18n:** vue-i18n with locale files in `frontend/src/locales/` (`de.ts`, `en.ts`). Default locale: `de`. Config in `frontend/src/i18n.ts`. Use `$t('key')` in templates, `t('key')` in script setup via `useI18n()`.
- **Sport nav icons:** `frontend/src/components/layout/SportNav.vue` maps league keys to country flag icons (e.g. Bundesliga → 🇩🇪, EPL → 🇬🇧) instead of a shared football icon.
- **Sport metadata:** `frontend/src/types/sports.ts` is the shared source for league labels and country flags (used by sport nav and homepage match cards).
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
