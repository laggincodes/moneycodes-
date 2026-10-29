# WebsiteLead Hunter — Milestone 3 Authentication, Authorization & Account Isolation Report

**Project:** WebsiteLead Hunter  
**Location:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Milestone:** 3 — Authentication, Authorization & Account Isolation  
**Date:** 2026-10-26  
**Status:** COMPLETE — PASSED (100% Offline Adversarial, Auth, and Regression Tests Passed)  
**Security Baseline:** Branch `master`, Commit `55491c9`  

---

## 1. Executive Summary

Milestone 3 establishes a security-first, multi-tenant authentication and authorization architecture for the WebsiteLead Hunter platform across both backend and frontend layers:
1. **Server-Managed Session Authentication:** Secure, `HttpOnly` browser cookies storing high-entropy session tokens (`secrets.token_urlsafe(32)`) backed by a persistent SQLite `sessions` table. No long-lived auth tokens in browser `localStorage`.
2. **Password Security & Scrypt Hashing:** Memory-hard, GPU/ASIC-resistant `scrypt` hashing ($N=16384, r=8, p=1$, 16-byte cryptographically secure random salt) with constant-time verification (`hmac.compare_digest`). Includes dummy scrypt execution on nonexistent accounts to prevent timing-based user enumeration, and generic `"Invalid email or password."` responses.
3. **Brute-Force Rate Limiting:** Sliding-window rate limiter enforcing a maximum of 5 failed attempts per 5 minutes per IP and normalized email, returning HTTP 429 Too Many Requests.
4. **CSRF Protection:** Double-protection architecture enforcing `SameSite="lax"` cookies and mandatory `X-CSRF-Token` header verification on all state-changing browser requests (`POST`, `PATCH`, `DELETE`). CLI/API requests using `Authorization: Bearer <token>` are supported and exempt from browser CSRF requirements.
5. **Strict Account Isolation (Anti-IDOR):** Every lead, job, audit result, sales pitch, pipeline status, and CSV export is strictly scoped to `user_id`. Querying, updating, or deleting another account's records via IDOR returns HTTP 404 Not Found to prevent data leakage.
6. **Non-Destructive Database Migration & Legacy Data Adoption:** Database schema migrated non-destructively to v1 with version tracking. The pre-existing 1 job and 8 leads in `websitelead_hunter.db` are completely preserved and automatically adopted by the initial registered/bootstrapped user.
7. **Frontend Authentication Integration:** Installable React frontend updated with reactive `AuthModal` (Log In & Create Account tabs), authenticated session checks via `/api/auth/me`, automatic `X-CSRF-Token` header attachment, user status in the Navbar, and graceful logout.

The architecture was verified with **89 offline tests passing 100%** (19 dedicated auth/isolation tests, 50 adversarial security tests, 6 API integration tests, 14 unit/resilience tests), clean frontend production build in 828ms, and zero external network calls.

---

## 2. Modified & Created Files Inventory

