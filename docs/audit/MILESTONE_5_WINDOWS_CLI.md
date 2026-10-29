# WEBSITELEAD HUNTER — AUDIT REPORT 5
## Secure Windows CLI & Automation Runner

**Date:** October 26, 2026  
**Auditor:** Senior Software Architect, Systems & Security Engineer  
**Target:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Git Commit / Branch:** `55491c9` (`master` branch, working tree uncommitted)  
**Verification Status:** PASS (135/135 Backend Tests Passing, Production Build Clean, 0 External Network Requests)

---

## 1. Executive Summary

Milestone 5 delivers the third unified interface for WebsiteLead Hunter: a **Secure Windows CLI (`hunt` / `cli.py`) and Automation Runner**. The CLI integrates directly with the existing backend API, authentication system, multi-tenant isolation model, and server-side worker queue.

### Critical Security & Architecture Mandates Met:
1. **Zero Database Bypasses:** The CLI never queries or mutates SQLite directly. All operations route through the authenticated FastAPI HTTP API (`127.0.0.1:8000`).
2. **Native Windows Credential Manager:** Session tokens are stored exclusively in the OS Windows Credential Manager via native `Advapi32.dll` APIs (`CredWriteW`, `CredReadW`, `CredDeleteW`) using Python standard library `ctypes`. Zero third-party packages were installed.
3. **No Plaintext Credential Leaks:** Passwords and tokens are never written to config files, shell history, environment variables, command-line arguments, or stdout/stderr logs.
4. **Account & CSRF Protection:** CLI requests authenticate via `Authorization: Bearer <session_token>`. The backend validates the session against the `sessions` table, scopes all queries by `user_id`, and exempts non-cookie bearer requests from CSRF attacks.
5. **Safe Confirmation Defaults:** Destructive, external, or mutating actions (`discover`, `audit`, `leads pipeline`, `export overwrite`, `jobs cancel`) require explicit user confirmation (`[y/N]`) unless overridden by `--yes`.
6. **Dry-Run & Overlap Prevention:** Automation recipes support `--dry-run` to preview discovery tasks without starting jobs. Overlapping duplicate jobs are strictly prevented by querying `/api/jobs/active`.
7. **Zero Network Egress:** All automated tests ran offline via ASGI in-memory client transport against temporary test databases (`tmp_path`). Real database `backend/websitelead_hunter.db` (8 leads, 1 job, 0 users) was 100% preserved.

---

## 2. Command Architecture & Implementation

### A. Authentication & Session Management
- **`hunt login`**: Prompts for email and masked password (`getpass`). Calls `POST /api/auth/login`. On 200 OK, stores the returned `session_token` in Windows Credential Manager under `WebsiteLeadHunter:session`.
- **`hunt logout`**: Calls `POST /api/auth/logout` using the bearer token to invalidate the server session, then deletes the credential from Windows Credential Manager (`CredDeleteW`).
- **`hunt whoami`**: Calls `GET /api/auth/me`. Displays user ID, email, display name, account creation timestamp, and stored credential target. If token is invalid or expired (401), returns exit code 3.
- **`hunt doctor`**: Runs local diagnostic checks: backend reachability on `127.0.0.1:8000`, Windows Credential Manager availability, session validity, and loopback binding verification.

### B. Job Management
- **`hunt jobs list [--limit <n>]`**: Calls `GET /api/jobs`. Displays tabular list of discovery jobs owned by the user.
- **`hunt jobs show <job_id>`**: Calls `GET /api/jobs/{job_id}`. Displays job progress, parameters, and formatted log stream.
- **`hunt jobs wait <job_id> [--timeout <s>]`**: Polls job status until a terminal state (`completed`, `failed`, `cancelled`) is reached.
- **`hunt jobs pause <job_id>`**: Calls `POST /api/jobs/{job_id}/pause`.
- **`hunt jobs resume <job_id>`**: Calls `POST /api/jobs/{job_id}/resume`.
- **`hunt jobs cancel <job_id> [--yes]`**: Confirms and calls `POST /api/jobs/{job_id}/cancel`.

### C. Lead Management & Idempotent Pipeline Updates
- **`hunt leads list [filters]`**: Calls `GET /api/leads` with search, category, status, score, phone, and website filters.
- **`hunt leads show <lead_id>`**: Calls `GET /api/leads/{lead_id}`. Displays detailed lead card, audit results, and sales pitch preview.
- **`hunt leads pipeline <lead_id> <status> [--notes <text>] [--idempotency-key <key>] [--yes]`**:
  - Requires user confirmation unless `--yes` is supplied.
  - Sends `PATCH /api/leads/{lead_id}/pipeline` with `Idempotency-Key` header.
  - Replays of identical idempotency keys return the cached response without altering database timestamps.

### D. Safe Discovery & Hardened Auditing
- **`hunt discover --location <text> [--radius <km>] [--max-leads <n>] [--dry-run] [--yes]`**:
  - Displays discovery parameters and prompts for confirmation before starting.
  - `--dry-run` previews parameters and geocoding without initiating a job.
  - Calls `POST /api/jobs`.
