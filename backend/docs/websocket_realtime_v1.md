# WebSocket Realtime V1

## Purpose
Realtime delivery for qbus events to authenticated clients without HTTP polling.

## Endpoints
- `/ws/live-scores`: legacy live score stream (kept for compatibility).
- `/ws`: authenticated qbus event stream.

## Authentication
- Preferred: `access_token` HTTP-only cookie.
- Fallback: `?token=<jwt>` query parameter.
- Token must be `type=access`, user must exist (`is_deleted=false`) and not be banned.

## Client Commands
- `subscribe`
- `unsubscribe`
- `replace_subscriptions`
- `ping`

Example:
```json
{
  "type": "subscribe",
  "match_ids": ["507f1f77bcf86cd799439011"],
  "league_codes": ["bl1"],
  "sport_keys": ["soccer_germany_bundesliga"],
  "event_types": ["odds.ingested", "match.updated"]
}
```

## Server Event Envelope
```json
{
  "type": "odds.ingested",
  "data": {},
  "meta": {
    "event_id": "...",
    "correlation_id": "...",
    "occurred_at": "..."
  }
}
```

## Event Routing Rules
- No filters set: receives all events.
- Filters set: OR match across `match_ids`, `league_ids`, `league_codes`, `sport_keys`.
- `event_types` acts as an additional gate.

## `odds.ingested` Efficiency Contract
- Handler deduplicates `match_ids` immediately.
- Match enrichment is done via one batch DB query (`$in`).
- Exactly one `websocket_manager.broadcast(...)` call per `odds.ingested` event.
- No per-match broadcast loop.

## Scope and Limits
- Single-process delivery only (same as qbus V1).
- At-most-once semantics.
- No cross-instance fanout in V1.
