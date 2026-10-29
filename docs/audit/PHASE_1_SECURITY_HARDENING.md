# WebsiteLead Hunter — Phase 1: Security Hardening Verification Report

**Date:** October 26, 2026  
**Status:** **PASSED / COMPLETE**  
**Milestone:** Phase 1 (Offline Security Hardening)  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`  
**Safety Mandate:** Offline only. Zero network requests, zero package installations, zero cloud provisioning. Real database strictly preserved (`leads: 8, jobs: 1, users: 0, schema_migrations: [1]`).

---

## 1. Executive Summary

Phase 1 of the [Hosting Readiness Master Plan](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/docs/audit/HOSTING_READINESS_MASTER_PLAN.md) has been implemented and verified. This phase resolves two critical security vulnerabilities prior to any network exposure or multi-device hosting:

1. **Elimination of the Legacy-Data Adoption Vulnerability:**
   - The web registration adoption flag (`adopt_legacy_data`) was permanently removed from [`RegisterRequest`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py) and [`AuthModal.tsx`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/src/components/AuthModal.tsx). Web registration can never claim unowned legacy leads or jobs.
   - The insecure web endpoint (`POST /api/auth/adopt-legacy-leads`) that relied on loopback IP checks (`request.client.host`) was removed.
   - A secure, dual-factor administrative CLI workflow was implemented (`hunt admin adopt-legacy`), requiring:
     - Authenticated administrator session
     - scrypt password re-verification
     - Explicit interactive confirmation (`[y/N]`)
     - An unforgeable, ephemeral local token generated on the host filesystem (`backend/.admin_token`), verified via `hmac.compare_digest` and immediately deleted upon consumption.
   - Silent adoption on CLI bootstrap ([`bootstrap.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/auth/bootstrap.py)) was eliminated.

2. **Host and Proxy Boundary Hardening:**
   - Integrated Starlette's `TrustedHostMiddleware` in [`backend/app/main.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/main.py), strictly restricting allowed hosts to `localhost`, `127.0.0.1`, `testserver`, and `testclient`.
   - Web server startup strictly enforces loopback binding (`127.0.0.1`), raising an explicit `ValueError` if configured to bind to `0.0.0.0` or wildcard interfaces.
   - Forwarded-header trust remains disabled at this phase. Exact header prerequisites for Phase 4 (Caddy reverse proxy) are formally specified.
   - Service worker cache guard confirmed: [`frontend/public/sw.js`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/public/sw.js) explicitly excludes `/api/*` from browser Cache Storage.

All 226 backend tests and the frontend build passed cleanly. The real database (`backend/websitelead_hunter.db`) remains completely untouched.

---

## 2. Threat Model & Vulnerability Analysis

| Vulnerability | Pre-Phase 1 State | Attack Vector / Risk | Phase 1 Remediation |
| :--- | :--- | :--- | :--- |
| **Reverse Proxy Loopback IP Spoofing** | Adoption endpoint checked `request.client.host in ("127.0.0.1", ...)` | Behind a local reverse proxy (Caddy/Nginx), all incoming TCP connections originate from `127.0.0.1`. A remote attacker connecting over the mesh or web could invoke adoption and hijack unowned leads. | Removed IP-based trust entirely. Replaced with local filesystem token (`.admin_token`) inaccessible to remote network callers. |
| **Web Registration Hijack** | `RegisterRequest.adopt_legacy_data: bool` | Any new web user registering when `user_count == 0` could claim all existing leads by checking a box. | Removed `adopt_legacy_data` from registration schema and UI. Registration now exclusively creates user accounts. |
| **Silent CLI Bootstrap Adoption** | `bootstrap.py` automatically ran `UPDATE leads ... WHERE user_id IS NULL` | Admin creating an account without realizing legacy leads exist would silently adopt them without audit trail or confirmation. | Removed auto-adoption. Bootstrap prints explicit instructions to run `hunt admin adopt-legacy`. |
| **Host Header Injection / Cache Poisoning** | No Host header validation | Attackers sending arbitrary `Host` headers could cause cache poisoning or password reset poisoning. | Enforced Starlette `TrustedHostMiddleware`, rejecting non-whitelisted hosts with HTTP 400. |
| **Wildcard Network Exposure** | Environment variable `HOST` could be configured to `0.0.0.0` | Accidental configuration could expose unauthenticated FastAPI server directly to LAN or public interfaces. | Explicit guard in `main.py` raises `ValueError` if `HOST` is `0.0.0.0`, `::`, or `0`. |