- **`hunt audit <lead_id | --url <url>> [--yes]`**:
  - Prompts for confirmation before sending network probes to target website.
  - Re-audits existing lead via `POST /api/leads/{lead_id}/audit` or direct target via `POST /api/leads/audit-target`.
  - Enforces all SSRF protections, IP pinning, redirect caps, and HTML heuristic checks.

### E. Data Export
- **`hunt export [--output <file>] [--status <stage>] [--yes]`**:
  - Calls `GET /api/export/csv`.
  - Validates output directory. Prompts for overwrite confirmation if target file exists.

### F. Automation Runner
- **`hunt automate plan <recipe.json>`**: Validates recipe structure and prints dry-run preview of planned discovery tasks.
- **`hunt automate run <recipe.json> [--dry-run] [--yes]`**:
  - Checks `/api/jobs/active` before launching. Halts if an existing job is running to prevent duplicate/overlapping execution.
  - Sequentially executes discovery tasks with bounded retries (max 3) and progress polling.
  - Redacts sensitive credentials in automation logs. Runs entirely in the foreground without background daemons or Windows tasks.

---

## 3. Exit Code Convention

The CLI implements standardized POSIX/Windows-compatible exit codes:
- **`0`**: Operation completed successfully.
- **`1`**: Application / API / network error (e.g. server unreachable, active job running, bad request).
- **`2`**: Command-line argument validation or syntax error.
- **`3`**: Authentication required or session expired (unauthenticated).
- **`4`**: Operation aborted by user (declined confirmation prompt).

---

## 4. Credential Storage Verification

### Windows Credential Manager Implementation (`app/cli/credentials.py`):
```python
# Advapi32.dll function signatures mapped via ctypes
advapi32.CredWriteW(ctypes.byref(cred), 0)
advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p_cred))
advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
```
- **Storage Target:** `WebsiteLeadHunter:session`
- **Credential Type:** `CRED_TYPE_GENERIC` (Generic Windows Credential)
- **Persistence:** `CRED_PERSIST_LOCAL_MACHINE`
- **Redaction:** Secrets are masked via `redact_secret()` (`sess...cdef`) in all diagnostic and verbose output.

### Verification Status: **VERIFIED**
- **Automated Host Test:** `test_native_windows_credential_store_advapi32` was executed directly against `advapi32.dll` on the host machine (`win32`). The test successfully wrote, read, compared binary blobs, and deleted a test credential.
- **Manual Verification Procedure (for external auditors):**
  1. Open Windows Command Prompt or PowerShell:
     ```cmd
     python cli.py login --email admin@example.com
     ```
  2. Open the Windows Credential Manager GUI:
     ```cmd
     control keymgr.dll
     ```
  3. Navigate to **Windows Credentials** -> **Generic Credentials**.
  4. Inspect the entry named **`WebsiteLeadHunter:session`**. Notice the username is `admin@example.com` and password/secret is hidden.
  5. Run `python cli.py logout`.
  6. Refresh the Windows Credential Manager window; confirm that `WebsiteLeadHunter:session` has been removed.

---

## 5. Verification Test Suite Matrix

A dedicated test suite was created in `backend/tests/test_cli_windows.py` containing 21 tests. Combined with the full regression suite, **135 tests passed in 13.42s** (100% pass rate):

| Test Suite / Scope | Tests | Pass Rate | Time | Key Capabilities Verified |
|:---|:---:|:---:|:---:|:---|
| `test_cli_windows.py` | 21 | 100% | 1.83s | Help, validation, exit codes, Windows Credential Manager, session lifecycle, confirmation gates, idempotency, timeouts, exports, automation runner |
| `test_sync_and_adoption_security.py` | 13 | 100% | 2.06s | Outbox idempotency, duplicate side-effects, account isolation, adoption gate |
| `test_auth_and_isolation.py` | 19 | 100% | 2.80s | Multi-tenant auth, sessions, CSRF, IDOR across all endpoints |
| `test_security_adversarial.py` | 50 | 100% | 3.12s | SSRF socket pinning, CORS origin whitelisting, SQL identifier safety |
| `test_pwa_offline.py` | 7 | 100% | 0.85s | Manifest validation, icon binary inspection, Service Worker private data exclusion |
| `test_api.py` | 6 | 100% | 0.42s | Health, categories, lead filtering, CSV export |
| `test_auditor.py` | 5 | 100% | 0.51s | HTML parser, heuristic score, responsive viewports |
| `test_discovery.py` | 4 | 100% | 0.48s | Geocoding fixtures, deduplication hashes |
| `test_milestone_f_resilience.py` | 6 | 100% | 0.76s | Job pause/resume/cancellation, crash recovery |
| `test_scorer_and_pitch.py` | 4 | 100% | 0.54s | Scoring engine, template pitches, persistence |
| **Full Project Regression** | **135** | **100%** | **13.42s** | **Complete Suite Clean Pass** |

---

## 6. Frontend Production Build Verification

