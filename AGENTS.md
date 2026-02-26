# AGENTS.md

**Quotico Constitution (Code Reality)**

This document is derived strictly from the active code and the strategic direction of the project. If the code disagrees with this document, the code is likely legacy and **this document wins** — the code must be updated to match the Greenfield standard.

---

## 0. Scope & Enforcement Model

**Source of Truth:** `backend/` and `frontend/`.

### 0.1 Greenfield Rule

Quotico is a strict greenfield build. Even if legacy code exists, **we do not write legacy code**.

- **No migration paths.** Do not design backward-compatible shims for `team_key` or string-based mappings.
- **No dual-writes.** When refactoring to `team_id`, we stop writing string keys immediately.
- **No grandfathering.** Every service refactored must comply with the "Team Tower" architecture.
- **No ORM.** We use Motor/PyMongo directly.

### 0.2 Agent Duty to Intervene

Start any response with `🛑 VETO: [Reasoning]` if a prompt would:
1. Reintroduce `team_key`, `home_team_key`, or named-based matching logic.
2. Hardcode timezones or use naive `datetime.now()`.
3. Suggest exposing PII (User Emails) in public API responses.
4. Bypass the i18n system for user-facing strings.
5. **Commit code without proper file headers or logic documentation.**

---

## 1. Identity & "The Tower" (Kernel)

The core integrity of Quotico relies on **The Tower** (Team & League Registries). This is the only source of truth for entity resolution.

### 1.1 Team Identity Law
*Source: `backend/app/services/team_registry_service.py`*

* **Single Truth:** The only valid reference to a team is its `ObjectId` (`team_id`).
* **Strings are for Display Only:** `home_team` / `away_team` string fields in `matches` are for UI convenience only. Business logic **MUST** use `home_team_id` / `away_team_id`.
* **Resolution:**
    * **Input:** Raw string + Sport Key.
    * **Process:** `TeamRegistry.resolve(name, sport_key)`.
    * **Fallback:** `get_or_create` with `needs_review=True`.
    * **Strict Review:** If a normalized name matches multiple teams globally (e.g., "Real") and no Sport Key is provided, the Registry **MUST NOT** guess. It must create a new reviewable entry.

### 1.2 Normalization Engine
All team name comparisons must pass through `normalize_team_name(raw: str)`:
1.  `lower().strip()`
2.  Char mapping (`ß->ss`, `ø->o`, etc.)
3.  Unicode NFKD + Accent removal.
4.  Noise removal: `fc, sv, 1., club, vfl`... (AND isolated `1` token).
5.  Sort tokens alphabetically.

### 1.3 League Identity Law
*Source: `backend/app/services/league_service.py`*

* **Validation:** Every ingest path (Odds, xG, Results) **MUST** call `LeagueRegistry.ensure_for_import(sport_key)` first.
* **Unknown Leagues:** Must be created with `is_active=False` and `needs_review=True`. Ingest must abort for inactive leagues.

---

## 2. Backend Architecture (FastAPI + MongoDB)

### 2.1 File Structure
* **Routers:** `backend/app/routers/` (HTTP Layer). Minimal logic. Permission checks here.
* **Services:** `backend/app/services/` (Business Logic). No HTTP return objects here.
* **Providers:** `backend/app/providers/` (External API Integration).
* **Workers:** `backend/app/workers/` (APScheduler Jobs).

### 2.2 Database & Datetime
* **Driver:** Motor (Async).
* **Timezone Discipline:**
    * MongoDB stores **naive UTC**.
    * **NEVER** use `datetime.now()`.
    * **ALWAYS** use `backend/app/utils.py`:
        * `utcnow()` for current time.
        * `parse_utc(str)` for inputs.
        * `ensure_utc(dt)` for arithmetic.

### 2.3 Provider Isolation
* No raw `httpx`/`requests` calls inside Services or Routers.
* All external data fetching must go through a dedicated class in `backend/app/providers/`.

### 2.4 API Conventions
* **Auth:** JWT via `httpOnly` cookies.
* **Response:** Standard JSON envelopes.
* **Privacy:** `email` fields must never be serialized in public-facing schemas (e.g., Leaderboards, Squads). Only `/auth/me` or `/admin/*` may return emails.
* **API-Documentation** all calls to Sportmonks have to be verified against and follow the official documentation on https://postman.sportmonks.com/

---

## 3. Frontend Architecture (Vue 3 + TS)

### 3.1 Tech Stack
* **Framework:** Vue 3 (Composition API).
* **State:** Pinia.
* **Styling:** Tailwind CSS.
* **Language:** TypeScript (Strict).

### 3.2 Internationalization (i18n)
* **Mandatory:** No hardcoded user-facing strings in `.vue` or `.ts` files.
* **Usage:**
    * Templates: `{{ $t('auth.login') }}`
    * Script: `const { t } = useI18n(); ... t('common.save')`
* **Locales:** `frontend/src/locales/de.ts` (Default), `en.ts`.

### 3.3 Components & Structure
* **No Options API:** Use `<script setup lang="ts">`.
* **Icons:** Use `SportNav.vue` logic for league flags.
* **Types:** Explicit interfaces for all Props and API responses. No `any`.

---

## 4. Documentation & Maintenance

### 4.1 Freshness
* **CLAUDE.md:** Must be updated if architecture, commands, or stack changes.
* **AGENTS.md:** Update if the "Constitution" changes.

### 4.2 Definition of Done
1.  Code implements the "Greenfield" standard (no legacy keys).
2.  Datetimes are UTC-safe.
3.  Strings are extracted to i18n.
4.  No PII leaks in API responses.

---

## 5. Code Documentation & Quality

**Undocumented code is legacy code.** Every file and complex function must explain itself.

### 5.1 File Headers (Mandatory)
Every source file (`.py`, `.ts`, `.vue`) MUST start with a header block describing its context.

**Python Example:**
```python
"""
backend/app/services/match_service.py

Purpose:
    Core business logic for match management, score updates, and status transitions.
    Handles the interaction between the Matchday Worker and the DB.

Dependencies:
    - app.services.team_registry_service (Team Resolution)
    - app.services.league_service (League Validation)
    - app.database (Motor Async Client)
"""
```

**TypeScript Example:**
```typescript
/**
 * frontend/src/composables/useTeamResolver.ts
 *
 * Purpose:
 * Frontend-side caching and resolution of Team IDs to display names/logos.
 * Prevents redundant API calls during list rendering.
 */
```