| File Path | Nature | Purpose / Security Controls Implemented |
|---|---|---|
| `backend/app/migrations.py` | Created | Versioned, non-destructive migration engine with `schema_migrations` tracking. Migration v1 adds `users`, `sessions`, `jobs.user_id`, `leads.user_id`, and composite `UNIQUE(user_id, provider_id)`. |
| `backend/app/database.py` | Modified | Integrated `run_migrations(conn)` on startup. Safe transactions with WAL mode. |
| `backend/app/auth/security.py` | Created | Scrypt hashing, constant-time verification, timing pad for user enumeration defense, email normalization, password length validation (8-128 chars), sliding-window rate limiting. |
| `backend/app/auth/dependencies.py` | Created | Session cookie extraction (`session_id`), Bearer token support, session expiration enforcement, `get_current_user`, and `verify_csrf_token` dependency for state-changing requests. |
| `backend/app/auth/bootstrap.py` | Created | Secure local CLI bootstrap command for headless or command-line account initialization (`python -m app.auth.bootstrap`). |
| `backend/app/api/auth.py` | Created | Authentication router: `POST /register`, `POST /login`, `POST /logout`, `GET /me`. Sets HttpOnly session cookie and readable CSRF cookie. Adopts unowned legacy records on first user creation. |
| `backend/app/api/jobs.py` | Modified | Ownership enforcement: `start_job` sets `user_id`, `active` filters by user, `get_job`, `pause`, `resume`, `cancel` enforce `WHERE id = ? AND user_id = ?` (returns 404 on IDOR). CSRF enforced on mutations. |
| `backend/app/api/leads.py` | Modified | Strict account isolation: `list_leads` filtered by `user_id = ?`, pagination and counts isolated. `get_lead`, `update_pipeline`, `delete_lead`, `bulk_delete` enforce user ownership. CSRF enforced on mutations. |
| `backend/app/api/export.py` | Modified | CSV export query filtered by `WHERE user_id = ?`. Users cannot export another account's leads. |
| `backend/app/api/discovery.py` | Modified | Protected geocoding endpoint behind `Depends(get_current_user)`. |
| `backend/app/queue/job_worker.py` | Modified | Worker accepts `user_id` on job start, saves `user_id` to `jobs` and `leads`. Composite upsert `ON CONFLICT(user_id, provider_id)`. Recovery preserves `user_id`. |
| `backend/app/main.py` | Modified | Included `auth.router`, added `X-CSRF-Token` to CORS `allow_headers`, minimalized public `/api/health` check. |
| `backend/tests/test_auth_and_isolation.py` | Created | 19 dedicated tests for registration, scrypt hashing, login, rate limiting, generic errors, logout, expiration, CSRF, IDOR prevention, bulk delete isolation, export isolation, and legacy data adoption. |
| `backend/tests/test_api.py` | Modified | Updated test fixture to seed and authenticate a test user session, verifying all endpoints pass with authentication. |
| `backend/tests/test_security_adversarial.py` | Modified | Updated database fixture to authenticate test client for SQL sorting checks. |
| `frontend/src/types/index.ts` | Modified | Added `User` and `AuthResponse` TypeScript interfaces. |
| `frontend/src/services/api.ts` | Modified | Added `credentials: 'same-origin'` to all `fetch` calls, automatic `X-CSRF-Token` header injection, and auth methods (`register`, `login`, `logout`, `getMe`). |
| `frontend/src/components/AuthModal.tsx` | Created | Clean authentication modal with Log In and Create Account modes, validation, error messaging, and loading spinners. |
| `frontend/src/components/Navbar.tsx` | Modified | Displays authenticated user email and Log Out button with confirmation. |
| `frontend/src/App.tsx` | Modified | Integrated `currentUser` state, startup `/api/auth/me` session check, reactive `AuthModal` display, and user-isolated lead/job actions. |
| `docs/audit/MILESTONE_3_AUTH_AND_ISOLATION.md` | Created | Comprehensive Milestone 3 security and verification report. |

---

## 3. Authentication & Password Security Architecture

### 3.1 Password Hashing & Verification
- **Algorithm:** Python standard library `hashlib.scrypt` (memory-hard, resistant to GPU and custom ASIC brute-force attacks).
- **Parameters:**
  - $N = 16384$ (CPU/memory cost parameter)
  - $r = 8$ (block size parameter)
  - $p = 1$ (parallelization parameter)
  - Salt: 16 bytes of cryptographically secure random bytes via `secrets.token_bytes(16)`.
  - Output Key Length: 32 bytes (256 bits).
- **Format:** `scrypt$16384$8$1$<salt_hex>$<derived_hex>`.
- **Constant-Time Verification:** Candidate password is hashed with the stored salt/parameters and compared via `hmac.compare_digest()` to eliminate timing side-channel attacks.

### 3.2 User Enumeration Defense
- When a login attempt specifies an unregistered email:
  1. The system calls `dummy_password_hash_check()` to execute a real `scrypt` computation identical in computational cost to a valid password check.
  2. The endpoint returns `HTTP 401 Unauthorized` with the generic message `"Invalid email or password."`.
- When a login attempt specifies a registered email with an incorrect password:
  1. Password verification fails.
  2. The endpoint returns `HTTP 401 Unauthorized` with the identical message `"Invalid email or password."`.
- An attacker cannot determine account existence by measuring server response times or inspecting error text.

### 3.3 Brute-Force Rate Limiting
- `app.auth.security` tracks failed login and registration attempts in a sliding-window store.
- Threshold: 5 failed attempts within 300 seconds (5 minutes).
- Tracking Keys: Client IP address AND normalized email address.
- Response on Breach: `HTTP 429 Too Many Requests` (`"Too many failed login attempts. Please try again in 5 minutes."`).
- Counter Reset: Successful authentication clears failed attempt counts for both IP and email.

### 3.4 Server-Managed Sessions & Cookie Security
- **Session Tokens:** 256 bits of cryptographically secure randomness generated via `secrets.token_urlsafe(32)`.
- **Database Storage:** Active sessions stored in `sessions` table with user foreign key, CSRF token, user agent, IP address, creation timestamp, and expiration timestamp.
- **Lifetime:** 7 days (`SESSION_MAX_AGE_SECONDS = 604,800`). Expired sessions are deleted upon lookup and return HTTP 401.
- **Cookie Attributes:**
  - `session_id`: `HttpOnly=True`, `SameSite="lax"`, `Path="/"`, `Max-Age=604800`.
  - `csrf_token`: `HttpOnly=False` (readable by frontend script to pass in headers), `SameSite="lax"`, `Path="/"`.
  - `Secure`: Defaults to `False` in local development (`COOKIE_SECURE=false`), with documentation that `COOKIE_SECURE=true` is enabled in HTTPS production environments.
