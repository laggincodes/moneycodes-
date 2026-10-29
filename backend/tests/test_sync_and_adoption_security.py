"""
Adversarial Verification Suite for Offline Sync Idempotency, Account Isolation,
and Safe Legacy Data Adoption Gate.
Strictly offline — uses isolated temporary SQLite databases per test run.
"""
import os
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app
from app.database import init_db, get_db
from app.auth.security import _failed_attempts
from app.migrations import run_migrations

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database for each test and resets rate limiting."""
    db_file = str(tmp_path / "test_sync_adoption.db")
    init_db(db_file)
    _failed_attempts.clear()
    yield
    _failed_attempts.clear()


def register_user(email: str, password: str, full_name: str = "Test User") -> dict:
    """Helper to register a user and return authentication tokens and cookies."""
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
    return {
        "user": data["user"],
        "csrf_token": data["csrf_token"],
        "session_token": data["session_token"],
        "cookies": res.cookies,
    }


def create_test_lead(user_id: str, lead_id: str = None, provider_id: str = None, status: str = "new") -> str:
    """Helper to insert a test lead owned by a specific user."""
    lid = lead_id or f"lead_{uuid.uuid4().hex[:8]}"
    pid = provider_id or f"osm:{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon,
                source_url, provenance, raw_data, score, pipeline_status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, 'Test Bakery', 'Bakery', 12.95, 77.60, '', '{}', '{}', 80, ?, ?, ?)
            """,
            (lid, user_id, pid, status, now, now),
        )
    return lid


# ==============================================================================
# A. OUTBOX IDEMPOTENCY & MUTATION DEDUPLICATION TESTS
# ==============================================================================

def test_migration_v2_creates_idempotency_table():
    """Verifies that Migration v2 adds idempotency_keys table with composite primary key."""
    with get_db() as conn:
        # Check table exists
        table_row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='idempotency_keys'"
        ).fetchone()
        assert table_row[0] == 1

        # Check schema version
        ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert ver >= 2

        # Check column names
        cols = [r[1] for r in conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()]
        assert "user_id" in cols
        assert "key" in cols
        assert "action_type" in cols
        assert "response_body" in cols
        assert "expires_at" in cols


def test_idempotent_pipeline_update_duplicate_returns_identical_response():
    """
    Submitting the exact same operation with the same Idempotency-Key twice
    must return the identical response without updating timestamps or re-running side effects.
    """
    user = register_user("idempotent1@test.com", "Password123!")
    lead_id = create_test_lead(user["user"]["id"])

    headers = {
        "X-CSRF-Token": user["csrf_token"],
        "Idempotency-Key": "idem_update_pipeline_001",
    }
    cookies = {"session_id": user["session_token"]}

    # 1. First execution
    res1 = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "contacted", "notes": "First pitch sent via email"},
        headers=headers,
        cookies=cookies,
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["pipeline_status"] == "contacted"
    assert data1["notes"] == "First pitch sent via email"
    updated_at_1 = data1["updated_at"]
    contacted_at_1 = data1["contacted_at"]
    assert contacted_at_1 is not None

    # Verify idempotency key is recorded in DB
    with get_db() as conn:
        key_row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?",
            (user["user"]["id"], "idem_update_pipeline_001"),
        ).fetchone()
        assert key_row is not None
        assert key_row["action_type"] == "UPDATE_PIPELINE"
        assert key_row["resource_id"] == lead_id

    # 2. Duplicate submission with exact same key
    res2 = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "contacted", "notes": "First pitch sent via email"},
        headers=headers,
        cookies=cookies,
    )
    assert res2.status_code == 200
    data2 = res2.json()

    # Exact equality of response
    assert data2["pipeline_status"] == data1["pipeline_status"]
    assert data2["updated_at"] == updated_at_1
    assert data2["contacted_at"] == contacted_at_1
    assert data2["notes"] == data1["notes"]

    # Verify database state was NOT modified again
    with get_db() as conn:
        lead_row = conn.execute("SELECT updated_at, contacted_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
        assert lead_row["updated_at"] == updated_at_1
        assert lead_row["contacted_at"] == contacted_at_1


def test_idempotent_duplicate_submission_blocks_different_side_effects():
    """
    If a client replays an idempotency key with modified payload (e.g. race condition or malformed retry),
    the server returns the original cached response and DOES NOT apply the new mutation.
    """
    user = register_user("idempotent2@test.com", "Password123!")
    lead_id = create_test_lead(user["user"]["id"])

    headers = {
        "X-CSRF-Token": user["csrf_token"],
        "Idempotency-Key": "idem_update_pipeline_guard",
    }
    cookies = {"session_id": user["session_token"]}

    # Original execution: set to 'contacted'
    res1 = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "contacted", "notes": "Initial contact"},
        headers=headers,
        cookies=cookies,
    )
    assert res1.status_code == 200
    assert res1.json()["pipeline_status"] == "contacted"

    # Replay with same key but conflicting status 'won'
    res2 = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "won", "notes": "Attempted override"},
        headers=headers,
        cookies=cookies,
    )
    assert res2.status_code == 200
    # Must return cached result, NOT 'won'
    assert res2.json()["pipeline_status"] == "contacted"
    assert res2.json()["notes"] == "Initial contact"

    # Verify DB still has 'contacted'
    with get_db() as conn:
        lead_row = conn.execute("SELECT pipeline_status, notes FROM leads WHERE id = ?", (lead_id,)).fetchone()
        assert lead_row["pipeline_status"] == "contacted"
        assert lead_row["notes"] == "Initial contact"


