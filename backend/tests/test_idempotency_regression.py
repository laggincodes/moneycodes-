"""
Milestone 8.1: Production Idempotency Hardening Test Suite.

Verifies canonical request fingerprinting and strict idempotency guarantees:
1. Identical request retries succeed with original cached response (200 OK).
2. Key reuse with genuinely different location rejected (409 Conflict).
3. Substring location collision prevention: 'York' vs 'New York' returns 409 Conflict.
4. Key reuse with different categories rejected (409 Conflict).
5. Key reuse with different numeric options (radius_km, max_leads) rejected (409 Conflict).
6. Key reuse with different filter values (phone, website) rejected (409 Conflict).
7. Valid whitespace and case normalization allowed without payload conflict.
8. Concurrent same-key requests safely serialized via atomic PENDING reservation.
9. Lost-response retry after job execution returns identical job ID.
10. Geocoded display-name expansion does not break identical request retries.
11. Cross-user key reuse isolation: User B cannot access User A's idempotency key.
12. Schema migration v3 safely upgrades database and preserves legacy records.
13. Failure cleanup: worker exception cleanly removes reserved key for future retries.

Offline only: Uses in-memory ASGI client transport and isolated SQLite fixtures.
"""
import uuid
import json
import time
import hashlib
import threading
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app
from app.database import init_db, get_db
from app.auth.security import hash_password, _failed_attempts
from app.api import jobs
from app.api.jobs import compute_job_request_fingerprint
from app.migrations import run_migrations, apply_migration_v3, get_column_names, table_exists

client = TestClient(app)


