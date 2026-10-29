"""
Milestone 7: Production Readiness & Multi-Client Integration Verification Suite.

Validates end-to-end multi-client behavior across Web PWA, Desktop Dashboard,
and Windows CLI. Covers:
1. Cross-client pipeline consistency (Web <-> CLI <-> DB).
2. Stale client and deleted lead handling (404 and clean recovery).
3. Session invalidation, re-login, and token lifecycle across clients.
4. Account isolation across all lead and job endpoints.
5. Idempotent outbox replay after simulated network drops.
6. Multi-client job lifecycle, terminal state immutability, and status polling.
7. Security regressions: CSRF enforcement, Bearer bypass, CORS restrictions, SSRF defenses, and secret redaction.
8. PWA Service Worker caching and offline outbox security contracts.

Offline only: All tests run against local ASGI in-memory transport and temporary SQLite databases.
"""
import uuid
import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi.testclient import TestClient
import httpx

from app.main import app
from app.database import init_db, get_db
from app.queue.job_worker import JobWorker
from app.api import jobs
from app.auth.security import hash_password, _failed_attempts
from app.cli.client import LeadHunterClient, CLINotFoundError, CLIValidationError, CLIError
from app.audit.website_auditor import WebsiteAuditor

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_multi_client_db(tmp_path):
    """Sets up an isolated temporary SQLite database for each test and resets worker & rate limiter."""
    db_file = str(tmp_path / "multi_client_test.db")
    init_db(db_file)
    _failed_attempts.clear()
    worker = JobWorker()
    jobs.set_worker(worker)
    yield db_file
    _failed_attempts.clear()


def create_user_and_session(email: str, password: str = "SecurePass123!", full_name: str = "Test User") -> dict:
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    session_id = f"sess_{uuid.uuid4().hex}"
    csrf_token = f"csrf_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, email, hash_password(password), full_name, now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, 'MultiClientTest', '127.0.0.1', ?, ?)
            """,
            (session_id, user_id, csrf_token, expires_at, now),
        )

    return {
        "user_id": user_id,
        "email": email,
        "session_token": session_id,
        "csrf_token": csrf_token,
    }


def seed_lead(user_id: str, lead_id: str = None, name: str = "Artisan Bakery", pipeline_status: str = "new") -> str:
    lid = lead_id or f"lead_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, address, lat, lon, phone,
                website, opening_hours, source_url, provenance, raw_data,
                audit_result, score, score_breakdown, pitches, pipeline_status,
                notes, contacted_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'bakery', '100 Baker St', 12.97, 77.64, '+91 9876543210',
                'https://artisanbakery.example.com', '', '', '{}', '{}',
                '{}', 75, '{}', '{}', ?,
                NULL, NULL, ?, ?)
            """,
            (lid, user_id, f"osm_{lid}", name, pipeline_status, now, now),
        )
    return lid


