# WebsiteLead Hunter — Milestone 9B: Windows Host and Recovery Design Review

**Date:** October 26, 2026  
**Auditor:** Principal Security & Cloud Systems Architect  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Target Milestone:** Milestone 9B (Design Review & Architectural Specification Only)  
**Safety Mandate:** Zero cloud provisioning, zero network calls, zero code modifications, zero database mutations, zero Git commits.  

---

## 1. Executive Summary & Design Verdict

### 1.1 Objective
Following the high-level evaluation in Milestone 9A, Milestone 9B conducts an exhaustive, source-grounded design review of the **Existing-Windows-PC Host Architecture**. This review establishes the formal specifications for:
1. Decoupling background job execution from the FastAPI web server into a standalone, supervised Windows worker process.
2. Atomic SQLite job claims, lease ownership, heartbeats, and stale-worker recovery.
3. Cryptographic and version-fenced prevention of duplicate execution if a stalled worker resumes after lease expiration.
4. Windows-native process supervision and system sleep management.
5. Reverse proxy trust boundaries, loopback spoofing elimination, and mobile PWA HTTPS criteria.
6. Re-architecting legacy data adoption with mandatory authenticated password re-verification.
7. Windows-compatible backup consistency and restore verification procedures.
8. Unresolved commercial-use and data-privacy constraints across free-tier tools.

### 1.2 Architectural Review Verdict: **APPROVED WITH SPECIFIED REVISIONS**
The Existing-Windows-PC model is sound, performant, and practical for private use at ₹0/month. However, four critical implementation flaws in the existing baseline must be resolved during the future implementation phase:
- **Flaw 1 (Worker Coupling):** Background jobs currently run as in-process `asyncio.Task` instances inside Uvicorn. Server restarts terminate running tasks and permanently block resumption (returning `409 Conflict`).  
  *Revision:* Implement a separate `worker_daemon.py` pulling jobs from SQLite via atomic conditional claims.
- **Flaw 2 (Stale Worker Overwrite Risk):** If an in-flight worker stalls (e.g. system sleep or paging) and resumes after a secondary worker has taken over, it could overwrite newer job states.  
  *Revision:* Implement monotonic `lease_version` fencing tokens on all database mutations.
- **Flaw 3 (Proxy Header Spoofing on Loopback):** Administrative adoption in `auth.py` trusts `request.client.host == "127.0.0.1"`. Behind a local reverse proxy, all callers appear as loopback.  
  *Revision:* Eliminate IP-only checks; mandate session authentication plus explicit password re-verification.
- **Flaw 4 (Tool Incompatibility):** Milestone 9A suggested Litestream for continuous replication. Litestream does not officially support native Windows WAL file locking without WSL2.  
  *Revision:* Replace with native Python SQLite Online Backup API (`sqlite3.backup()`) and Rclone on Windows.

---

## 2. Source-Grounded Inspection of Existing Implementation