---

## 3. Implementation Details

### 3.1 Backend Authentication API Refactoring ([`backend/app/api/auth.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/api/auth.py))
- Removed `adopt_legacy_data` from `RegisterRequest`.
- Removed adoption branch in `register()`.
- Removed `POST /api/auth/adopt-legacy-leads`.
- Added `AdminAdoptRequest(BaseModel)` with `password: str` and `confirm: bool`.
- Added `GET /api/auth/admin/legacy-summary`:
  - Returns `{"unowned_leads": int, "unowned_jobs": int}` for administrative pre-check.
- Added `POST /api/auth/admin/adopt-legacy`:
  - Enforces `user = Depends(get_current_user)` and `_csrf = Depends(verify_csrf_token)`.
  - Verifies `payload.confirm is True`.
  - Re-verifies `verify_password(payload.password, user["password_hash"])`.
  - Checks for local `backend/.admin_token`, reads content, immediately deletes file, and compares with `X-Admin-Bootstrap-Token` using `hmac.compare_digest`.
  - Updates only records `WHERE user_id IS NULL`.
  - Logs audit entry with user ID and email.

### 3.2 CLI Administration Command ([`backend/app/cli/main.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/cli/main.py) & [`client.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/cli/client.py))
- Added `admin` subcommand: `hunt admin adopt-legacy [--password PWD] [-y] [--json]`.
- Checks for active session in Windows Credential Manager (`hunt login`).
- Queries summary endpoint to verify unowned records exist. If 0, exits cleanly with code 0.
- Displays exact count: *"Discovered unowned legacy data: X lead(s) and Y job(s)."*
- Prompts for confirmation: `Are you sure you want to adopt...? [y/N]`.
- Prompts for password securely via `getpass.getpass()`.
- Generates 32-byte hex token (`secrets.token_hex(32)`) and writes to `backend/.admin_token`.
- Calls backend API with `X-Admin-Bootstrap-Token` header.
- Always cleans up `.admin_token` in a `finally` block.

### 3.3 Host Whitelisting & Loopback Binding ([`backend/app/main.py`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/backend/app/main.py))
- Added `TrustedHostMiddleware(app, allowed_hosts=ALLOWED_HOSTS)` where `ALLOWED_HOSTS` defaults to `["localhost", "127.0.0.1", "testserver", "testclient"]`.
- Added `"X-Admin-Bootstrap-Token"` to CORS `allow_headers`.
- Added safety check in `__main__` prohibiting binding to `0.0.0.0`.

### 3.4 Frontend Cleanup ([`AuthModal.tsx`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/src/components/AuthModal.tsx) & [`api.ts`](file:///C:/Users/Yatha/.gemini/antigravity/scratch/websitelead-hunter/frontend/src/services/api.ts))
- Removed `adoptLegacy` state variable and checkbox from registration modal.
- Removed `adopt_legacy_data` argument from `api.register()`.
- Removed `adoptLegacyLeads()` method from frontend API service.

---

## 4. Verification Evidence

### 4.1 Test Suites Executed

```powershell
# 1. Targeted Auth, CSRF, and Isolation Suite
pytest tests/test_auth_and_isolation.py -v
# Result: 20 passed in 3.12s

# 2. Sync Idempotency & Adoption Security Gate Suite
pytest tests/test_sync_and_adoption_security.py -v
# Result: 17 passed in 2.28s

# 3. Windows CLI & Admin Suite
pytest tests/test_cli_windows.py -v
# Result: 38 passed in 5.10s

# 4. Full Backend Regression Suite
pytest
# Result: 226 passed, 0 failed in 43.56s

# 5. Frontend Production Build
cd frontend; npm run build
# Result: tsc -b && vite build completed in 1.03s (0 errors)
```

### 4.2 Specific Security Scenarios Verified

