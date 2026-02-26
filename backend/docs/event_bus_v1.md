# Event Bus V1

## Purpose
In-process reactive event handling for match and odds workflows with minimal coupling.

## Guarantees
- Delivery semantics: `at-most-once` (V1).
- Scope: single process memory only.
- No durability across process restarts.

## Event Contracts
Source of truth: `backend/app/services/event_models.py`.

### Match Events
- `match.created`
- `match.updated`
- `match.finalized`
- `match.postponed`
- `match.cancelled`

### Odds Events
- `odds.ingested`

### Matchday Events (reserved)
- `matchday.started`
- `matchday.completed`

## Payload Strategy
ID-first payloads only:
- `match_id`, `league_id`, `sport_key`, `season`
- compact status deltas / counters

Subscribers must fetch full documents from DB if needed.

## Idempotency Requirements
Every handler must be idempotent:
- check if target state already exists before side effects
- use guarded updates (`$set` with conditions, upserts where useful)
- avoid duplicate external effects

## Monitoring
- `GET /api/admin/event-bus/status` exposes:
  - queue depths
  - published / handled / failed / dropped counters
  - recent handler errors (bounded buffer)
- `GET /api/admin/event-bus/history` exposes historical trend data from `event_bus_stats`.
- `GET /api/admin/event-bus/handlers` exposes 1h handler rollups aggregated from snapshots.

## QBus Health Monitor V1
- Sampling interval: 10 seconds (`QBUS_MONITOR_SAMPLING_SECONDS`).
- Historical persistence: `event_bus_stats` with TTL (`QBUS_MONITOR_TTL_DAYS`).
- Rates are computed in qbus via sliding-window ringbuffers (1-minute window).
- Snapshot documents intentionally store only `recent_errors_count` (not full error payloads).
- Full recent error details are served directly from in-memory qbus ringbuffer.

## Rollback Playbook
1. Set `EVENT_BUS_ENABLED=false`.
2. Redeploy backend.
3. Reduce fallback polling intervals for resolver/leaderboard jobs if needed.
4. Inspect structured handler error logs and admin status before re-enabling.