def test_simulated_lost_response_and_retry():
    """
    Simulates network drop: server processes update, client loses HTTP connection before
    receiving response, then resends identical request from outbox. Client gets identical result.
    """
    user = register_user("idempotent3@test.com", "Password123!")
    lead_id = create_test_lead(user["user"]["id"])

    idem_key = f"idem_retry_{uuid.uuid4().hex}"
    headers = {
        "X-CSRF-Token": user["csrf_token"],
        "Idempotency-Key": idem_key,
    }
    cookies = {"session_id": user["session_token"]}

    # Initial request (client assumes connection severed before response body arrives)
    res_initial = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "qualified", "notes": "Audit completed"},
        headers=headers,
        cookies=cookies,
    )
    assert res_initial.status_code == 200

    # Retried request from outbox sync engine
    res_retry = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "qualified", "notes": "Audit completed"},
        headers=headers,
        cookies=cookies,
    )
    assert res_retry.status_code == 200
    assert res_retry.json() == res_initial.json()


def test_idempotency_key_account_isolation():
    """
    Verifies that User A and User B can use the same idempotency key string without collisions
    or cross-tenant data leakage, because the key is scoped to (user_id, key).
    """
    user_a = register_user("user_a_idem@test.com", "Password123!")
    user_b = register_user("user_b_idem@test.com", "Password123!")

    lead_a = create_test_lead(user_a["user"]["id"])
    lead_b = create_test_lead(user_b["user"]["id"])

    shared_key = "common_outbox_key_001"

    # User A performs mutation
    res_a = client.patch(
        f"/api/leads/{lead_a}/pipeline",
        json={"pipeline_status": "contacted", "notes": "User A note"},
        headers={"X-CSRF-Token": user_a["csrf_token"], "Idempotency-Key": shared_key},
        cookies={"session_id": user_a["session_token"]},
    )
    assert res_a.status_code == 200
    assert res_a.json()["notes"] == "User A note"

    # User B performs mutation on their own lead with the same key
    res_b = client.patch(
        f"/api/leads/{lead_b}/pipeline",
        json={"pipeline_status": "meeting", "notes": "User B note"},
        headers={"X-CSRF-Token": user_b["csrf_token"], "Idempotency-Key": shared_key},
        cookies={"session_id": user_b["session_token"]},
    )
    assert res_b.status_code == 200
    # Must NOT return User A's cached response
    assert res_b.json()["notes"] == "User B note"
    assert res_b.json()["pipeline_status"] == "meeting"
    assert res_b.json()["id"] == lead_b


def test_idempotency_key_length_validation():
    """Idempotency keys longer than 255 characters must be rejected with 400 Bad Request."""
    user = register_user("idem_long@test.com", "Password123!")
    lead_id = create_test_lead(user["user"]["id"])

    huge_key = "k" * 256
    res = client.patch(
        f"/api/leads/{lead_id}/pipeline",
        json={"pipeline_status": "contacted"},
        headers={"X-CSRF-Token": user["csrf_token"], "Idempotency-Key": huge_key},
        cookies={"session_id": user["session_token"]},
    )
    assert res.status_code == 400
    assert "exceeds maximum allowed length" in res.json()["detail"]


# ==============================================================================
# B. ACCOUNT ISOLATION IN SYNC & FORGED REPLAY DEFENSE
# ==============================================================================