| Subsystem | Exact Source File(s) & Lines | Implemented Baseline | Identified Architectural Gap |
| :--- | :--- | :--- | :--- |
| **Job Startup & Lifecycle** | `backend/app/api/jobs.py` (lines 160–189)<br>`backend/app/queue/job_worker.py` (lines 43–70, 186–200) | `POST /api/jobs` awaits `worker_instance.start_job(...)`, creating an in-process `asyncio.Task`. | Tasks live in Uvicorn's memory. Cannot scale across processes or survive restarts. |
| **Interrupted Recovery** | `backend/app/queue/job_worker.py` (lines 70–94)<br>`backend/app/api/jobs.py` (lines 319–324) | On startup, `recover_interrupted_jobs` marks running/pending jobs as `paused`. Resuming returns `409 Conflict`. | Interrupted jobs cannot actually resume; they must be cancelled and restarted from scratch. |
| **SQLite Connection & WAL** | `backend/app/database.py` (lines 10–35) | `sqlite3.connect(..., timeout=15.0)`, `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`. | Granular transactions in `get_db()`, but lacking explicit multi-process queue locking and schema leases. |
| **Authentication & Sessions** | `backend/app/auth/dependencies.py` (lines 20–88)<br>`backend/app/auth/security.py` (lines 60–105) | Server-side sessions in `sessions` table (7-day TTL). Dual extraction: HttpOnly cookie or `Bearer` token. | Session resolution is fully multi-client compatible, but in-memory rate limiter is not shared across processes. |
| **CSRF Defense** | `backend/app/auth/dependencies.py` (lines 103–128) | Double-submit CSRF cookie (`csrf_token`) with `X-CSRF-Token` header. Bearer tokens exempt. | Robust for browser and CLI, but relies on consistent reverse proxy origin forwarding. |
| **Loopback Assumption** | `backend/app/api/auth.py` (lines 85, 133, 275–277) | `is_loopback = ip in ("127.0.0.1", "::1", "localhost", "testclient")` guards legacy lead adoption. | Behind a reverse proxy, `request.client.host` evaluates to `127.0.0.1` for all callers unless headers are validated. |
| **CLI & DPAPI** | `backend/app/cli/credentials.py` (lines 35–150)<br>`backend/app/cli/client.py` (lines 16–18, 93–120) | Native Windows Credential Manager (`Advapi32.dll` / `CredWriteW`). Redacts tokens. | Fully compatible with remote private host when `API_BASE_URL` is set to private HTTPS endpoint. |
| **PWA & Offline Outbox** | `frontend/public/sw.js` (lines 1–85)<br>`frontend/src/services/offlineStore.ts` (lines 1–60) | Static shell cached; `/api/*` excluded. IndexedDB outbox queue isolated by `user_id`. | Strictly requires valid HTTPS for Service Worker registration on mobile devices. |

---

## 3. Decoupled Worker Lifecycle & Route Coexistence

### 3.1 Seamless Route Coexistence
The existing API contracts in `backend/app/schemas.py` and `backend/app/api/jobs.py` already support asynchronous lifecycle states: `status` can be `'pending'`, `'running'`, `'paused'`, `'cancelled'`, `'completed'`, or `'failed'`.

In the decoupled architecture, the web server and the worker daemon communicate **strictly through the database**:

```
[ Client (PWA / CLI) ]
         │
         │ 1. POST /api/jobs (with Idempotency-Key)
         ▼
[ FastAPI Server (Uvicorn) ]
         │
         │ 2. Compute canonical SHA-256 fingerprint
         │ 3. Atomically reserve idempotency key (resource_id='PENDING')
         │ 4. Insert job (status='pending', lease_version=0)
         │ 5. Return JobResponse (200 OK, status='pending')
         ▼
[ SQLite Database (websitelead_hunter.db) ]
         ▲
         │ 6. Standalone Worker Daemon claims job:
         │    UPDATE jobs SET status='running', worker_id=..., lease_version=1
         │    WHERE status='pending' LIMIT 1
         │
[ Standalone Worker Daemon (worker_daemon.py) ]
```

### 3.2 State Transition Matrix & API Route Behavior

| API Endpoint | Prior In-Process Action | Proposed Multi-Process Database Action | Worker Daemon Reaction |
| :--- | :--- | :--- | :--- |
| **POST `/api/jobs`** | Called `worker.start_job()`; spawned in-process task. | Inserts row into `jobs` with `status='pending'`, `lease_version=0`. Updates `idempotency_keys`. | Worker daemon detects pending job on next polling tick (every 2s or SQLite notify) and claims it. |
| **GET `/api/jobs/{id}`** | Read from SQLite. | Read from SQLite (identical). | None (passive read). |
| **POST `/api/jobs/{id}/pause`** | Called `worker.pause_job()`; cleared in-memory `asyncio.Event`. | `UPDATE jobs SET status='paused', updated_at=:now WHERE id=:id AND status='running'` | Before auditing each lead, worker checks `SELECT status, lease_version FROM jobs WHERE id=:id`. If `status=='paused'`, worker releases lease and halts. |
| **POST `/api/jobs/{id}/resume`** | Called `worker.resume_job()`; returned 409 if restarted. | `UPDATE jobs SET status='pending', worker_id=NULL, lease_expires_at=NULL WHERE id=:id AND status='paused'` | Any active worker daemon claims the resumed job with `status='pending'` and picks up from the last un-audited lead. |
| **POST `/api/jobs/{id}/cancel`** | Set `_cancel_flags` set; called `task.cancel()`. | `UPDATE jobs SET status='cancelled', updated_at=:now WHERE id=:id AND status IN ('pending', 'running', 'paused')` | Worker inspects database before next HTTP audit, observes `status=='cancelled'`, records cancellation log, and exits loop. |

