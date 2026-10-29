# WebsiteLead Hunter — Milestone 9D: Private-Hosting Architecture Review

**Date:** October 26, 2026  
**Auditor:** Principal Systems, Security & Cloud Architect  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Target Milestone:** Milestone 9D (Private-Hosting Architecture Review & Feasibility Assessment)  
**Safety Mandate:** Design and local inspection only. Zero deployment, zero network calls, zero package installs, zero database mutations, real database preserved intact (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 1. Executive Summary & Review Verdict

### 1.1 Review Verdict: **READY WITH FINDINGS**

This architecture review assesses the technical, security, and operational path for transitioning WebsiteLead Hunter from a strictly local application (`127.0.0.1:8000`) to a **privately accessible multi-device platform** (accessible securely from the owner's phone, desktop browser, and Windows CLI) hosted on the owner's existing Windows 11 PC.

The assessment concludes that private self-hosting on the existing Windows PC via a **Private Mesh VPN (Tailscale) + Local Reverse Proxy (Caddy)** is the safest, most performant, and lowest-cost architecture. However, several critical localhost dependencies and security boundaries must be remediated before any network exposure or real database migration can be authorized:

1. **Legacy Data Adoption IP Vulnerability (High):** The legacy data adoption logic (`/api/auth/adopt-legacy-leads` and `/api/auth/register`) currently relies on checking `request.client.host in ("127.0.0.1", ...)`. Behind a local reverse proxy, all TCP connections originate from `127.0.0.1`. If not remediated, any remote caller reaching the reverse proxy would be treated as a local administrator.
2. **CORS & Cookie Transport (Medium):** CORS `ALLOWED_ORIGINS` currently hardcodes `http://localhost:5173` and `http://localhost:3000`. `COOKIE_SECURE` defaults to `false`. When accessed remotely, the reverse proxy must consolidate frontend and API under a single origin (`https://<machine>.<tailnet>.ts.net`) and enforce `COOKIE_SECURE=true`.
3. **Dual Worker Architecture Transition (Medium):** FastAPI `lifespan` still instantiates the legacy in-process `JobWorker`, while durable background execution now belongs to the standalone `DurableJobWorker` (`worker_daemon.py`). FastAPI must be updated to enqueue jobs into SQLite without launching in-process tasks.
4. **Database File Safety (High):** SQLite WAL mode requires strict POSIX/Win32 byte-range locking. The live database file must **never** reside inside a cloud-sync folder (OneDrive, Google Drive, Dropbox) or on an SMB/NFS network share, which guarantees file locking failures and corruption.
5. **Zero Deployment Guarantee:** No cloud resources were provisioned, no network requests were sent, no services were installed, and `backend/websitelead_hunter.db` remains 100% intact (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 2. Part A: Current Architecture & Localhost Assumptions

### 2.1 Subsystem Inventory

| Architectural Subsystem | Source Code Path | Inspected Behavior |
| :--- | :--- | :--- |
| **FastAPI Startup & Lifespan** | [`backend/app/main.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/main.py#L19-L27) | `lifespan` executes `init_db()`, `worker_instance.recover_interrupted_jobs()`, and mounts routers. Default binding is `127.0.0.1:8000`. |
| **Database Connection & WAL** | [`backend/app/database.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/database.py#L10-L35) | Connects via `DATABASE_PATH` env var (default `websitelead_hunter.db`). Enforces `PRAGMA foreign_keys = ON;` and `PRAGMA journal_mode = WAL;`. `timeout = 15.0`. |
| **Migration Manager** | [`backend/app/migrations.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/migrations.py#L216-L246) | Tracks schema in `schema_migrations` (`version`, `name`, `applied_at`). Four versioned migrations registered: v1 (users/sessions), v2 (idempotency), v3 (fingerprints), v4 (durable worker leases & fencing). |
| **Durable Worker Daemon** | [`backend/app/queue/durable_worker.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/queue/durable_worker.py) | Standalone process model. Atomic claims (`UPDATE ... RETURNING`), 30s leases, 10s heartbeats, monotonic `lease_version` fencing, retry limit enforcement (`1 + max_retries`), candidate checkpointing. |
| **Frontend PWA Shell** | [`frontend/public/sw.js`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/public/sw.js) | Caches static shell (`index.html`, icons, manifest). **Strictly excludes and never caches `/api/*`**. Relative API client in [`frontend/src/services/api.ts`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/src/services/api.ts) (`BASE_URL = '/api'`). |
| **Authentication & CSRF** | [`backend/app/auth/dependencies.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/auth/dependencies.py) | Dual auth: HttpOnly `session_id` cookie (browser/PWA) and `Authorization: Bearer <token>` (CLI). Double-submit CSRF cookie (`csrf_token` cookie + `X-CSRF-Token` header). Passwords hashed with `scrypt`. |
| **Windows CLI Client** | [`backend/app/cli/client.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/cli/client.py) | Defaults to `API_BASE_URL="http://127.0.0.1:8000"`. Stores tokens in Windows Credential Manager via native `Advapi32.dll` (`CredWriteW` / `CredReadW`). |
| **Online Backup API** | [`backend/tests/test_durable_worker.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/tests/test_durable_worker.py#L614-L670) | Python native `src.backup(dst, pages=100, sleep=0.005)`. Verified non-blocking backup with `PRAGMA integrity_check` validation. |

### 2.2 Localhost Assumptions & Failure Modes

The current implementation contains six specific assumptions that rely on localhost and would break upon remote private connection:

```
+----------------------------------------------------------------------------------------------------+
| CURRENT LOCALHOST ASSUMPTION                   | BREAKAGE IN PRIVATE NETWORK ACCESS                |
+------------------------------------------------+---------------------------------------------------+
| 1. Loopback Binding (HOST="127.0.0.1")         | Remote devices cannot establish TCP connection.    |
| 2. Hardcoded CORS (ALLOWED_ORIGINS="localhost")| Mobile browser requests from IP/domain rejected.  |
| 3. Plaintext Cookies (COOKIE_SECURE="false")   | Insecure over non-localhost; rejected by mobile.  |
| 4. IP-Based Admin Check (ip in "127.0.0.1")    | Spoofed by reverse proxy (all proxy calls 127.0.0.1)|
| 5. CLI Default (API_BASE_URL="127.0.0.1:8000") | Remote laptop CLI fails unless URL overridden.    |
| 6. Plain HTTP PWA Serving                      | Mobile Chrome/Safari strictly block PWA install.  |
+----------------------------------------------------------------------------------------------------+
```

---

## 3. Part B: Private Access Options Comparison

Three private access designs were evaluated against operational, security, and financial criteria:

```
                                      PRIVATE ACCESS OPTIONS
                                                |
               +--------------------------------+-------------------------------+
               |                                |                               |
         Option 1: Windows PC            Option 2: Windows PC            Option 3: Privately
          + Tailscale Mesh               + Headscale Control              Hosted Cloud VPS
     (Recommended ₹0/mo Baseline)      (Self-Hosted Open Source)        (Future 24/7 Target)
```

### 3.1 Comparative Analysis Matrix

| Evaluation Dimension | Option 1: Windows PC + Tailscale Mesh | Option 2: Windows PC + Headscale Control | Option 3: Privately Hosted Cloud VPS |
| :--- | :--- | :--- | :--- |
| **Connectivity Architecture** | WireGuard point-to-point mesh via Tailscale NAT traversal (STUN/DERP). Direct peer-to-peer tunnels. | WireGuard point-to-point mesh via self-hosted Headscale control server. | Client-to-server WireGuard/Tailscale tunnel to cloud VPS. |
| **Phone Access (PWA)** | Tailscale Android/iOS app. Connects to `https://<pc>.<tailnet>.ts.net`. Full PWA install. | Tailscale mobile app pointing to custom Headscale URL. Full PWA install. | Tailscale/WireGuard app to cloud IP or custom private domain. Full PWA install. |
| **Desktop & CLI Access** | Native Tailscale Windows client. CLI uses `https://<pc>.<tailnet>.ts.net`. | Tailscale Windows client pointing to Headscale login server. | Desktop browser & CLI connect to VPS private IP/domain. |
| **HTTPS & Certificate Model** | **Automated Zero-Config:** Tailscale MagicDNS provisions trusted Let's Encrypt certificates automatically via Tailscale Serve or Caddy. | **Manual / Complex:** Requires self-signed CA installed on phone OR custom public domain with DNS-01 Let's Encrypt validation. | **Automated:** Caddy on VPS with public DNS or Tailscale MagicDNS. |
| **Host PC Dependence** | **High:** Windows PC must remain powered on and connected to internet. Sleep/hibernate suspends access. | **High:** Windows PC must remain awake. Headscale server must also have 100% uptime. | **Zero:** Cloud VPS runs 24/7 independently of home hardware state. |
| **Monetary Cost & Uncertainties** | **₹0 Infrastructure Cost.**  
*Uncertainty:* Tailscale Personal Free Tier terms distinguish personal vs commercial use; legal eligibility for business lead generation is unverified. | **₹0 Licensing.**  
*Cost:* Requires hosting Headscale (needs always-on VPS or server with public IP, ~₹300/mo unless free VPS used). | **₹0 to ₹500/mo.**  
*Uncertainty:* Oracle Cloud Always Free has high reclamation risk and capacity shortages. Commercial VPS costs ₹350–₹500/mo. |
| **Operational Complexity** | **Very Low:** Install Tailscale on PC and phone, enable MagicDNS + HTTPS, run Caddy. | **High:** Deploy, secure, and patch Headscale control plane, manage SQLite/Postgres DB, configure OIDC. | **Moderate-High:** Linux system administration, firewall, automated offsite database backups, kernel updates. |
| **Disaster Recovery** | Simple PC reboot; Task Scheduler restarts Caddy & Durable Worker. | Must recover both host PC and Headscale server if either fails. | VPS snapshot restoration; remote SQLite backup restore. |

### 3.2 Plan Eligibility & Cost Finding
> [!IMPORTANT]
> **Plan Eligibility Notice:** Commercial or business lead-generation activities on "free-for-personal-use" tiers (such as Tailscale Personal Free) carry contractual uncertainties unless explicitly permitted by provider terms. For commercial operations, a self-hosted Headscale control plane or a dedicated paid Tailscale/VPS plan may be required long-term. For immediate private evaluation by the sole owner, Tailscale Free remains technically functional at ₹0.

---

## 4. Part C: Reverse Proxy Architecture & Security Boundaries

### 4.1 Topology & Port Binding Mandate

FastAPI must **never** be exposed directly to non-loopback network interfaces (`0.0.0.0` or physical Ethernet/Wi-Fi adapters).

```
   [ Phone PWA ]          [ Remote Windows CLI ]          [ Desktop Browser ]
         |                         |                              |
         +-------------------------+------------------------------+
                                   |
                     WireGuard Tunnel (Tailscale Mesh)
                                   |
                                   v  (Port 443 / HTTPS)
                    +------------------------------+
                    |    Caddy Reverse Proxy       |  <-- Bound to Tailscale IP
                    |  - Automatic Let's Encrypt   |      or 127.0.0.1
                    |  - Rewrites Forwarded Headers|
                    +------------------------------+
                                   |
                     HTTP (Loopback Only: 127.0.0.1:8000)
                                   |
                                   v
                    +------------------------------+
                    |    FastAPI Backend Server    |  <-- Bound ONLY to 127.0.0.1
                    |  - Validates Session & CSRF  |
                    |  - Scopes Tenant Data (user) |
                    +------------------------------+
                                   |
                            SQLite WAL Mode
                                   |
                                   v
                    +------------------------------+
                    | websitelead_hunter.db        |
                    +------------------------------+
                                   ^
                                   |  Claims & Heartbeats (Atomic WAL)
                    +------------------------------+
                    | Standalone Durable Worker    |  <-- Independent Process
                    | - Windows Sleep Inhibitor    |
                    +------------------------------+
```

### 4.2 Critical Security Boundaries & Header Trust

#### 1. Forwarded Header Trust
FastAPI must trust `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host` **strictly and exclusively** from `127.0.0.1`.
- Caddy must be configured to strip untrusted incoming client headers and set:
  ```caddyfile
  header_up X-Forwarded-For {remote_host}
  header_up X-Forwarded-Proto {scheme}
  header_up X-Forwarded-Host {host}
  ```
- Uvicorn must run with `--proxy-headers --forwarded-allow-ips="127.0.0.1"` to prevent external IP spoofing.

#### 2. Remediation of Legacy Data Adoption Vulnerability
> [!WARNING]
> **Vulnerability Identified in [`backend/app/api/auth.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py#L133-L141):**
> Lines 133 and 275 evaluate `is_loopback = ip in ("127.0.0.1", "::1", "localhost", "testclient")`. When FastAPI is proxied by Caddy on localhost, `request.client.host` is ALWAYS `127.0.0.1`!
> Consequently, **any remote caller on the mesh calling `/api/auth/adopt-legacy-leads` would be erroneously authorized as a local administrator.**

**Required Remediation:**
1. Deprecate IP-based authorization for legacy adoption entirely.
2. Require an explicit **one-time administrative bootstrap token** (`BOOTSTRAP_TOKEN` passed via header `X-Admin-Bootstrap-Key`) or restrict adoption exclusively to a local CLI command (`python cli.py admin adopt-legacy`) that operates directly against the database via local process invocation.

#### 3. Single-Origin Consolidation (Eliminating CORS Issues)
By configuring Caddy to serve both the frontend static assets and the `/api` reverse proxy under the exact same domain:
```caddyfile
my-pc.tailnet.ts.net {
    reverse_proxy /api/* 127.0.0.1:8000
    root * C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/dist
    file_server
    try_files {path} /index.html
}
```
All frontend fetch requests become same-origin (`/api/...`). CORS preflight checks are bypassed entirely, eliminating browser cross-origin policy friction.

#### 4. Cookie Transport Hardening
In production:
- `COOKIE_SECURE=true` must be set in `.env` to enforce the `Secure` flag on `session_id` and `csrf_token` cookies.
- `COOKIE_SAMESITE=lax` protects against cross-site request forgery while permitting normal bookmark navigation.

#### 5. PWA Service Worker Cache Protection
Verified in [`frontend/public/sw.js`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/public/sw.js#L65-L82):
- The Service Worker intercepts `/api/*` and directs it to the network without storing responses in Cache Storage.
- Private customer data, lead scores, audit findings, and session tokens never persist in browser disk cache.

---

## 5. Part D: Windows Worker & Database Reliability

### 5.1 Worker Process Supervision on Windows

The durable worker daemon ([`backend/app/queue/worker_daemon.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/queue/worker_daemon.py)) must run as an unattended background process.

#### Supervision Options Evaluated:
1. **Windows Task Scheduler (Recommended Baseline):**
   - Trigger: "At system startup" or "At log on".
   - Settings: "Run whether user is logged on or not", "If the task fails, restart every 1 minute", "Stop the task if it runs longer than: Disabled".
   - Pros: Native to Windows 11; zero third-party software; GUI and PowerShell management.
2. **WinSW / NSSM Windows Service Wrapper:**
   - Wraps `python -m app.queue.worker_daemon` as a native Windows Service (`services.msc`).
   - Pros: True service lifecycle; automatic restart on failure; standard Windows Event Log integration.
   - Requirement: Requires installing a small binary (`WinSW.exe`).

### 5.2 SQLite WAL Mode & Storage Rules

> [!CAUTION]
> **Database File Placement Rule:**
> `websitelead_hunter.db` must **never** be placed inside a cloud-synchronized folder (OneDrive, Dropbox, Google Drive, iCloud) or on an SMB/NFS network share.

#### Why Cloud Sync Corrupts SQLite WAL:
1. **File Locking Conflicts:** Cloud-sync engines asynchronously open, read, and lock files when changes occur. When SQLite attempts to write to `websitelead_hunter.db-wal` while OneDrive is reading it, SQLite encounters `sqlite3.OperationalError: database is locked`.
2. **Split-Brain Corrupted Copies:** If two machines or processes write while syncing, cloud storage creates conflict files (e.g. `websitelead_hunter (Conflicted Copy).db`), tearing transactions and causing silent database corruption.
3. **Lock Incompatibility on Network Shares:** SMB/CIFS network filesystems do not fully support POSIX byte-range locking and memory-mapped shared memory (`.db-shm`), leading to broken WAL consistency.

### 5.3 Windows Sleep, Power, and Hibernation Management

1. **Win32 Execution State:** Verified in Milestone 9C.1 via native `kernel32.SetThreadExecutionState`:
   `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED` prevents Windows from entering S3 sleep while active jobs are processing.
2. **Modern Standby (S0) Caveat:** On battery power, Windows Modern Standby may throttle network adapters even when Away Mode is active. **The host Windows PC must be connected to AC wall power.**
3. **Power Loss & Dirty Shutdowns:** If power is cut violently, transactions in WAL are replayed upon next connection. As verified in Milestone 9C.2 (real OS process kill drill), interrupted jobs are cleanly reclaimed after the 30-second lease timeout, candidates are resumed from checkpoint, and zero duplicate leads are audited.

### 5.4 Live Backup & Offsite Retention Strategy

1. **Online Snapshot Generation:**
   - Do NOT copy `websitelead_hunter.db` using file copy tools (`copy`, `xcopy`) while WAL mode is active.
   - Use Python's native `sqlite3.backup()` API (as verified in Milestone 9C/9C.1):
     ```python
     with sqlite3.connect(source_db) as src, sqlite3.connect(backup_db) as dst:
         src.backup(dst, pages=100, sleep=0.005)
     ```
2. **Integrity Validation:**
   - Every backup file must execute `PRAGMA integrity_check;` and `PRAGMA foreign_key_check;` before being accepted into retention.
3. **Offsite Encryption & Retention:**
   - Backups compressed and encrypted locally via AES-256 (`age` or GPG) using an offline passphrase.
   - Encrypted snapshots pushed to offsite cold storage (Backblaze B2 or AWS S3 Glacier).
   - Retention policy: 7 daily snapshots, 4 weekly snapshots, 1 monthly snapshot.

---

## 6. Part E: Concrete Risks & Severity Ranking

| Risk ID | Severity | Threat / Failure Mode | Impact | Mitigation Strategy |
| :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | **High** | Reverse proxy loopback spoofing on legacy data adoption endpoint (`/api/auth/adopt-legacy-leads`). | Remote attacker on private mesh claims unowned legacy leads. | Replace IP check with one-time CLI bootstrap command or secret token. |
| **SEC-02** | **High** | Untrusted `X-Forwarded-*` headers accepted by FastAPI from external clients. | Rate limit bypass; incorrect client IP logging; host header attacks. | Caddy sanitizes headers; Uvicorn configured with `--forwarded-allow-ips="127.0.0.1"`. |
| **OPS-01** | **Medium** | Database placed in OneDrive / Dropbox synchronized folder. | Database lock errors; split-brain corruption; lost leads. | Strictly locate database in local NTFS path (`C:\Users\Yatha\...`) excluded from sync. |
| **OPS-02** | **Medium** | Windows PC enters sleep or disconnects on battery power. | Remote PWA unreachable; background crawl jobs stalled. | Host PC must run on AC power; verify `SetThreadExecutionState` Away Mode. |
| **OPS-03** | **Medium** | Dual worker conflict: FastAPI running legacy `job_worker` alongside `durable_worker`. | Interrupted jobs marked paused by legacy worker instead of leased by durable worker. | Refactor `main.py` lifespan to remove legacy `JobWorker` instance. |
| **LEG-01** | **Low** | Commercial usage on Tailscale Personal Free Tier. | Potential terms of service policy violation if used for commercial scale. | Monitor usage; evaluate self-hosted Headscale control plane for long-term production. |

---

## 7. Part F: Staged Implementation Roadmap & Approval Gates

No deployment or modification will occur without passing through seven explicit gates:

```
[ Gate 1: Architecture Review Approval ]
                     |
                     v
[ Gate 2: Isolated Configuration & Reverse Proxy Loopback Tests ]
                     |
                     v
[ Gate 3: Windows Worker Supervision & Reboot Tests ]
                     |
                     v
[ Gate 4: Backup & Restore Drill with Disposable Database ]
                     |
                     v
[ Gate 5: Private HTTPS & Phone PWA Validation ]
                     |
                     v
[ Gate 6: Migration Rehearsal & Rollback Verification ]
                     |
                     v
[ Gate 7: Explicit User Approval Before Real Database Migration ]
```

### Gate Specifications

- **Gate 1: Architecture & Threat Model Approval**
  - **Action:** User inspects and approves this Milestone 9D design document.
  - **Gate Criteria:** No code or infrastructure modified until explicit sign-off.
- **Gate 2: Isolated Configuration & Reverse Proxy Loopback Tests**
  - **Action:** Install Caddy locally. Test reverse proxying `127.0.0.1:8000` with self-signed TLS strictly on loopback.
  - **Gate Criteria:** Verify `X-Forwarded-For` rewriting; verify CORS rejection on untrusted origins.
- **Gate 3: Windows Worker Supervision Tests**
  - **Action:** Configure Task Scheduler / service wrapper for `worker_daemon.py` against a disposable test database.
  - **Gate Criteria:** Reboot Windows test VM/host; verify daemon auto-starts and recovers test jobs.
- **Gate 4: Backup & Restore Drill with Disposable Database**
  - **Action:** Run automated backup script against seeded temporary database.
  - **Gate Criteria:** Backup passes `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Full restore validated.
- **Gate 5: Private HTTPS & Phone PWA Validation**
  - **Action:** Join host PC and mobile phone to Tailscale mesh. Configure MagicDNS TLS.
  - **Gate Criteria:** Phone accesses `https://<pc>.<tailnet>.ts.net`; PWA installs cleanly; Service Worker activates; zero mixed-content errors.
- **Gate 6: Migration Rehearsal & Rollback Verification**
  - **Action:** Copy `websitelead_hunter.db` to a disposable test copy and execute `init_db()` to rehearse migrations v2, v3, and v4.
  - **Gate Criteria:** All 8 legacy leads, 1 job, and schema versions migrate with zero data loss. Rollback script verified.
- **Gate 7: Explicit User Approval Before Real Database Migration**
  - **Action:** Present final verification checklist to the user.
  - **Gate Criteria:** Real database `backend/websitelead_hunter.db` is migrated **only after explicit user command**.

---

## 8. Files Inspected and Commands Run

### 8.1 Files Inspected
- `backend/app/main.py` (FastAPI startup, CORS, lifespan, static mounting)
- `backend/app/database.py` (SQLite connection flags, WAL mode, pathing)
- `backend/app/migrations.py` (Migration runner, schemas v1–v4)
- `backend/app/api/auth.py` (Registration, login, legacy adoption IP gate)
- `backend/app/auth/dependencies.py` (Session lookup, CSRF token validation, cookie secure/samesite settings)
- `backend/app/api/jobs.py` (Job creation, canonical fingerprinting, reservation)
- `backend/app/queue/durable_worker.py` (Atomic claims, lease renewal, crash boundaries)
- `backend/app/queue/worker_daemon.py` (Daemon signal handling, process lifecycle)
- `backend/app/cli/client.py` (CLI HTTP client, base URL, timeout)
- `backend/app/cli/credentials.py` (Windows Credential Manager integration)
- `frontend/src/services/api.ts` (API client base URL, CSRF headers)
- `frontend/public/sw.js` (PWA Service Worker caching policies, API bypass)
- `docs/audit/MILESTONE_9A_ARCHITECTURE_VALIDATION.md` (Prior hosting analysis)
- `docs/audit/MILESTONE_9B_WINDOWS_HOST_AND_RECOVERY_DESIGN.md` (Prior recovery design)
- `docs/audit/MILESTONE_9C_DURABLE_WORKER_IMPLEMENTATION.md` (Durable worker implementation)
- `docs/audit/MILESTONE_9C_2_RETRY_AND_RECOVERY_VERIFICATION.md` (Retry exhaustion & crash recovery report)

### 8.2 Commands Run
- `git status --short` (verified working tree state)
- `python scratch/check_db.py` (verified real database counts remain 8 leads, 1 job, 0 users)
- `Get-ChildItem -Path ...` (inspected directory layouts)

---

## 9. Real Database Preservation Confirmation

Before and after this assessment, `backend/websitelead_hunter.db` was inspected read-only:
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

## 10. Final Git Status and Diff Summary

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
  modified:   backend/tests/test_durable_worker.py
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

No code changes or database writes were made in Milestone 9D. All analysis was conducted strictly offline.