def test_forged_outbox_replay_idor_blocked_by_server():
    """
    Even if an offline outbox item is tampered with or submitted under User B's session
    referencing User A's lead, the server enforces independent authorization and returns 404.
    """
    user_a = register_user("owner_a@test.com", "Password123!")
    user_b = register_user("attacker_b@test.com", "Password123!")

    lead_a = create_test_lead(user_a["user"]["id"])

    # User B tries to update Lead A
    res = client.patch(
        f"/api/leads/{lead_a}/pipeline",
        json={"pipeline_status": "won", "notes": "Malicious takeover attempt"},
        headers={
            "X-CSRF-Token": user_b["csrf_token"],
            "Idempotency-Key": "forged_replay_key_01",
        },
        cookies={"session_id": user_b["session_token"]},
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "Lead not found."

    # Verify Lead A was unchanged
    with get_db() as conn:
        row = conn.execute("SELECT pipeline_status, notes FROM leads WHERE id = ?", (lead_a,)).fetchone()
        assert row["pipeline_status"] == "new"
        assert row["notes"] is None


# ==============================================================================
# C. LEGACY DATA ADOPTION SECURITY GATE TESTS (HARDENED PHASE 1)
# ==============================================================================

def test_default_registration_does_not_adopt_legacy_data():
    """
    Registration must NEVER claim unowned records.
    The new user account starts clean, and legacy records remain preserved with user_id NULL.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES ('leg-job-def', NULL, 'completed', 'Indiranagar', 12.97, 77.64, 1.0, '[]', 10, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('leg-lead-def', NULL, 'osm:def01', 'Legacy Cafe', 'Cafe', 12.97, 77.64, '', '{}', '{}', ?, ?)
            """,
            (now, now),
        )

    # Register first user
    auth = register_user("clean_first_user@test.com", "Password123!")

    # Verify user owns 0 leads
    res = client.get("/api/leads", cookies={"session_id": auth["session_token"]})
    assert res.status_code == 200
    assert res.json()["total"] == 0

    # Verify legacy records in DB still have user_id IS NULL
    with get_db() as conn:
        job_owner = conn.execute("SELECT user_id FROM jobs WHERE id = 'leg-job-def'").fetchone()[0]
        lead_owner = conn.execute("SELECT user_id FROM leads WHERE id = 'leg-lead-def'").fetchone()[0]
        assert job_owner is None
        assert lead_owner is None


def test_registration_with_legacy_flag_ignored():
    """
    Even if an adversary sends an explicit adopt_legacy_data flag in registration payload,
    it is ignored and never adopts legacy records.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('leg-lead-ignored', NULL, 'osm:ign01', 'Ignored Legacy Cafe', 'Cafe', 12.97, 77.64, '', '{}', '{}', ?, ?)
            """,
            (now, now),
        )

    res = client.post(
        "/api/auth/register",
        json={
            "email": "flag_user@test.com",
            "password": "Password123!",
            "adopt_legacy_data": True,
        },
    )
    assert res.status_code == 200

    # Verify legacy lead was NOT adopted
    with get_db() as conn:
        lead_owner = conn.execute("SELECT user_id FROM leads WHERE id = 'leg-lead-ignored'").fetchone()[0]
        assert lead_owner is None


def test_old_web_adoption_endpoint_removed():
    """The legacy /api/auth/adopt-legacy-leads endpoint must return 404 or method not allowed (eliminated)."""
    user = register_user("endpoint_check_user@test.com", "Password123!")
    res = client.post(
        "/api/auth/adopt-legacy-leads",
        cookies={"session_id": user["session_token"]},
        headers={"X-CSRF-Token": user["csrf_token"]},
    )
    assert res.status_code in (404, 405)


def test_admin_adoption_endpoint_requires_auth():
    """Unauthenticated requests to POST /api/auth/admin/adopt-legacy must fail with 401."""
    res = client.post(
        "/api/auth/admin/adopt-legacy",
        json={"password": "AnyPassword123!", "confirm": True},
    )
    assert res.status_code == 401


def test_admin_adoption_endpoint_requires_confirmation():
    """Authenticated request with confirm=False must fail with 400 Bad Request."""
    user = register_user("admin_confirm_test@test.com", "Password123!")
    res = client.post(
        "/api/auth/admin/adopt-legacy",
        json={"password": "Password123!", "confirm": False},
        cookies={"session_id": user["session_token"]},
        headers={"X-CSRF-Token": user["csrf_token"]},
    )
    assert res.status_code == 400
    assert "explicit confirmation" in res.json()["detail"].lower()


def test_admin_adoption_endpoint_requires_valid_password():
    """Authenticated request with wrong password must fail with 403 Forbidden."""
    user = register_user("admin_pass_test@test.com", "Password123!")
    res = client.post(
        "/api/auth/admin/adopt-legacy",
        json={"password": "WrongPassword123!", "confirm": True},
        cookies={"session_id": user["session_token"]},
        headers={"X-CSRF-Token": user["csrf_token"]},
    )
    assert res.status_code == 403
    assert "Invalid administrative credentials" in res.json()["detail"]