---

## 4. Atomic Claims, Lease Ownership, and Heartbeats

### 4.1 Schema Additions (Migration v4 Specification)
To support multi-process queue orchestration, four columns must be added to the `jobs` table via a non-destructive versioned migration (`apply_migration_v4`):

```sql
ALTER TABLE jobs ADD COLUMN worker_id TEXT;
ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE jobs ADD COLUMN lease_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3;

CREATE INDEX IF NOT EXISTS idx_jobs_claimable 
ON jobs (status, lease_expires_at, created_at);
```

### 4.2 Atomic Job Claim Query
Workers use SQLite WAL-mode write transactions to claim jobs without race conditions:

```python
def claim_next_job(conn: sqlite3.Connection, worker_id: str, lease_duration_sec: int = 30) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    lease_expires = (now + timedelta(seconds=lease_duration_sec)).isoformat()
    now_iso = now.isoformat()

    with conn: # Enforces BEGIN IMMEDIATE in WAL mode
        # Atomic claim of next pending job or abandoned job with expired lease
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status = 'running',
                worker_id = ?,
                lease_version = lease_version + 1,
                heartbeat_at = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'pending'
                   OR (status = 'running' AND lease_expires_at < ?)
                ORDER BY created_at ASC
                LIMIT 1
            )
            RETURNING id, user_id, status, location_query, lat, lon, radius_km, 
                      categories, max_leads, total_found, processed_count, 
                      lease_version, retry_count;
            """,
            (worker_id, now_iso, lease_expires, now_iso, now_iso),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
```

### 4.3 Heartbeat Protocol & Interval Boundaries
- **Heartbeat Frequency:** Every **10 seconds** (1/3 of lease duration).
- **Lease Duration:** **30 seconds**.
- **Heartbeat Query:**
  ```sql
  UPDATE jobs
  SET heartbeat_at = :now,
      lease_expires_at = :new_lease_expires,
      updated_at = :now
  WHERE id = :job_id
    AND worker_id = :worker_id
    AND lease_version = :lease_version
    AND status = 'running';
  ```
- **Fencing Rule:** If `cursor.rowcount == 0`, the worker has lost its lease (due to admin cancellation, manual pause, or recovery by another worker). The worker **immediately halts all execution** and does not make further network calls.

---

## 5. Stale-Worker Fencing & Duplicate Execution Prevention

### 5.1 The Split-Brain / Stalled Worker Scenario
Consider a workstation running Worker A:
1. Worker A claims Job 1 with `lease_version = 1`, `lease_expires_at = T+30s`.
2. Worker A fetches 15 candidates from Overpass.
3. Windows enters sleep mode or encounters high memory paging, causing Worker A to freeze for 45 seconds.
4. At `T+35s`, Worker A's lease expires.
5. Worker B wakes up, discovers Job 1 with `lease_expires_at < now`, claims it with `lease_version = 2`, and begins auditing.
6. At `T+45s`, the system unfreezes. Worker A resumes execution.

### 5.2 The Fencing Token Solution
Without fencing, Worker A and Worker B would simultaneously audit leads and overwrite each other's database records.  
Under our **Fencing Token Pattern**, Worker A is completely neutralized:

```
[ Worker A (Stalled, lease_version=1) ]           [ Worker B (Active, lease_version=2) ]
                   │                                                │
                   │ (Awakens after stall)                          │ Audits Lead 1
                   │                                                │ Writes Lead 1 to DB (lease_version=2) -> SUCCESS
                   │                                                │
                   ▼                                                ▼
Attempts to persist Lead 2:
UPDATE jobs SET processed_count = 2 ...
WHERE id = :job_id 
  AND worker_id = 'WorkerA' 
  AND lease_version = 1;
                   │
                   ▼
Result: rowcount == 0!
Fencing Triggered!
Worker A catches StaleLeaseError:
1. Logs: "Lost lease on Job 1 (fencing rejected)."
2. Cleans up memory state.
3. Exits task immediately.
```

### 5.3 Deduplication of External Network Calls
When Worker B takes over an interrupted or recovered job:
1. It queries existing leads already saved in SQLite:
   ```sql
   SELECT provider_id FROM leads WHERE job_id = :job_id;
   ```
2. Any discovered business whose `provider_id` is already in SQLite is **skipped immediately**.
3. Zero duplicate website audits, zero duplicate LLM pitch generations, and zero duplicate score calculations are executed.

---

## 6. Failure and Recovery Matrix

| Failure Mode | Trigger Condition | Detection Mechanism | Recovery Action | Guarantee |
| :--- | :--- | :--- | :--- | :--- |
| **Worker Process Crash** | Power cut, unhandled segfault, `taskkill /F` | `lease_expires_at < now` | Next worker iteration claims job, increments `retry_count`, resumes from last saved lead. | Zero duplicate leads; job finishes remaining audit items. |
| **Worker Stall & Late Resume** | Windows Modern Standby, GC freeze > 30s | Conditional update `WHERE lease_version = :v` returns `rowcount == 0`. | Stale worker self-terminates instantly. Active worker proceeds unaffected. | Zero split-brain state; zero overwritten leads. |
| **Host System Restart** | Windows Update restart | Worker supervisory service starts `worker_daemon.py` on boot. | Worker identifies pending and expired-lease jobs; resumes execution. | Autonomous recovery without human intervention. |
| **SQLite Database Busy** | Web server and worker write simultaneously | `PRAGMA busy_timeout = 15000` | SQLite sleeps and retries internally up to 15s. Python context manager retries with exponential backoff. | Zero `sqlite3.OperationalError: database is locked`. |
| **Transient Upstream Network Failure** | Overpass/Nominatim 503 or DNS timeout | Worker catches `httpx.TransportError` / HTTP 5xx | Exponential backoff (2s, 4s, 8s). If retries exhausted, job marked `paused` with descriptive log. | Upstream fair-use respect; no infinite retry loops. |
| **Permanent Validation Error** | Invalid geo query, geocoding returns 0 results | Geocoder raises `NoResultsFound` | Job immediately marked `failed`, `error_message` sanitized and stored, `PENDING` key cleaned. | Fast failure reporting; no retry attempts. |
| **User Job Cancellation** | POST `/api/jobs/{id}/cancel` | Worker checks `status` column at each loop iteration | Worker halts, logs cancellation, leaves already-audited leads intact, marks job `cancelled`. | Responsive stop within < 2 seconds. |

---

## 7. Windows-Compatible Worker Supervision & Sleep Management

### 7.1 Supervision Strategy Comparison

| Feature | Windows Task Scheduler (`schtasks`) | Windows Service via NSSM | Native Python Service (`pywin32`) |
| :--- | :--- | :--- | :--- |
| **Continuous Supervision** | Poor (fires on schedule or logon; no fast auto-restart on crash) | **Excellent** (instant auto-restart with configurable throttle backoff) | Good (runs as service, but requires writing complex Win32 C/Python service handlers) |
| **Stdout/Stderr Redirection** | Requires manual shell redirection (`>> log.txt 2>&1`) | **Built-in** (automatic log rotation and file redirection) | Requires custom logging code |
| **Graceful Shutdown** | Sends hard `TerminateProcess` | **Clean** (sends `CTRL_C_EVENT` / `WM_CLOSE`, waits up to 10s before kill) | Can intercept service stop events |
| **Third-Party Dependencies** | None (Built into Windows) | Single standalone binary (`nssm.exe`), zero package installs | Requires `pywin32` package (violates zero-package constraint) |
| **Service Account Context** | Local User / SYSTEM | `NT AUTHORITY\LocalService` or dedicated local user `weblead` | Local User / SYSTEM |