- **Session Invalidation:** `POST /api/auth/logout` deletes the session from the SQLite `sessions` table and instructs the browser to immediately delete both cookies (`max_age=0`).

### 3.5 CSRF Protection Mechanism
- State-changing HTTP methods (`POST`, `PATCH`, `DELETE`, `PUT`) handled by browser cookie sessions require the `X-CSRF-Token` header.
- The `verify_csrf_token` FastAPI dependency compares the request header against the session's stored `csrf_token` using `hmac.compare_digest()`.
- Missing or mismatched CSRF tokens return `HTTP 403 Forbidden`.
- Safe read methods (`GET`, `HEAD`, `OPTIONS`) are exempt.
- CLI and programmatic requests using `Authorization: Bearer <token>` are exempt because browsers cannot send custom `Authorization` headers cross-origin without CORS preflight permission.

---

## 4. Multi-Tenant Account Isolation (Anti-IDOR)

### 4.1 Persisted Schema & Compound Constraints
```sql
-- Users
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_users_email ON users (email);

-- Sessions
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    user_agent TEXT,
    ip_address TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX idx_sessions_user_id ON sessions (user_id);
CREATE INDEX idx_sessions_expires_at ON sessions (expires_at);

-- Jobs
ALTER TABLE jobs ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
CREATE INDEX idx_jobs_user_id ON jobs (user_id);

-- Leads
CREATE TABLE leads (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    job_id TEXT,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    ...
    UNIQUE (user_id, provider_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
);
CREATE INDEX idx_leads_user_id ON leads (user_id);
```

### 4.2 Endpoint Ownership Enforcement Matrix

| Endpoint | Method | Ownership Check | IDOR Behavior |
|---|---|---|---|
| `/api/leads` | `GET` | Injected `WHERE user_id = ?` into query and total count | Returns only authenticated user's leads |
| `/api/leads/{id}` | `GET` | `SELECT ... WHERE id = ? AND user_id = ?` | Returns HTTP 404 (no existence leakage) |
| `/api/leads/{id}/pipeline` | `PATCH` | `UPDATE ... WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/leads/{id}` | `DELETE` | `DELETE FROM leads WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/leads?all_leads=true` | `DELETE` | `DELETE FROM leads WHERE user_id = ?` | Deletes only caller's leads |
| `/api/jobs` | `POST` | Sets `user_id = current_user["id"]` | Job created under caller's account |
| `/api/jobs/active` | `GET` | `SELECT ... WHERE user_id = ? ...` | Returns caller's active job only |
| `/api/jobs/{id}` | `GET` | `SELECT ... WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/jobs/{id}/pause` | `POST` | Verified `WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/jobs/{id}/resume` | `POST` | Verified `WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/jobs/{id}/cancel` | `POST` | Verified `WHERE id = ? AND user_id = ?` | Returns HTTP 404 if not owned |
| `/api/export/csv` | `GET` | `SELECT ... WHERE user_id = ?` | CSV contains strictly caller's leads |
| `/api/discovery/geocode` | `GET` | Protected by `Depends(get_current_user)` | Returns HTTP 401 if unauthenticated |

### 4.3 Background Worker Scoping
- `JobWorker.start_job` accepts and persists `user_id`.
- `_process_single_business` inserts leads with `user_id`.
- Upsert constraint `ON CONFLICT(user_id, provider_id)` guarantees that two different users discovering the same business (e.g. OpenStreetMap node 123) maintain completely isolated lead records, notes, pipeline stages, and generated pitches.
- Worker server crash recovery preserves `user_id` on all interrupted jobs.

---

## 5. Database Migration & Data Preservation Verification

### 5.1 Migration Execution & Idempotence
- Migration versioning is tracked in `schema_migrations` (`version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT`).
- Running `init_db()` executes `run_migrations()` within the active transaction. If migration 1 has already been applied, it skips execution immediately.
- SQLite foreign key constraints remain enforced (`PRAGMA foreign_keys = ON`).

### 5.2 Preservation of Pre-Existing Data
- Initial state in `backend/websitelead_hunter.db`: 1 completed job, 8 discovered leads.
- Post-migration verification:
  - Jobs count: **1** (intact)
  - Leads count: **8** (intact)
  - Data columns: All coordinates, phone numbers, audit results, scores, and pitches preserved.
- Safe backup created at `backend/websitelead_hunter.db.backup_m2`.

### 5.3 Legacy Data Adoption
- When the first user registers or executes `python -m app.auth.bootstrap`, the system detects `user_count == 0` and executes:
  ```sql
  UPDATE jobs SET user_id = ? WHERE user_id IS NULL;
  UPDATE leads SET user_id = ? WHERE user_id IS NULL;
  ```
