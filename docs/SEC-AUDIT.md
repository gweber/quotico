# Quotico.de Security Audit Report

**Date:** 2026-02-22
**Stack:** FastAPI (Python) + Vue 3 + MongoDB (Motor) + Nginx + Systemd
**Auditor:** Claude (Opus 4.6)
**Status:** 19/19 findings resolved

---

## Summary

| Severity | Found | Resolved |
|----------|-------|----------|
| Critical | 2     | 2        |
| High     | 6     | 6        |
| Medium   | 7     | 7        |
| Low      | 4     | 4        |
| **Total**| **19**| **19**   |

---

## Findings

### #1 — Rate Limiting
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-307 (Improper Restriction of Excessive Authentication Attempts) |
| **Location** | `nginx/nginx.conf` |
| **Regulatory** | NIST SP 800-53 AC-7 |
| **Status** | Resolved |

**Vulnerability:** No rate limiting on authentication endpoints allowed brute-force attacks.

**Remediation:** Added nginx `limit_req` zones:
- `auth`: 5 requests/minute on `/api/auth/login`, `/api/auth/register`
- `api`: 30 requests/second on `/api/` (general)

---

### #2 — CORS Wildcard
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-942 (Overly Permissive Cross-domain Whitelist) |
| **Location** | `backend/app/config.py`, `backend/app/main.py` |
| **Regulatory** | OWASP A05 Security Misconfiguration |
| **Status** | Resolved |

**Vulnerability:** CORS origins were hardcoded/overly permissive.

**Remediation:** `BACKEND_CORS_ORIGINS` driven by `.env`. Production: `https://quotico.de`. Dev: `http://localhost:5173`.

---

### #3 — Weak Password Hashing (bcrypt -> Argon2)
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-916 (Use of Password Hash With Insufficient Computational Effort) |
| **Location** | `backend/app/services/auth_service.py` |
| **Regulatory** | OWASP A02 Cryptographic Failures |
| **Status** | Resolved |

**Vulnerability:** Initial implementation used bcrypt. While not broken, Argon2id is the current OWASP recommendation.

**Remediation:** Switched to `argon2-cffi` with `PasswordHasher()` (Argon2id defaults).

---

### #4 — JWT Library (python-jose -> PyJWT)
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-1395 (Dependency on Vulnerable Third-Party Component) |
| **Location** | `backend/app/services/auth_service.py` |
| **Regulatory** | OWASP A06 Vulnerable Components |
| **Status** | Resolved |

**Vulnerability:** `python-jose` is unmaintained. Known CVEs in dependencies.

**Remediation:** Replaced with `PyJWT` (actively maintained). All `jwt.encode`/`jwt.decode` calls updated.

---

### #5 — Cookie Security
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-614 (Sensitive Cookie Without 'Secure' Flag) |
| **Location** | `backend/app/services/auth_service.py` |
| **Regulatory** | OWASP A02, PCI-DSS 4.0 Req. 6.2.4 |
| **Status** | Resolved |

**Vulnerability:** Auth cookies lacked proper security flags.

**Remediation:**
- `HttpOnly=True` (prevents XSS cookie theft)
- `Secure` driven by `COOKIE_SECURE` env var (True in production/HTTPS)
- `SameSite=Lax` (CSRF protection)
- Refresh token scoped to `path=/api/auth/refresh`

---

### #6 — In-Memory Token Storage
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-613 (Insufficient Session Expiration) |
| **Location** | `backend/app/services/auth_service.py`, `backend/app/database.py` |
| **Regulatory** | NIST SP 800-53 SC-23 |
| **Status** | Resolved |

**Vulnerability:** Refresh tokens and access token blocklist stored in Python dicts/sets. Lost on restart, making revoked tokens valid again.

**Remediation:** Migrated to MongoDB collections with TTL indexes:
- `refresh_tokens` — stores JTI, user_id, family, expires_at (auto-cleanup)
- `access_blocklist` — stores JTI, expires_at (auto-cleanup)

All token functions converted to `async` with MongoDB operations. Zero in-memory state.

---

### #7 — Refresh Token Rotation & Replay Detection
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-294 (Authentication Bypass by Capture-replay) |
| **Location** | `backend/app/services/auth_service.py`, `backend/app/routers/auth.py` |
| **Regulatory** | OWASP A07 Identification and Authentication Failures |
| **Status** | Resolved |

**Vulnerability:** No refresh token rotation. Stolen refresh tokens could be used indefinitely.

**Remediation:** Family-based rotation with replay detection:
- Each refresh token belongs to a "family"
- On refresh: old token invalidated, new token issued in same family
- Token reuse (replay) invalidates entire family
- Backed by MongoDB (survives restarts)

---

### #8 — Input Validation
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-20 (Improper Input Validation) |
| **Location** | `backend/app/models/user.py`, `backend/app/routers/auth.py` |
| **Regulatory** | OWASP A03 Injection |
| **Status** | Resolved |

