# WEBSITELEAD HUNTER — AUDIT REPORT 5.1
## CLI Security & Automation Reliability Audit

**Date:** October 26, 2026  
**Auditor:** Senior Software Architect, Application Security Engineer & Systems Lead  
**Target:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Git Commit / Branch:** `55491c9` (`master` branch, working tree uncommitted)  
**Verification Status:** PASS (144/144 Full Backend Tests Passing, 30/30 CLI Tests Passing, Frontend Production Build Clean, 0 External Network Requests)

---

## 1. Executive Summary

Milestone 5.1 completes a rigorous security and reliability audit of the **Windows CLI (`hunt` / `cli.py`) and Automation Runner**. Following the baseline established in Milestone 5, this audit subjected session invalidation, job authorization, automation idempotency, and Windows Credential Manager memory handling to adversarial stress testing and negative-path verification.

### Audit Highlights & Key Accomplishments:
1. **Server-Enforced Job Idempotency:** Implemented and verified backend idempotency caching on `POST /api/jobs` via `Idempotency-Key` headers. When a client experiences a network timeout or lost response, retrying with the same idempotency key safely returns the existing job without scheduling duplicate background tasks or wasting system resources.
2. **Deterministic Automation Idempotency:** The automation runner now generates deterministic idempotency keys per recipe task (`task_idem_key`), preventing duplicate job execution across retries.
3. **Session Invalidation & Failure Modes:** `hunt logout` strictly distinguishes between confirmed server-side session revocation and local credential deletion when the server is unreachable. The CLI never reports successful logout if server revocation cannot be confirmed, and exits with code 1.
4. **Cross-Account Job Authorization:** Verified that all CLI job operations (`list`, `show`, `wait`, `pause`, `resume`, `cancel`) map to real authenticated FastAPI endpoints that enforce strict account isolation. User A cannot view or manipulate User B's jobs (all unauthorized accesses return 404 Not Found).
5. **Credential Buffer Safety & Native Memory Cleanup:** Hardened `Advapi32.dll` ctypes integration with strict 512-byte limits (`CRED_MAX_CREDENTIAL_BLOB_SIZE`), UTF-8 replacement decoding for malformed blobs, and mandatory `advapi32.CredFree` calls to prevent native memory leaks. Successfully validated against the native Windows Credential Manager hardware API on this Windows host.
6. **Zero Credential Leaks:** Verified across all error and exception paths that session tokens and passwords are never printed to stdout or stderr.

---

## 2. Session Invalidation & Failure Mode Analysis

### A. Server Revocation + Local Clearance Workflow
The CLI logout process (`cmd_logout` in `backend/app/cli/main.py`) coordinates between the remote server and the local Windows Credential Manager:
1. Reads the stored session token from Windows Credential Manager (`WebsiteLeadHunter:session`).
2. Sends an authenticated `POST /api/auth/logout` request using the bearer token to immediately delete the session from the backend `sessions` table.
3. Calls `Advapi32.dll` `CredDeleteW` to purge the stored credential from the operating system.

### B. Unreachable Server / Network Drop Failure Mode
When the backend server is offline or unreachable during logout:
- The CLI catches `CLINetworkError` or `CLIError`.
- It proceeds to delete the local credential from Windows Credential Manager to protect the local device.
- **Critical Security Invariant:** It prints an explicit warning to `sys.stderr` stating that the server session could not be confirmed revoked and may remain active on the server until expiration.
- It returns exit code `1`, **never falsely claiming successful logout**.

```text
[!] Warning: Server session could not be revoked (Connection refused to 127.0.0.1:8000).
[INFO] Local credentials for 'user@example.com' removed from Windows Credential Manager, but server session may remain active until expiration.
```

### C. Windows Credential Manager Read/Write/Delete Exceptions
If native OS credential calls fail (e.g. access violations, read failures, or deletion errors):
- The CLI catches the exception and displays an actionable error message to `sys.stderr` without exposing token values.
- In `cmd_login`, if `save_session_token` fails, the command logs an error and returns exit code 1.
- In `cmd_logout`, if credential deletion fails, the user is warned that the entry remains in Windows Credential Manager and exit code 1 is returned.

---

## 3. Job Operation Authorization & Cross-Account Isolation

Every CLI job command maps directly to an authenticated backend route. Fake or simulated client-only operations are strictly prohibited.

### Backend Route Authorization Mapping:
| CLI Command | Backend Endpoint | Authorization Enforcement | IDOR Resistance |
| :--- | :--- | :--- | :--- |
| `hunt jobs list` | `GET /api/jobs` | Scoped to `current_user.id` | Returns only caller's jobs |
| `hunt jobs show <id>` | `GET /api/jobs/{id}` | Checks `job["user_id"] == current_user.id` | 404 Not Found if owned by other user |
| `hunt jobs wait <id>` | `GET /api/jobs/{id}` | Checks `job["user_id"] == current_user.id` | 404 Not Found if owned by other user |
| `hunt jobs pause <id>` | `POST /api/jobs/{id}/pause` | Checks `job["user_id"] == current_user.id` | 404 Not Found if owned by other user |
| `hunt jobs resume <id>` | `POST /api/jobs/{id}/resume` | Checks `job["user_id"] == current_user.id` | 404 Not Found if owned by other user |
| `hunt jobs cancel <id>` | `POST /api/jobs/{id}/cancel` | Checks `job["user_id"] == current_user.id` | 404 Not Found if owned by other user |

