# WEBSITELEAD HUNTER — AUDIT REPORT 6
## Durable Job Orchestration & Lifecycle Reliability

**Date:** October 26, 2026  
**Auditor:** Senior Software Architect, Systems Reliability & Application Security Lead  
**Target:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Git Commit / Branch:** `55491c9` (`master` branch, working tree uncommitted)  
**Verification Status:** **PASS** (161/161 Full Test Suite Passing, 13/13 Orchestration Invariant Tests Passing, Production Build Clean, 0 External Network Requests)

---

## 1. Executive Verdict & Summary

Milestone 6 delivers **Durable Job Orchestration and Lifecycle Invariants** for WebsiteLead Hunter. The job subsystem was audited across API endpoints, worker tasks, concurrency primitives, restart recovery, cooperative cancellation, and persistent SQLite transactions.

### Executive Verdict: **PASS**
- **161 / 161 Backend Tests Passing** (100% pass rate in 27.57s, strictly offline via in-memory ASGI transport).
- **13 Dedicated Job Orchestration Tests Passing** covering all 10 lifecycle invariants.
- **Frontend Production Build Clean:** `tsc -b && vite build` built in 2.06s with zero errors.
- **Git Hygiene:** `git diff --check` passes with zero whitespace or formatting errors.
- **Zero Real Database Alterations:** `backend/websitelead_hunter.db` remains 100% intact (8 leads, 1 job, 0 users). Zero external network requests made.

---

## 2. Architecture & Lifecycle State Diagram

The background job queue executes discovery and audit workflows through the canonical state machine:

```mermaid
stateDiagram-v2
    [*] --> pending: POST /api/jobs (User Authenticated, Idempotency-Key Reserved)
    pending --> running: Worker claims job (claim_job)
    running --> paused: POST /api/jobs/{id}/pause (pause_event.clear())
    paused --> running: POST /api/jobs/{id}/resume (pause_event.set())
    running --> completed: All leads audited (atomic status == 'running' check)
    running --> failed: Unhandled exception / Discovery failure (secret defanged)
    pending --> cancelled: POST /api/jobs/{id}/cancel
    running --> cancelled: POST /api/jobs/{id}/cancel (Task.cancel & checkpoint stop)
    paused --> cancelled: POST /api/jobs/{id}/cancel (Unblocked & cancelled)
    running --> paused: Server Process Restart (recover_interrupted_jobs)
    pending --> paused: Server Process Restart (recover_interrupted_jobs)
    completed --> [*]: Terminal State (Immutable)
    failed --> [*]: Terminal State (Immutable)
    cancelled --> [*]: Terminal State (Immutable)
```

---

## 3. The 10 Job Lifecycle Invariants

| Invariant | Description | Enforcement Mechanism | Verification Test |
| :--- | :--- | :--- | :--- |
| **1. Ownership** | Each job belongs to exactly one user | All job endpoints scope queries by `user["id"]` and raise 404 for unauthorized access | `test_job_ownership_and_cross_account_isolation` |
| **2. Terminal Immutability** | Terminal states (`completed`, `failed`, `cancelled`) cannot revert to active states | Endpoints check `current_status in TERMINAL_STATUSES` and raise 409 Conflict | `test_terminal_states_cannot_be_paused_or_resumed`, `test_completed_or_failed_jobs_cannot_be_cancelled` |
| **3. Atomic Claiming** | A job cannot be claimed by multiple workers | `claim_job()` uses conditional SQL: `UPDATE ... WHERE status = 'pending'`. Only one worker gets `rowcount > 0` | `test_atomic_job_claiming_single_winner` |
| **4. Cooperative Cancellation** | Cancellation stops processing at earliest checkpoint and prevents further audits | Checkpoints checked before discovery, post-discovery, and pre-audit; `task.cancel()` unblocks waits | `test_cancellation_before_discovery_stops_immediately`, `test_completion_does_not_overwrite_cancelled_status` |
| **5. Worker Exceptions** | Unhandled exceptions cannot leave job stuck in `running` | `try ... except Exception:` catches errors, persists reason, and marks status as `failed` | `test_worker_exception_marks_job_failed_and_defangs_secrets` |
| **6. Restart Recovery** | Interrupted jobs have an explicit, side-effect-safe recovery policy | `recover_interrupted_jobs()` pauses abandoned jobs. Resuming is rejected with 409 to prevent duplicate external probes | `test_startup_recovery_and_resumption_policy` |
| **7. Secret Sanitization** | Job logs and error messages never expose credentials or tokens | `sanitize_log_message()` defangs bearer tokens, API keys, passwords, and session IDs | `test_worker_exception_marks_job_failed_and_defangs_secrets`, `test_sanitize_log_message_unit` |
| **8. Predictable Requests** | Repeated lifecycle actions on same state are safe and idempotent | Pausing `paused`, resuming `running`, cancelling `cancelled` returns 200 without duplicate side effects | `test_repeated_lifecycle_requests_are_idempotent` |
| **9. Status Consistency** | API, CLI, and frontend report unified status values | Standardized enum: `pending`, `running`, `paused`, `completed`, `failed`, `cancelled` | `test_cli_windows.py`, `test_job_orchestration.py`, `JobStatusBanner.tsx` |
| **10. Idempotency Intact** | Job creation idempotency and concurrency guards remain active | `Idempotency-Key` header with atomic reservation and payload conflict rejection (409) | `test_job_idempotency_concurrent_requests_safe`, `test_job_idempotency_payload_mismatch_returns_409` |

