# WebsiteLead Hunter — Milestone 9C.2: Retry Exhaustion & Crash Recovery Verification

**Date:** October 26, 2026  
**Auditor:** Principal Systems & Concurrency Engineer  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Target Milestone:** Milestone 9C.2 (Retry Limits and 6 Crash-Recovery Boundaries Verification)  
**Safety Mandate:** Offline verification only; zero cloud provisioning; zero network requests; zero live scraping; real SQLite database preserved intact (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 1. Executive Summary & Verification Verdict

### 1.1 Verdict: **PASS — FULLY VERIFIED**

The durable worker architecture has been independently verified across strict retry exhaustion limits and all six crash-recovery boundaries. All tests were conducted against isolated temporary SQLite databases with controlled timestamps and both simulated in-process exceptions and an actual OS process-kill drill (`SIGKILL` via `proc.kill()`).

Key verifications and architectural guarantees achieved:
1. **Source-Grounded Retry Semantics:**
   - Attempt 1 is the initial execution (`retry_count = 0`).
   - `max_retries = 0` permits zero retries. Total executions = 1.
   - `max_retries = 1` permits one retry. Total executions = 2.
   - Total permitted executions strictly equals `1 + max_retries`.
   - **No Inference from Lease Versions:** Claims increment monotonic `lease_version` to provide fencing tokens. In contrast, `retry_count` is incremented **if and only if** the claimed job was an abandoned running lease (`status = 'running' AND lease_expires_at < now`). Paused jobs, fresh pending jobs, or healthy lease renewals never consume retries.
2. **Atomic Single-Statement Limit Enforcement:**
   - Updated `claim_next_job` to conditionally evaluate `retry_count + 1 > max_retries` within the single SQLite atomic `UPDATE ... RETURNING` query.
   - If an abandoned job has reached its limit upon claim, SQLite atomically sets `status = 'failed'`, records an explicit descriptive `error_message`, fences out stale workers, and prevents any worker from executing it.
   - Concurrent workers competing for an exhausted job cannot bypass the limit; exactly one worker records the failure, and zero workers claim it as running.
3. **Six Crash-Recovery Boundaries Verified:**
   - Each boundary was isolated and tested independently with simulated crashes.
   - An actual Windows OS child process-kill test was executed, validating that violent OS termination during auditing preserves `checkpoint_data` and allows subsequent workers to resume without re-running discovery or duplicating leads.
4. **Zero Test Regressions & Clean Builds:**
   - Durable worker & retry matrix suite: **29/29 passed** (13 original durable worker + 16 new matrix tests).
   - Windows CLI & Idempotency suite: **34/34 passed**.
   - Full backend test suite: **217/217 passed** (100% pass rate).
   - Frontend Vite PWA build: Succeeded with 0 errors in 1.22s.
5. **Real Database Unconditionally Preserved:**
   - `backend/websitelead_hunter.db` was checked and verified before and after all drills: exactly 8 leads, 1 job, 0 users, schema_migrations=[1].

---

## 2. Retry Semantics & Atomic Implementation

### 2.1 Formal Retry Definition

The durable worker follows strict deterministic retry mathematics:
- **Attempt 1 (Initial Claim):** Job claimed from `status = 'pending'`. Starts with `retry_count = 0`.
- **Permitted Retries:** `max_retries` specifies how many additional recovery attempts are allowed after a crash or lease expiration.
- **Total Permitted Executions:** `1 + max_retries`.
- **Exhaustion Trigger:** An expired lease reclamation attempt where `retry_count + 1 > max_retries` triggers immediate transition to `status = 'failed'` with `error_message = 'Job failed: Exceeded maximum lease recovery retry attempts (N).'`.

### 2.2 Atomic Single-Query Claim Enforcement

In [`backend/app/queue/durable_worker.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/queue/durable_worker.py#L188-L260), `claim_next_job` executes atomic conditional updates directly in SQLite:

```sql
UPDATE jobs
SET 
    status = CASE
        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) > MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) THEN 'failed'
        ELSE 'running'
    END,
    error_message = CASE
        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) > MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) 
            THEN 'Job failed: Exceeded maximum lease recovery retry attempts (' || MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) || ').'
        WHEN status = 'pending' THEN NULL
        ELSE error_message
    END,
    retry_count = CASE
        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) <= MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) 
            THEN COALESCE(retry_count, 0) + 1
        ELSE COALESCE(retry_count, 0)
    END,
    worker_id = ?,
    lease_version = lease_version + 1,
    heartbeat_at = ?,
    lease_expires_at = ?,
    updated_at = ?
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'pending'
       OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
    ORDER BY created_at ASC
    LIMIT 1
)
RETURNING id, user_id, status, location_query, lat, lon, radius_km,
          categories, max_leads, total_found, processed_count,
          logs, worker_id, lease_version, lease_expires_at,
          retry_count, max_retries, checkpoint_data, error_message;
