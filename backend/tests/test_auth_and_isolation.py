"""
Adversarial Verification Suite for Authentication, Authorization & Account Isolation.
Covers registration, login, scrypt verification, rate limiting, generic errors,
session lifecycle, CSRF protection, and multi-tenant isolation across all endpoints.
Offline only — never contacts external networks.
"""
import os
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app
from app.database import init_db, get_db
from app.auth.security import (
    hash_password,
    verify_password,
    _failed_attempts,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_auth_db(tmp_path):
    """Sets up an isolated SQLite database and clears rate limiter state."""
    db_file = str(tmp_path / "test_auth_isolation.db")
    init_db(db_file)
    _failed_attempts.clear()
    yield
    _failed_attempts.clear()


def register_user(email: str, password: str, full_name: str = "Test User") -> dict:
    """Helper to register a user and return the client-authenticated session dictionary."""
    res = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": full_name,
        },
    )
    assert res.status_code == 200, f"Registration failed: {res.text}"
    data = res.json()
    # Save cookies and tokens
    return {
        "user": data["user"],
        "csrf_token": data["csrf_token"],
        "session_token": data["session_token"],
        "cookies": res.cookies,
    }


# ==============================================================================
# 1. AUTHENTICATION & CREDENTIAL SECURITY TESTS
# ==============================================================================

def test_user_registration_success():
    auth = register_user("Alice@Example.COM ", "SecurePass123!", "Alice")
    assert auth["user"]["email"] == "alice@example.com"  # Normalized
    assert auth["csrf_token"]
    assert auth["session_token"]

    # Verify password hash in DB is scrypt and not plaintext
    with get_db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (auth["user"]["id"],)).fetchone()
        assert row is not None
        assert row["password_hash"].startswith("scrypt$16384$8$1$")
        assert "SecurePass123!" not in row["password_hash"]
        assert verify_password("SecurePass123!", row["password_hash"])


def test_user_registration_duplicate_email():
    register_user("bob@example.com", "Password123!")
    res = client.post(
        "/api/auth/register",
        json={"email": "BOB@example.com", "password": "AnotherPassword123!"},
    )
    assert res.status_code == 400
    assert "already exists" in res.json()["detail"]


def test_password_policy_enforcement():
    # Less than 8 characters
    res_short = client.post(
        "/api/auth/register",
        json={"email": "short@example.com", "password": "short"},
    )
    assert res_short.status_code == 400
    assert "at least 8 characters" in res_short.json()["detail"]

    # More than 128 characters
    huge_pass = "A" * 129
    res_huge = client.post(
        "/api/auth/register",
        json={"email": "huge@example.com", "password": huge_pass},
    )
    assert res_huge.status_code == 400
    assert "cannot exceed 128 characters" in res_huge.json()["detail"]


def test_invalid_email_format():
    res = client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "Password123!"},
    )
    assert res.status_code == 400
    assert "Invalid email format" in res.json()["detail"]


def test_login_success_and_cookies():
    register_user("carol@example.com", "MyPassword123!")

    # Login
    res = client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": "MyPassword123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["user"]["email"] == "carol@example.com"
    assert "session_id" in res.cookies
    assert "csrf_token" in res.cookies


def test_login_invalid_credentials_generic_error():
    register_user("dan@example.com", "CorrectPass123!")

    # 1. Wrong password -> generic error
    res_bad_pw = client.post(
        "/api/auth/login",
        json={"email": "dan@example.com", "password": "WrongPassword123!"},
    )
    assert res_bad_pw.status_code == 401
    assert res_bad_pw.json()["detail"] == "Invalid email or password."

    # 2. Non-existent email -> same generic error (no user enumeration)
    res_no_user = client.post(
        "/api/auth/login",
        json={"email": "nonexistent@example.com", "password": "AnyPassword123!"},
    )
    assert res_no_user.status_code == 401
    assert res_no_user.json()["detail"] == "Invalid email or password."


def test_login_rate_limiting():
    # 5 failed attempts
    for _ in range(5):
        res = client.post(
            "/api/auth/login",
            json={"email": "target@example.com", "password": "badpassword"},
        )
        assert res.status_code == 401

    # 6th attempt should be blocked by rate limiter
    res_blocked = client.post(
        "/api/auth/login",
        json={"email": "target@example.com", "password": "badpassword"},
    )
    assert res_blocked.status_code == 429
    assert "Too many failed login attempts" in res_blocked.json()["detail"]