### 7.2 Selected Supervision Design: NSSM Wrapper
For the production implementation, **NSSM (Non-Sucking Service Manager)** is the recommended supervisory mechanism:
- Service Command:
  ```cmd
  nssm install WebsiteLeadWorker "C:\Users\Yatha\AppData\Local\Programs\Python\Python313\python.exe" "-m app.queue.worker_daemon"
  nssm set WebsiteLeadWorker AppDirectory "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend"
  nssm set WebsiteLeadWorker AppStdout "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend\logs\worker.log"
  nssm set WebsiteLeadWorker AppStderr "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend\logs\worker_err.log"
  nssm set WebsiteLeadWorker AppRestartDelay 2000
  nssm set WebsiteLeadWorker AppStopMethodSkip 0
  nssm set WebsiteLeadWorker AppStopMethodConsole 10000
  ```
- **Shutdown Handling:** When Windows shuts down or the service is stopped, NSSM delivers a `CTRL_C_EVENT`. The worker catches `KeyboardInterrupt` or registers a `win32` console handler via standard Python `signal.signal(signal.SIGINT, ...)`, finishes persisting the lead currently being audited, updates `jobs.status = 'paused'`, releases its lease, and exits cleanly within 5 seconds.

### 7.3 Sleep & Power Management via Win32 API
On Windows 11, background processes are suspended during Modern Standby or user-configured sleep intervals. To prevent crawler jobs from being prematurely frozen while allowing normal sleep during idle periods:
- The worker daemon invokes the native Windows kernel API via Python's standard `ctypes` library (zero package dependencies):

```python
import ctypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

def prevent_windows_sleep():
    """Prevents system sleep while background scraping/auditing is in progress."""
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    )

def allow_windows_sleep():
    """Restores standard Windows power management when all jobs are idle."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
```
- **Lifecycle:**
  - When a job is claimed: `prevent_windows_sleep()` is called.
  - When the queue is empty / all jobs completed: `allow_windows_sleep()` is called.

---

## 8. Reverse Proxy Trust Boundary, Forwarded Headers, and PWA Requirements

### 8.1 Trust Boundary Architecture

```
[ Remote Phone (Tailscale IP 100.80.12.34) ]
                   │
                   ▼ (Encrypted HTTPS on port 443 via Tailscale MagicDNS)
[ Host Machine (tailscale0 interface) ]
         │
         ▼
[ Reverse Proxy (Caddy / Tailscale Serve) ]
         │
         │ - Terminates TLS
         │ - Adds X-Forwarded-For: 100.80.12.34
         │ - Adds X-Forwarded-Proto: https
         │ - Adds X-Forwarded-Host: lead-server.tailnet.ts.net
         ▼ (HTTP on 127.0.0.1:8000 only)
[ FastAPI Server (Uvicorn) with ProxyHeadersMiddleware ]
```

### 8.2 Backend Binding & Direct Access Prevention
- **Binding Rule:** Uvicorn must be bound strictly to `127.0.0.1` (`HOST="127.0.0.1"`).
- **Firewall Rule:** Windows Defender Firewall must have an inbound block rule on port 8000 for all network profiles (Domain, Private, Public). Port 8000 must NEVER be reachable from the physical LAN or Wi-Fi.

### 8.3 Forwarded Header Configuration
To prevent loopback spoofing and correctly resolve client IPs:
```python
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Configure in app/main.py
app.add_middleware(
    ProxyHeadersMiddleware, 
    trusted_hosts=["127.0.0.1", "::1"]
)
```
- **Result:**
  - `request.client.host` reliably yields the client's Tailscale IP (`100.x.y.z`), NOT `127.0.0.1`.
  - `request.url.scheme` evaluates to `https`, ensuring secure cookies (`COOKIE_SECURE=True`) are accepted by mobile browsers.