### Adversarial Verification (`test_job_authorization_cross_account_isolation`):
- Created User A (`user_a_jobs@test.com`) and User B (`user_b_jobs@test.com`).
- User B owns an active job (`job-b-isolated-001`).
- User A authenticates via CLI:
  1. `hunt jobs list`: Job `job-b-isolated-001` does **not** appear in User A's job list.
  2. `hunt jobs show job-b-isolated-001`: Returns exit code 1 with `404 Not Found`.
  3. `hunt jobs pause job-b-isolated-001`: Returns exit code 1 with `404 Not Found`.
  4. `hunt jobs resume job-b-isolated-001`: Returns exit code 1 with `404 Not Found`.
  5. `hunt jobs cancel job-b-isolated-001 --yes`: Returns exit code 1 with `404 Not Found`.
- Verified in database that User B's job remained in `running` status and was untouched.

---

## 4. Automation Idempotency & Lost Response Recovery

### A. Server-Side Job Idempotency (`backend/app/api/jobs.py`)
To prevent duplicate job creation when network packets are dropped, `POST /api/jobs` was upgraded with server-side idempotency:
```python
@router.post("", response_model=JobResponse)
def start_discovery_job(
    request: JobCreateRequest,
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    if idempotency_key:
        cached = check_idempotency_key(current_user.id, idempotency_key)
        if cached:
            return JobResponse(**cached)
    ...
    # After creating job in database and queue:
    if idempotency_key:
        save_idempotency_key(current_user.id, idempotency_key, resp_dict)
```

### B. Automation Runner Idempotency (`backend/app/cli/automation.py`)
- For each recipe task, the runner derives a deterministic idempotency key:
  $$\text{task\_idem\_key} = \text{SHA256}(\text{task\_idx} + \text{location} + \text{categories} + \text{max\_leads})[:24]$$
- If the runner initiates a job and the HTTP response is dropped/lost after the server accepts it, the runner's retry loop sends the same `task_idem_key`.
- The server recognizes the idempotency key and returns the already-created job, preventing duplicate background tasks.

### C. Bounded Retries & Permanent Error Handling
- Permanent validation errors (HTTP 400 `CLIValidationError`, such as invalid location or unparseable query) are recognized as non-retryable.
- The runner breaks immediately without wasting 3 retry attempts against a permanent client error.
- Overlapping job runs are prevented by checking `/api/jobs/active` before launching tasks.

---

## 5. Credential Storage & Native Win32 Buffer Safety

The Windows Credential Manager interface (`backend/app/cli/credentials.py`) was audited and hardened against buffer overflows and memory corruption:

1. **Size Boundary Validation:**
   - Windows Credential Manager sets `CRED_MAX_CREDENTIAL_BLOB_SIZE = 512` bytes.
   - `write()` now validates that `secret.encode("utf-8")` does not exceed 512 bytes, and target/username do not exceed `CRED_MAX_GENERIC_TARGET_NAME_SIZE` (512 chars). Oversized inputs raise `ValueError` before calling native code.
2. **Native Memory Management (`CredFree`):**
   - Win32 `CredReadW` allocates native unmanaged memory for `PCREDENTIALW`.
   - The read wrapper wraps extraction in `try ... finally: advapi32.CredFree(p_cred)` to guarantee that allocated Win32 memory is freed, preventing C runtime memory leaks.
3. **Malformed Entry Resilience:**
   - If Windows Credential Manager returns a credential with null pointers, zero blob size, or malformed UTF-8 bytes, `read()` handles it safely using `errors="replace"` and returns `None` without crashing.
4. **Hardware Verification on Windows (`test_native_windows_credential_store_advapi32`):**
   - Executed against the live Windows Credential Manager API on this machine.
   - Successfully wrote a dynamic generic credential (`WebsiteLeadHunter_Test_*`), retrieved it, verified byte identity, deleted it, and verified non-existence.
5. **Zero Credential Leakage:**
   - Verified that session tokens and passwords are redacted via `redact_secret()` and never printed to stdout/stderr in command outputs, error reports, or doctor diagnostics.

---

## 6. Verification Test Suite Matrix