| Test Function | Location | Verification Passed |
| :--- | :--- | :---: |
| `test_trusted_host_middleware_blocks_disallowed_hosts` | `test_auth_and_isolation.py` | `evil.com` -> 400, `attacker.net:8000` -> 400, `localhost` -> 200, `127.0.0.1` -> 200 |
| `test_default_registration_does_not_adopt_legacy_data` | `test_sync_and_adoption_security.py` | Registration leaves legacy leads/jobs with `user_id IS NULL` |
| `test_registration_with_legacy_flag_ignored` | `test_sync_and_adoption_security.py` | Sending `adopt_legacy_data: true` in body is ignored, records unowned |
| `test_old_web_adoption_endpoint_removed` | `test_sync_and_adoption_security.py` | `POST /api/auth/adopt-legacy-leads` returns 404/405 |
| `test_admin_adoption_endpoint_requires_auth` | `test_sync_and_adoption_security.py` | Unauthenticated call -> 401 |
| `test_admin_adoption_endpoint_requires_confirmation` | `test_sync_and_adoption_security.py` | `confirm=False` -> 400 |
| `test_admin_adoption_endpoint_requires_valid_password` | `test_sync_and_adoption_security.py` | Incorrect password -> 403 |
| `test_admin_adoption_endpoint_requires_bootstrap_token` | `test_sync_and_adoption_security.py` | Missing header or missing file -> 403 |
| `test_admin_adoption_endpoint_rejects_mismatched_bootstrap_token` | `test_sync_and_adoption_security.py` | Mismatched token -> 403 |
| `test_admin_adoption_endpoint_consumes_token_immediately` | `test_sync_and_adoption_security.py` | Token unlinked upon first use; replay fails with 403 |
| `test_admin_adoption_endpoint_success_and_idempotency` | `test_sync_and_adoption_security.py` | Claims records; rerun adopts 0 records |
| `test_cli_admin_adopt_legacy_unauthenticated` | `test_cli_windows.py` | CLI exits with code 3 when unauthenticated |
| `test_cli_admin_adopt_legacy_empty_inventory` | `test_cli_windows.py` | CLI exits with code 0 when 0 unowned records |
| `test_cli_admin_adopt_legacy_confirmation_aborted` | `test_cli_windows.py` | Answering `n` aborts adoption, DB untouched |
| `test_cli_admin_adopt_legacy_success` | `test_cli_windows.py` | CLI executes full flow, claims records, cleans up `.admin_token` |

### 4.3 Real Database Integrity

Inspected `backend/websitelead_hunter.db` read-only before and after all changes:
```python
conn = sqlite3.connect('file:backend/websitelead_hunter.db?mode=ro', uri=True)
# Result: {'jobs': 1, 'schema_migrations': 1, 'users': 0, 'sessions': 0, 'leads': 8}
```
**Conclusion:** Zero modifications, zero migrations, zero record adoptions on the real production database.

---

## 5. Forwarded Header Prerequisites for Phase 4 (Caddy Integration)

Forwarded-header trust is **deliberately disabled** in Phase 1 because no reverse proxy is currently deployed. When Caddy is introduced in Phase 4, the following prerequisites must be applied simultaneously:

1. **Uvicorn Invocation:**
   ```powershell
   uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips="127.0.0.1,::1"
   ```
   *Strict Rule:* `--forwarded-allow-ips` must strictly be restricted to loopback IPs (`127.0.0.1,::1`). It must NEVER be set to `*`.

2. **Caddy Header Sanitization:**
   Caddy must strip untrusted client-supplied `X-Forwarded-*` headers and inject authoritative values:
   ```caddyfile
   header_up X-Forwarded-For {remote_host}
   header_up X-Forwarded-Proto {scheme}
   header_up X-Forwarded-Host {host}
   header_up -X-Forwarded-Server
   ```

3. **Tailscale Host Whitelisting:**
   The Tailscale MagicDNS domain (`<pc>.<tailnet>.ts.net`) must be appended to `ALLOWED_HOSTS` in `.env` so `TrustedHostMiddleware` permits reverse-proxied traffic.

---

## 6. Residual Risks & Next Steps

1. **Residual Risks:**
   - **Environment Variable Isolation:** If `ALLOWED_HOSTS` is overridden with `*`, Starlette host validation is bypassed. The default fallback remains safe (`localhost, 127.0.0.1, testserver, testclient`).
   - **Token Path Overrides:** In testing, `ADMIN_TOKEN_PATH` can be redirected via environment variable. In production, it defaults to the secured `backend/.admin_token` path with restrictive Windows NTFS permissions.

2. **Phase 1 Completion Status:**
   - All objectives from `HOSTING_READINESS_MASTER_PLAN.md` Phase 1 are 100% complete.
   - Code changes, tests, and documentation are verified offline.

> [!IMPORTANT]
> **GATE 1 APPROVAL REQUIRED:**
> In accordance with safety constraints, execution halts here. Do NOT proceed to Phase 2 (Windows Worker Task Scheduler Configuration) until explicit user authorization is provided.