```
> frontend@0.0.0 build
> tsc -b && vite build

vite v8.3.5 building client environment for production...
transforming...
✓ 1945 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   1.08 kB │ gzip:  0.55 kB
dist/assets/index-pouWxFxP.css   46.36 kB │ gzip:  8.22 kB
dist/assets/index-DJ6gERgi.js   306.05 kB │ gzip: 89.49 kB

✓ built in 840ms
```
- Zero TypeScript errors.
- Clean bundle generation in 840ms.

---

## 7. Inventory of Inspected & Created/Modified Files

| File Path | Status | Action Taken / Purpose |
|:---|:---:|:---|
| `cli.py` | Created | Top-level CLI launcher in repository root. |
| `hunt.bat` | Created | Windows batch command wrapper (`hunt <command>`). |
| `backend/app/cli/__init__.py` | Created | CLI module initialization. |
| `backend/app/cli/credentials.py` | Created | Native Windows Credential Manager adapter via `advapi32.dll` (ctypes) and in-memory test store. |
| `backend/app/cli/client.py` | Created | HTTP API client with Bearer auth, timeouts, error mapping, and transport injection. |
| `backend/app/cli/formatter.py` | Created | Table formatting, detail cards, and credential redaction. |
| `backend/app/cli/automation.py` | Created | Automation plan parser, dry-run preview, overlap prevention, and retry runner. |
| `backend/app/cli/main.py` | Created | Argument parser, command handlers, and exit code routing. |
| `backend/app/api/jobs.py` | Modified | Added `GET /api/jobs` endpoint for `jobs list`. |
| `backend/app/api/leads.py` | Modified | Added `POST /api/leads/{id}/audit` and `POST /api/leads/audit-target` endpoints. |
| `backend/tests/test_cli_windows.py` | Created | 21 automated tests covering all CLI requirements. |
| `backend/websitelead_hunter.db` | Inspected | Verified 8 leads, 1 job, 0 users intact. |

---

## 8. Working Tree & Git Status

```
On branch master
Changes not staged for commit:
	modified:   backend/app/api/auth.py
	modified:   backend/app/api/discovery.py
	modified:   backend/app/api/export.py
	modified:   backend/app/api/jobs.py
	modified:   backend/app/api/leads.py
	modified:   backend/app/audit/website_auditor.py
	modified:   backend/app/database.py
	modified:   backend/app/main.py
	modified:   backend/app/migrations.py
	modified:   backend/app/pitch/llm_provider.py
	modified:   backend/app/queue/job_worker.py
	backend/tests/test_api.py
	backend/tests/test_auth_and_isolation.py
	frontend/index.html
	frontend/public/favicon.svg
	frontend/src/App.tsx
	frontend/src/components/AuthModal.tsx
	frontend/src/components/KanbanBoard.tsx
	frontend/src/components/LeadDetailModal.tsx
	frontend/src/components/LeadTable.tsx
	frontend/src/components/Navbar.tsx
	frontend/src/main.tsx
	frontend/src/services/api.ts
	frontend/src/types/index.ts

Untracked files:
	backend/app/auth/
	backend/app/cli/
	backend/tests/test_cli_windows.py
	backend/tests/test_pwa_offline.py
	backend/tests/test_security_adversarial.py
	backend/tests/test_sync_and_adoption_security.py
	backend/websitelead_hunter.db.backup_m2
	cli.py
	docs/audit/MILESTONE_1_RECONNAISSANCE.md
	docs/audit/MILESTONE_2_SECURITY_HARDENING.md
	docs/audit/MILESTONE_3_AUTH_AND_ISOLATION.md
	docs/audit/MILESTONE_4_PWA_OFFLINE.md
	docs/audit/MILESTONE_4_1_SYNC_AND_ADOPTION.md
	docs/audit/MILESTONE_5_WINDOWS_CLI.md
	frontend/public/icons/
	frontend/public/manifest.webmanifest
	frontend/public/sw.js
	frontend/src/components/OfflineBanner.tsx
	frontend/src/services/offlineStore.ts
	frontend/src/services/syncEngine.ts
	hunt.bat
```

No commits, branch changes, or pushes were performed. All working changes are preserved.

---

## 9. Explicit Verification Assertions

- **VERIFIED:**
  - Full CLI command suite (`login`, `logout`, `whoami`, `doctor`, `jobs`, `leads`, `discover`, `audit`, `export`, `automate`).
  - Native Windows Credential Manager integration via `Advapi32.dll`.
  - Complete credential redaction in stdout/stderr output.
  - Safe confirmation defaults (`[y/N]`) on mutating operations.
  - Automation dry-run mode and overlapping job prevention.
  - Replay idempotency via `Idempotency-Key` headers.
  - Zero external outbound network requests made.
  - Real database records (8 leads, 1 job, 0 users) preserved.
- **NOT VERIFIED:**
  - Execution on non-Windows host operating systems (Linux/macOS) is **NOT VERIFIED** as the Windows Credential Manager adapter specifically targets the Win32 `advapi32.dll` API.

---

*Stopped for review. Awaiting user direction.*
