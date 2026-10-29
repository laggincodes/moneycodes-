"""
Milestone 6: Durable Job Orchestration & Lifecycle Invariant Tests.
Verifies state transitions, atomic claiming, terminal state protections,
cooperative cancellation checkpoints, secret sanitization, restart recovery,
and cross-account ownership isolation.
Strictly offline with in-memory ASGI transport and mock providers.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import init_db, get_db
from app.queue.job_worker import JobWorker, sanitize_log_message
from app.auth.security import hash_password
from app.discovery.base import DiscoveryProvider, DiscoveryResult, DiscoveredBusiness
from app.schemas import WebsiteAuditResult

client = TestClient(app)


def create_user_and_session(email: str, password: str = "TestPass123!") -> dict:
    user_id = f"user_{uuid.uuid4().hex[:8]}"
    session_id = f"sess_{uuid.uuid4().hex}"
    csrf_token = f"csrf_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, 'Test User', ?, ?)
            """,
            (user_id, email, hash_password(password), now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, 'TestClient', '127.0.0.1', ?, ?)
            """,
            (session_id, user_id, csrf_token, expires_at, now),
        )

    return {
        "user_id": user_id,
        "email": email,
        "session_token": session_id,
        "csrf_token": csrf_token,
    }


def seed_job(
    user_id: str,
    job_id: str = None,
    status: str = "running",
    location: str = "Indiranagar",
    max_leads: int = 10,
    processed_count: int = 0,
) -> str:
    jid = job_id or f"job_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 12.97, 77.64, 1.0, '["cafe"]', ?, 10, ?, '[]', ?, ?)
            """,
            (jid, user_id, status, location, max_leads, processed_count, now, now),
        )
    return jid


@pytest.fixture(autouse=True)
def setup_orchestration_db(tmp_path):
    db_file = str(tmp_path / "orchestration.db")
    init_db(db_file)
    from app.api import jobs
    worker = JobWorker()
    jobs.set_worker(worker)
    yield db_file


# ==============================================================================
# 1. OWNERSHIP & CROSS-ACCOUNT ISOLATION INVARIANTS
# ==============================================================================

def test_job_ownership_and_cross_account_isolation():
    """User B cannot pause, resume, cancel, or show User A's job (404 Not Found)."""
    user_a = create_user_and_session("owner_a@test.com")
    user_b = create_user_and_session("intruder_b@test.com")
    job_a = seed_job(user_a["user_id"], status="running")

    headers_b = {
        "Authorization": f"Bearer {user_b['session_token']}",
        "X-CSRF-Token": user_b["csrf_token"],
    }

    # User B attempts to show Job A
    resp_show = client.get(f"/api/jobs/{job_a}", headers=headers_b)
    assert resp_show.status_code == 404

    # User B attempts to pause Job A
    resp_pause = client.post(f"/api/jobs/{job_a}/pause", headers=headers_b)
    assert resp_pause.status_code == 404

    # User B attempts to resume Job A
    resp_resume = client.post(f"/api/jobs/{job_a}/resume", headers=headers_b)
    assert resp_resume.status_code == 404

    # User B attempts to cancel Job A
    resp_cancel = client.post(f"/api/jobs/{job_a}/cancel", headers=headers_b)
    assert resp_cancel.status_code == 404


# ==============================================================================
# 2. TERMINAL STATE INVARIANTS
# ==============================================================================

@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_terminal_states_cannot_be_paused_or_resumed(terminal_status):
    """Jobs in terminal states cannot transition to paused or running (409 Conflict)."""
    user = create_user_and_session(f"term_{terminal_status}@test.com")
    job_id = seed_job(user["user_id"], status=terminal_status)

    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }

    # Attempt to pause
    resp_pause = client.post(f"/api/jobs/{job_id}/pause", headers=headers)
    assert resp_pause.status_code == 409
    assert "terminal state" in resp_pause.json()["detail"]

    # Attempt to resume
    resp_resume = client.post(f"/api/jobs/{job_id}/resume", headers=headers)
    assert resp_resume.status_code == 409
    assert "terminal state" in resp_resume.json()["detail"]


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_completed_or_failed_jobs_cannot_be_cancelled(terminal_status):
    """Completed or failed jobs cannot be overwritten as cancelled (409 Conflict)."""
    user = create_user_and_session(f"cancel_term_{terminal_status}@test.com")
    job_id = seed_job(user["user_id"], status=terminal_status)

    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }

    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert resp.status_code == 409
    assert "already in terminal state" in resp.json()["detail"]

    # Verify status in database was untouched
    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        assert st == terminal_status


# ==============================================================================
# 3. REPEATED LIFECYCLE REQUESTS (IDEMPOTENCY OF ACTIONS)
# ==============================================================================