def test_logout_invalidates_session():
    auth = register_user("eve@example.com", "Password123!")

    # /api/auth/me should succeed with session
    res_me = client.get("/api/auth/me", cookies={"session_id": auth["session_token"]})
    assert res_me.status_code == 200
    assert res_me.json()["user"]["email"] == "eve@example.com"

    # Logout
    res_logout = client.post("/api/auth/logout", cookies={"session_id": auth["session_token"]})
    assert res_logout.status_code == 200

    # /api/auth/me should now fail with 401
    res_me_after = client.get("/api/auth/me", cookies={"session_id": auth["session_token"]})
    assert res_me_after.status_code == 401


def test_session_expiration():
    auth = register_user("frank@example.com", "Password123!")

    # Manually expire the session in database
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (past_time, auth["session_token"]))

    # Request with expired session token
    res = client.get("/api/auth/me", cookies={"session_id": auth["session_token"]})
    assert res.status_code == 401


# ==============================================================================
# 2. CSRF & AUTHORIZATION BOUNDARY TESTS
# ==============================================================================

def test_unauthenticated_requests_blocked():
    # All protected routes return 401 when no session is provided
    assert client.get("/api/leads").status_code == 401
    assert client.get("/api/jobs/active").status_code == 401
    assert client.get("/api/export/csv").status_code == 401
    assert client.get("/api/discovery/geocode?q=Bengaluru").status_code == 401


def test_csrf_token_required_on_browser_mutations():
    auth = register_user("grace@example.com", "Password123!")
    cookies = {"session_id": auth["session_token"]}

    # Seed a test lead for Grace
    lead_id = "grace-lead-01"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon, source_url,
                provenance, raw_data, pipeline_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, auth["user"]["id"], "osm:101", "Grace Cafe", "Cafe", 12.0, 77.0, "https://osm.org", "{}", "{}", "new", now, now),
        )

    # 1. State-changing request WITHOUT X-CSRF-Token header -> 403 Forbidden
    res_no_csrf = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "qualified"},
        cookies=cookies,
    )
    assert res_no_csrf.status_code == 403
    assert "CSRF" in res_no_csrf.json()["detail"]

    # 2. State-changing request with WRONG X-CSRF-Token header -> 403 Forbidden
    res_bad_csrf = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "qualified"},
        headers={"X-CSRF-Token": "wrong_token_123"},
        cookies=cookies,
    )
    assert res_bad_csrf.status_code == 403

    # 3. State-changing request with VALID X-CSRF-Token header -> 200 OK
    res_valid_csrf = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "qualified"},
        headers={"X-CSRF-Token": auth["csrf_token"]},
        cookies=cookies,
    )
    assert res_valid_csrf.status_code == 200
    assert res_valid_csrf.json()["pipeline_status"] == "qualified"


def test_cli_bearer_token_access():
    auth = register_user("cli_user@example.com", "Password123!")

    # CLI requests pass Authorization: Bearer <token> and do not need CSRF header
    res = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {auth['session_token']}"},
    )
    assert res.status_code == 200
    assert res.json()["user"]["email"] == "cli_user@example.com"


# ==============================================================================
# 3. STRICT MULTI-TENANT ACCOUNT ISOLATION TESTS
# ==============================================================================

def test_account_isolation_leads_listing():
    user_a = register_user("user_a@test.com", "Password123!", "User A")
    user_b = register_user("user_b@test.com", "Password123!", "User B")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        # Seed Lead for User A
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon, source_url,
                provenance, raw_data, pipeline_status, score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("lead-a", user_a["user"]["id"], "osm:a", "A's Bakery", "Bakery", 12.0, 77.0, "https://osm.org", "{}", "{}", "new", 80, now, now),
        )
        # Seed Lead for User B
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon, source_url,
                provenance, raw_data, pipeline_status, score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("lead-b", user_b["user"]["id"], "osm:b", "B's Gym", "Gym", 12.0, 77.0, "https://osm.org", "{}", "{}", "new", 60, now, now),
        )

    # User A requests /api/leads -> sees only Lead A
    res_a = client.get("/api/leads", cookies={"session_id": user_a["session_token"]})
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total"] == 1
    assert data_a["items"][0]["id"] == "lead-a"
    assert data_a["items"][0]["name"] == "A's Bakery"

    # User B requests /api/leads -> sees only Lead B
    res_b = client.get("/api/leads", cookies={"session_id": user_b["session_token"]})
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["total"] == 1
    assert data_b["items"][0]["id"] == "lead-b"
    assert data_b["items"][0]["name"] == "B's Gym"