def seed_job(user_id: str, job_id: str = None, status: str = "running") -> str:
    jid = job_id or f"job_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, 'Indiranagar', 12.97, 77.64, 1.5, '["cafe"]', 10, 5, 2, '[]', ?, ?)
            """,
            (jid, user_id, status, now, now),
        )
    return jid


# ==============================================================================
# 1. CROSS-CLIENT CONSISTENCY (WEB <-> CLI <-> DB)
# ==============================================================================

def test_cross_client_lead_pipeline_consistency():
    """Verify changes made by Web client are immediately visible to CLI, and vice versa."""
    user = create_user_and_session("shared_user@test.com")
    lead_id = seed_lead(user["user_id"], name="Corner Cafe", pipeline_status="new")

    # 1. Web client fetches lead detail
    web_headers = {
        "Cookie": f"session_id={user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }
    resp_web = client.get(f"/api/leads/{lead_id}", headers=web_headers)
    assert resp_web.status_code == 200
    assert resp_web.json()["pipeline_status"] == "new"

    # 2. CLI client fetches lead detail using TestClient transport
    cli_client = LeadHunterClient(
        base_url="http://testserver",
        transport=client._transport,
    )
    # Patch credential loader for CLI
    import app.cli.client as cli_mod
    orig_cred = cli_mod.load_session_token
    cli_mod.load_session_token = lambda: (user["email"], user["session_token"])

    try:
        cli_lead = cli_client.get_lead(lead_id)
        assert cli_lead["pipeline_status"] == "new"
        assert cli_lead["name"] == "Corner Cafe"

        # 3. Web client updates pipeline stage to 'contacted'
        web_update_resp = client.patch(
            f"/api/leads/{lead_id}/pipeline",
            headers=web_headers,
            json={"pipeline_status": "contacted", "notes": "Called owner from Web PWA"},
        )
        assert web_update_resp.status_code == 200
        assert web_update_resp.json()["pipeline_status"] == "contacted"

        # 4. CLI client verifies the update is immediately reflected
        cli_lead_after_web = cli_client.get_lead(lead_id)
        assert cli_lead_after_web["pipeline_status"] == "contacted"
        assert cli_lead_after_web["notes"] == "Called owner from Web PWA"

        # 5. CLI client updates pipeline stage to 'won'
        cli_client.update_pipeline(lead_id, pipeline_status="won", notes="Deal closed via CLI")

        # 6. Web client verifies the update is immediately reflected
        web_lead_after_cli = client.get(f"/api/leads/{lead_id}", headers=web_headers).json()
        assert web_lead_after_cli["pipeline_status"] == "won"
        assert web_lead_after_cli["notes"] == "Deal closed via CLI"

    finally:
        cli_mod.load_session_token = orig_cred


# ==============================================================================
# 2. DELETED LEADS & STALE CLIENT STATE HANDLING
# ==============================================================================

def test_deleted_lead_stale_client_handling():
    """Verify deleted leads return consistent 404s and stale clients recover cleanly."""
    user = create_user_and_session("stale_test@test.com")
    lead_id = seed_lead(user["user_id"], name="Deleted Cafe")

    web_headers = {
        "Cookie": f"session_id={user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }
    cli_headers = {
        "Authorization": f"Bearer {user['session_token']}",
    }

    # 1. CLI deletes the lead
    del_resp = client.delete(f"/api/leads/{lead_id}", headers=cli_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["message"] == "Lead successfully deleted."

    # 2. Web client (stale state) tries to fetch deleted lead -> 404
    get_resp = client.get(f"/api/leads/{lead_id}", headers=web_headers)
    assert get_resp.status_code == 404
    assert get_resp.json()["detail"] == "Lead not found."

    # 3. Web client (stale state) tries to update pipeline of deleted lead -> 404
    patch_resp = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        headers=web_headers,
        json={"pipeline_status": "contacted"},
    )
    assert patch_resp.status_code == 404
    assert patch_resp.json()["detail"] == "Lead not found."

    # 4. Another delete attempt on already-deleted lead -> 404
    del_again = client.delete(f"/api/leads/{lead_id}", headers=web_headers)
    assert del_again.status_code == 404
    assert del_again.json()["detail"] == "Lead not found."

    # 5. Leads listing contains 0 items for this user
    list_resp = client.get("/api/leads", headers=web_headers).json()
    assert list_resp["total"] == 0
    assert len(list_resp["items"]) == 0


# ==============================================================================
# 3. MULTI-CLIENT AUTH EXPIRY AND LOGOUT
# ==============================================================================

def test_multi_client_auth_expiry_and_logout():
    """Verify logout invalidates server session across both cookie and bearer clients."""
    user = create_user_and_session("auth_multi@test.com")

    web_headers = {
        "Cookie": f"session_id={user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }
    cli_headers = {
        "Authorization": f"Bearer {user['session_token']}",
    }

    # Both clients can authenticate
    assert client.get("/api/auth/me", headers=web_headers).status_code == 200
    assert client.get("/api/auth/me", headers=cli_headers).status_code == 200

    # Web client initiates logout
    logout_resp = client.post("/api/auth/logout", headers=web_headers)
    assert logout_resp.status_code == 200

    # Subsequent requests from both clients are rejected
    assert client.get("/api/auth/me", headers=web_headers).status_code == 401
    assert client.get("/api/auth/me", headers=cli_headers).status_code == 401
    assert client.get("/api/leads", headers=cli_headers).status_code == 401


def test_multi_client_session_expiry_rejection():
    """Verify expired sessions return 401 across both cookie and bearer clients."""
    user = create_user_and_session("expired_multi@test.com")

    # Manually backdate session expiry in database
    past_date = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (past_date, user["session_token"]))

    web_headers = {"Cookie": f"session_id={user['session_token']}"}
    cli_headers = {"Authorization": f"Bearer {user['session_token']}"}

    assert client.get("/api/auth/me", headers=web_headers).status_code == 401
    assert client.get("/api/auth/me", headers=cli_headers).status_code == 401


# ==============================================================================
# 4. CROSS-ACCOUNT ISOLATION (ZERO DATA LEAKAGE)
# ==============================================================================

def test_multi_client_cross_account_isolation():
    """Verify tenant isolation across all endpoints: User B cannot access User A's data."""
    alice = create_user_and_session("alice@isolation.com")
    bob = create_user_and_session("bob@isolation.com")

    lead_a = seed_lead(alice["user_id"], name="Alice Secret Shop")
    job_a = seed_job(alice["user_id"], status="running")

    bob_cli_headers = {"Authorization": f"Bearer {bob['session_token']}"}
    bob_web_headers = {
        "Cookie": f"session_id={bob['session_token']}",
        "X-CSRF-Token": bob["csrf_token"],
    }

    # Bob lists leads -> receives 0 leads
    bob_leads = client.get("/api/leads", headers=bob_cli_headers).json()
    assert bob_leads["total"] == 0

    # Bob attempts to fetch Alice's lead detail -> 404
    assert client.get(f"/api/leads/{lead_a}", headers=bob_cli_headers).status_code == 404
    assert client.get(f"/api/leads/{lead_a}", headers=bob_web_headers).status_code == 404

    # Bob attempts to mutate Alice's lead -> 404
    assert client.patch(
        f"/api/leads/{lead_a}/pipeline",
        headers=bob_web_headers,
        json={"pipeline_status": "won"},
    ).status_code == 404

    # Bob attempts to delete Alice's lead -> 404
    assert client.delete(f"/api/leads/{lead_a}", headers=bob_web_headers).status_code == 404

    # Bob lists jobs -> receives 0 jobs
    bob_jobs = client.get("/api/jobs", headers=bob_cli_headers).json()
    assert len(bob_jobs) == 0

    # Bob attempts to fetch Alice's job -> 404
    assert client.get(f"/api/jobs/{job_a}", headers=bob_cli_headers).status_code == 404

    # Bob attempts to pause, resume, cancel Alice's job -> 404
    assert client.post(f"/api/jobs/{job_a}/pause", headers=bob_web_headers).status_code == 404
    assert client.post(f"/api/jobs/{job_a}/resume", headers=bob_web_headers).status_code == 404
    assert client.post(f"/api/jobs/{job_a}/cancel", headers=bob_web_headers).status_code == 404

    # Bob exports CSV -> gets empty CSV header only, Alice's data not present
    csv_resp = client.get("/api/export/csv", headers=bob_cli_headers)
    assert csv_resp.status_code == 200
    assert "Alice Secret Shop" not in csv_resp.text