- All historical leads from Milestone 1 & 2 reconnaissance immediately belong to the primary developer account upon initial login.

---

## 6. Test Suite & Verification Inventory

### 6.1 Authentication & Isolation Suite (`test_auth_and_isolation.py`)
- **Execution:** `pytest -v tests/test_auth_and_isolation.py`
- **Result:** **19 passed in 3.05s** (0 failures)
- **Key Test Coverage:**
  - `test_user_registration_success`: Normalization, scrypt hashing, cookie issuance.
  - `test_user_registration_duplicate_email`: Rejection of duplicate email with HTTP 400.
  - `test_password_policy_enforcement`: Rejection of passwords < 8 or > 128 chars.
  - `test_invalid_email_format`: Rejection of malformed email formats.
  - `test_login_success_and_cookies`: Successful login, `session_id` and `csrf_token` cookies.
  - `test_login_invalid_credentials_generic_error`: Generic error on nonexistent email and incorrect password.
  - `test_login_rate_limiting`: Rate limiter triggers HTTP 429 on 6th failed attempt.
  - `test_logout_invalidates_session`: Session row deleted from DB, cookies cleared, subsequent `/me` returns 401.
  - `test_session_expiration`: Expired session rejected with 401 and pruned.
  - `test_unauthenticated_requests_blocked`: Protected routes return 401 without auth.
  - `test_csrf_token_required_on_browser_mutations`: Missing or bad CSRF token returns 403; valid token succeeds.
  - `test_cli_bearer_token_access`: CLI Bearer token requests succeed without CSRF header.
  - `test_account_isolation_leads_listing`: User A sees only Lead A; User B sees only Lead B.
  - `test_account_isolation_lead_detail_idor`: User A querying Lead B gets HTTP 404.
  - `test_account_isolation_lead_update_and_delete_idor`: User A modifying Lead B gets HTTP 404.
  - `test_account_isolation_bulk_delete`: User A bulk deleting leads deletes only User A's leads.
  - `test_account_isolation_export_csv`: User A CSV export contains zero records from User B.
  - `test_account_isolation_jobs`: User A cannot read or see User B's active jobs.
  - `test_legacy_data_adoption_by_first_user`: First registered user claims legacy jobs and leads.

### 6.2 Full Regression & Adversarial Verification
- **Total Test Count:** **89 tests executed**
  - `test_auth_and_isolation.py`: 19 passed
  - `test_security_adversarial.py`: 50 passed
  - `test_api.py`: 6 passed
  - `test_milestone_f_resilience.py`: 6 passed
  - `test_scorer_and_pitch.py`: 4 passed
  - `test_auditor.py`: 4 passed (1 live test deselected)
- **Overall Result:** **89 passed, 1 deselected in 5.57s** (100% pass rate).

### 6.3 Frontend Production Build
- **Command:** `cd frontend && npm run build`
- **Result:** TypeScript 0 errors, Vite bundle generated in **828ms**.

### 6.4 Safety Verification Attestation
- **Live Overpass / Nominatim Requests:** 0
- **Live Gemini LLM Requests:** 0
- **Live Business Contacts / Emails Sent:** 0
- **Cloud Infrastructure Provisioned / Costs:** $0.00
- **Server Binding:** Confirmed strictly `127.0.0.1`

---

## 7. Git State

- **Branch:** `master`
- **Head Commit:** `55491c9`
- All Milestone 2 and Milestone 3 changes remain uncommitted in working directory for user review.

---

## 8. Milestone 3 Acceptance Gate Checklist

- [x] Versioned non-destructive migration engine implemented (`backend/app/migrations.py`)
- [x] Memory-hard `scrypt` password hashing with salt and constant-time comparison
- [x] Timing-safe user enumeration defense via dummy hash execution
- [x] Sliding-window brute-force rate limiter (5 failed attempts per 5 minutes)
- [x] Server-side HttpOnly cookie session management (7 days expiry, session invalidation on logout)
- [x] CSRF protection enforcing `X-CSRF-Token` header on all browser mutations
- [x] CLI / API `Authorization: Bearer <token>` compatibility
- [x] Strict account isolation across leads, jobs, pipelines, and CSV exports (IDOR returns 404)
- [x] Background worker tasks scoped to explicit owner identity
- [x] Legacy data adoption for initial user account
- [x] SQLite database records (1 job, 8 leads) preserved intact
- [x] React frontend updated with `AuthModal`, Navbar user info, and session management
- [x] 89 offline backend tests passed 100%
- [x] Frontend builds cleanly with zero errors (828ms)
- [x] Zero external network calls made, zero emails sent, zero cost incurred

**Next Step:** Milestone 4 — Installable Mobile-First PWA, Offline Caching & Background Sync. Awaiting user review and explicit approval.