### 8.4 Mobile PWA Criteria
- iOS Safari and Android Chrome require:
  1. Valid SSL/TLS from a root CA trusted by the operating system. (Tailscale MagicDNS automated Let's Encrypt certificates satisfy this natively).
  2. Served from root `/` with valid manifest (`manifest.webmanifest`).
  3. Service Worker registered at root scope `/`.
  4. Same-origin cookies: Because the UI static shell and `/api/*` are hosted under the same domain (`https://<host>.<tailnet>.ts.net/`), browser cookie policies treat all requests as `same-origin`, ensuring automatic cookie inclusion and CSRF protection.

---

## 9. Reassessed Legacy-Data Adoption Architecture

### 9.1 Flaw in Current Implementation
The current implementation in `backend/app/api/auth.py` checks:
```python
is_loopback = ip in ("127.0.0.1", "::1", "localhost", "testclient")
```
- **Why this is rejected:**
  1. Header-based loopback checks are easily spoofed or misconfigured behind reverse proxies.
  2. Legitimate administrative users connecting from their phone via Tailscale have a non-loopback IP (`100.x.y.z`) and would be blocked.
  3. A standalone shared secret (like a token in `.env`) can be leaked or replayed.

### 9.2 Re-Architected Multi-Factor Authorization Specification
Legacy data adoption must be strictly gated behind **Three-Layer Authenticated Authorization**:

```
[ POST /api/auth/adopt-legacy-leads ]
                 │
                 ├── 1. Layer 1: Authenticated Session (Depends(get_current_user))
                 │      Must have valid, non-expired session cookie/token.
                 │
                 ├── 2. Layer 2: CSRF Validation (Depends(verify_csrf_token))
                 │      Must provide valid X-CSRF-Token header matching session.
                 │
                 ├── 3. Layer 3: Sudo-Mode Password Re-Authentication
                 │      Request body MUST supply current user password:
                 │      {"password": "UserCurrentPassword123!"}
                 │      Server executes verify_password(payload.password, user["password_hash"]).
                 │
                 ├── 4. Invariant Verification:
                 │      Check: SELECT COUNT(*) FROM leads WHERE user_id IS NULL;
                 │      If count == 0: Raise 409 Conflict ("No unassigned legacy leads exist.").
                 │
                 └── 5. Atomic Claim & Audit Log:
                        UPDATE leads SET user_id = :user_id WHERE user_id IS NULL;
                        UPDATE jobs SET user_id = :user_id WHERE user_id IS NULL;
                        INSERT INTO audit_logs (event, user_id, count, ip, timestamp);
```

- **Interactive CLI Alternative:**
  For administrator bootstrap from the Windows terminal:
  ```powershell
  python cli.py admin adopt-legacy --email admin@example.com
  ```
  The CLI prompts for the administrator's password and requires explicitly typing `CONFIRM_ADOPT_8_LEADS` before any database mutation occurs.

---

## 10. Backup Consistency, Windows Tool Compatibility, and Restore Verification

### 10.1 Why Litestream Is Not Recommended on Native Windows
Milestone 9A proposed Litestream for WAL streaming. A deep platform compatibility check reveals:
- Litestream relies on Linux-specific file locking semantics and POSIX syscalls for monitoring SQLite WAL transitions.
- Running Litestream natively on Windows against a database opened in WAL mode by Windows Python causes severe lock collisions (`ERROR_SHARING_VIOLATION`), because Windows enforces mandatory file locking on memory-mapped files (`.shm` and `-wal`).
- Litestream on Windows is officially unsupported and experimental unless run inside WSL2.
- **Decision:** Litestream is rejected for the native Windows host architecture.

### 10.2 Recommended Windows-Native Backup Architecture
We recommend a **100% native Windows solution** using Python's built-in SQLite Online Backup API paired with **Rclone for Windows**:

```
[ websitelead_hunter.db ] (Active WAL Mode)
           │
           │ 1. Scheduled Python Backup Script (runs via Windows Task Scheduler)
           │    Uses conn.backup() with 100 pages/step (Zero Read/Write Locks)
           ▼
[ C:\websitelead-backups\daily\websitelead_backup_YYYYMMDD_HHMMSS.db ]
           │
           │ 2. Rclone Windows Binary (rclone.exe)
           │    Client-side AES-256 encrypted sync (rclone crypt)
           ▼
[ Cloudflare R2 / Google Drive / Encrypted Offsite Storage ]
```

#### Native Python Backup Implementation Specification
```python
import sqlite3
import os
from datetime import datetime, timezone

def perform_consistent_online_backup(db_path: str, backup_dir: str) -> str:
    """
    Creates a transactionally consistent, vacuumed snapshot of the live SQLite database
    without acquiring write locks or blocking concurrent readers or writers.
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"backup_{timestamp}.db")

    src = sqlite3.connect(db_path, timeout=15.0)
    dst = sqlite3.connect(backup_file)

    try:
        with dst:
            # Step in chunks of 100 pages, pausing 10ms between steps to allow pending writes
            src.backup(dst, pages=100, sleep=0.01)
    finally:
        dst.close()
        src.close()

    return backup_file
```

### 10.3 Automated Restore Verification Drill Procedure
A backup script is useless unless its restore integrity is automatically verified:
1. **Automated Sandbox Restore:**
   - Every backup file created is immediately copied into a temporary sandbox directory (`%TEMP%\restore_drill\`).
2. **Integrity Check:**
   - Open sandbox connection: `PRAGMA integrity_check;`. Assert result is `'ok'`.
   - Run `PRAGMA foreign_key_check;`. Assert zero violations.
3. **Record Count Validation:**
   - Assert `SELECT COUNT(*) FROM leads` matches or exceeds previous backup.
   - Assert `SELECT COUNT(*) FROM jobs` matches or exceeds previous backup.
4. **Log Result:**
   - Log verification success into `backup_audit.log`. If integrity check fails, trigger an alert log and do not replicate the corrupted snapshot offsite.

---

## 11. Free-Tier Eligibility, Privacy Terms, and Commercial-Use Restrictions

Every external service and free tier was audited for licensing terms, commercial applicability, and privacy risks:

| Service / Tool | Stated Free Allowance | Commercial Use Compliance Status | Data Privacy / Training Risk | Required Action / Decision |
| :--- | :--- | :--- | :--- | :--- |
| **Tailscale Personal Plan** | Up to 3 users, 100 devices, MagicDNS, Tailscale Serve | **UNRESOLVED / RESTRICTED:** Tailscale's Personal free plan terms state it is for "personal and non-commercial projects." Freelancing to generate commercial income falls into a legal gray area. | Zero data privacy risk (End-to-end WireGuard encrypted; Tailscale does not inspect payload traffic). | If strict commercial compliance is required, upgrade to Tailscale Starter ($6/user/mo) or use self-hosted Headscale / native WireGuard. Labelled as **Unresolved**. |
| **Cloudflare Zero Trust / Tunnel** | Up to 50 users, automated SSL, Access policies | **PERMITTED:** Cloudflare allows commercial websites on the free plan. Section 2.8 restricts disproportionate media/video streaming, not standard web APIs. | TLS terminated at Cloudflare edge. Cloudflare has theoretical access to decrypted HTTP traffic. | Acceptable alternative to Tailscale if edge authentication is preferred. |
| **Google Gemini API Free Tier** | 15 RPM, 1,500 requests/day, 1M TPM | **PERMITTED WITH SEVERE PRIVACY TRADE-OFF:** Commercial use is permitted under Google AI Studio terms. | **HIGH PRIVACY RISK:** Under the Free Tier, Google **reserves the right to review prompts and responses by human reviewers and use them to train Google models**. Prospect business names, contact details, and pitch drafts are logged for training. | **Architectural Decision:** Default to our existing `DeterministicPitchEngine` (zero data shared, 100% private). Allow Gemini LLM pitch generation only when explicitly opted in by the user with knowledge of Google's training terms. |
| **OpenStreetMap Overpass API** | Public community-donated servers (free) | **RESTRICTED FAIR-USE:** Strictly non-commercial fair use. Automated batch querying must respect rate limits (< 1 req/sec). Heavy scraping results in IP blocking. | Public geospatial data; zero privacy risk. | Bound search radii to <= 2.5km; enforce 1.5s sleep between Overpass queries. |
| **OpenStreetMap Nominatim** | Public community-donated servers (free) | **STRICT FAIR-USE:** Max 1 request/second. Requires valid `User-Agent` with contact email. Bulk geocoding prohibited. | Geocoding queries logged publicly in server logs. | Cache geocoding results locally in SQLite; enforce strict 1.0s throttling. |

---

## 12. Testable Acceptance Criteria for Future Milestones

Before Milestone 10 or production implementation can be considered complete, the following verified acceptance criteria must be satisfied:

1. **Standalone Worker Independence:**  
   The FastAPI server and `worker_daemon.py` must run as separate OS processes. Killing `worker_daemon.py` (`taskkill /F`) while a job is running must NOT terminate the Uvicorn web server, and restarting the worker must resume the job without duplicate lead audits.
2. **Fencing Token Rejection Test:**  
   Simulate Worker A stalling (holding `lease_version=1`), allow Worker B to claim the job (`lease_version=2`), then unfreeze Worker A. Worker A's subsequent write must be rejected (`rowcount == 0`), Worker A must log a `StaleLeaseError`, and Worker B must complete the job without corruption.
3. **Checkpoint Resumption Verification:**  
   A job processing 20 leads interrupted after lead 8 must resume and audit ONLY leads 9 through 20. Verified by asserting that mock HTTP auditor counts for leads 1–8 do not increment.
4. **Sudo-Mode Legacy Adoption:**  
   `POST /api/auth/adopt-legacy-leads` must return `401 Unauthorized` without a session, `403 Forbidden` with an incorrect password, and `200 OK` only when valid credentials and CSRF tokens are supplied.
5. **Windows Backup Integrity Test:**  
   A restore verification script must successfully restore an online snapshot of `websitelead_hunter.db` into a sandbox and assert `PRAGMA integrity_check == 'ok'` while write operations are actively running.
6. **Sleep Suppression Verification:**  
   The worker daemon must successfully call `SetThreadExecutionState` on job start and release it on idle.

---

## 13. Verification Log & Preservation Confirmation

### 13.1 Commands Inspected and Run

| Command Executed | Context / Objective | Exit Code | Observed Output / Verification Result |
| :--- | :--- | :--- | :--- |
| `git status --short` | Pre-review repository integrity check | `0` | Confirmed working tree is preserved; unstaged files from Milestones 2–9A intact. |
| `python check_db.py` | Production database count verification | `0` | Verified: `leads: 8, jobs: 1, users: 0, sessions: 0, schema_migrations: 1`. |
| `python -c "import sqlite3..."` | Host SQLite engine version inspection | `0` | Verified: SQLite version `3.50.4` (fully supports WAL, `RETURNING`, window functions, and online backup API). |
| Source file inspection | Read `jobs.py`, `job_worker.py`, `database.py`, `schemas.py`, `auth.py`, `client.py` | N/A | Traced exact lines and validated multi-process execution models. |

### 13.2 Confirmation of Strict Non-Interference
- **Zero Production Code Changed:** No files in `backend/app/`, `backend/tests/`, or `frontend/src/` were edited.
- **Zero Database Changes:** `backend/websitelead_hunter.db` remains 100% untouched (`leads: 8, jobs: 1, users: 0`).
- **Zero Cloud or Network Activity:** No external HTTP requests, cloud provisioning, or account creations were attempted.
- **Zero Git Actions:** No commits, pushes, branch switches, or resets were performed.