# ==============================================================================
# 5. IDEMPOTENT OUTBOX REPLAY AFTER NETWORK DROPS
# ==============================================================================

def test_idempotency_retries_after_simulated_network_drop():
    """Verify outbox replay with same Idempotency-Key returns cached response without duplicate side effects."""
    user = create_user_and_session("idempotency_user@test.com")
    lead_id = seed_lead(user["user_id"], pipeline_status="new")

    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "outbox-replay-uuid-9999",
    }
    payload = {"pipeline_status": "contacted", "notes": "Offline meeting agreed"}

    # 1. Initial submission
    res1 = client.patch(f"/api/leads/{lead_id}/pipeline", headers=headers, json=payload)
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["pipeline_status"] == "contacted"
    assert body1["notes"] == "Offline meeting agreed"

    # 2. Simulate dropped connection and client retry with same idempotency key
    res2 = client.patch(f"/api/leads/{lead_id}/pipeline", headers=headers, json=payload)
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2 == body1

    # 3. Idempotency key isolation across users: Bob cannot reuse Alice's key
    bob = create_user_and_session("bob_idempotency@test.com")
    bob_lead = seed_lead(bob["user_id"], pipeline_status="new")
    bob_headers = {
        "Authorization": f"Bearer {bob['session_token']}",
        "Idempotency-Key": "outbox-replay-uuid-9999",
    }
    res_bob = client.patch(
        f"/api/leads/{bob_lead}/pipeline",
        headers=bob_headers,
        json={"pipeline_status": "qualified"},
    )
    assert res_bob.status_code == 200
    assert res_bob.json()["pipeline_status"] == "qualified"
    assert res_bob.json()["id"] == bob_lead


# ==============================================================================
# 6. JOB LIFECYCLE COORDINATION & TERMINAL STATE IMMUTABILITY
# ==============================================================================