```

#### Why This Design Guarantees Invariants
1. **Pre-Update Status Introspection:** In SQLite `UPDATE ... SET`, column references on the right-hand side evaluate the row's pre-update values. `status = 'running'` evaluates to `True` **only** if the job was previously running with an expired lease.
2. **Fresh Pending Exemption:** If `status = 'pending'`, `WHEN status = 'running'` evaluates to `False`. The job enters `status = 'running'` while `retry_count` remains 0.
3. **No Inference from Lease Versions:** Claims increment monotonic `lease_version = lease_version + 1` purely for fencing tokens. Resuming a paused job or claiming fresh work does not increment `retry_count`.
4. **Sanitized Input Resilience:** `MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER))` safely clamps negative values (`-1` -> 0), handles malformed strings (`"abc"` -> 0), and defaults `NULL` to 3.

---

## 3. Part A: Retry Semantics Verification Matrix

All 10 scenarios were implemented as automated unit tests in [`backend/tests/test_retry_and_recovery_matrix.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/tests/test_retry_and_recovery_matrix.py):

| # | Test Scenario | Pre-Conditions | Claim / Action | Expected Result | Verified Result | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **Initial Job Claim** | `status='pending'`, `retry_count=0`, `max_retries=3` | Worker 1 claims job | `status='running'`, `retry_count=0`, `lease_version=1`. Attempt 1. | Exactly matched. In DB: `status='running'`, `retry_count=0`. | **PASS** |
| **2** | **Retry Limit Zero** | `max_retries=0`, `retry_count=0` | Attempt 1 runs; worker crashes; Worker 2 claims | Claim returns `None`. DB marks `status='failed'`, `error_message` records limit exceeded (0). Total executions = 1. | Job failed immediately on first recovery attempt. Zero retries permitted. | **PASS** |
| **3** | **Retry Limit One** | `max_retries=1`, `retry_count=0` | Attempt 1 crashes; Worker 2 recovers; Worker 2 crashes; Worker 3 claims | Claim 2 succeeds with `retry_count=1`. Claim 3 returns `None`, marks job `failed`. Total executions = 2 (1 initial + 1 retry). | Exactly 2 executions permitted. Third attempt marked failed. | **PASS** |
| **4** | **Retry Limit Two** | `max_retries=2`, `retry_count=0` | Attempt 1 crashes; Recv 1 crashes; Recv 2 crashes; Recv 3 claims | Recv 1 has `retry_count=1`. Recv 2 has `retry_count=2`. Recv 3 marks job `failed`. Total executions = 3 (1 initial + 2 retries). | Exactly 3 executions permitted. Fourth attempt marked failed. | **PASS** |
| **5** | **Healthy Lease Renewal** | `status='running'`, `retry_count=0` | Worker calls `renew_lease` 5 consecutive times | `heartbeat_at` advances. `retry_count` remains 0. | `retry_count` stayed 0 across all renewals. | **PASS** |
| **6** | **Expired Lease Counted Once** | `lease_expires_at` in past | Worker 2 reclaims; sends logs & heartbeats | `retry_count` increments from 0 to 1 once upon claim. Operations do not increment. | `retry_count` remained 1 throughout Worker 2's execution. | **PASS** |
| **7** | **Concurrent Worker Limit** | `retry_count=1`, `max_retries=1`, expired lease | 5 concurrent threads/workers attempt claim simultaneously | Exactly 1 worker transitions job to `failed`. 0 workers claim it as running. | 0 workers claimed job. DB row is `failed`. Race condition prevented. | **PASS** |
| **8** | **Stale Worker Mutation Rejection** | Worker 1 lost lease to Worker 2 (`lease_version=2`) | Stale Worker 1 attempts `renew_lease`, `persist_lead_fenced`, or status update | All mutations rejected (rowcount=0 or `FencingRejectionError`). | Worker 1 unable to mutate DB. Worker 2 ownership preserved. | **PASS** |
| **9** | **Terminal Jobs Non-Reclaimable** | `completed`, `cancelled`, and `failed` jobs with expired timestamps | Worker attempts `claim_next_job` | Returns `None`. Terminal jobs are never re-queued. | `claim_next_job` returned `None`. | **PASS** |
| **10** | **Malformed Retry Settings** | `max_retries = -1`, `"invalid"`, `0` | Workers claim jobs | `-1` clamped to 0 (fails immediately on recovery). `"invalid"` CASTs to 0. Safe execution without exceptions. | Handled gracefully with zero unhandled exceptions. | **PASS** |

---

## 4. Part B: Six Crash-Recovery Boundaries

