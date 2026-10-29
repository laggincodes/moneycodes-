# WebsiteLead Hunter — Milestone 9D.1: Architecture Findings Remediation Plan

**Date:** October 26, 2026  
**Auditor:** Principal Systems, Security & Cloud Architect  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Target Milestone:** Milestone 9D.1 (Remediation Design & Specification Gate)  
**Safety Mandate:** Specification and offline inspection only. Zero deployment, zero network requests, zero package installs, zero database mutations, real database preserved intact (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 1. Executive Summary & Review Verdict

### 1.1 Review Verdict: **READY FOR IMPLEMENTATION REVIEW**

This report establishes the complete, concrete engineering specification to remediate all architectural, security, and operational findings identified in Milestone 9D. No production code, database schema, or network configuration is altered in this milestone.

Every design proposal in this plan is backed by inspected framework capabilities, explicit failure-mode defenses, and measurable acceptance tests.

### 1.2 Summary of Remediations Designed

1. **Legacy-Data Adoption Security (Finding 1):** Replaces insecure loopback IP inspection (`request.client.host in ("127.0.0.1", ...)`) with a secure **CLI-first administrative workflow** requiring user authentication, explicit interactive confirmation, password re-verification, and a local one-time bootstrap secret. Web endpoints cannot trigger adoption.
2. **Reverse Proxy Trust Boundary (Finding 2):** Defines the exact loopback binding constraint (`127.0.0.1:8000`), proxy header trust policies via Uvicorn's verified `ProxyHeadersMiddleware(trusted_hosts='127.0.0.1')`, Starlette's verified `TrustedHostMiddleware`, Caddy header sanitization, HTTPS-only cookie attributes (`COOKIE_SECURE=true`), and single-origin consolidation (`my-pc.ts.net`) to eliminate CORS preflight friction.
3. **Private-Network Feasibility & Cost Categorization (Finding 3):** Disentangles verified local technical facts from unresearched external cloud/plan terms, detailing exact licensing, power, and commercial-use uncertainties for Tailscale Free, Headscale, and Cloud VPS options.
4. **Backup & Restore Engineering (Finding 4):** Specifies a resilient, automated backup workflow using Python's native `sqlite3.backup()` API with page batching, PRAGMA integrity and foreign-key checks, AES-256 local encryption, offsite retention, and a measurable <60s restore drill.
5. **Windows Worker Supervision (Finding 5):** Establishes a native Windows Task Scheduler specification for `worker_daemon.py`, complete with startup delay, automatic restart after failure, rotating file logging, Away Mode sleep inhibition, and clean crash recovery.

---

## 2. Finding 1 — Legacy-Data Adoption Security Remediation

### 2.1 Problem Analysis & Threat Model

In [`backend/app/api/auth.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py#L130-L153) and [lines 265–299](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py#L265-L299), legacy data adoption is gated by:
```python
ip = request.client.host if request.client else "127.0.0.1"
is_loopback = ip in ("127.0.0.1", "::1", "localhost", "testclient")
if not is_loopback:
    raise HTTPException(status_code=403, detail="...")
```

#### Vulnerability Analysis Behind a Reverse Proxy:
When a local reverse proxy (Caddy / Nginx) terminates HTTPS on the private mesh and proxies requests to Uvicorn over loopback (`127.0.0.1:8000`), the TCP connection received by Uvicorn originates from `127.0.0.1`.
- If Uvicorn does not unpack proxy headers, `request.client.host` is ALWAYS `127.0.0.1`.
- Any remote caller reaching the reverse proxy (e.g. from an untrusted mobile device on a shared network or via forged headers) would be treated by the application as a local loopback administrator.
- Furthermore, IP addresses are an unauthenticated transport artifact that should never serve as the sole authorization gate for permanently altering database tenant ownership.

### 2.2 Proposed Secure Architecture: CLI-First Administrative Command

The adoption of unowned legacy data (`user_id IS NULL`) must be removed from general web-accessible HTTP registration and isolated to an authenticated, interactive local administrative workflow.

```
+-----------------------------------------------------------------------------------------+
|                  LOCAL WINDOWS HOST CONSOLE (Physical / SSH Access)                     |
|                                                                                         |
|  $ python cli.py admin adopt-legacy                                                     |
|                                                                                         |
|  1. Authenticates against stored/prompted user credentials (user@example.com).          |
|  2. Re-verifies account password via scrypt comparison.                                 |
|  3. Queries unowned records: "Found 8 unowned legacy leads and 1 unowned job."           |
|  4. Prompts interactive confirmation: "Assign all unowned records to this account? [y/N]"|
|  5. Generates local ephemeral bootstrap token in backend/.admin_token.                  |
|  6. Submits atomic transaction request with X-Admin-Bootstrap-Token header.             |
|  7. Server verifies token on disk, executes atomic UPDATE, and wipes token immediately. |
+-----------------------------------------------------------------------------------------+
```

### 2.3 Detailed Endpoint Hardening Specification

In [`backend/app/api/auth.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py):

#### 1. Registration (`POST /api/auth/register`)
- **Deprecate `adopt_legacy_data` flag:** Remove `adopt_legacy_data` from `RegisterRequest`. Registration will **never** assign legacy data, preventing silent or accidental ownership adoption during account creation.

#### 2. Adoption Endpoint (`POST /api/auth/adopt-legacy-leads`)
Require four independent security checks:
1. **User Authentication:** Valid session (`get_current_user`).
2. **CSRF Validation:** Valid `X-CSRF-Token` header.
3. **Step-Up Password Verification:** Request body must supply `{"password": "...", "confirm": true}`. The endpoint verifies `verify_password(payload.password, user["password_hash"])`.
4. **Local Host Console Bootstrap Secret (Dual-Factor Boundary):**
   - The CLI generates a high-entropy cryptographically random token (32 bytes hex) and writes it to a file accessible only to the local Windows OS user (`backend/.admin_token`).
   - The CLI passes this token in the header `X-Admin-Bootstrap-Token`.
   - The server reads the file on local disk, verifies it matches the header using `hmac.compare_digest`, executes the adoption update in an atomic transaction, and immediately deletes `.admin_token`.
   - Any remote web caller unable to write to the Windows host's filesystem is fundamentally barred from adopting data.

### 2.4 Acceptance Tests (Future Implementation Gate)

| Test Identifier | Scenario | Injected Condition | Expected Response / DB State |
| :--- | :--- | :--- | :--- |
| `test_adopt_unauthenticated_rejected` | Unauthenticated request | No session cookie or bearer token | HTTP 401 Unauthorized |
| `test_adopt_invalid_password_rejected` | Authenticated request | Wrong account password in payload | HTTP 403 Forbidden ("Invalid password confirmation") |
| `test_adopt_missing_bootstrap_token` | Authenticated + valid password | Missing or invalid `X-Admin-Bootstrap-Token` | HTTP 403 Forbidden ("Local bootstrap token required") |
| `test_adopt_valid_full_workflow` | Valid session, password, and token | Correct credentials & ephemeral disk token | HTTP 200 OK; `adopted_leads=8, adopted_jobs=1`; token deleted |
| `test_adopt_idempotent_zero_remaining` | Repeated execution after success | Records already have `user_id` assigned | HTTP 200 OK; `adopted_leads=0, adopted_jobs=0` |
| `test_register_cannot_adopt_legacy` | New account registration | Payload contains `adopt_legacy_data=True` | Account created; legacy leads remain `user_id IS NULL` |

### 2.5 Rollback Strategy
If legacy records are adopted by an unintended user account, ownership can be cleanly restored without data loss:
```sql
UPDATE leads SET user_id = NULL WHERE user_id = :target_user_id AND created_at < :cutoff_timestamp;
UPDATE jobs SET user_id = NULL WHERE user_id = :target_user_id AND created_at < :cutoff_cutoff;
```

---

## 3. Finding 2 — Reverse Proxy Trust Boundary Specification

### 3.1 Framework & Middleware Capabilities Verified

Inspection of the active workspace environment verified:
- **FastAPI Version:** `0.162.0`
- **Starlette Version:** `1.10.0`
- **Uvicorn Version:** `0.54.0`
- **`uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware`:** Verified present in Uvicorn installation with signature `(app, trusted_hosts: 'list[str] | str' = '127.0.0.1')`.
- **`starlette.middleware.trustedhost.TrustedHostMiddleware`:** Verified present in Starlette installation with signature `(app, allowed_hosts: Sequence[str] | None = None)`.

### 3.2 Exact Trusted Proxy Addresses & Network Boundary

```
      [ Private Mesh Network (Tailscale) ]
                      |
                      v  (Port 443 / HTTPS)
        +----------------------------+
        |    Caddy Reverse Proxy     |  <-- Listens on Tailscale IP / 127.0.0.1
        +----------------------------+
                      |
                      v  (HTTP over Loopback ONLY)
        +----------------------------+
        |  127.0.0.1:8000 / [::1]   |  <-- Uvicorn / FastAPI
        +----------------------------+
```

1. **Exact Trusted Addresses:** `127.0.0.1` and `::1`.
2. **Why They Are Trusted:** Caddy runs as a local process on the Windows host and proxies traffic to Uvicorn strictly over loopback. Packets originating from `127.0.0.1` cannot be injected from external network interfaces under the Windows TCP/IP stack.
3. **Blocking Direct Backend Access:**
   - Uvicorn MUST be started with `--host 127.0.0.1 --port 8000`.
   - Never bind to `0.0.0.0`.
   - Windows Advanced Firewall rule blocks external inbound TCP connections on port 8000.

### 3.3 Forwarded Header Sanitization & Overwriting

External clients on the mesh could attempt to spoof client IPs by sending fake headers (`X-Forwarded-For: 127.0.0.1`).
- **Caddy Reverse Proxy Policy:** Caddy must overwrite, not append, untrusted client headers:
  ```caddyfile
  header_up X-Forwarded-For {remote_host}
  header_up X-Forwarded-Proto {scheme}
  header_up X-Forwarded-Host {host}
  header_up -X-Forwarded-Server
  ```
- **Uvicorn ProxyHeaders Configuration:**
  Uvicorn runs with:
  `--proxy-headers --forwarded-allow-ips="127.0.0.1,::1"`
  If an incoming connection arrives from any IP other than loopback, `ProxyHeadersMiddleware` ignores all `X-Forwarded-*` headers, preserving the raw socket IP.

### 3.4 Host and Origin Validation

1. **Host Header Validation:**
   Add `TrustedHostMiddleware` to `backend/app/main.py`:
   ```python
   raw_hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,::1")
   ALLOWED_HOSTS = [h.strip() for h in raw_hosts.split(",") if h.strip()]
   app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
   ```
   Any request with an unrecognized `Host` header (e.g. DNS rebinding or direct IP probing) receives HTTP 400.
2. **Single-Origin Consolidation (CORS Elimination):**
   Caddy serves compiled frontend assets from `frontend/dist` and proxies `/api/*` to `127.0.0.1:8000` under the same hostname (`https://<pc>.<tailnet>.ts.net`).
   Because all API requests are same-origin (`/api`), browsers never trigger CORS preflight checks.

### 3.5 Cookie & Cache Security Settings

1. **HTTPS Cookie Policy:**
   - In production `.env`, set `COOKIE_SECURE=true`.
   - Session cookie: `session_id; HttpOnly; Secure; SameSite=Lax; Path=/`.
   - CSRF cookie: `csrf_token; Secure; SameSite=Lax; Path=/`.
2. **Service Worker Private Cache Exclusion:**
   - Verified in [`frontend/public/sw.js`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/public/sw.js#L65-L82):
     All `/api/*` requests bypass Cache Storage and go directly to network.
     Private lead data, audit scores, and session tokens are never cached on device storage.

---

## 4. Finding 3 — Private-Network Choice, Facts, and Cost Breakdown

In compliance with the safety mandate, facts are strictly partitioned into **verified local facts**, **unresearched external facts**, and **assumptions & cost estimates**:

```
+----------------------------------------------------------------------------------------------------+
| 1. VERIFIED LOCAL FACTS (Grounded in Codebase & Host Environment)                                  |
+----------------------------------------------------------------------------------------------------+
| - Python 3.13.15, SQLite 3.50.4 with WAL mode, and Node.js Vite build are functional on host.      |
| - Standalone durable worker passes all 217 backend tests and 29 worker/matrix crash tests.         |
| - Win32 kernel32.SetThreadExecutionState Away Mode verified live on Windows 11 host.              |
| - Native Windows Credential Manager (advapi32.dll) verified for CLI credential persistence.        |
+----------------------------------------------------------------------------------------------------+
| 2. UNRESEARCHED EXTERNAL FACTS (Must be Verified Online in Later Stage)                            |
+----------------------------------------------------------------------------------------------------+
| - Tailscale Personal Free Tier Terms of Service: Commercial use eligibility for solo freelancer.  |
| - Tailscale MagicDNS Let's Encrypt automated certificate issuance rate limits for single nodes.    |
| - Headscale AGPLv3 control server stability, maintenance burden, and mobile client compatibility.  |
| - Oracle Cloud Always Free instance capacity and idle-shutdown reclamation policies in India/US.   |
+----------------------------------------------------------------------------------------------------+
| 3. ASSUMPTIONS & COST ESTIMATES                                                                    |
+----------------------------------------------------------------------------------------------------+
| - Existing PC + Tailscale: Zero software/hosting fees. Electric power cost: ~₹150–₹300/month.      |
| - Existing PC + Headscale: Zero software fees. Requires VPS for control plane: ~₹300–₹450/month.   |
| - Dedicated Cloud VPS: Commercial Linux VPS (2 vCPU, 2GB RAM): ~₹400–₹800/month.                   |
+----------------------------------------------------------------------------------------------------+
```

### Plan Terms & Verification Checklist for Later Stage:
- [ ] Check Tailscale Terms of Service (Section 4.1 "Personal vs Commercial Use") when network access is authorized.
- [ ] Verify home router UPnP or NAT behavior for direct peer-to-peer WireGuard connections without DERP relay latency.
- [ ] Check whether dynamic residential ISP IP changes cause connection resets on active long-polling sessions.

---

## 5. Finding 4 — Backup & Restore Engineering Specification

### 5.1 Architecture & Disposable Test Methodology

Backups must be generated without taking the database offline and without blocking concurrent readers or writers.
All tests must execute against temporary SQLite instances (`tmp_path / "backup_test.db"`). **The real database `websitelead_hunter.db` will not be touched.**

### 5.2 Step-by-Step Online Backup Specification

```python
def create_online_snapshot(source_db_path: str, snapshot_target_path: str, pages_per_batch: int = 100):
    """
    Executes a non-blocking online backup using Python's native sqlite3.backup() API.
    Releases database write locks between page batches to avoid starving active transactions.
    """
    with sqlite3.connect(source_db_path, timeout=15.0) as src, sqlite3.connect(snapshot_target_path) as dst:
        src.backup(dst, pages=pages_per_batch, sleep=0.005)
```

### 5.3 Integrity Verification Gate

Immediately following snapshot creation, the snapshot must pass automated validation before being accepted:
1. `PRAGMA integrity_check;` -> Must return `ok`.
2. `PRAGMA foreign_key_check;` -> Must return 0 violations.
3. Row count validation: Compare row counts against live source.
4. Cryptographic Hash: Calculate SHA-256 checksum and write `<snapshot_name>.sha256`.

### 5.4 Encryption & Key Management

- **Algorithm:** AES-256-GCM (via Python `cryptography` package or `age` CLI).
- **Key Storage:**
  - Key generated as a 256-bit cryptographically secure key.
  - Stored in Windows Credential Manager under target `WebsiteLeadHunter:backup_key`.
  - Offline Emergency Recovery Kit: Key exported to a secure passphrase-protected paper/USB file.
  - Key is NEVER stored in Git, plaintext files, or application code.

### 5.5 Retention & Pruning Policy

- **Daily Snapshots:** 7 days retention (pruned on day 8).
- **Weekly Snapshots:** 4 weeks retention.
- **Monthly Snapshots:** 3 months retention.
- **Pruning Script:** Evaluates ISO 8601 timestamps in backup filenames and deletes expired files.

### 5.6 Measurable Restore Drill Acceptance Criteria

1. **Integrity Validation:** Restored SQLite file passes `PRAGMA integrity_check` (`ok`) and `PRAGMA foreign_key_check` (0 rows).
2. **Data Parity:** Restored leads count and jobs count match pre-backup metadata exactly.
3. **Application Boot:** Temporary FastAPI instance connects to restored database and serves `/api/leads` with HTTP 200.
4. **Time Constraint:** Total restoration and verification completes in under 60 seconds.

---

## 6. Finding 5 — Windows Worker Supervision Specification

### 6.1 Native Windows Task Scheduler Configuration

The standalone durable worker ([`backend/app/queue/worker_daemon.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/queue/worker_daemon.py)) will be supervised via native Windows Task Scheduler (`schtasks` / Task Scheduler GUI).

```
Task Name:       WebsiteLeadHunter-DurableWorker
Trigger:         At system startup (with 30-second delay for networking)
User Account:    Owner Windows Account (Run whether user is logged on or not)
Action:          Start a program
Program/script:  C:\Users\Yatha\AppData\Local\Programs\Python\Python313\python.exe
Arguments:       -m app.queue.worker_daemon
Start in:        C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend
Settings:        
  - If task fails, restart every 1 minute (retry up to 3 times)
  - Stop task if running longer than: Disabled (continuous execution)
  - If running task does not end when requested, force it to stop
```

### 6.2 Environment & Secrets

- Environment configuration loaded via `backend/.env` (permissions restricted to owner user).
- Contains:
  `DATABASE_PATH=websitelead_hunter.db`
  `ENVIRONMENT=production`
  `COOKIE_SECURE=true`
  `LOG_LEVEL=INFO`
- API keys (OpenAI, Gemini) loaded from `.env` or Windows Credential Manager.

### 6.3 Signal Handling & Graceful Shutdown

- `worker_daemon.py` traps `signal.SIGINT` and `signal.SIGTERM` (and on Windows, console close events via `SetConsoleCtrlHandler` if needed).
- When shutdown signal is received:
  1. Sets internal flag `_stop_requested = True`.
  2. If a job is currently executing, triggers `JobPausedException` to release the lease:
     `UPDATE jobs SET status = 'paused', worker_id = NULL, lease_expires_at = NULL ...`
  3. Restores Windows execution state via `SetThreadExecutionState(ES_CONTINUOUS)`.
  4. Flushes logs and exits cleanly within 5 seconds.

### 6.4 Rotating Log Policy

Configure `logging.handlers.RotatingFileHandler`:
- File: `backend/logs/worker.log`
- Max Size: `5 * 1024 * 1024` (5 MB per file).
- Backup Count: 5 files.
- Total Disk Cap: 25 MB maximum.

### 6.5 Power & Network Failure Resilience

- **Windows Modern Standby (S0) Constraint:** The PC must be plugged into AC power. Battery operation may throttle networking despite Away Mode.
- **Power Loss:** Unplanned shutdown leaves SQLite in WAL mode. Upon reboot, Task Scheduler restarts the worker daemon. After the 30-second lease timeout, the worker claims the job, checks `checkpoint_data`, and resumes execution with zero duplicate audits.
- **Network Outage:** Discovery or audit requests timeout safely (httpx timeout = 15s). Provider errors are recorded; jobs are retried up to `max_retries` before being marked failed.

---

## 7. Concrete Files & Settings Expected to Change

| Target File | Intended Changes (In Later Implementation Milestones) |
| :--- | :--- |
| `backend/app/main.py` | Add `TrustedHostMiddleware`; remove legacy `JobWorker` from `lifespan`; read `ALLOWED_HOSTS` and `COOKIE_SECURE` from `.env`. |
| `backend/app/api/auth.py` | Remove `adopt_legacy_data` from registration; update `/api/auth/adopt-legacy-leads` to require step-up password and `X-Admin-Bootstrap-Token`. |
| `backend/app/cli/main.py` | Add `admin adopt-legacy` interactive CLI command with password verification and bootstrap token emission. |
| `backend/app/queue/worker_daemon.py` | Add `RotatingFileHandler` logging configuration and enhanced Windows console ctrl-break handler. |
| `frontend/src/services/api.ts` | Verify same-origin relative `/api` paths with credentials. |
| `Caddyfile` (New) | Reverse proxy configuration for Tailscale domain, static file serving, and header rewriting. |
| `backend/scripts/backup.py` (New) | Automated script implementing `create_online_snapshot`, integrity check, and encryption. |

---

## 8. Staged Implementation Gates & Approval Strategy

No consequential action or configuration change will occur without explicit user approval at each gate:

```
[ Gate 1: Architecture Remediation Approval ]  <-- CURRENT POSITION (Sign-off on this report)
                     |
                     v
[ Gate 2: Local Reverse Proxy Loopback Drill ]
  - Install Caddy on loopback only. Verify header rewriting and trusted host middleware on disposable app.
                     |
                     v
[ Gate 3: CLI Administrative Adoption Implementation & Unit Tests ]
  - Implement CLI adoption workflow and test against temporary database.
                     |
                     v
[ Gate 4: Windows Worker Supervision & Reboot Tests ]
  - Configure Task Scheduler against test script; test service failure, sleep, and reboot recovery.
                     |
                     v
[ Gate 5: Backup & Restore Automated Drill ]
  - Test online backup, encryption, and restore against disposable database.
                     |
                     v
[ Gate 6: Private Mesh Integration & Phone PWA HTTPS Verification ]
  - Enable Tailscale MagicDNS HTTPS on Caddy; test mobile PWA installation from phone.
                     |
                     v
[ Gate 7: Explicit Sign-Off Before Real Database Migration ]
  - Rehearse migration v1->v2->v3->v4 on copy. Migrate real database only with explicit user command.
```

---

## 9. Real Database Preservation Confirmation

Before and after compiling this specification, `backend/websitelead_hunter.db` was inspected read-only:
```
Tables in websitelead_hunter.db: ['jobs', 'schema_migrations', 'users', 'sessions', 'leads']
  jobs: 1 rows
  schema_migrations: 1 rows
  users: 0 rows
  sessions: 0 rows
  leads: 8 rows
```
**Confirmation:** Exactly 1 job, 8 leads, 0 users, 0 sessions, 1 migration. Zero writes, zero schema modifications, zero migrations performed.

---

## 10. Final Git Status and Working Tree Summary

```
On branch master
Changes not staged for commit:
  modified:   backend/app/api/discovery.py
  modified:   backend/app/api/export.py
  modified:   backend/app/api/jobs.py
  modified:   backend/app/api/leads.py
  modified:   backend/app/audit/website_auditor.py
  modified:   backend/app/database.py
  modified:   backend/app/main.py
  modified:   backend/app/pitch/llm_provider.py
  modified:   backend/app/queue/durable_worker.py
  modified:   backend/app/queue/job_worker.py
  modified:   backend/app/schemas.py
  modified:   backend/tests/test_api.py
  backend/tests/test_durable_worker.py
  modified:   frontend/index.html
  modified:   frontend/public/favicon.svg
  modified:   frontend/src/App.tsx
  modified:   frontend/src/components/KanbanBoard.tsx
  modified:   frontend/src/components/LeadDetailModal.tsx
  modified:   frontend/src/components/LeadTable.tsx
  modified:   frontend/src/components/Navbar.tsx
  modified:   frontend/src/main.tsx
  modified:   frontend/src/services/api.ts
  modified:   frontend/src/types/index.ts

Untracked files:
  backend/app/api/auth.py
  backend/app/auth/
  backend/app/cli/
  backend/app/migrations.py
  backend/app/queue/worker_daemon.py
  backend/tests/test_auth_and_isolation.py
  backend/tests/test_cli_windows.py
  backend/tests/test_idempotency_regression.py
  backend/tests/test_job_orchestration.py
  backend/tests/test_multi_client_integration.py
  backend/tests/test_pwa_offline.py
  backend/tests/test_retry_and_recovery_matrix.py
  backend/tests/test_security_adversarial.py
  backend/tests/test_sync_and_adoption_security.py
  backend/websitelead_hunter.db.backup_m2
  cli.py
  docs/
  frontend/public/icons/
  frontend/public/manifest.webmanifest
  frontend/public/sw.js
  frontend/src/components/AuthModal.tsx
  frontend/src/components/OfflineBanner.tsx
  frontend/src/services/offlineStore.ts
  frontend/src/services/syncEngine.ts
  hunt.bat
```

No code changes or database writes were made in Milestone 9D.1. All analysis was conducted strictly offline.