def test_job_lifecycle_coordination_and_terminal_immutability():
    """Verify job lifecycle across clients and immutable terminal states."""
    user = create_user_and_session("job_lifecycle@test.com")
    job_id = seed_job(user["user_id"], status="running")

    web_headers = {
        "Cookie": f"session_id={user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }
    cli_headers = {"Authorization": f"Bearer {user['session_token']}"}

    # 1. Web client polls active job
    active_resp = client.get("/api/jobs/active", headers=web_headers)
    assert active_resp.status_code == 200
    assert active_resp.json()["id"] == job_id
    assert active_resp.json()["status"] == "running"

    # 2. CLI client cancels the job
    cancel_resp = client.post(f"/api/jobs/{job_id}/cancel", headers=cli_headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # 3. Web client polls job again -> sees cancelled
    updated_web = client.get(f"/api/jobs/{job_id}", headers=web_headers).json()
    assert updated_web["status"] == "cancelled"

    # 4. Terminal state immutability: attempting to pause or resume cancelled job returns 409
    pause_conflict = client.post(f"/api/jobs/{job_id}/pause", headers=web_headers)
    assert pause_conflict.status_code == 409
    assert "terminal state 'cancelled'" in pause_conflict.json()["detail"]

    resume_conflict = client.post(f"/api/jobs/{job_id}/resume", headers=cli_headers)
    assert resume_conflict.status_code == 409
    assert "terminal state 'cancelled'" in resume_conflict.json()["detail"]

    # 5. Cancelling already cancelled job is idempotent no-op (200)
    cancel_noop = client.post(f"/api/jobs/{job_id}/cancel", headers=web_headers)
    assert cancel_noop.status_code == 200
    assert cancel_noop.json()["status"] == "cancelled"


# ==============================================================================
# 7. SECURITY REGRESSION SUITE (CSRF, CORS, SSRF, SECRETS)
# ==============================================================================

def test_csrf_protection_and_bearer_exemption():
    """Verify CSRF token is strictly enforced for cookies and exempt for Bearer tokens."""
    user = create_user_and_session("csrf_test@test.com")
    lead_id = seed_lead(user["user_id"])

    # 1. Cookie request WITHOUT CSRF header -> 403 Forbidden
    cookie_no_csrf = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        headers={"Cookie": f"session_id={user['session_token']}"},
        json={"pipeline_status": "contacted"},
    )
    assert cookie_no_csrf.status_code == 403
    assert "CSRF" in cookie_no_csrf.json()["detail"]

    # 2. Cookie request with INVALID CSRF header -> 403 Forbidden
    cookie_bad_csrf = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        headers={
            "Cookie": f"session_id={user['session_token']}",
            "X-CSRF-Token": "invalid_forged_csrf_token",
        },
        json={"pipeline_status": "contacted"},
    )
    assert cookie_bad_csrf.status_code == 403

    # 3. Bearer token request WITHOUT CSRF header -> 200 OK
    bearer_ok = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        headers={"Authorization": f"Bearer {user['session_token']}"},
        json={"pipeline_status": "contacted"},
    )
    assert bearer_ok.status_code == 200


def test_cors_origin_restrictions():
    """Verify CORS rejects untrusted origins and approves localhost / 127.0.0.1."""
    # Untrusted origin: Starlette CORS middleware omits Access-Control-Allow-Origin
    untrusted_resp = client.get(
        "/api/health",
        headers={"Origin": "https://evil-attacker-website.com"},
    )
    assert untrusted_resp.status_code == 200
    assert untrusted_resp.headers.get("access-control-allow-origin") is None

    # Trusted dev server origin
    trusted_resp = client.get(
        "/api/health",
        headers={"Origin": "http://localhost:5173"},
    )
    assert trusted_resp.status_code == 200
    assert trusted_resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


@pytest.mark.asyncio
async def test_ssrf_protection_auditor_blocks_loopback_and_metadata():
    """Verify WebsiteAuditor safely blocks loopback, private IP, and cloud metadata targets."""
    auditor = WebsiteAuditor()

    # Loopback target blocked
    res_loopback = await auditor.audit("http://127.0.0.1:8000/admin", business_name="Loopback Target")
    assert res_loopback.is_reachable is False
    assert any("security policy" in f.summary.lower() or "blocked" in f.summary.lower() for f in res_loopback.findings)

    # AWS Metadata target blocked
    res_metadata = await auditor.audit("http://169.254.169.254/latest/meta-data/", business_name="Metadata Target")
    assert res_metadata.is_reachable is False
    assert any("security policy" in f.summary.lower() or "blocked" in f.summary.lower() for f in res_metadata.findings)

    # Private network RFC 1918 blocked
    res_private = await auditor.audit("http://192.168.1.1/router", business_name="Private IP Target")
    assert res_private.is_reachable is False
    assert any("security policy" in f.summary.lower() or "blocked" in f.summary.lower() for f in res_private.findings)