Each boundary was tested independently in [`backend/tests/test_retry_and_recovery_matrix.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/tests/test_retry_and_recovery_matrix.py) using mock discovery and audit recorders.

### Boundary 1: Crash After Claim, Before Discovery
- **Crash Simulation Method:** Worker 1 claims job; process terminates immediately before calling `discovery_provider.discover()`. Controlled lease expiration simulated in temporary database.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = NULL`, `lease_version = 1`, `retry_count = 0`, `leads = 0`.
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:** Checkpoint was empty, so discovery ran for Worker 2 (call count = 1). Audited 2 candidates.
- **Persisted Lead Counts:** Before: 0 | After: 2.
- **Duplicate Prevention Evidence:** Zero duplicate provider IDs.
- **Final Outcome:** `completed`.

### Boundary 2: Crash During Discovery Before Checkpoint Persistence
- **Crash Simulation Method:** Discovery is executing or just finished in memory, but process crashes before `update_checkpoint()` is committed.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = NULL`, `total_found = 0`, `lease_version = 1`, `retry_count = 0`, `leads = 0`.
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:** Because checkpoint was not committed, Worker 2 executed discovery once, saved checkpoint, and audited candidates.
- **Persisted Lead Counts:** Before: 0 | After: 2.
- **Duplicate Prevention Evidence:** Zero duplicates persisted.
- **Final Outcome:** `completed`.

### Boundary 3: Crash After Checkpoint Persistence, Before First Lead
- **Crash Simulation Method:** Checkpoint committed with 2 candidates. Worker 1 crashes before auditing or persisting Candidate 1.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = JSON(2 candidates)`, `total_found = 2`, `processed_count = 0`, `lease_version = 1`, `retry_count = 0`, `leads = 0`.
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:** **Discovery DID NOT repeat!** `mock_disc.call_count == 0`. Candidates were loaded directly from `checkpoint_data`. Both candidates audited.
- **Persisted Lead Counts:** Before: 0 | After: 2.
- **Duplicate Prevention Evidence:** Zero duplicate provider IDs.
- **Final Outcome:** `completed`.

### Boundary 4: Crash After Audit Finishes, Before Lead Persistence
- **Crash Simulation Method:** Candidate 1 audit finished in memory, but process crashed before `persist_lead_fenced` was called.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = JSON(2 candidates)`, `leads = 0` (Candidate 1 uncommitted).
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:** Discovery did not repeat. Candidate 1 was not in `leads`, so Worker 2 audited both candidates and persisted both.
- **Persisted Lead Counts:** Before: 0 | After: 2.
- **Duplicate Prevention Evidence:** Zero duplicates.
- **Final Outcome:** `completed`.

### Boundary 5: Crash After Lead Persistence, Before Next Lead
- **Crash Simulation Method:** Candidate 1 was audited and persisted in SQLite (`leads = 1`, `processed_count = 1`). Worker 1 crashed before Candidate 2 was audited.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = JSON(2 candidates)`, `leads = 1` (`biz_osm_40` committed), `processed_count = 1`.
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:**
  - Discovery did not repeat (`mock_disc.call_count == 0`).
  - **Candidate 1 audit DID NOT repeat!** Worker 2 queried existing `leads` by `job_id`, detected `biz_osm_40` was already persisted, and skipped auditing `https://cafe40.com`. Only `https://cafe41.com` was audited (`audited_urls == ['https://cafe41.com']`).
- **Persisted Lead Counts:** Before: 1 | After: 2.
- **Duplicate Prevention Evidence:** Database query confirmed exactly 2 rows with distinct provider IDs `biz_osm_40` and `biz_osm_41`.
- **Final Outcome:** `completed`.

### Boundary 6: Crash During Final Completion
- **Crash Simulation Method:** All candidates (1 & 2) audited and persisted in SQLite (`leads = 2`, `processed_count = 2`). Worker 1 crashed immediately before executing `UPDATE jobs SET status = 'completed'`.
- **Job State & Checkpoint Before Recovery:** `status = 'running'`, `checkpoint_data = JSON(2 candidates)`, `leads = 2`, `processed_count = 2`, `lease_version = 1`, `retry_count = 0`.
- **Lease Version & Retry Count:**
  - Before: `lease_version = 1`, `retry_count = 0`
  - After: `lease_version = 2`, `retry_count = 1`
- **Repetition Check:**
  - Discovery did not repeat (`mock_disc.call_count == 0`).
  - **Zero audits repeated!** `len(auditor.audited_urls) == 0`. Worker 2 observed that all candidates were already persisted, skipped the audit loop, and immediately marked the job completed.
- **Persisted Lead Counts:** Before: 2 | After: 2.
- **Duplicate Prevention Evidence:** Exactly 2 leads in database.
- **Final Outcome:** `completed`.

---

## 5. Actual OS Process-Kill Drill vs Simulated Crash

In strict compliance with audit safety requirements, simulated crashes were explicitly distinguished from real OS process terminations.

An actual operating system process-kill test was authored and executed in [`scratch/test_real_os_process_kill.py`](file:///C:/Users/Yatha/.gemini/antigravity/brain/8e9f842c-e890-4e32-be88-3329601bd3bf/scratch/test_real_os_process_kill.py):

### Drill Execution Log
```
=== LIVE OS PROCESS KILL & RECOVERY TEST ===
Step 1: Spawning child worker process...
Child PID: 17300
Step 2: Child process confirmed to have checkpointed and begun auditing.
Step 3: Violently killing child process (SIGKILL / proc.kill())...
Child terminated! Return code: 1
Post-kill Job State: status=running, lease_version=1, retry_count=0
Post-kill Checkpoint Data: {"candidates": [{"provider_id": "proc_osm_1", "name": "Proc ...
Pre-recovery persisted leads: 0
Step 4: Worker 2 reclaiming job after real process crash...
Worker 2 completed recovery! Audited URLs: ['https://proccafe1.com', 'https://proccafe2.com']
Final Job State: status=completed, lease_version=2, retry_count=1
Final Persisted Leads: 2

>>> LIVE OS PROCESS KILL & RECOVERY DRILL SUCCEEDED! <<<
```

### Analysis of Live Process Kill
1. **No Finally Block Execution:** Violent OS termination (`proc.kill()`) terminated the child worker process without executing `finally:` blocks or database writes. The job was left in SQLite in a dirty state (`status = 'running'`, `lease_version = 1`, `retry_count = 0`).
2. **Lease Expiry Reclaim:** After lease expiry, Worker 2 cleanly claimed ownership (`lease_version = 2`, `retry_count = 1`).
3. **Flawless Resumption:** Worker 2 verified `checkpoint_data` written by the dead child process before its demise, bypassed Overpass discovery, completed audits, and finalized the job with exactly 2 leads.

---

## 6. Real Database Isolation Confirmation

Before and after all audit drills and test executions, `backend/websitelead_hunter.db` was inspected read-only:
```powershell
python scratch/check_db.py
```
**Database Counts:**
- `jobs`: Exactly **1** row (original legacy job)
- `leads`: Exactly **8** rows (original legacy leads)
- `users`: Exactly **0** rows
- `sessions`: Exactly **0** rows
- `schema_migrations`: Exactly **1** row (`version: 1`)

**Formal Isolation Confirmation:**
- Zero test data was written to `websitelead_hunter.db`.
- No migrations were executed against `websitelead_hunter.db`.
- Zero resets, deletions, or schema modifications occurred.
- All tests used Python `tempfile.mkdtemp()` or Pytest `tmp_path` fixtures with automatic teardown.

---

## 7. Test Suite Execution & Build Evidence

### 7.1 Durable Worker & Retry Matrix Tests
```powershell
cd backend; $env:PYTHONPATH="."; pytest tests/test_durable_worker.py tests/test_retry_and_recovery_matrix.py -v
```
**Result:** **29 passed** in 2.55s.

### 7.2 Windows CLI & Idempotency Suite
```powershell
cd backend; $env:PYTHONPATH="."; pytest tests/test_cli_windows.py -v
```
**Result:** **34 passed**, 1 warning in 4.70s.

### 7.3 Full Backend Test Suite
```powershell
cd backend; $env:PYTHONPATH="."; pytest -v
```
**Result:** **217 passed**, 32 warnings in 21.18s.

### 7.4 Frontend Vite Build
```powershell
cd frontend; npm run build
```
**Result:**
```
✓ 1945 modules transformed.
dist/index.html                   1.07 kB │ gzip:  0.55 kB
dist/assets/index-pouWxFxP.css   46.36 kB │ gzip:  8.22 kB
dist/assets/index-C83J8bXH.js   306.59 kB │ gzip: 89.70 kB
✓ built in 1.22s
```

### 7.5 Git Diff Check
```powershell
git diff --check
```
**Result:** Code 0 (zero whitespace, line ending, or conflict markers).

---

## 8. Git Status & Working Tree Summary

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

---

## 9. Remaining Risks & Recommended Next Step

### 9.1 Remaining Risks
1. **Overpass Public Rate Limits:** In production, if multiple jobs are triggered in rapid succession, public Overpass instances may return HTTP 429. The worker records discovery errors and marks the job failed; backoff or secondary OSM mirrors could be added in future enhancement milestones.
2. **Unattended Windows Power Management:** On battery power, Windows Modern Standby may throttle network adapters even when `SetThreadExecutionState` is active. For production runs, the host machine should be plugged into AC power.

### 9.2 Recommended Next Step
As requested by the mandate, **STOP after Milestone 9C.2**.  
Do not begin hosting, tunneling, or remote-access setup until explicitly directed. The standalone durable worker, crash boundaries, fencing tokens, and retry limits are 100% hardened and verified.