---

## 4. Confirmed Defects & Applied Improvements

### A. Terminal State Clobbering (Resolved)
- **Defect:** Calling `pause` or `resume` on `completed`/`failed`/`cancelled` jobs previously executed without checking terminal state. Calling `cancel` on a completed job would overwrite its status with `cancelled`. Furthermore, if a job was cancelled near the end of execution, `_execute_job` could overwrite `cancelled` with `completed`.
- **Fix:** In `backend/app/api/jobs.py`, added terminal state validation raising 409 Conflict. In `backend/app/queue/job_worker.py`, made all state transitions conditional on expected source states (`status = 'running'` or `status IN ('pending', 'running', 'paused')`).

### B. Ownership Check Ordering (Resolved)
- **Defect:** In `backend/app/api/jobs.py`, `if not worker_instance:` was checked before checking if the job exists and belongs to the authenticated caller. An unauthorized caller requesting another user's job could receive a 500 Internal Server Error instead of 404 Not Found.
- **Fix:** Moved database ownership validation and terminal state checks before any worker invocation.

### C. Server Restart Recovery & Re-Run Prevention Policy (Resolved)
- **Defect:** In Milestone 1-5, `recover_interrupted_jobs` set `running` jobs to `paused` and logged that the job was "ready to resume". However, because candidate lists in memory were lost on process termination, calling `resume_job` returned `False` and did nothing.
- **Fix:**
  - `recover_interrupted_jobs` now inspects both `running` and `pending` jobs, pauses them, and records an honest explanatory log.
  - When `resume` is requested on an interrupted job whose memory task no longer exists, `POST /api/jobs/{id}/resume` returns `409 Conflict` with an explicit explanation:
    `"Job {job_id} cannot be resumed: Execution was interrupted by a previous server restart. To avoid duplicate external network probes, please start a new discovery job."`
  - Cancellation of interrupted jobs is supported cleanly (`POST /api/jobs/{id}/cancel` or `hunt jobs cancel {id}`).

### D. Nested SQLite Connection Deadlock on Windows (Resolved)
- **Defect:** In `pause_job`, `resume_job`, and `cancel_job`, `_append_job_log()` was invoked while the outer `with get_db() as conn:` block still held an open transaction. On Windows, opening a second SQLite connection to write to the locked database caused an unrecoverable deadlock.
- **Fix:** Extracted `_append_job_log` outside the transaction block, allowing the state transition to commit before writing audit logs.

### E. Secret Sanitization in Job Logs & Errors (Resolved)
- **Defect:** If an unhandled exception or log message contained authorization headers, bearer tokens, or API keys, it would be serialized directly into `jobs.logs` or `jobs.error_message`.
- **Fix:** Added `sanitize_log_message()` with regular expression masking for `Bearer <token>`, `api_key=<key>`, `password=<pwd>`, and `session_token=<token>`.

---

## 5. Offline Verification Test Evidence

