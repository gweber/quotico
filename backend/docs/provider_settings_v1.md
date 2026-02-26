<!--
backend/docs/provider_settings_v1.md

Purpose:
    Operational guide for DB-first provider runtime settings and secrets.
-->

# Provider Settings V1

## Effective Resolution

Runtime precedence is:

1. DB league override
2. DB global
3. ENV fallback
4. hardcoded defaults

For secrets:

1. DB league secret
2. DB global secret
3. ENV fallback

## Rate Limiting

- `rate_limit_rpm` is provider-wide across all endpoints.
- V1 uses an in-memory token bucket per provider, per process.
- In multi-instance deployments this is instance-local, not globally coordinated.

## Cache Behavior

- Provider settings cache default TTL: 15 seconds (`PROVIDER_SETTINGS_CACHE_TTL`).
- Admin updates invalidate local cache immediately.
- Multi-instance fallback: set cache TTL to `0` until distributed invalidation exists.