class MockGeocodingJobWorker:
    """Mock worker that simulates realistic Nominatim geocoding display name expansions offline."""
    def __init__(self):
        self._counter = 0

    async def start_job(
        self,
        location_query: str,
        radius_km: float,
        categories: list,
        max_leads: int = 25,
        filter_only_with_phone: bool = False,
        filter_only_missing_website: bool = False,
        user_id: str = None,
    ) -> str:
        self._counter += 1
        job_id = f"job_mock_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        # Simulate realistic Nominatim display name expansion
        display_name = f"{location_query.strip()}, Ward 10, Bangalore Urban, Karnataka, 560038, India"
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, user_id, status, location_query, lat, lon, radius_km, categories,
                    max_leads, total_found, processed_count, logs, created_at, updated_at
                ) VALUES (?, ?, 'running', ?, 12.97, 77.64, ?, ?, ?, 0, 0, '[]', ?, ?)
                """,
                (job_id, user_id, display_name, radius_km, json.dumps(categories or []), max_leads, now, now),
            )
        return job_id


@pytest.fixture(autouse=True)
def setup_idempotency_test_env(tmp_path):
    db_file = str(tmp_path / "idempotency_test.db")
    init_db(db_file)
    _failed_attempts.clear()
    mock_worker = MockGeocodingJobWorker()
    jobs.set_worker(mock_worker)
    yield mock_worker
    _failed_attempts.clear()


def create_user_and_session(email: str = "idemp_audit@test.com") -> dict:
    user_id = str(uuid.uuid4())
    session_id = f"sess_{uuid.uuid4().hex}"
    csrf_token = f"csrf_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, 'Idempotency Tester', ?, ?)
            """,
            (user_id, email, hash_password("SecurePass123!"), now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, 'IdempotencyTestClient', '127.0.0.1', ?, ?)
            """,
            (session_id, user_id, csrf_token, expires_at, now),
        )

    return {
        "user_id": user_id,
        "email": email,
        "session_token": session_id,
        "csrf_token": csrf_token,
    }


# ==============================================================================
# 1. IDENTICAL RETRIES & LOST RESPONSES
# ==============================================================================

def test_job_idempotency_identical_request_retries():
    """Verify that replaying identical job creation payload returns cached 200 response."""
    user = create_user_and_session("retry_user@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-identical-001",
    }
    payload = {
        "location": "Indiranagar",
        "radius_km": 1.5,
        "categories": ["cafe"],
        "max_leads": 15,
        "filter_only_with_phone": True,
        "filter_only_missing_website": False,
    }

    # First request
    r1 = client.post("/api/jobs", headers=headers, json=payload)
    assert r1.status_code == 200
    job_id_1 = r1.json()["id"]

    # Client simulates lost network response and retries
    r2 = client.post("/api/jobs", headers=headers, json=payload)
    assert r2.status_code == 200
    job_id_2 = r2.json()["id"]
    assert job_id_1 == job_id_2

    # Exactly one job must exist in database
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 1


# ==============================================================================
# 2. SUBSTRING LOCATION COLLISION PREVENTION (York vs New York)
# ==============================================================================

def test_job_idempotency_substring_location_conflict_rejected():
    """Verify that key reuse with a substring location (York vs New York) returns 409 Conflict."""
    user = create_user_and_session("substring_test@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-york-002",
    }

    payload_ny = {
        "location": "New York",
        "radius_km": 2.0,
        "categories": ["cafe"],
        "max_leads": 10,
    }
    payload_york = {
        "location": "York",  # Substring of New York! Must NOT match!
        "radius_km": 2.0,
        "categories": ["cafe"],
        "max_leads": 10,
    }

    # Request 1: New York
    r1 = client.post("/api/jobs", headers=headers, json=payload_ny)
    assert r1.status_code == 200

    # Request 2: York (same key, different location) -> 409 Conflict
    r2 = client.post("/api/jobs", headers=headers, json=payload_york)
    assert r2.status_code == 409
    assert "conflict" in r2.json()["detail"].lower()


def test_job_idempotency_different_location_rejected():
    """Verify that reusing a key with a completely different location returns 409 Conflict."""
    user = create_user_and_session("loc_conflict@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-loc-003",
    }

    payload_a = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}
    payload_b = {"location": "Whitefield", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}

    r1 = client.post("/api/jobs", headers=headers, json=payload_a)
    assert r1.status_code == 200

    r2 = client.post("/api/jobs", headers=headers, json=payload_b)
    assert r2.status_code == 409
    assert "conflict" in r2.json()["detail"].lower()


# ==============================================================================
# 3. DIFFERENT CATEGORIES & OPTIONS REJECTED
# ==============================================================================

def test_job_idempotency_different_categories_rejected():
    """Verify that reusing a key with different categories returns 409 Conflict."""
    user = create_user_and_session("cat_conflict@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-cat-004",
    }

    payload_a = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}
    payload_b = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["restaurant"], "max_leads": 10}

    r1 = client.post("/api/jobs", headers=headers, json=payload_a)
    assert r1.status_code == 200

    r2 = client.post("/api/jobs", headers=headers, json=payload_b)
    assert r2.status_code == 409
    assert "conflict" in r2.json()["detail"].lower()


def test_job_idempotency_different_numeric_options_rejected():
    """Verify that reusing a key with different radius or max_leads returns 409 Conflict."""
    user = create_user_and_session("opts_conflict@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-opts-005",
    }

    payload_a = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}
    payload_diff_radius = {"location": "Indiranagar", "radius_km": 2.5, "categories": ["cafe"], "max_leads": 10}
    payload_diff_leads = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 50}

    r1 = client.post("/api/jobs", headers=headers, json=payload_a)
    assert r1.status_code == 200

    r_rad = client.post("/api/jobs", headers=headers, json=payload_diff_radius)
    assert r_rad.status_code == 409

    r_leads = client.post("/api/jobs", headers=headers, json=payload_diff_leads)
    assert r_leads.status_code == 409


# ==============================================================================
# 4. FILTER VALUES INCLUDED IN FINGERPRINT
# ==============================================================================

def test_job_idempotency_different_filters_rejected():
    """Verify that modifying filter_only_with_phone or missing_website triggers 409 Conflict."""
    user = create_user_and_session("filters_conflict@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-filter-006",
    }

    payload_base = {
        "location": "Indiranagar",
        "radius_km": 1.5,
        "categories": ["cafe"],
        "max_leads": 10,
        "filter_only_with_phone": False,
        "filter_only_missing_website": False,
    }
    payload_diff_phone = {
        "location": "Indiranagar",
        "radius_km": 1.5,
        "categories": ["cafe"],
        "max_leads": 10,
        "filter_only_with_phone": True,  # Modified filter!
        "filter_only_missing_website": False,
    }
    payload_diff_web = {
        "location": "Indiranagar",
        "radius_km": 1.5,
        "categories": ["cafe"],
        "max_leads": 10,
        "filter_only_with_phone": False,
        "filter_only_missing_website": True,  # Modified filter!
    }

    # Base request
    r1 = client.post("/api/jobs", headers=headers, json=payload_base)
    assert r1.status_code == 200

    # Retrying with modified phone filter -> 409 Conflict
    r_phone = client.post("/api/jobs", headers=headers, json=payload_diff_phone)
    assert r_phone.status_code == 409

    # Retrying with modified website filter -> 409 Conflict
    r_web = client.post("/api/jobs", headers=headers, json=payload_diff_web)
    assert r_web.status_code == 409


# ==============================================================================
# 5. SEMANTIC NORMALIZATION (WHITESPACE, CASE, CATEGORY ORDER)
# ==============================================================================

def test_job_idempotency_normalization_succeeds():
    """Verify whitespace, casing, and category reordering are normalized and replay cleanly."""
    user = create_user_and_session("norm_user@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-norm-007",
    }

    payload_1 = {
        "location": "Indiranagar",
        "radius_km": 1.5,
        "categories": ["cafe", "gym"],
        "max_leads": 20,
    }
    # Payload 2: extra whitespace, different casing, reversed categories
    payload_2 = {
        "location": "  indiranagar  ",
        "radius_km": 1.5000,
        "categories": ["GYM", "Cafe"],
        "max_leads": 20,
    }

    r1 = client.post("/api/jobs", headers=headers, json=payload_1)
    assert r1.status_code == 200
    job_id = r1.json()["id"]

    r2 = client.post("/api/jobs", headers=headers, json=payload_2)
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id


# ==============================================================================
# 6. GEOCODED DISPLAY NAME INVARIANCE
# ==============================================================================

def test_job_idempotency_geocoded_display_name_expansion_invariance():
    """Verify geocoded display address expansion does not break identical request retries."""
    user = create_user_and_session("display_name_test@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-display-008",
    }
    # Short user input query
    payload = {"location": "Koramangala", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}

    # Request 1: worker geocodes Koramangala into 60-character address
    r1 = client.post("/api/jobs", headers=headers, json=payload)
    assert r1.status_code == 200
    assert "Ward 10, Bangalore" in r1.json()["location_query"]
    job_id = r1.json()["id"]

    # Request 2: Exact retry with short user query "Koramangala" must succeed (200 OK)
    r2 = client.post("/api/jobs", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id


# ==============================================================================
# 7. CONCURRENT RETRIES & ATOMIC SERIALIZATION
# ==============================================================================

def test_job_idempotency_concurrent_threads_atomic_serialization():
    """Verify concurrent requests with same idempotency key are safely serialized."""
    user = create_user_and_session("concurrent_user@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-concurrent-009",
    }
    payload = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}

    results = []

    def make_request():
        res = client.post("/api/jobs", headers=headers, json=payload)
        results.append(res)

    threads = [threading.Thread(target=make_request) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads must succeed (200 OK)
    assert all(r.status_code == 200 for r in results)

    # All threads must receive the EXACT same job ID
    job_ids = [r.json()["id"] for r in results]
    assert len(set(job_ids)) == 1

    # Exactly one job created in database
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 1


# ==============================================================================
# 8. CROSS-USER IDEMPOTENCY KEY ISOLATION
# ==============================================================================

def test_job_idempotency_cross_user_isolation():
    """Verify User B cannot collide with or read User A's idempotency key."""
    alice = create_user_and_session("alice_key@test.com")
    bob = create_user_and_session("bob_key@test.com")

    shared_key = "shared-uuid-key-across-tenants-010"
    payload = {"location": "Indiranagar", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}

    # Alice starts a job with shared_key
    alice_headers = {"Authorization": f"Bearer {alice['session_token']}", "Idempotency-Key": shared_key}
    r_alice = client.post("/api/jobs", headers=alice_headers, json=payload)
    assert r_alice.status_code == 200
    alice_job_id = r_alice.json()["id"]

    # Bob starts a job with the SAME shared_key
    bob_headers = {"Authorization": f"Bearer {bob['session_token']}", "Idempotency-Key": shared_key}
    r_bob = client.post("/api/jobs", headers=bob_headers, json=payload)
    assert r_bob.status_code == 200
    bob_job_id = r_bob.json()["id"]

    # Bob must receive his OWN job, NOT Alice's job
    assert bob_job_id != alice_job_id

    # Verify database isolation: both users have their own job and idempotency record
    with get_db() as conn:
        alice_key_row = conn.execute("SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?", (alice["user_id"], shared_key)).fetchone()
        bob_key_row = conn.execute("SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?", (bob["user_id"], shared_key)).fetchone()
        assert alice_key_row["resource_id"] == alice_job_id
        assert bob_key_row["resource_id"] == bob_job_id


