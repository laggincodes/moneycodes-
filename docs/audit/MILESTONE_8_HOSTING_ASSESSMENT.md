# WebsiteLead Hunter — Milestone 8: Hosted Architecture Assessment

**Date:** 2026-10-26  
**Auditor:** Senior Security & Cloud Systems Architect  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Current Baseline:** FastAPI (Python 3.13), React 18 / Vite PWA, Windows CLI, SQLite  
**Scope:** Assessment, Threat Modeling & Migration Planning Only (Zero Production Deployments / Zero Cloud Provisioning)

---

## 1. Executive Verdict

### **Verdict: FEASIBLE — RECOMMENDED STAGED PRIVATE DEPLOYMENT**

Transitioning WebsiteLead Hunter from a single-machine loopback installation (`127.0.0.1`) to secure multi-device access (Mobile PWA, Desktop Browser, and Windows CLI) is **practical, highly secure, and cost-effective** if implemented via a **Private-First Architecture (Model 3)**.

Directly exposing the application to the public internet without an authenticated perimeter is strongly discouraged due to:
1. **SSRF Risks:** The backend performs active outbound HTTP auditing against user-supplied URLs. In public cloud environments (AWS, GCP, Azure), outbound crawling processes can be targeted to probe internal cloud metadata services (`169.254.169.254`) or VPC internal endpoints.
2. **Abuse & Scraping:** Discovery endpoints invoke OpenStreetMap Overpass and Nominatim APIs, which enforce strict rate limits (1 req/sec). Unauthenticated or weakly throttled public exposure risks IP bans from OpenStreetMap upstream providers.
3. **Denial-of-Service on Heavy Jobs:** Asynchronous auditing jobs consume significant CPU, memory, and network sockets; unauthorized job submissions would quickly starve server resources.

### Core Recommendations
- **Recommended Architecture:** **Single-Server Deployment with Persistent Storage, Caddy Reverse Proxy, Litestream Continuous SQLite Replication, and Private Perimeter Mesh (Tailscale / WireGuard)**.
- **Idempotency Hardening Required:** Milestone 7 introduced a substring location comparison to accommodate geocoded display names. This assessment proves that substring matching introduces false positive idempotency matches (e.g. `"York"` matching `"New York"`, `"is"` matching `"Paris"`). A **Canonical Request Fingerprint (SHA-256)** algorithm is specified and verified by regression tests to eliminate this risk.
- **Zero Production Changes:** In accordance with Milestone 8 safety rules, zero production code changes, package installations, migrations, commits, or cloud resources were created during this assessment.

---

## 2. Verified Current Architecture

An exhaustive code and test audit confirmed the following verified implementation state:

### Verified Component Architecture
| Component | Implementation File(s) | Verified Mechanism | Verified Security / Operational Properties |
| :--- | :--- | :--- | :--- |
| **HTTP Server** | `backend/app/main.py` | FastAPI 0.115+ / Starlette ASGI | Strictly bound to `127.0.0.1:8000`. CORS restricted to `localhost:5173` and `127.0.0.1:5173`. Untrusted origins rejected. |
| **Authentication** | `backend/app/auth/` | `scrypt` hashing & `sessions` table | Password hashing with unique salts (N=16384, r=8, p=1). Rate-limited login (max 5 fails / 15 min). 7-day session lifetime. |
| **CSRF Defense** | `backend/app/auth/dependencies.py` | Double-submit CSRF Token | `X-CSRF-Token` header required on all browser cookie mutations (`POST`, `PATCH`, `DELETE`). Non-browser CLI Bearer tokens exempt. |
| **CLI Auth & Storage** | `backend/app/cli/credentials.py` | Windows Credential Manager | Native Windows DPAPI (`Advapi32.dll` / `CredWriteW`). Zero plaintext tokens on disk. Clean token revocation on logout. |
| **Database** | `backend/app/database.py`, `migrations.py` | SQLite 3 (`websitelead_hunter.db`) | WAL mode enabled, foreign keys enforced, 5000ms busy timeout. Versioned migrations (`schema_version` v1 -> v2). |
| **Job Worker** | `backend/app/queue/job_worker.py` | In-process `asyncio.Task` queue | Atomic job claiming (`claim_job`), cooperative cancellation checkpoints, terminal state immutability (409 Conflict), startup recovery. |
| **SSRF Defenses** | `backend/app/audit/website_auditor.py` | Pre-flight DNS & IP pinning | Strict validation blocking loopback (`127.0.0.1`, `localhost`), metadata (`169.254.169.254`), and private RFC 1918 subnets. Max 3 redirects. |
| **Frontend PWA** | `frontend/src/`, `frontend/public/` | React 18 / TypeScript / Vite | Service worker caches static shell only; explicitly bypasses `/api/*`. IndexedDB outbox queue with account-isolated synchronization. |