def test_secret_sanitization_in_errors():
    """Verify that sensitive tokens and credentials are not leaked in error messages."""
    # 1. Login failure produces generic message
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "nonexistent@test.com", "password": "WrongPassword123!"},
    )
    assert login_resp.status_code == 401
    assert login_resp.json()["detail"] == "Invalid email or password."
    assert "hash" not in login_resp.text
    assert "password" not in login_resp.json()["detail"].lower() or login_resp.json()["detail"] == "Invalid email or password."


# ==============================================================================
# 8. PWA SERVICE WORKER & OFFLINE SAFETY CONTRACTS
# ==============================================================================

def test_pwa_service_worker_does_not_cache_authenticated_api():
    """Verify frontend/public/sw.js never caches API routes in browser Cache Storage."""
    sw_path = Path(__file__).resolve().parent.parent.parent / "frontend" / "public" / "sw.js"
    assert sw_path.exists(), f"Service Worker not found at {sw_path}"

    sw_content = sw_path.read_text(encoding="utf-8")

    # Confirm cache name is defined for shell
    assert "CACHE_NAME" in sw_content

    # Confirm static shell URLs allowlist is defined
    assert "STATIC_SHELL_URLS" in sw_content

    # Confirm explicit API bypass rule exists and avoids cache storage
    assert "url.pathname.startsWith('/api/')" in sw_content
    assert "fetch(request)" in sw_content


def test_start_job_idempotency_conflict_rejection():
    """Verify reusing an Idempotency-Key with conflicting job payload returns 409 Conflict."""
    user = create_user_and_session("idemp_conflict@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "unique-job-key-12345",
    }
    initial_payload = {
        "location": "Koramangala",
        "radius_km": 1.5,
        "categories": ["cafe"],
        "max_leads": 10,
    }

    # Initial job start
    res1 = client.post("/api/jobs", headers=headers, json=initial_payload)
    assert res1.status_code == 200
    job_id = res1.json()["id"]

    # Replay with identical payload -> 200 cached response
    res_replay = client.post("/api/jobs", headers=headers, json=initial_payload)
    assert res_replay.status_code == 200
    assert res_replay.json()["id"] == job_id

    # Replay with conflicting payload -> 409 Conflict
    conflict_payload = {
        "location": "Indiranagar",  # Different location!
        "radius_km": 2.0,
        "categories": ["restaurant"],
        "max_leads": 50,
    }
    res_conflict = client.post("/api/jobs", headers=headers, json=conflict_payload)
    assert res_conflict.status_code == 409
    assert "conflict" in res_conflict.json()["detail"].lower()


def test_outbox_replay_idor_prevention():
    """Verify an attacker cannot replay another user's outbox item across accounts."""
    alice = create_user_and_session("alice_outbox@test.com")
    bob = create_user_and_session("bob_outbox@test.com")

    alice_lead = seed_lead(alice["user_id"], pipeline_status="new")

    # Bob's client attempts to submit an outbox mutation targeting Alice's lead
    bob_headers = {
        "Authorization": f"Bearer {bob['session_token']}",
        "Idempotency-Key": "forged-outbox-key-001",
    }
    bob_replay = client.patch(
        f"/api/leads/{alice_lead}/pipeline",
        headers=bob_headers,
        json={"pipeline_status": "won", "notes": "Hacked deal"},
    )
    # Server rejects mutation with 404 (lead does not exist in Bob's tenant partition)
    assert bob_replay.status_code == 404
    assert bob_replay.json()["detail"] == "Lead not found."

    # Alice's lead remains completely untouched
    with get_db() as conn:
        row = conn.execute("SELECT pipeline_status, notes FROM leads WHERE id = ?", (alice_lead,)).fetchone()
        assert row["pipeline_status"] == "new"
        assert row["notes"] is None


def test_cli_credential_store_advapi32_security():
    """Verify native Windows Credential Manager store stores, reads, and deletes safely."""
    from app.cli.credentials import WindowsCredentialStore
    store = WindowsCredentialStore()

    target = "WebsiteLeadHunterTest/multi_client_verification"
    user = "verify_user@example.com"
    token = "sess_mock_token_secret_12345"

    try:
        # Write
        saved = store.write(target, user, token)
        assert saved is True

        # Read
        read_user, read_token = store.read(target)
        assert read_user == user
        assert read_token == token

        # Delete
        deleted = store.delete(target)
        assert deleted is True

        # Read after delete returns None
        assert store.read(target) is None
    finally:
        # Cleanup
        try:
            store.delete(target)
        except Exception:
            pass