def test_admin_adoption_endpoint_requires_bootstrap_token(tmp_path):
    """Authenticated request with correct password but missing token file or header fails with 403."""
    user = register_user("admin_token_test@test.com", "Password123!")
    fake_token_path = tmp_path / "nonexistent.token"
    os.environ["ADMIN_TOKEN_PATH"] = str(fake_token_path)

    try:
        # 1. Missing header
        res_no_hdr = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={"X-CSRF-Token": user["csrf_token"]},
        )
        assert res_no_hdr.status_code == 403

        # 2. Header provided but disk file does not exist
        res_no_disk = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "some-token",
            },
        )
        assert res_no_disk.status_code == 403
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)


def test_admin_adoption_endpoint_rejects_mismatched_bootstrap_token(tmp_path):
    """Header token not matching disk token must fail with 403."""
    user = register_user("admin_mismatch_test@test.com", "Password123!")
    token_file = tmp_path / ".admin_token"
    token_file.write_text("actual-disk-token-abc", encoding="utf-8")
    os.environ["ADMIN_TOKEN_PATH"] = str(token_file)

    try:
        res = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "forged-or-wrong-token-xyz",
            },
        )
        assert res.status_code == 403
        assert "Invalid local bootstrap token" in res.json()["detail"]
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)


def test_admin_adoption_endpoint_consumes_token_immediately(tmp_path):
    """The local bootstrap token is deleted upon consumption and cannot be replayed."""
    user = register_user("admin_replay_test@test.com", "Password123!")
    token_file = tmp_path / ".admin_token"
    token_file.write_text("single-use-token-777", encoding="utf-8")
    os.environ["ADMIN_TOKEN_PATH"] = str(token_file)

    try:
        # First call succeeds
        res1 = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "single-use-token-777",
            },
        )
        assert res1.status_code == 200

        # Token file on disk must now be gone
        assert not token_file.exists()

        # Replay attempt must fail with 403
        res2 = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "single-use-token-777",
            },
        )
        assert res2.status_code == 403
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)


def test_admin_adoption_endpoint_success_and_idempotency(tmp_path):
    """
    Valid local administrative adoption claims unowned records.
    Subsequent adoption runs report 0 adopted records (idempotent).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES ('leg-job-idem', NULL, 'completed', 'Indiranagar', 12.97, 77.64, 1.0, '[]', 10, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('leg-lead-idem-1', NULL, 'osm:idem01', 'Idem Cafe 1', 'Cafe', 12.97, 77.64, '', '{}', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('leg-lead-idem-2', NULL, 'osm:idem02', 'Idem Cafe 2', 'Cafe', 12.97, 77.64, '', '{}', '{}', ?, ?)
            """,
            (now, now),
        )

    user = register_user("admin_idem_test@test.com", "Password123!")

    # Check summary endpoint
    res_summary = client.get(
        "/api/auth/admin/legacy-summary",
        cookies={"session_id": user["session_token"]},
    )
    assert res_summary.status_code == 200
    assert res_summary.json()["unowned_leads"] == 2
    assert res_summary.json()["unowned_jobs"] == 1

    # First adoption run
    token_file1 = tmp_path / ".admin_token"
    token_file1.write_text("first-token-run", encoding="utf-8")
    os.environ["ADMIN_TOKEN_PATH"] = str(token_file1)

    try:
        res1 = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "first-token-run",
            },
        )
        assert res1.status_code == 200
        assert res1.json()["adopted_leads"] == 2
        assert res1.json()["adopted_jobs"] == 1
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)

    # Summary should now be 0
    res_summary2 = client.get(
        "/api/auth/admin/legacy-summary",
        cookies={"session_id": user["session_token"]},
    )
    assert res_summary2.json()["unowned_leads"] == 0
    assert res_summary2.json()["unowned_jobs"] == 0

    # Second adoption run with a new token
    token_file2 = tmp_path / ".admin_token_2"
    token_file2.write_text("second-token-run", encoding="utf-8")
    os.environ["ADMIN_TOKEN_PATH"] = str(token_file2)

    try:
        res2 = client.post(
            "/api/auth/admin/adopt-legacy",
            json={"password": "Password123!", "confirm": True},
            cookies={"session_id": user["session_token"]},
            headers={
                "X-CSRF-Token": user["csrf_token"],
                "X-Admin-Bootstrap-Token": "second-token-run",
            },
        )
        assert res2.status_code == 200
        assert res2.json()["adopted_leads"] == 0
        assert res2.json()["adopted_jobs"] == 0
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)