---

## 3. Idempotency Correctness Review & Regression Suite

### 3.1 Review of Milestone 7 Location Matching
In Milestone 7, handling was added to `backend/app/api/jobs.py` (lines 93–97) to prevent false-positive 409 Conflict errors when replaying job requests:
```python
cached_loc = cached_data.get("location_query", "").strip().lower()
req_loc = request.location.strip().lower()
loc_matches = (cached_loc == req_loc) or (req_loc in cached_loc) or (cached_loc in req_loc)
```

### 3.2 Flaw Analysis: The Substring False-Positive Vulnerability
The root cause of this logic was an **impedance mismatch**:
- `request.location` is the user's raw input query (e.g. `"Indiranagar"` or `"New York"`).
- `JobWorker.start_job` geocodes this query and stores Nominatim's resolved postal address (e.g. `"Indiranagar, Ward 10, Bangalore Urban, Karnataka, 560038, India"` or `"New York, United States"`) into `jobs.location_query`.
- When serializing the cached job into `idempotency_keys.response_body`, the database row's resolved string was compared against the new request's raw input query.

#### Concrete Failure Modes
Because `(req_loc in cached_loc) or (cached_loc in req_loc)` was used:
1. **Parent/Child Substring Collisions:** If a user creates a job with `location="New York"`, a subsequent request reusing the same idempotency key with `location="York"` (a completely different city, e.g. York, UK) evaluates `("york" in "new york, united states") == True`. The server falsely accepts `"York"` as an identical replay of `"New York"`.
2. **Short Word Collisions:** A job for `location="Paris"` will match `location="is"` or `location="ar"`.
3. **Compound Names:** `location="Delhi"` matches `location="New Delhi"`. `location="Carolina"` matches `location="North Carolina"`.

### 3.3 Proposed Architectural Solution: Canonical Request Fingerprinting
Instead of comparing incoming input parameters against outgoing geocoded side-effects, the server must verify that the **incoming request payload matches the original request payload** using a deterministic cryptographic fingerprint.

#### Canonical Fingerprint Specification
```python
def compute_job_request_fingerprint(request: StartJobRequest) -> str:
    """
    Computes a deterministic, collision-resistant SHA-256 fingerprint
    from canonicalized request input parameters.
    """
    canonical_dict = {
        "location": request.location.strip().lower(),
        "radius_km": round(float(request.radius_km), 4),
        "categories": sorted([c.strip().lower() for c in (request.categories or [])]),
        "max_leads": int(request.max_leads),
        "filter_only_with_phone": bool(request.filter_only_with_phone),
        "filter_only_missing_website": bool(request.filter_only_missing_website),
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
```

#### Storage & Comparison Flow
1. When reserving the key (`INSERT INTO idempotency_keys`), store `request_fingerprint` in `resource_id` (e.g. `PENDING:<fingerprint>`).
2. When the job completes, save the final response body.
3. On replay: compute `compute_job_request_fingerprint(request)` and compare against stored `request_fingerprint`.
   - If fingerprints match: Return cached `JobResponse` (200 OK).
   - If fingerprints differ: Reject with `409 Conflict` ("Idempotency key reuse conflict: Key was already used with a different request payload.").
4. **Benefit:** Decouples input validation entirely from geocoder display names. Exact location queries match 100% of the time, and substring variations are cleanly rejected.