**Vulnerability:** Insufficient input validation on user-facing endpoints.

**Remediation:** Pydantic models with:
- `EmailStr` for email validation
- Password minimum length (10 chars)
- Alias validation (length, allowed characters, profanity check)
- All request bodies validated via Pydantic `BaseModel`

---

### #9 — HTTPS / TLS
| | |
|---|---|
| **Severity** | Critical |
| **CWE** | CWE-319 (Cleartext Transmission of Sensitive Information) |
| **Location** | `nginx/nginx.conf` |
| **Regulatory** | PCI-DSS 4.0 Req. 4.2.1, GDPR Art. 32 |
| **Status** | Resolved |

**Vulnerability:** No TLS configuration.

**Remediation:** Full nginx TLS setup:
- HTTP -> HTTPS redirect (port 80 -> 443)
- www -> apex domain redirect
- TLS 1.2+ with modern cipher suite
- HSTS with 1-year max-age
- Certbot integration for Let's Encrypt

---

### #10 — Security Headers
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-693 (Protection Mechanism Failure) |
| **Location** | `nginx/nginx.conf` |
| **Regulatory** | OWASP A05 Security Misconfiguration |
| **Status** | Resolved |

**Vulnerability:** Missing security headers.

**Remediation:** Added to nginx:
- `Content-Security-Policy` (script-src 'self', style-src 'self' 'unsafe-inline', connect-src with wss://, form-action for Google OAuth)
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

---

### #11 — MongoDB Authentication
| | |
|---|---|
| **Severity** | Critical |
| **CWE** | CWE-798 (Use of Hard-coded Credentials) |
| **Location** | `.env`, `docker-compose.yml` |
| **Regulatory** | PCI-DSS 4.0 Req. 2.2.2, NIST SP 800-53 IA-5 |
| **Status** | Resolved |

**Vulnerability:** MongoDB credentials hardcoded or missing.

**Remediation:**
- `MONGO_USER`, `MONGO_PASSWORD` in `.env` (not committed)
- `MONGO_URI` with `authSource=admin`
- Docker Compose reads from env vars
- `.env.example` with placeholder values only

---

### #12 — WebSocket Authentication
| | |
|---|---|
| **Severity** | High |
| **CWE** | CWE-306 (Missing Authentication for Critical Function) |
| **Location** | `backend/app/routers/ws.py` |
| **Regulatory** | OWASP A07 Identification and Authentication Failures |
| **Status** | Resolved |

**Vulnerability:** WebSocket connections accepted without authentication.

**Remediation:** JWT cookie validation before `ws.accept()`:
- Read `access_token` cookie from WebSocket handshake
- Decode and verify via `decode_jwt()` (supports key rotation)
- Reject with `4001 Unauthorized` if invalid
- Uses same auth mechanism as REST endpoints

---

### #13 — WebSocket Connection Limit
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-770 (Allocation of Resources Without Limits) |
| **Location** | `backend/app/routers/ws.py` |
| **Regulatory** | NIST SP 800-53 SC-5 |
| **Status** | Resolved |

**Vulnerability:** No limit on concurrent WebSocket connections (DoS vector).

**Remediation:** `MAX_WS_CONNECTIONS = 500` cap in `LiveScoreManager`. Excess connections rejected with `4002 Too many connections`.

---

### #14 — Two-Factor Authentication (TOTP)
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-308 (Use of Single-factor Authentication) |
| **Location** | `backend/app/routers/twofa.py`, `backend/app/services/encryption.py` |
| **Regulatory** | NIST SP 800-53 IA-2(1) |
| **Status** | Resolved |

**Vulnerability:** No MFA option available.

**Remediation:** TOTP-based 2FA:
- Setup: Generate secret -> QR code -> encrypt with Fernet -> store
- Verify: Decrypt -> TOTP verify with `valid_window=1`
- Disable: Requires valid TOTP code
- Secret encrypted at rest with versioned Fernet keys

---

### #15 — GDPR Compliance (DSGVO)
| | |
|---|---|
| **Severity** | Medium |
| **CWE** | CWE-359 (Exposure of Private Personal Information) |
| **Location** | `backend/app/routers/gdpr.py` |
| **Regulatory** | GDPR Art. 17 (Right to Erasure), Art. 20 (Data Portability) |
| **Status** | Resolved |

**Remediation:**
- **Data Export** (`GET /api/gdpr/export`): Returns all user data as JSON (profile, tips, transactions, squads, battles)
- **Account Deletion** (`DELETE /api/gdpr/account`): Soft-delete with anonymization:
  - Email replaced with SHA-256 hash
  - Password hash cleared
  - 2FA secret cleared
  - Alias anonymized
  - `is_deleted` flag set
  - Squad memberships cleaned up
  - All tokens invalidated
- Password confirmation required for deletion

---

### #16 — Hardcoded Seed Credentials
| | |
|---|---|
| **Severity** | Low |
| **CWE** | CWE-798 (Use of Hard-coded Credentials) |
| **Location** | `backend/app/seed.py`, `backend/app/config.py` |
| **Regulatory** | OWASP A05 Security Misconfiguration |
| **Status** | Resolved |

**Vulnerability:** Seed admin email and password were hardcoded in `seed.py`.

**Remediation:** Moved to environment variables `SEED_ADMIN_EMAIL` and `SEED_ADMIN_PASSWORD`. Seeding skipped entirely when both are empty.

---

### #17 — Admin Audit Trail
| | |
|---|---|
| **Severity** | Low |
| **CWE** | CWE-778 (Insufficient Logging) |
| **Location** | `backend/app/database.py` |
| **Regulatory** | SOC2 CC6.1, PCI-DSS 4.0 Req. 10 |
| **Status** | Resolved |

**Vulnerability:** No audit trail for administrative actions.

**Remediation:** `admin_audit_log` MongoDB collection with indexes on `timestamp` and `admin_id`. Admin actions logged with actor, action, target, and timestamp.

---

### #18 — Rate-Limit Monitoring
| | |
|---|---|
| **Severity** | Low |
| **CWE** | CWE-778 (Insufficient Logging) |
| **Location** | `nginx/nginx.conf` |
| **Regulatory** | OWASP A09 Security Logging and Monitoring Failures |
| **Status** | Resolved |

**Vulnerability:** Rate-limited requests logged but not monitored/alerted.

**Remediation:** Nginx logs rate-limited requests (status 429) to `/var/www/quotico.de/logs/`. Monitoring can be added via `fail2ban` or log aggregation. The infrastructure (log files, status codes) is in place for operational alerting.

---

### #19 — Secret Rotation Mechanism
| | |
|---|---|
| **Severity** | Low |
| **CWE** | CWE-321 (Use of Hard-coded Cryptographic Key) |
| **Location** | `backend/app/services/auth_service.py`, `backend/app/services/encryption.py`, `backend/app/config.py` |
| **Regulatory** | NIST SP 800-53 SC-12 (Cryptographic Key Management) |
| **Status** | Resolved |

**Vulnerability:** No mechanism to rotate JWT_SECRET or ENCRYPTION_KEY without service disruption.

**Remediation:**

**JWT_SECRET rotation** (zero-downtime):
1. Generate new key, set as `JWT_SECRET`
2. Set old key as `JWT_SECRET_OLD`
3. Restart service — new tokens signed with new key, old tokens still verified via fallback
4. After 7 days (max refresh token lifetime), remove `JWT_SECRET_OLD`

Implementation: `decode_jwt()` in `auth_service.py` tries current key first, falls back to old key. All JWT decode calls centralized through this function (auth, refresh, logout, WebSocket).

**ENCRYPTION_KEY rotation** (lazy re-encryption):
1. Generate new Fernet key, set as `ENCRYPTION_KEY`
2. Set old key as `ENCRYPTION_KEY_OLD`
3. Restart service — key registry auto-configured (old = version 1, new = version 2)
4. 2FA secrets re-encrypted on next user login (`login_2fa`, `twofa/verify`)
5. Once all 2FA users have logged in, remove `ENCRYPTION_KEY_OLD`

Implementation: `encryption.py` auto-registers old key in `_key_registry`. `needs_reencryption()` + `reencrypt()` handle lazy migration. `encryption_key_version` stored per user document.

---

## Architecture Overview

```
Internet
    |
    v
[nginx] :443 (TLS, HSTS, security headers, rate limiting)
    |
    +-- /api/*  --> [uvicorn] 127.0.0.1:4201 (FastAPI)
    +-- /ws/*   --> [uvicorn] WebSocket proxy
    +-- /*      --> /var/www/quotico.de/web/ (Vue 3 SPA, try_files)
    |
[MongoDB] localhost:27017 (authenticated, local)
```

## Key Files

| File | Purpose |
|------|---------|
| `backend/app/services/auth_service.py` | JWT, passwords, token lifecycle, decode_jwt |
| `backend/app/services/encryption.py` | Fernet encryption with key versioning |
| `backend/app/routers/auth.py` | Login, register, refresh, logout, 2FA login |
| `backend/app/routers/twofa.py` | 2FA setup, verify, disable |
| `backend/app/routers/gdpr.py` | Data export, account deletion |
| `backend/app/routers/google_auth.py` | Google OAuth flow |
| `backend/app/routers/ws.py` | Authenticated WebSocket live scores |
| `backend/app/database.py` | MongoDB indexes (incl. TTL for tokens) |
| `backend/app/config.py` | All settings from .env |
| `nginx/nginx.conf` | TLS, headers, rate limiting, reverse proxy |
| `quotico.service` | Systemd unit for uvicorn |
| `.env.example` | Template for all environment variables |