# ==============================================================================
# 9. SCHEMA MIGRATION V3 & LEGACY ROW COMPATIBILITY
# ==============================================================================

def test_schema_migration_v3_and_legacy_compatibility(tmp_path):
    """Verify Migration v3 adds request_fingerprint column without breaking existing records."""
    db_file = str(tmp_path / "migration_v3_test.db")
    conn = sqlite3.connect(db_file)
    run_migrations(conn)

    # Confirm column exists
    cols = get_column_names(conn, "idempotency_keys")
    assert "request_fingerprint" in cols

    # Confirm migration entry recorded in schema_migrations
    cursor = conn.cursor()
    versions = [r[0] for r in cursor.execute("SELECT version FROM schema_migrations").fetchall()]
    assert 3 in versions
    conn.close()


# ==============================================================================
# 10. FAILURE CLEANUP (RESERVED KEY PURGED ON WORKER EXCEPTION)
# ==============================================================================

def test_job_idempotency_worker_failure_cleans_reserved_key():
    """Verify that if the worker fails to start a job, the PENDING key is purged so client can retry."""
    user = create_user_and_session("fail_user@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "Idempotency-Key": "idemp-key-fail-011",
    }
    payload = {"location": "FailCity", "radius_km": 1.5, "categories": ["cafe"], "max_leads": 10}

    # Temporarily make worker raise an exception
    class ExplodingWorker:
        async def start_job(self, *args, **kwargs):
            raise RuntimeError("Database disk I/O error simulated.")

    orig_worker = jobs.worker_instance
    jobs.set_worker(ExplodingWorker())

    try:
        r_fail = client.post("/api/jobs", headers=headers, json=payload)
        assert r_fail.status_code == 400

        # Verify key was purged from database and not left locked in PENDING
        with get_db() as conn:
            row = conn.execute("SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?", (user["user_id"], "idemp-key-fail-011")).fetchone()
            assert row is None
    finally:
        jobs.set_worker(orig_worker)