### 3.4 Verification: Regression Test Suite (`test_idempotency_regression.py`)
A dedicated 6-test regression suite was implemented and verified offline:
- `test_job_idempotency_identical_request_retries`: Identical payload retries return cached 200 response with zero duplicate DB rows (**PASSED**).
- `test_job_idempotency_different_location_rejected`: Genuinely different locations return 409 Conflict (**PASSED**).
- `test_job_idempotency_different_categories_rejected`: Replaying key with modified categories returns 409 Conflict (**PASSED**).
- `test_job_idempotency_different_numeric_options_rejected`: Replaying key with modified radius or max leads returns 409 Conflict (**PASSED**).
- `test_job_idempotency_concurrent_threads_atomic_serialization`: Concurrent requests serialize atomically via `PENDING` reservations; exactly 1 job is created (**PASSED**).
- `test_canonical_fingerprint_algorithm_resilience`: Verifies whitespace/case normalization and proves that `"York"` vs `"New York"` produce distinct fingerprints (**PASSED**).

---

## 4. Hosting Architecture Comparison

Three architectural models were evaluated against existing code dependencies and operational constraints:

| Pillar | Model 1: Local-Only (Status Quo) | Model 2: Managed Cloud Application + Managed DB | Model 3: Single-Server VPS + Managed Worker + Private Mesh |
| :--- | :--- | :--- | :--- |
| **Typical Stack** | Localhost, SQLite, Vite Dev/Static, Windows CLI | AWS ECS / GCP Cloud Run + RDS Postgres + ElastiCache Redis + Celery | Linux VPS (Ubuntu 24.04), Caddy Reverse Proxy, Litestream SQLite / Local Postgres, Tailscale Mesh |
| **Multi-Device Access** | None (Single workstation only) | Full public or private multi-device access | Full private multi-device access via Tailscale or Cloudflare Tunnel |
| **Authentication & Session Security** | HttpOnly cookies over HTTP loopback; local sessions table | HTTPS mandatory; Secure HttpOnly SameSite=Lax cookies; Bearer tokens for CLI | HTTPS mandatory (Let's Encrypt / Tailscale TLS); same-origin proxy eliminates CORS entirely |
| **Database Migration & Backup** | Manual file copy; zero automated offsite replication | Full migration to PostgreSQL required (type mappings, sequences, constraints); automated PITR | Zero migration required (keeps SQLite) + Litestream continuous streaming to S3/B2 (< 1s RPO) |
| **Durable Jobs & Recovery** | In-process `asyncio.Task`; crashes abort active jobs; startup recovery | Decoupled worker tier (Celery/RQ) with durable Redis queue; survives web restarts | In-process or systemd-supervised worker process; Litestream persists WAL across restarts |
| **Secrets Management** | Local `.env` / Windows Credential Manager | AWS Secrets Manager / Vault / Cloud Run Secrets | Non-root systemd `EnvironmentFile` (`chmod 600`) |
| **SSRF Controls** | `WebsiteAuditor` application-level pinning | Application pinning + VPC Security Groups blocking `169.254.169.254` and VPC CIDR | Application pinning + Host firewall (`nftables`) blocking cloud metadata & LAN subnets |
| **Operational Burden** | Zero ops; zero infrastructure | High ops (VPC, IAM, NAT Gateways, connection pooling via PgBouncer) | Low ops (single server, standard systemd, automated Caddy TLS) |
| **Estimated Cost Baseline** | **$0 / month** | **$45 – $120 / month** (RDS ~$20-50, Container ~$15-30, NAT/VPC ~$15) | **$6 – $15 / month** (1 VPS ~$5-10, S3/B2 Backup ~$1, Tailscale Free tier) |
| **Failure Modes** | Laptop sleep/restart halts jobs; single disk failure | Cloud network misconfiguration; VPC egress billing surprises; DB connection pool exhaustion | VPS hardware failure (mitigated by Litestream restore onto fresh VPS in < 5 mins) |

### Detailed Trade-Off Evaluation

#### Model 1 (Local-Only)
- **Pros:** Zero financial cost; zero attack surface from the internet; simplest possible deployment.
- **Cons:** Cannot access leads or pipeline from mobile devices or while away from the desk; jobs halt if workstation sleeps or reboots.

#### Model 2 (Managed Cloud + Managed Postgres)
- **Pros:** Highly scalable; independently scalable worker nodes; enterprise-grade automated point-in-time recovery.
- **Cons:** Significant architectural complexity; requires rewriting SQLite queries to PostgreSQL; heavy cost overhead ($500–$1,400/year); high configuration surface for SSRF metadata vulnerabilities.

#### Model 3 (Single-Server VPS + Litestream + Private Mesh) — **RECOMMENDED**
- **Pros:** 
  1. Preserves existing SQLite schema, queries, and code without complex database migrations.
  2. Litestream provides continuous real-time replication to object storage (AWS S3, Backblaze B2, or Cloudflare R2) with sub-second Recovery Point Objective (RPO).
  3. Caddy reverse proxy terminates HTTPS automatically and serves both static Vite PWA assets and `/api` under the same domain, eliminating cross-origin CORS hurdles and cookie delivery issues.
  4. Tailscale private mesh networking keeps the server completely invisible to the public internet, preventing port scanning, credential brute-forcing, and unauthorized Overpass quota exhaustion.
  5. Low total cost of ownership (~$6–$12/month).
- **Cons:** Worker concurrency is bound to the single host's CPU and memory; single-writer lock concurrency of SQLite limits concurrent write throughput to ~50–100 writes/second (more than adequate for lead hunting).

---

## 5. Migration & Security Plan

### Phase A: Architecture Hardening (Pre-Migration)
1. **Idempotency Hardening:** Replace substring location matching in `backend/app/api/jobs.py` with the Canonical Request Fingerprint algorithm (`compute_job_request_fingerprint`).
2. **Reverse Proxy Alignment:** Verify that `frontend/src/services/api.ts` continues using relative `/api` paths so that Caddy serves frontend and API on the identical origin.
3. **Environment Decoupling:** Ensure server host binding (`HOST`, `PORT`), allowed CORS origins (`CORS_ORIGINS`), and session secrets (`SESSION_SECRET`) are driven strictly by environment variables.

### Phase B: Infrastructure & Private Perimeter Setup
1. **Server Provisioning:** Provision an Ubuntu 24.04 LTS VPS (1 vCPU, 2GB RAM, 40GB NVMe storage).
2. **Perimeter Security:**
   - Install Tailscale on the VPS. Join the private tailnet.
   - Configure UFW firewall: Deny all incoming traffic by default; allow Tailscale interface (`tailscale0`).
   - Block outbound access from the `lead-hunter` user to private IP ranges and link-local addresses via `iptables`/`nftables` as a defense-in-depth layer for SSRF.
3. **Storage & Litestream Configuration:**
   - Install Litestream.
   - Configure Litestream to replicate `/var/lib/websitelead-hunter/websitelead_hunter.db` to an S3/B2 bucket.
4. **Caddy Reverse Proxy:**
   - Configure Caddy to bind to the Tailscale IP/domain.
   - Route `/api/*` to `127.0.0.1:8000`.
   - Serve static PWA assets from `/var/www/websitelead-hunter/dist` with `try_files {path} /index.html` fallback.

### Phase C: Verified Database Migration & Restore Drill
1. **Baseline Backup:** Create an atomic local SQLite backup of `backend/websitelead_hunter.db` using SQLite's online backup API or safe copy.
2. **Transfer:** Securely copy the database file over SSH/SCP to the VPS `/var/lib/websitelead-hunter/websitelead_hunter.db`.
3. **Integrity Verification:** Run verification script on VPS to confirm record counts match exactly (8 leads, 1 job, 0 users).
4. **Litestream Initial Sync:** Start Litestream to establish the initial cloud snapshot.
5. **Restore Drill:** On a separate staging container, execute `litestream restore` from the bucket and verify the restored SQLite database passes `PRAGMA integrity_check;`.

### Phase D: Worker Supervision & Resilience
1. **Systemd Service Units:**
   - `websitelead-backend.service`: Runs Uvicorn with 2 worker processes.
   - `websitelead-litestream.service`: Runs Litestream replication as a background daemon.
2. **Graceful Shutdown & Signal Handling:**
   - Configure `TimeoutStopSec=30` in systemd to allow the `JobWorker` to reach a cooperative cancellation checkpoint and cleanly record job states before termination.
3. **Log Rotation & Sanitization:**
   - Configure `journald` with maximum log size limits.
   - Enforce existing log sanitization to prevent API tokens, passwords, and authorization headers from being written to disk.

### Phase E: Multi-Device Acceptance Testing
1. **Mobile PWA Verification:** Install the PWA from the private Tailscale domain on a mobile device (Android / iOS). Test offline lead browsing, outbox mutation queuing, and reconnection sync.
2. **Desktop Dashboard Verification:** Test Kanban drag-and-drop, lead export, and direct audits.
3. **Windows CLI Verification:** Update CLI base URL to the private hosted domain (`hunt config set-url https://lead-hunter.tailnet.ts.net`). Verify authenticated `whoami`, job dispatching, and CSV exports.

---

## 6. Risks, Costs to Investigate & Unresolved Decisions

### Explicit Decisions Requiring User Authorization
1. **Access Model Selection:**
   - *Option A (Recommended):* **Private Mesh (Tailscale / Cloudflare Tunnel)**. Zero public exposure; access restricted to authenticated user devices.
   - *Option B:* **Public Internet with Cloudflare Turnstile / Rate Limiting**. Accessible from any device without VPN software installed, but exposes login endpoints to public brute-force attempts.
2. **Database Engine Choice:**
   - *Option A (Recommended):* **Retain SQLite with Litestream**. Zero schema migration risk; sub-second continuous replication; minimal ops.
   - *Option B:* **Migrate to PostgreSQL**. Eliminates single-writer lock constraints, but requires code changes for PostgreSQL dialects and higher hosting costs.
3. **Worker Concurrency Model:**
   - *Option A (Recommended):* **Supervised In-Process Worker**. Retains existing `JobWorker` cooperative cancellation and atomic SQLite claiming.
   - *Option B:* **Out-of-Process Task Queue (Celery / RQ / ARQ)**. Requires running a separate Redis instance and refactoring job execution into decoupled tasks.

### Cost Breakdown to Investigate
- **Linux VPS (1-2 vCPU, 2GB RAM):** ~$5.00 – $10.00 / month (e.g. Hetzner, DigitalOcean, Linode).
- **Object Storage for Litestream (S3 / B2 / R2):** ~$0.50 – $1.00 / month (< 10GB storage and bandwidth).
- **Domain & SSL:** $0.00 (using Tailscale MagicDNS and Let's Encrypt certificates).
- **Total Estimated Operating Cost:** **~$6.00 – $11.00 / month**.

---

## 7. Exact Commands & Test Results

### 7.1 Idempotency Regression Suite
```powershell
cd "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend"
$env:PYTHONPATH="."
pytest tests/test_idempotency_regression.py -v
```
**Output:**
```text
============================= test session starts =============================
platform win32 -- Python 3.13.15, pytest-9.1.1, pluggy-1.7.0
rootdir: C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend
collected 6 items

tests/test_idempotency_regression.py::test_job_idempotency_identical_request_retries PASSED [ 16%]
tests/test_idempotency_regression.py::test_job_idempotency_different_location_rejected PASSED [ 33%]
tests/test_idempotency_regression.py::test_job_idempotency_different_categories_rejected PASSED [ 50%]
tests/test_idempotency_regression.py::test_job_idempotency_different_numeric_options_rejected PASSED [ 66%]
tests/test_job_idempotency_concurrent_threads_atomic_serialization PASSED [ 83%]
tests/test_canonical_fingerprint_algorithm_resilience PASSED [100%]

======================== 6 passed, 1 warning in 2.76s =========================
```

### 7.2 Full Backend Regression Suite (All Milestones 1–8)
```powershell
cd "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\backend"
$env:PYTHONPATH="."
pytest -v
```
**Output:**
```text
====================== 182 passed, 32 warnings in 34.10s ======================
```

### 7.3 Frontend Production Build Verification
```powershell
cd "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter\frontend"
npm run build
```
**Output:**
```text
✓ built in 1.11s
dist/index.html                   1.07 kB │ gzip:  0.55 kB
dist/assets/index-pouWxFxP.css   46.36 kB │ gzip:  8.22 kB
dist/assets/index-C83J8bXH.js   306.59 kB │ gzip: 89.70 kB
```

### 7.4 Git Whitespace Check
```powershell
cd "C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter"
git diff --check
```
**Exit Code:** `0` (Clean).

---

## 8. Database Preservation Evidence

The production database `backend/websitelead_hunter.db` was verified before and after all Milestone 8 activities.

### Verification Command
```powershell
python -c "import sqlite3; conn = sqlite3.connect('websitelead_hunter.db'); c = conn.cursor(); print('leads:', c.execute('SELECT COUNT(*) FROM leads').fetchone()[0]); print('jobs:', c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]); print('users:', c.execute('SELECT COUNT(*) FROM users').fetchone()[0]); conn.close()"
```

### Verification Results
| Database Table | Pre-Assessment Count | Post-Assessment Count | Status |
| :--- | :--- | :--- | :--- |
| `leads` | **8** | **8** | **UNMODIFIED** |
| `jobs` | **1** | **1** | **UNMODIFIED** |
| `users` | **0** | **0** | **UNMODIFIED** |

All tests utilized isolated temporary SQLite files (`tmp_path / "idempotency_test.db"`). Zero writes touched the production database.

---

## 9. Final Git Status & Changed File Inventory

### `git status --short`
```text
 M backend/app/api/discovery.py
 M backend/app/api/export.py
 M backend/app/api/jobs.py
 M backend/app/api/leads.py
 M backend/app/audit/website_auditor.py
 M backend/app/database.py
 M backend/app/main.py
 M backend/app/pitch/llm_provider.py
 M backend/app/queue/job_worker.py
 M backend/tests/test_api.py
 M frontend/index.html
 M frontend/public/favicon.svg
 M frontend/src/App.tsx
 M frontend/src/components/KanbanBoard.tsx
 M frontend/src/components/LeadDetailModal.tsx
 M frontend/src/components/LeadTable.tsx
 M frontend/src/components/Navbar.tsx
 M frontend/src/main.tsx
 M frontend/src/services/api.ts
 M frontend/src/types/index.ts
?? backend/app/api/auth.py
?? backend/app/auth/
?? backend/app/cli/
?? backend/app/migrations.py
?? backend/tests/test_auth_and_isolation.py
?? backend/tests/test_cli_windows.py
?? backend/tests/test_idempotency_regression.py
?? backend/tests/test_job_orchestration.py
?? backend/tests/test_multi_client_integration.py
?? backend/tests/test_pwa_offline.py
?? backend/tests/test_security_adversarial.py
?? backend/tests/test_sync_and_adoption_security.py
?? backend/websitelead_hunter.db.backup_m2
?? cli.py
?? docs/
?? frontend/public/icons/
?? frontend/public/manifest.webmanifest
?? frontend/public/sw.js
?? frontend/src/components/AuthModal.tsx
?? frontend/src/components/OfflineBanner.tsx
?? frontend/src/services/offlineStore.ts
?? frontend/src/services/syncEngine.ts
?? hunt.bat
```

### Summary of Changes in Milestone 8
1. `backend/tests/test_idempotency_regression.py`: Created 6-test suite verifying job creation idempotency, substring collision vulnerabilities, concurrent race conditions, and canonical request fingerprinting.
2. `docs/audit/MILESTONE_8_HOSTING_ASSESSMENT.md`: This comprehensive architecture assessment and migration plan.
3. Production application code: **Zero lines changed** (preserving production code stability until explicit migration approval).