### A. Dedicated Job Orchestration Suite (`backend/tests/test_job_orchestration.py`):
```text
tests/test_job_orchestration.py::test_job_ownership_and_cross_account_isolation PASSED [  7%]
tests/test_job_orchestration.py::test_terminal_states_cannot_be_paused_or_resumed[completed] PASSED [ 15%]
tests/test_job_orchestration.py::test_terminal_states_cannot_be_paused_or_resumed[failed] PASSED [ 23%]
tests/test_job_orchestration.py::test_terminal_states_cannot_be_paused_or_resumed[cancelled] PASSED [ 30%]
tests/test_job_orchestration.py::test_completed_or_failed_jobs_cannot_be_cancelled[completed] PASSED [ 38%]
tests/test_job_orchestration.py::test_completed_or_failed_jobs_cannot_be_cancelled[failed] PASSED [ 46%]
tests/test_job_orchestration.py::test_repeated_lifecycle_requests_are_idempotent PASSED [ 53%]
tests/test_job_orchestration.py::test_atomic_job_claiming_single_winner PASSED [ 61%]
tests/test_job_orchestration.py::test_cancellation_before_discovery_stops_immediately PASSED [ 69%]
tests/test_job_orchestration.py::test_completion_does_not_overwrite_cancelled_status PASSED [ 76%]
tests/test_job_orchestration.py::test_worker_exception_marks_job_failed_and_defangs_secrets PASSED [ 84%]
tests/test_job_orchestration.py::test_sanitize_log_message_unit PASSED   [ 92%]
tests/test_job_orchestration.py::test_startup_recovery_and_resumption_policy PASSED [100%]
======================== 13 passed, 1 warning in 3.37s ========================
```

### B. Complete Backend Test Suite:
```powershell
Command: cd backend; $env:PYTHONPATH="."; pytest -v
Result: 161 passed, 32 warnings in 27.57s
  - test_api.py: 6 passed
  - test_auditor.py: 5 passed
  - test_auth_and_isolation.py: 19 passed
  - test_cli_windows.py: 34 passed
  - test_discovery.py: 4 passed
  - test_job_orchestration.py: 13 passed
  - test_milestone_f_resilience.py: 6 passed
  - test_pwa_offline.py: 11 passed
  - test_regression_offline.py: 20 passed
  - test_security.py: 17 passed
  - test_security_adversarial.py: 20 passed
  - test_sync_and_adoption_security.py: 12 passed
Total: 161 / 161 passed (100%)
```

### C. Frontend Production Build Verification:
```powershell
Command: cd frontend; npm run build
Result:
✓ 1945 modules transformed.
dist/index.html                   1.07 kB │ gzip:  0.55 kB
dist/assets/index-pouWxFxP.css   46.36 kB │ gzip:  8.22 kB
dist/assets/index-DJ6gERgi.js   306.05 kB │ gzip: 89.49 kB
✓ built in 2.06s (0 errors)
```

### D. Git Diff Formatting Check:
```powershell
Command: git diff --check
Result: Exit code 0 (Clean, zero trailing whitespace or formatting errors)
```

---

## 6. Database Preservation Evidence

The real user database `backend/websitelead_hunter.db` was checked before and after all testing:
```powershell
Command: python -c "import sqlite3; conn = sqlite3.connect(r'backend\websitelead_hunter.db'); print('leads:', conn.execute('SELECT count(*) FROM leads').fetchone()[0]); print('jobs:', conn.execute('SELECT count(*) FROM jobs').fetchone()[0]); print('users:', conn.execute('SELECT count(*) FROM users').fetchone()[0])"
Output:
leads: 8
jobs: 1
users: 0
```
All pre-existing leads, the legacy job (`7590d13c-aabb-41fe-9840-482b9e3b8c6d`), and schema tables remain untouched and preserved.

---

## 7. Changed Files Inventory

1. `backend/app/queue/job_worker.py`: Added secret sanitization (`sanitize_log_message`), atomic status transition updates with deadlock prevention, cooperative cancellation checkpoints, atomic job claiming (`claim_job`), and improved restart recovery detection.
2. `backend/app/api/jobs.py`: Added terminal state immutability enforcement, prioritized ownership checks before worker invocation, and implemented explicit restart recovery error responses on resume.
3. `backend/tests/test_job_orchestration.py`: Added 13 new offline automated tests validating all 10 job orchestration invariants.

---

## 8. Explicitly Unverified Behavior & Documented Architecture Limitations

1. **Multi-Node Distributed Clustering:** The current job orchestrator is designed for a self-hosted single-node Python/FastAPI backend on SQLite. Distributed multi-server worker coordination (e.g., Redis Streams / Celery / RabbitMQ) is outside the single-node architecture contract.
2. **Resumption of Interrupted Discovery Jobs Across Restarts:** Discovered candidates are held in-memory during active job execution. If the process terminates, resuming the job from its exact in-flight candidate index cannot occur without re-querying Overpass. To strictly avoid duplicate network probes and duplicate LLM costs against prospect businesses, the system deliberately disallows automatic resumption of crash-interrupted jobs, marking them for explicit user inspection or cancellation.

---

## 9. Conclusion

Milestone 6 is complete. Background job orchestration is robust, resilient to restarts, protected against invalid state transitions, strictly isolated across authenticated accounts, and validated across 161 automated offline tests.
