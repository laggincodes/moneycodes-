# WebsiteLead Hunter — Consolidated Hosting Readiness Master Plan

**Date:** October 26, 2026  
**Status:** **APPROVED FOR ACTION**  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Safety Mandate:** Planning document only. Zero deployment, zero network calls, zero code changes, real database strictly preserved (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 1. Executive Recommendation & Architectural Decision

### 1.1 Selected Architecture: **Existing Windows PC + Tailscale Mesh + Caddy Reverse Proxy**

To provide secure, low-maintenance private access across your **phone (PWA)**, **desktop browser**, and **Windows CLI** with minimal operational complexity, the optimal architecture uses your existing Windows PC as the application host:

```
[ Phone PWA ]          [ Desktop Browser ]          [ Remote Windows CLI ]
      |                         |                              |
      +-------------------------+------------------------------+
                                |
                  WireGuard Tunnel (Tailscale Mesh)
                                |
                                v  (Port 443 / HTTPS: my-pc.ts.net)
                 +------------------------------+
                 |    Caddy Reverse Proxy       |  <-- Listens on Tailscale IP
                 |  - Automatic Let's Encrypt   |  <-- Serves frontend/dist
                 |  - Rewrites Forwarded Headers|
                 +------------------------------+
                                |
                  HTTP (Loopback Only: 127.0.0.1:8000)
                                |
                                v
                 +------------------------------+
                 |    FastAPI Backend Server    |  <-- Bound strictly to 127.0.0.1
                 |  - Session Auth & CSRF Guard |
                 |  - Multi-tenant user_id scope|
                 +------------------------------+
                                |
                         SQLite WAL Mode
                                |
                                v
                 +------------------------------+
                 | websitelead_hunter.db        |
                 +------------------------------+
                                ^
                                |  Atomic claims, heartbeats, & checkpointing
                 +------------------------------+
                 | Standalone Durable Worker    |  <-- Windows Task Scheduler
                 | - Win32 Away Mode Sleepless  |      (Auto-restart on fail)
                 +------------------------------+
```

### 1.2 Why This is the Most Practical Choice
1. **₹0 Infrastructure Capital Expense:** Eliminates recurring cloud server costs by using existing workstation compute, memory, and fast NVMe storage.
2. **Minimal Maintenance:** No Linux VPS to patch, no public IP to defend, no router port-forwarding (`NAT-PMP`/`UPnP`) required.
3. **Automated Mobile HTTPS:** Tailscale MagicDNS provisions trusted Let's Encrypt TLS certificates automatically (`https://<pc>.<tailnet>.ts.net`), satisfying iOS Safari and Android Chrome strict requirements for PWA installation and Service Worker activation.
4. **Complete Backend Isolation:** FastAPI binds strictly to `127.0.0.1:8000`. The backend is completely unreachable from the public internet or LAN without an authenticated WireGuard key.

### 1.3 Unresolved External Facts & Cost Uncertainties
The following items are outside the local codebase and must be verified during initial setup:
- **Tailscale Free Tier Commercial Eligibility:** Tailscale Personal Free Tier permits up to 3 users and 100 devices. Its terms distinguish personal/hobbyist vs commercial use; eligibility for commercial lead generation at scale must be confirmed against Tailscale's terms. (Fallback: Headscale open-source self-hosted control plane).
- **Residential Electricity Cost:** Running the PC continuously during active search campaigns consumes ~30W–65W idle, adding ~₹150–₹300/month in domestic power.
- **Dynamic Residential ISP IP Changes:** Direct P2P WireGuard connections handle IP changes via DERP relays, but active long-polling sessions could briefly reconnect.

---

## 2. Security & Boundary Hardening

### 2.1 Legacy-Data Adoption Vulnerability Remediation
- **The Defect:** [`backend/app/api/auth.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py#L133) checked `request.client.host in ("127.0.0.1", ...)`. Behind a local reverse proxy, all TCP connections originate from `127.0.0.1`, allowing any remote mesh caller to adopt unowned legacy leads.
- **Remediation Specification:**
  1. **Deprecate Web Registration Adoption:** Remove `adopt_legacy_data` flag from `/api/auth/register`. Web account creation will never adopt unowned records.
  2. **CLI-First Administrative Workflow:** Add dedicated administrative command:
     ```powershell
     python cli.py admin adopt-legacy
     ```
     - Authenticates user locally against `users` table via `scrypt` password check.
     - Displays exact counts: *"Found 8 unowned legacy leads and 1 unowned job."*
     - Requires interactive `[y/N]` confirmation and password re-entry.
     - Generates ephemeral local secret file `backend/.admin_token` (32-byte hex).
     - Submits adoption mutation with `X-Admin-Bootstrap-Token` header.
     - Server verifies disk token via `hmac.compare_digest`, runs atomic `UPDATE`, and immediately deletes the token.
     - Remote web clients cannot trigger adoption because they cannot create files on the Windows host.

### 2.2 Reverse Proxy Trust & Isolation
- **Uvicorn Loopback Binding:** Started strictly with `--host 127.0.0.1 --port 8000`. Windows Advanced Firewall blocks inbound external traffic on port 8000.
- **Trusted Forwarded Headers:** Uvicorn runs with `--proxy-headers --forwarded-allow-ips="127.0.0.1,::1"`. Headers from any non-loopback source are ignored.
- **Header Sanitization (Caddy):** Caddy strips client-supplied `X-Forwarded-*` headers and injects trusted values:
  ```caddyfile
  header_up X-Forwarded-For {remote_host}
  header_up X-Forwarded-Proto {scheme}
  header_up X-Forwarded-Host {host}
  header_up -X-Forwarded-Server
  ```
- **Host Whitelisting:** Add Starlette `TrustedHostMiddleware(app, allowed_hosts=["localhost", "127.0.0.1", "<pc>.<tailnet>.ts.net"])` in `backend/app/main.py`.
- **Single-Origin Consolidation:** Caddy serves static assets (`frontend/dist`) and proxies `/api/*` on the same domain, eliminating CORS preflight friction.
- **Cookie Security:** Enforce `COOKIE_SECURE=true` and `COOKIE_SAMESITE=lax` in production `.env`.
- **Service Worker Cache Guard:** Confirmed [`frontend/public/sw.js`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/public/sw.js) explicitly bypasses and never caches `/api/*` responses in browser Cache Storage.

---

## 3. Windows Worker Supervision & SQLite Reliability

### 3.1 Task Scheduler Process Supervision
The standalone durable worker ([`backend/app/queue/worker_daemon.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/queue/worker_daemon.py)) will run as an unattended Windows background task:
- **Task Name:** `WebsiteLeadHunter-DurableWorker`
- **Trigger:** At system startup (with 30-second delay for network/Tailscale initialization).
- **Execution:** `python.exe -m app.queue.worker_daemon` in `backend/`.
- **Failure Recovery:** Configured to restart every 1 minute on failure (up to 3 retries).
- **Graceful Shutdown:** Traps `SIGINT`/`SIGTERM`, marks active jobs `paused`, releases leases, and clears sleep state within 5 seconds.
- **Log Rotation:** Configured with `RotatingFileHandler` writing to `backend/logs/worker.log` (5 MB per file, 5 backups, 25 MB cap).

### 3.2 Sleep & Power Management
- Host PC must be connected to AC power (Windows Modern Standby S0 throttles network on battery).
- Durable worker activates `kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` during active jobs, preventing OS sleep while allowing display power-down.

### 3.3 Database File Placement Rule
> [!CAUTION]
> **Zero Cloud-Sync Placement:**
> `websitelead_hunter.db` must **NEVER** be placed inside a cloud-synchronized folder (OneDrive, Google Drive, Dropbox) or SMB share. Cloud sync software locks `.db-wal` files asynchronously, causing `sqlite3.OperationalError: database is locked` and split-brain corruption. The database must reside strictly on a local NTFS path.

---

## 4. Encrypted SQLite Backup & Restore Strategy

### 4.1 Non-Blocking Online Backup Workflow
1. **Online Snapshot Generation:**
   Use Python's native `sqlite3.backup()` API with page batching:
   ```python
   def create_backup(source_path: str, target_path: str):
       with sqlite3.connect(source_path) as src, sqlite3.connect(target_path) as dst:
           src.backup(dst, pages=100, sleep=0.005)
   ```
   Releases locks every 100 pages (0.4 MB) to prevent blocking active crawler transactions.
2. **Integrity Validation:** Snapshot must pass `PRAGMA integrity_check` (`ok`) and `PRAGMA foreign_key_check` (0 violations) before acceptance. Calculate SHA-256 hash.
3. **Local AES-256 Encryption:** Encrypt snapshot using AES-256-GCM. Key stored in Windows Credential Manager (`WebsiteLeadHunter:backup_key`) and printed offline.
4. **Retention Policy:** 7 daily snapshots, 4 weekly snapshots, 3 monthly snapshots. Automated pruning script deletes expired backups.
5. **Measurable Restore Acceptance:** Restore drill must complete in <60 seconds on a disposable database with 100% data parity.

---

## 5. Ordered Implementation Roadmap & Approval Gates

Implementation is partitioned into 5 sequential phases. **No stage authorizes the next without explicit user sign-off:**

```
+-----------------------------------------------------------------------------------------+
| PHASE 1: Code & Security Hardening (Offline)                                            |
| - Remove adopt_legacy_data from registration.                                           |
| - Implement dual-factor admin adopt-legacy CLI command and endpoint verification.       |
| - Add TrustedHostMiddleware to main.py; remove legacy JobWorker instantiation.          |
| - Set COOKIE_SECURE=true; run test suites against temporary databases.                 |
+-----------------------------------------------------------------------------------------+
                                           |
                                [ GATE 1: USER APPROVAL ]
                                           v
+-----------------------------------------------------------------------------------------+
| PHASE 2: Windows Worker Task Scheduler Configuration                                    |
| - Add RotatingFileHandler logging to worker_daemon.py.                                  |
| - Create Task Scheduler task WebsiteLeadHunter-DurableWorker against test instance.     |
| - Test process kill, failure restart, and reboot recovery on disposable DB.             |
+-----------------------------------------------------------------------------------------+
                                           |
                                [ GATE 2: USER APPROVAL ]
                                           v
+-----------------------------------------------------------------------------------------+
| PHASE 3: Automated Backup & Restore Drill                                                |
| - Implement backend/scripts/backup.py with online backup, integrity check, & encryption.|
| - Execute automated restore test against disposable database; verify <60s criteria.     |
+-----------------------------------------------------------------------------------------+
                                           |
                                [ GATE 3: USER APPROVAL ]
                                           v
+-----------------------------------------------------------------------------------------+
| PHASE 4: Caddy Reverse Proxy & Tailscale Mesh Setup                                     |
| - Author Caddyfile (reverse proxy /api/*, serve frontend/dist, rewrite headers).        |
| - Bind Caddy to Tailscale MagicDNS domain with automated Let's Encrypt TLS.             |
| - Verify HTTPS handshake, phone PWA installation, and remote CLI connection.            |
+-----------------------------------------------------------------------------------------+
                                           |
                       [ GATE 4: FINAL REAL DATABASE GO-LIVE APPROVAL ]
                                           v
+-----------------------------------------------------------------------------------------+
| PHASE 5: Real Database Migration & Go-Live                                              |
| - Create verified cold backup copy of backend/websitelead_hunter.db.                    |
| - Rehearse migration v1->v2->v3->v4 on copy.                                            |
| - Prompt user for final confirmation.                                                   |
| - Start production Uvicorn and Task Scheduler worker; execute CLI adopt-legacy.         |
| - Verify 8 legacy leads, 1 job, and new multi-client operational dashboard.             |
+-----------------------------------------------------------------------------------------+
```

---

## 6. Real Database Preservation Confirmation

Before and after compiling this master plan, `backend/websitelead_hunter.db` was inspected read-only:
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

## 7. Commands Run and Final Git Status

### 7.1 Commands Run
- `git status --short`
- `python scratch/check_db.py`

### 7.2 Working Tree Status
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
No code modifications, network calls, or deployment tasks were initiated.