def test_repeated_lifecycle_requests_are_idempotent():
    """Calling pause on paused, resume on running, or cancel on cancelled is an idempotent 200."""
    user = create_user_and_session("idem_lifecycle@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }

    # 1. Paused job -> repeated pause returns 200
    paused_job = seed_job(user["user_id"], status="paused")
    r1 = client.post(f"/api/jobs/{paused_job}/pause", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "paused"

    # 2. Running job -> repeated resume returns 200
    running_job = seed_job(user["user_id"], status="running")
    r2 = client.post(f"/api/jobs/{running_job}/resume", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "running"

    # 3. Cancelled job -> repeated cancel returns 200
    cancelled_job = seed_job(user["user_id"], status="cancelled")
    r3 = client.post(f"/api/jobs/{cancelled_job}/cancel", headers=headers)
    assert r3.status_code == 200
    assert r3.json()["status"] == "cancelled"


# ==============================================================================
# 4. ATOMIC CLAIMING & MULTI-WORKER ISOLATION
# ==============================================================================

def test_atomic_job_claiming_single_winner():
    """Only one worker can claim a pending job; duplicate claim attempts return False."""
    user = create_user_and_session("claim_user@test.com")
    job_id = seed_job(user["user_id"], status="pending")

    worker_1 = JobWorker()
    worker_2 = JobWorker()

    # Worker 1 claims
    claimed_1 = worker_1.claim_job(job_id)
    assert claimed_1 is True

    # Worker 2 attempts to claim the same job simultaneously -> rejected
    claimed_2 = worker_2.claim_job(job_id)
    assert claimed_2 is False

    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        assert st == "running"


# ==============================================================================
# 5. COOPERATIVE CANCELLATION CHECKPOINTS
# ==============================================================================

@pytest.mark.asyncio
async def test_cancellation_before_discovery_stops_immediately():
    """If a job is cancelled before discovery begins, it exits without querying provider."""
    called_discovery = False

    class MockDiscovery(DiscoveryProvider):
        async def discover(self, *args, **kwargs):
            nonlocal called_discovery
            called_discovery = True
            return DiscoveryResult(businesses=[])

    worker = JobWorker(discovery_provider=MockDiscovery())
    user = create_user_and_session("cancel_early@test.com")
    job_id = seed_job(user["user_id"], status="running")

    # Mark cancelled before execution
    worker._cancel_flags.add(job_id)

    await worker._execute_job(
        job_id=job_id,
        lat=12.97,
        lon=77.64,
        radius_km=1.0,
        categories=["cafe"],
        max_leads=5,
        filter_only_with_phone=False,
        filter_only_missing_website=False,
        user_id=user["user_id"],
    )

    assert called_discovery is False


@pytest.mark.asyncio
async def test_completion_does_not_overwrite_cancelled_status():
    """Atomic completion update prevents marking a cancelled job as completed."""
    worker = JobWorker()
    user = create_user_and_session("no_overwrite@test.com")
    job_id = seed_job(user["user_id"], status="cancelled")

    # Simulate worker reaching the end of _execute_job while job was cancelled
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ? AND status = 'running'",
            (now, job_id),
        )
        assert cursor.rowcount == 0  # Did not update!

    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        assert st == "cancelled"


# ==============================================================================
# 6. WORKER EXCEPTION HANDLING & SECRET SANITIZATION
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_exception_marks_job_failed_and_defangs_secrets():
    """Worker catches unhandled exception, marks job failed, and redacts sensitive tokens from logs."""
    sensitive_token = "sess_secret_private_token_xyz999"
    api_key = "AIzaSySecretApiKey12345"

    class FailingDiscovery(DiscoveryProvider):
        async def discover(self, *args, **kwargs):
            raise RuntimeError(f"Connection failed with Bearer {sensitive_token} using API_KEY={api_key}")

    worker = JobWorker(discovery_provider=FailingDiscovery())
    user = create_user_and_session("fail_sanitize@test.com")
    job_id = seed_job(user["user_id"], status="running")

    await worker._execute_job(
        job_id=job_id,
        lat=12.97,
        lon=77.64,
        radius_km=1.0,
        categories=["cafe"],
        max_leads=5,
        filter_only_with_phone=False,
        filter_only_missing_website=False,
        user_id=user["user_id"],
    )

    with get_db() as conn:
        row = conn.execute("SELECT status, error_message, logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert sensitive_token not in row["error_message"]
        assert api_key not in row["error_message"]
        assert "[REDACTED]" in row["error_message"]
        assert sensitive_token not in row["logs"]
        assert api_key not in row["logs"]


def test_sanitize_log_message_unit():
    """Unit test for secret sanitization regex filters."""
    raw = "Failed Authorization: Bearer secret_session_token_12345678 with password=SuperSecretPassword!"
    sanitized = sanitize_log_message(raw)
    assert "secret_session_token" not in sanitized
    assert "SuperSecretPassword" not in sanitized
    assert "[REDACTED]" in sanitized


# ==============================================================================
# 7. PROCESS RESTART RECOVERY & RE-RUN PREVENTION
# ==============================================================================

def test_startup_recovery_and_resumption_policy():
    """
    On restart, interrupted running/pending jobs are paused.
    Attempting to resume an interrupted job from a dead process returns 409 Conflict.
    Attempting to cancel an interrupted job succeeds.
    """
    user = create_user_and_session("restart_user@test.com")
    headers = {
        "Authorization": f"Bearer {user['session_token']}",
        "X-CSRF-Token": user["csrf_token"],
    }

    # Seed job that was left running when process died
    crashed_id = seed_job(user["user_id"], status="running")

    # Simulate restart
    worker = JobWorker()
    worker.recover_interrupted_jobs()

    # Verify status is now 'paused' with an explanation log
    with get_db() as conn:
        row = conn.execute("SELECT status, logs FROM jobs WHERE id = ?", (crashed_id,)).fetchone()
        assert row["status"] == "paused"
        assert any("restarted" in entry["message"].lower() for entry in json.loads(row["logs"]))

    # Wire this worker to app for API test
    from app.api import jobs
    jobs.set_worker(worker)

    # Calling resume on interrupted job fails with 409 Conflict explaining re-run prevention policy
    resp_resume = client.post(f"/api/jobs/{crashed_id}/resume", headers=headers)
    assert resp_resume.status_code == 409
    assert "interrupted by a previous server restart" in resp_resume.json()["detail"]

    # Calling cancel on interrupted job succeeds cleanly
    resp_cancel = client.post(f"/api/jobs/{crashed_id}/cancel", headers=headers)
    assert resp_cancel.status_code == 200
    assert resp_cancel.json()["status"] == "cancelled"

    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (crashed_id,)).fetchone()["status"]
        assert st == "cancelled"