def test_account_isolation_lead_detail_idor():
    user_a = register_user("user_a_idor@test.com", "Password123!")
    user_b = register_user("user_b_idor@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon, source_url,
                provenance, raw_data, pipeline_status, score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("lead-private-b", user_b["user"]["id"], "osm:b_private", "Secret B Corp", "Tech", 12.0, 77.0, "https://osm.org", "{}", "{}", "new", 90, now, now),
        )

    # User A attempts to view User B's lead by ID -> 404 Not Found (no data leakage)
    res = client.get(
        "/api/leads/lead-private-b",
        cookies={"session_id": user_a["session_token"]},
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "Lead not found."


def test_account_isolation_lead_update_and_delete_idor():
    user_a = register_user("user_a_mut@test.com", "Password123!")
    user_b = register_user("user_b_mut@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon, source_url,
                provenance, raw_data, pipeline_status, score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("lead-target-b", user_b["user"]["id"], "osm:target_b", "B Target", "Shop", 12.0, 77.0, "https://osm.org", "{}", "{}", "new", 75, now, now),
        )

    # User A attempts to PATCH User B's lead -> 404
    res_patch = client.patch(
        "/api/leads/lead-target-b/pipeline",
        json={"pipeline_status": "won"},
        headers={"X-CSRF-Token": user_a["csrf_token"]},
        cookies={"session_id": user_a["session_token"]},
    )
    assert res_patch.status_code == 404

    # User A attempts to DELETE User B's lead -> 404
    res_del = client.delete(
        "/api/leads/lead-target-b",
        headers={"X-CSRF-Token": user_a["csrf_token"]},
        cookies={"session_id": user_a["session_token"]},
    )
    assert res_del.status_code == 404

    # Verify B's lead was NOT modified or deleted
    with get_db() as conn:
        row = conn.execute("SELECT pipeline_status FROM leads WHERE id = 'lead-target-b'").fetchone()
        assert row is not None
        assert row["pipeline_status"] == "new"


def test_account_isolation_bulk_delete():
    user_a = register_user("user_a_bulk@test.com", "Password123!")
    user_b = register_user("user_b_bulk@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('lead-a1', ?, 'osm:a1', 'A1', 'Cat', 12.0, 77.0, '', '{}', '{}', ?, ?)
            """,
            (user_a["user"]["id"], now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('lead-b1', ?, 'osm:b1', 'B1', 'Cat', 12.0, 77.0, '', '{}', '{}', ?, ?)
            """,
            (user_b["user"]["id"], now, now),
        )

    # User A performs bulk delete
    res = client.delete(
        "/api/leads?all_leads=true",
        headers={"X-CSRF-Token": user_a["csrf_token"]},
        cookies={"session_id": user_a["session_token"]},
    )
    assert res.status_code == 200

    # User A's leads count is 0; User B's lead remains intact
    with get_db() as conn:
        count_a = conn.execute("SELECT COUNT(*) FROM leads WHERE user_id = ?", (user_a["user"]["id"],)).fetchone()[0]
        count_b = conn.execute("SELECT COUNT(*) FROM leads WHERE user_id = ?", (user_b["user"]["id"],)).fetchone()[0]
        assert count_a == 0
        assert count_b == 1


def test_account_isolation_export_csv():
    user_a = register_user("user_a_export@test.com", "Password123!")
    user_b = register_user("user_b_export@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, score, pipeline_status, created_at, updated_at)
            VALUES ('lead-exp-a', ?, 'osm:exp_a', 'Exclusive A Client', 'Law', 12.0, 77.0, '', '{}', '{}', 95, 'qualified', ?, ?)
            """,
            (user_a["user"]["id"], now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, score, pipeline_status, created_at, updated_at)
            VALUES ('lead-exp-b', ?, 'osm:exp_b', 'Secret B Prospect', 'Finance', 12.0, 77.0, '', '{}', '{}', 90, 'qualified', ?, ?)
            """,
            (user_b["user"]["id"], now, now),
        )

    # Export User A's leads
    res = client.get("/api/export/csv", cookies={"session_id": user_a["session_token"]})
    assert res.status_code == 200
    csv_text = res.text
    assert "Exclusive A Client" in csv_text
    assert "Secret B Prospect" not in csv_text


def test_account_isolation_jobs():
    user_a = register_user("user_a_jobs@test.com", "Password123!")
    user_b = register_user("user_b_jobs@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, total_found, processed_count, logs, created_at, updated_at)
            VALUES ('job-b', ?, 'running', 'Koramangala', 12.93, 77.62, 1.5, '[]', 25, 10, 5, '[]', ?, ?)
            """,
            (user_b["user"]["id"], now, now),
        )

    # User A tries to get User B's job by ID -> 404
    res_get = client.get("/api/jobs/job-b", cookies={"session_id": user_a["session_token"]})
    assert res_get.status_code == 404

    # User A checks active jobs -> does not see User B's running job
    res_active = client.get("/api/jobs/active", cookies={"session_id": user_a["session_token"]})
    assert res_active.status_code == 200
    assert res_active.json() is None


def test_legacy_data_adoption_by_first_user(tmp_path):
    """Verifies that unowned legacy leads and jobs are NOT claimed by registration, but through admin workflow."""
    db_file = str(tmp_path / "test_legacy_adoption.db")
    init_db(db_file)

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, total_found, processed_count, logs, created_at, updated_at)
            VALUES ('legacy-job-01', NULL, 'completed', 'Indiranagar', 12.97, 77.64, 1.0, '[]', 10, 5, 5, '[]', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, score, pipeline_status, created_at, updated_at)
            VALUES ('legacy-lead-01', NULL, 'osm:leg01', 'Legacy Cafe', 'Cafe', 12.97, 77.64, '', '{}', '{}', 70, 'qualified', ?, ?)
            """,
            (now, now),
        )

    # Register first user
    auth = register_user("founder@company.com", "Password123!", "Founder")
    user_id = auth["user"]["id"]

    # Verify registration did NOT adopt the records
    with get_db() as conn:
        job_owner = conn.execute("SELECT user_id FROM jobs WHERE id = 'legacy-job-01'").fetchone()[0]
        lead_owner = conn.execute("SELECT user_id FROM leads WHERE id = 'legacy-lead-01'").fetchone()[0]
        assert job_owner is None
        assert lead_owner is None

    # Now execute administrative adoption with local bootstrap token
    token_file = tmp_path / ".admin_token"
    token_file.write_text("secret-test-token-12345", encoding="utf-8")
    os.environ["ADMIN_TOKEN_PATH"] = str(token_file)

    try:
        res_adopt = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            headers={
                "Authorization": f"Bearer {auth['session_token']}",
                "X-CSRF-Token": auth["csrf_token"],
                "X-Admin-Bootstrap-Token": "secret-test-token-12345",
            },
        )
        assert res_adopt.status_code == 200
        assert res_adopt.json()["adopted_leads"] == 1
        assert res_adopt.json()["adopted_jobs"] == 1

        # Verify both the legacy job and lead were claimed
        with get_db() as conn:
            job_owner = conn.execute("SELECT user_id FROM jobs WHERE id = 'legacy-job-01'").fetchone()[0]
            lead_owner = conn.execute("SELECT user_id FROM leads WHERE id = 'legacy-lead-01'").fetchone()[0]
            assert job_owner == user_id
            assert lead_owner == user_id
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)


def test_trusted_host_middleware_blocks_disallowed_hosts():
    """Verifies that TrustedHostMiddleware rejects requests with untrusted Host headers."""
    # Disallowed host header
    res_bad = client.get("/api/health", headers={"Host": "evil.com"})
    assert res_bad.status_code == 400

    # Disallowed host with port
    res_bad_port = client.get("/api/health", headers={"Host": "attacker.net:8000"})
    assert res_bad_port.status_code == 400

    # Allowed localhost
    res_local = client.get("/api/health", headers={"Host": "localhost:8000"})
    assert res_local.status_code == 200

    # Allowed 127.0.0.1
    res_loopback = client.get("/api/health", headers={"Host": "127.0.0.1:8000"})
    assert res_loopback.status_code == 200