### A. Milestone 5.1 CLI Test Suite (`backend/tests/test_cli_windows.py`):
```text
tests/test_cli_windows.py::test_cli_help_exit_code_zero PASSED           [  3%]
tests/test_cli_windows.py::test_cli_unknown_command_exit_code_two PASSED [  6%]
tests/test_cli_windows.py::test_cli_jobs_missing_action_exit_code_two PASSED [ 10%]
tests/test_cli_windows.py::test_cli_leads_pipeline_invalid_status_exit_code_two PASSED [ 13%]
tests/test_cli_windows.py::test_whoami_without_credentials_returns_exit_code_three PASSED [ 16%]
tests/test_cli_windows.py::test_credential_redaction_utility PASSED      [ 20%]
tests/test_cli_windows.py::test_credential_store_failure_handled_gracefully PASSED [ 23%]
tests/test_cli_windows.py::test_logout_removes_stored_credentials PASSED [ 26%]
tests/test_cli_windows.py::test_authenticated_whoami_success PASSED      [ 30%]
tests/test_cli_windows.py::test_expired_session_returns_exit_code_three PASSED [ 33%]
tests/test_cli_windows.py::test_account_isolation_forbidden_operations PASSED [ 36%]
tests/test_cli_windows.py::test_discover_dry_run_does_not_create_job PASSED [ 40%]
tests/test_cli_windows.py::test_discover_declined_confirmation_returns_exit_code_four PASSED [ 43%]
tests/test_cli_windows.py::test_pipeline_update_declined_confirmation_returns_code_four PASSED [ 46%]
tests/test_cli_windows.py::test_pipeline_update_with_idempotency_key_replay PASSED [ 50%]
tests/test_cli_windows.py::test_http_timeout_safe_reporting PASSED       [ 53%]
tests/test_cli_windows.py::test_export_csv_success PASSED                [ 56%]
tests/test_cli_windows.py::test_export_overwrite_declined PASSED         [ 60%]
tests/test_cli_windows.py::test_automation_plan_dry_run PASSED           [ 63%]
tests/test_cli_windows.py::test_automation_blocks_overlapping_running_job PASSED [ 66%]
tests/test_cli_windows.py::test_native_windows_credential_store_advapi32 PASSED [ 70%]
tests/test_cli_windows.py::test_logout_when_server_unreachable_distinguishes_local_removal PASSED [ 73%]
tests/test_cli_windows.py::test_logout_credential_delete_failure PASSED  [ 76%]
tests/test_cli_windows.py::test_logout_credential_read_failure PASSED    [ 80%]
tests/test_cli_windows.py::test_job_authorization_cross_account_isolation PASSED [ 83%]
tests/test_cli_windows.py::test_start_job_idempotency_key_replay PASSED  [ 86%]
tests/test_cli_windows.py::test_automation_lost_response_retry_idempotency PASSED [ 90%]
tests/test_cli_windows.py::test_automation_permanent_validation_error_skips_retries PASSED [ 93%]
tests/test_cli_windows.py::test_credential_size_limit_rejection PASSED   [ 96%]
tests/test_cli_windows.py::test_no_credentials_leaked_in_output PASSED   [100%]
======================== 30 passed, 1 warning in 4.51s ========================
```

### B. Full Backend Regression Suite:
```text
====================== 144 passed, 32 warnings in 13.05s ======================
- test_api.py: 6 passed
- test_auditor.py: 5 passed
- test_auth_and_isolation.py: 19 passed
- test_cli_windows.py: 30 passed
- test_discovery.py: 4 passed
- test_pwa_offline.py: 11 passed
- test_regression_offline.py: 20 passed
- test_security.py: 17 passed
- test_security_adversarial.py: 20 passed
- test_sync_and_adoption_security.py: 12 passed
Total: 144 passed (100%), 0 failed.
```

### C. Frontend Production Build Verification:
```text
> frontend@0.0.0 build
> tsc -b && vite build

vite v8.3.5 building client environment for production...
✓ 1945 modules transformed.
dist/index.html                   1.08 kB │ gzip:  0.55 kB
dist/assets/index-pouWxFxP.css   46.36 kB │ gzip:  8.22 kB
dist/assets/index-DJ6gERgi.js   306.05 kB │ gzip: 89.49 kB
✓ built in 586ms
```

---

## 7. Safety, Database & Environment Preservations

1. **Database Integrity:**
   - `backend/websitelead_hunter.db`:
     - Leads: 8 (Intact)
     - Jobs: 1 (Intact)
     - Users: 0 (Intact)
     - Tables: `['jobs', 'schema_migrations', 'users', 'sessions', 'leads']` (Intact)
2. **Network Egress:**
   - 0 outbound requests made to real businesses, Overpass, Nominatim, Gemini, or email endpoints.
   - All tests used in-memory SQLite and ASGI transport.
3. **Git Hygiene:**
   - Branch: `master` (HEAD: `55491c9`).
   - No commits made, no branches changed, no remote push attempted.

---

## 8. Conclusion

Milestone 5.1 verification is complete and fully satisfied. The Windows CLI and Automation Runner satisfy all enterprise security requirements: session invalidation distinguishing local vs server state, cross-account job authorization, server-side job idempotency, native Win32 credential safety, and zero credential exposure.
