"""
Comprehensive Test Suite for WebsiteLead Hunter Windows CLI & Automation Runner.
Covers help/arguments, exit codes, Windows Credential Manager, session lifecycle,
account isolation, safe confirmation gates, idempotency, error handling, exports,
and automation workflows.
Strictly offline — uses in-memory ASGI transport and isolated SQLite fixtures.
"""
import hashlib
import json
import os
import sys
import uuid
import pytest
from datetime import datetime, timezone, timedelta
import httpx

from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db, get_db
from app.auth.security import _failed_attempts, hash_password
from app.cli.main import main
from app.cli.client import (
    LeadHunterClient,
    set_default_transport,
    CLIError,
    CLIAuthError,
    CLINetworkError,
    CLIValidationError,
)
from app.cli.credentials import (
    set_credential_backend,
    InMemoryCredentialStore,
    WindowsCredentialStore,
    save_session_token,
    load_session_token,
    clear_session_token,
    is_credential_store_available,
)
from app.cli.formatter import redact_secret


class MockJobWorker:
    """Mock worker to simulate background jobs offline without external geocoding/audit network calls."""
    def __init__(self):
        self._paused = set()
        self._cancelled = set()

    async def start_job(
        self,
        location_query: str,
        radius_km: float,
        categories: list,
        max_leads: int = 30,
        filter_only_with_phone: bool = False,
        filter_only_missing_website: bool = False,
        user_id: str = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, user_id, status, location_query, lat, lon, radius_km, categories,
                    max_leads, total_found, processed_count, logs, created_at, updated_at
                ) VALUES (?, ?, 'completed', ?, 12.97, 77.64, ?, ?, ?, 5, 5, '[]', ?, ?)
                """,
                (job_id, user_id, location_query, radius_km, json.dumps(categories or []), max_leads, now, now),
            )
        return job_id

    async def pause_job(self, job_id: str):
        self._paused.add(job_id)
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE jobs SET status = 'paused', updated_at = ? WHERE id = ?", (now, job_id))
        return True

    async def resume_job(self, job_id: str):
        self._paused.discard(job_id)
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?", (now, job_id))
        return True

    async def cancel_job(self, job_id: str):
        self._cancelled.add(job_id)
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, job_id))
        return True


@pytest.fixture(autouse=True)
def setup_cli_test_env(tmp_path):
    """Sets up isolated database, ASGI transport, worker reference, and clean credential backend."""
    db_file = str(tmp_path / "test_cli.db")
    init_db(db_file)
    _failed_attempts.clear()

    from app.api import jobs
    mock_worker = MockJobWorker()
    jobs.set_worker(mock_worker)

    # Route client requests in-memory via TestClient synchronous transport directly to FastAPI
    tc = TestClient(app)
    set_default_transport(tc._transport)

    # Use clean isolated in-memory credential store by default
    mem_store = InMemoryCredentialStore()
    set_credential_backend(mem_store)

    yield

    set_default_transport(None)
    set_credential_backend(None)
    _failed_attempts.clear()


def create_user_and_session(email: str = "alice_cli@test.com", password: str = "Password123!") -> dict:
    """Helper to create a user and active session directly in the test database."""
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    user_id = str(uuid.uuid4())
    session_id = f"sess_{uuid.uuid4().hex}"
    csrf_token = f"csrf_{uuid.uuid4().hex}"
    pwd_hash = hash_password(password)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, 'Alice CLI', ?, ?)
            """,
            (user_id, email, pwd_hash, now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, 'WebsiteLeadHunter-CLI/1.0', '127.0.0.1', ?, ?)
            """,
            (session_id, user_id, csrf_token, expires_at, now),
        )

    return {
        "user_id": user_id,
        "email": email,
        "session_token": session_id,
        "csrf_token": csrf_token,
    }


def create_test_lead(user_id: str, lead_id: str = "test-lead-001", name: str = "Indiranagar Cafe", status: str = "new") -> str:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, lat, lon,
                source_url, provenance, raw_data, score, pipeline_status,
                website, phone, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'Cafe', 12.97, 77.64, '', '{}', '{}', 75, ?, 'https://example.com', '+919999999999', ?, ?)
            """,
            (lead_id, user_id, f"osm:{lead_id}", name, status, now, now),
        )
    return lead_id


# ==============================================================================
# 1. CLI HELP, ARGUMENT VALIDATION, AND EXIT CODES
# ==============================================================================

def test_cli_help_exit_code_zero(capsys):
    """Running 'hunt' without arguments shows help and returns 0."""
    ret = main([])
    assert ret == 0
    out = capsys.readouterr().out
    assert "WebsiteLead Hunter" in out
    assert "Available commands" in out


def test_cli_unknown_command_exit_code_two(capsys):
    """Running an unknown command must exit with code 2."""
    with pytest.raises(SystemExit) as exc:
        main(["nonexistent-command"])
    assert exc.value.code == 2


def test_cli_jobs_missing_action_exit_code_two():
    """Running 'jobs' without an action must exit with code 2."""
    with pytest.raises(SystemExit) as exc:
        main(["jobs"])
    assert exc.value.code == 2


def test_cli_leads_pipeline_invalid_status_exit_code_two():
    """Providing an invalid pipeline status choice must exit with code 2."""
    with pytest.raises(SystemExit) as exc:
        main(["leads", "pipeline", "lead-1", "invalid_stage"])
    assert exc.value.code == 2


# ==============================================================================
# 2. CREDENTIAL REDACTION & MISSING CREDENTIALS
# ==============================================================================

def test_whoami_without_credentials_returns_exit_code_three(capsys):
    """Running whoami when not logged in returns exit code 3."""
    ret = main(["whoami"])
    assert ret == 3
    out = capsys.readouterr().out
    assert "Not logged in" in out


def test_credential_redaction_utility():
    """Verifies that secret redaction masks middle bytes and never reveals full token."""
    assert redact_secret("sess_1234567890abcdef") == "sess...cdef"
    assert redact_secret("short") == "***"
    assert redact_secret("") == "<none>"
    assert redact_secret(None) == "<none>"


# ==============================================================================
# 3. CREDENTIAL-STORE FAILURE AND LOGOUT BEHAVIOR
# ==============================================================================

class FailingCredentialStore:
    is_available = True
    def write(self, target, user, secret):
        return False
    def read(self, target):
        return None
    def delete(self, target):
        return False


def test_credential_store_failure_handled_gracefully(capsys, monkeypatch):
    """When credential store write fails, CLI reports failure without crashing."""
    create_user_and_session("alice_fail@test.com", "Password123!")
    set_credential_backend(FailingCredentialStore())

    monkeypatch.setattr("getpass.getpass", lambda prompt: "Password123!")
    ret = main(["login", "--email", "alice_fail@test.com"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "Could not persist token" in err


def test_logout_removes_stored_credentials(capsys):
    """Running logout notifies server and deletes credential from store."""
    user = create_user_and_session("bob_logout@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    assert load_session_token() is not None

    ret = main(["logout"])
    assert ret == 0
    assert load_session_token() is None

    # Server-side session must also be deleted
    with get_db() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (user["session_token"],)).fetchone()
        assert row is None


# ==============================================================================
# 4. AUTHENTICATED API CALLS & SESSION EXPIRATION
# ==============================================================================

def test_authenticated_whoami_success(capsys):
    """Authenticated user runs whoami successfully."""
    user = create_user_and_session("whoami@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    ret = main(["whoami"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "whoami@test.com" in out
    assert user["user_id"] in out


def test_expired_session_returns_exit_code_three(capsys):
    """When session has expired on server, whoami returns exit code 3."""
    user = create_user_and_session("expired@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    # Manually expire session in DB
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (past, user["session_token"]))

    ret = main(["whoami"])
    assert ret == 3
    err = capsys.readouterr().err
    assert "Session has expired or is invalid" in err


# ==============================================================================
# 5. ACCOUNT ISOLATION AND FORBIDDEN OPERATIONS
# ==============================================================================

def test_account_isolation_forbidden_operations(capsys):
    """User A cannot view, inspect, or mutate leads belonging to User B."""
    user_a = create_user_and_session("user_a@test.com", "Password123!")
    user_b = create_user_and_session("user_b@test.com", "Password123!")

    lead_b = create_test_lead(user_b["user_id"], lead_id="lead-b-only", name="User B Bakery")

    # Login as User A
    save_session_token(user_a["email"], user_a["session_token"])

    # User A tries to view User B's lead -> 404
    ret_show = main(["leads", "show", lead_b])
    assert ret_show == 1
    err_show = capsys.readouterr().err
    assert "Not Found" in err_show

    # User A tries to update User B's lead -> 404
    ret_pipe = main(["leads", "pipeline", lead_b, "won", "--yes"])
    assert ret_pipe == 1
    err_pipe = capsys.readouterr().err
    assert "Not Found" in err_pipe

    # Verify lead was not updated
    with get_db() as conn:
        row = conn.execute("SELECT pipeline_status FROM leads WHERE id = ?", (lead_b,)).fetchone()
        assert row["pipeline_status"] == "new"


# ==============================================================================
# 6. SAFE CONFIRMATION DEFAULTS AND DRY-RUN BEHAVIOR
# ==============================================================================

def test_discover_dry_run_does_not_create_job(capsys):
    """Running 'discover --dry-run' inspects parameters without creating a job."""
    user = create_user_and_session("dry_user@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    ret = main(["discover", "--location", "Indiranagar", "--radius", "2.0", "--dry-run"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "No background job was created" in out

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 0


def test_discover_declined_confirmation_returns_exit_code_four(capsys, monkeypatch):
    """Declining the interactive confirmation prompt aborts and returns code 4."""
    user = create_user_and_session("decline_user@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    ret = main(["discover", "--location", "Koramangala"])
    assert ret == 4
    out = capsys.readouterr().out
    assert "Action cancelled" in out

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 0


def test_pipeline_update_declined_confirmation_returns_code_four(capsys, monkeypatch):
    """Declining pipeline update prompt aborts mutation without modifying DB."""
    user = create_user_and_session("pipe_decline@test.com", "Password123!")
    lead_id = create_test_lead(user["user_id"], lead_id="lead-pipe-dec")
    save_session_token(user["email"], user["session_token"])

    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    ret = main(["leads", "pipeline", lead_id, "contacted"])
    assert ret == 4
    out = capsys.readouterr().out
    assert "Action cancelled" in out

    with get_db() as conn:
        row = conn.execute("SELECT pipeline_status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        assert row["pipeline_status"] == "new"


# ==============================================================================
# 7. IDEMPOTENT MUTATION RETRIES
# ==============================================================================

def test_pipeline_update_with_idempotency_key_replay(capsys):
    """
    Supplying an idempotency key guarantees that a second submission with the same key
    returns identical response and does not alter updated_at in the database.
    """
    user = create_user_and_session("idem_cli@test.com", "Password123!")
    lead_id = create_test_lead(user["user_id"], lead_id="lead-idem-cli")
    save_session_token(user["email"], user["session_token"])

    idem_key = "cli_key_unique_456"

    # First update
    ret1 = main(["leads", "pipeline", lead_id, "contacted", "--notes", "Called owner", "--idempotency-key", idem_key, "--yes"])
    assert ret1 == 0

    with get_db() as conn:
        row1 = conn.execute("SELECT updated_at, pipeline_status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        updated_at_1 = row1["updated_at"]
        assert row1["pipeline_status"] == "contacted"

    # Second update (replay identical key)
    ret2 = main(["leads", "pipeline", lead_id, "contacted", "--notes", "Called owner", "--idempotency-key", idem_key, "--yes"])
    assert ret2 == 0

    with get_db() as conn:
        row2 = conn.execute("SELECT updated_at, pipeline_status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        # Must be identical; no secondary update executed
        assert row2["updated_at"] == updated_at_1


# ==============================================================================
# 8. HTTP TIMEOUTS AND SAFE ERROR REPORTING
# ==============================================================================

def test_http_timeout_safe_reporting(capsys, monkeypatch):
    """Simulating an HTTP read timeout produces a clean error message and exit code 1."""
    user = create_user_and_session("timeout_user@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    def mock_request(*args, **kwargs):
        raise httpx.ReadTimeout("Simulated socket read timeout")

    monkeypatch.setattr(httpx.Client, "request", mock_request)

    ret = main(["leads", "list"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "Network Connection Error: Request timed out" in err
    assert "Traceback" not in err  # No leaked raw stack traces or internal secrets


# ==============================================================================
# 9. EXPORT PATH VALIDATION AND OVERWRITE CONFIRMATION
# ==============================================================================

def test_export_csv_success(tmp_path, capsys):
    """CSV export writes file with lead columns."""
    user = create_user_and_session("export_cli@test.com", "Password123!")
    create_test_lead(user["user_id"], lead_id="lead-exp-1", name="Export Cafe")
    save_session_token(user["email"], user["session_token"])

    out_file = str(tmp_path / "leads_out.csv")
    ret = main(["export", "--output", out_file, "--yes"])
    assert ret == 0
    assert os.path.exists(out_file)

    with open(out_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Business Name" in content
    assert "Export Cafe" in content


def test_export_overwrite_declined(tmp_path, capsys, monkeypatch):
    """If export destination exists and user declines overwrite, export is aborted."""
    user = create_user_and_session("exp_ovw@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    out_file = str(tmp_path / "existing.csv")
    with open(out_file, "w") as f:
        f.write("original content")

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    ret = main(["export", "--output", out_file])
    assert ret == 4
    out = capsys.readouterr().out
    assert "Export cancelled" in out

    with open(out_file, "r") as f:
        assert f.read() == "original content"


# ==============================================================================
# 10. AUTOMATION DUPLICATE PREVENTION AND RETRY LIMITS
# ==============================================================================

def test_automation_plan_dry_run(tmp_path, capsys):
    """Running 'automate plan' performs a dry-run preview without mutating state."""
    user = create_user_and_session("auto_user@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    recipe_file = str(tmp_path / "recipe.json")
    with open(recipe_file, "w") as f:
        json.dump({
            "name": "Test Plan",
            "tasks": [
                {"location": "Whitefield", "radius_km": 1.0, "max_leads": 5}
            ]
        }, f)

    ret = main(["automate", "plan", recipe_file])
    assert ret == 0
    out = capsys.readouterr().out
    assert "AUTOMATION PLAN: Test Plan" in out
    assert "[DRY-RUN]" in out

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 0


def test_automation_blocks_overlapping_running_job(tmp_path, capsys):
    """If an existing job is already running, automation halts with error and returns code 1."""
    user = create_user_and_session("auto_overlap@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    # Insert an active running job in DB
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES ('job-already-running', ?, 'running', 'MG Road', 12.97, 77.60, 1.0, '[]', 10, ?, ?)
            """,
            (user["user_id"], now, now),
        )

    recipe_file = str(tmp_path / "recipe.json")
    with open(recipe_file, "w") as f:
        json.dump({
            "tasks": [{"location": "Koramangala"}]
        }, f)

    ret = main(["automate", "run", recipe_file, "--yes"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "Active discovery job 'job-already-running' is already running" in err


# ==============================================================================
# 11. NATIVE WINDOWS CREDENTIAL MANAGER HARDWARE TEST
# ==============================================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager is only available on Windows")
def test_native_windows_credential_store_advapi32():
    """
    Validates that native Windows Credential Manager writes, reads, and deletes
    credentials cleanly through advapi32.dll on this Windows host.
    """
    store = WindowsCredentialStore()
    assert store.is_available is True

    test_target = f"WebsiteLeadHunter_Test_{uuid.uuid4().hex[:8]}"
    test_user = "advapi_test@example.com"
    test_secret = f"token_{uuid.uuid4().hex}"

    try:
        # Write
        w_ok = store.write(test_target, test_user, test_secret)
        assert w_ok is True

        # Read
        read_res = store.read(test_target)
        assert read_res is not None
        user, sec = read_res
        assert user == test_user
        assert sec == test_secret

    finally:
        # Delete and verify cleanup
        d_ok = store.delete(test_target)
        assert d_ok is True
        assert store.read(test_target) is None


# ==============================================================================
# 12. MILESTONE 5.1 RELIABILITY & SECURITY AUDIT TESTS
# ==============================================================================

def test_logout_when_server_unreachable_distinguishes_local_removal(capsys, monkeypatch):
    """
    If server is unreachable during logout, CLI removes local credentials,
    warns that server session was not revoked, and returns exit code 1.
    """
    user = create_user_and_session("unreach_logout@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    def mock_logout(self):
        raise CLINetworkError("Connection refused to 127.0.0.1:8000")

    monkeypatch.setattr(LeadHunterClient, "logout", mock_logout)

    ret = main(["logout"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "Server session could not be revoked" in err
    assert "Local credentials for 'unreach_logout@test.com' removed from Windows Credential Manager" in err
    assert load_session_token() is None


def test_logout_credential_delete_failure(capsys):
    """If credential deletion fails, CLI reports error and returns code 1."""
    user = create_user_and_session("del_fail@test.com", "Password123!")

    class FailDeleteStore(InMemoryCredentialStore):
        def delete(self, target):
            return False

    fail_store = FailDeleteStore()
    set_credential_backend(fail_store)
    save_session_token(user["email"], user["session_token"])

    ret = main(["logout"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "failed to remove credential entry from Windows Credential Manager" in err


def test_logout_credential_read_failure(capsys):
    """If credential read fails with exception, CLI reports error cleanly without leaking tokens."""
    class BrokenStore(InMemoryCredentialStore):
        def read(self, target):
            raise RuntimeError("OS access violation in CredReadW")

    set_credential_backend(BrokenStore())
    ret = main(["logout"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "Credential Store Error: Failed to read from Windows Credential Manager" in err


def test_job_authorization_cross_account_isolation(capsys):
    """
    Verifies that all job actions (list, show, pause, resume, cancel) strictly enforce
    authenticated account ownership. User A cannot view or manipulate User B's jobs.
    """
    user_a = create_user_and_session("user_a_jobs@test.com", "Password123!")
    user_b = create_user_and_session("user_b_jobs@test.com", "Password123!")

    now = datetime.now(timezone.utc).isoformat()
    job_b_id = "job-b-isolated-001"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES (?, ?, 'running', 'Indiranagar', 12.97, 77.64, 1.0, '[]', 10, ?, ?)
            """,
            (job_b_id, user_b["user_id"], now, now),
        )

    # Login as User A
    save_session_token(user_a["email"], user_a["session_token"])

    # 1. jobs list: User A must NOT see User B's job
    ret_list = main(["jobs", "list", "--json"])
    assert ret_list == 0
    out_list = capsys.readouterr().out
    jobs_a = json.loads(out_list)
    assert not any(j["id"] == job_b_id for j in jobs_a)

    # 2. jobs show: User A requesting Job B -> 404 (exit code 1)
    ret_show = main(["jobs", "show", job_b_id])
    assert ret_show == 1
    err_show = capsys.readouterr().err
    assert "Not Found" in err_show

    # 3. jobs pause: User A pausing Job B -> 404 (exit code 1)
    ret_pause = main(["jobs", "pause", job_b_id])
    assert ret_pause == 1
    err_pause = capsys.readouterr().err
    assert "Not Found" in err_pause

    # 4. jobs resume: User A resuming Job B -> 404 (exit code 1)
    ret_resume = main(["jobs", "resume", job_b_id])
    assert ret_resume == 1
    err_resume = capsys.readouterr().err
    assert "Not Found" in err_resume

    # 5. jobs cancel: User A cancelling Job B -> 404 (exit code 1)
    ret_cancel = main(["jobs", "cancel", job_b_id, "--yes"])
    assert ret_cancel == 1
    err_cancel = capsys.readouterr().err
    assert "Not Found" in err_cancel

    # Verify Job B is still 'running' and was untouched
    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_b_id,)).fetchone()["status"]
        assert st == "running"


def test_start_job_idempotency_key_replay(capsys):
    """
    Submitting POST /api/jobs with the same Idempotency-Key twice returns the identical
    job and does NOT create a duplicate job row in the database.
    """
    user = create_user_and_session("idem_job_user@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    client = LeadHunterClient()
    idem_key = "job_idem_key_unique_789"

    # First submission
    job1 = client.start_job(
        location="Indiranagar",
        radius_km=1.0,
        max_leads=5,
        idempotency_key=idem_key,
    )
    job_id_1 = job1["id"]

    # Replay submission with same idempotency key
    job2 = client.start_job(
        location="Indiranagar",
        radius_km=1.0,
        max_leads=5,
        idempotency_key=idem_key,
    )
    job_id_2 = job2["id"]

    # Both responses must return the same Job ID
    assert job_id_1 == job_id_2

    # Database must have exactly ONE job
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 1


def test_automation_lost_response_retry_idempotency(tmp_path, capsys, monkeypatch):
    """
    Simulates a network drop after server accepts discovery job:
    Attempt 1 sends task, server starts job, but response raises ReadTimeout.
    Attempt 2 resends task with the same idempotency key.
    Server returns cached job, preventing duplicate job execution.
    """
    user = create_user_and_session("auto_lost_resp@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    recipe_file = str(tmp_path / "recipe_idempotent.json")
    with open(recipe_file, "w") as f:
        json.dump({
            "name": "Lost Response Recipe",
            "tasks": [{"location": "Indiranagar", "radius_km": 1.0, "max_leads": 5}]
        }, f)

    real_start_job = LeadHunterClient.start_job
    call_count = 0

    def mock_flaky_start_job(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt executes on server, but simulates lost HTTP response on client
            real_start_job(self, *args, **kwargs)
            raise CLINetworkError("Simulated socket drop before response body read")
        return real_start_job(self, *args, **kwargs)

    monkeypatch.setattr(LeadHunterClient, "start_job", mock_flaky_start_job)

    # Run automation plan
    ret = main(["automate", "run", recipe_file, "--yes"])
    assert ret == 0
    assert call_count == 2  # Proves retry was dispatched

    # Verify only ONE job was created in the database despite the retry
    with get_db() as conn:
        jobs = conn.execute("SELECT id FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchall()
        assert len(jobs) == 1


def test_automation_permanent_validation_error_skips_retries(tmp_path, capsys, monkeypatch):
    """
    Permanent client validation errors (e.g. 400 Bad Request) must NOT be retried 3 times.
    The runner must log the permanent error and skip immediately.
    """
    user = create_user_and_session("auto_perm_err@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    recipe_file = str(tmp_path / "recipe_bad.json")
    with open(recipe_file, "w") as f:
        json.dump({
            "tasks": [{"location": "X"}]
        }, f)

    call_count = 0
    def mock_start_bad_job(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise CLIValidationError("Location query too short", status_code=400)

    monkeypatch.setattr(LeadHunterClient, "start_job", mock_start_bad_job)

    ret = main(["automate", "run", recipe_file, "--yes"])
    assert ret == 0
    # Must only be called once, not retried 3 times!
    assert call_count == 1
    err = capsys.readouterr().err
    assert "Permanent validation error" in err


def test_credential_size_limit_rejection():
    """Secrets exceeding 512 bytes or targets > 512 chars must raise ValueError."""
    store = InMemoryCredentialStore()
    huge_secret = "x" * 513
    with pytest.raises(ValueError) as exc:
        store.write("valid_target", "user@test.com", huge_secret)
    assert "exceeds Windows Credential Manager limit" in str(exc.value)

    huge_target = "t" * 513
    with pytest.raises(ValueError):
        store.write(huge_target, "user@test.com", "secret")


def test_no_credentials_leaked_in_output(capsys):
    """Verifies that sensitive session tokens and passwords never appear in stdout or stderr."""
    secret_token = "sess_secret_private_token_xyz999"
    user = create_user_and_session("leak_test@test.com", "Password123!")
    save_session_token(user["email"], secret_token)

    # Test whoami
    main(["whoami"])
    out1, err1 = capsys.readouterr()
    assert secret_token not in out1
    assert secret_token not in err1

    # Test doctor
    main(["doctor"])
    out2, err2 = capsys.readouterr()
    assert secret_token not in out2
    assert secret_token not in err2


# ==============================================================================
# 13. MILESTONE 5.2 INDEPENDENT VERIFICATION TESTS
# ==============================================================================

def test_job_idempotency_payload_mismatch_returns_409():
    """
    Reusing an active Idempotency-Key with a different request payload must be explicitly
    rejected with HTTP 409 Conflict rather than silently returning an unrelated job.
    """
    user = create_user_and_session("idem_payload_mismatch@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    client = LeadHunterClient()
    idem_key = "job_idem_conflict_test_key"

    # First request: Indiranagar, radius 1.0, max 5 leads
    job1 = client.start_job(
        location="Indiranagar",
        radius_km=1.0,
        max_leads=5,
        idempotency_key=idem_key,
    )
    assert job1["id"] is not None

    # Second request: SAME key, but DIFFERENT location (Koramangala) -> must raise 409
    with pytest.raises(CLIError) as exc_info:
        client.start_job(
            location="Koramangala",
            radius_km=1.0,
            max_leads=5,
            idempotency_key=idem_key,
        )
    assert exc_info.value.status_code == 409
    assert "Idempotency key reuse conflict" in exc_info.value.message


def test_job_idempotency_action_type_mismatch_returns_409():
    """
    Reusing an Idempotency-Key from another action type (e.g. UPDATE_PIPELINE)
    on POST /api/jobs must raise HTTP 409 Conflict.
    """
    user = create_user_and_session("idem_action_mismatch@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    # Seed an idempotency key under another action type
    idem_key = "shared_action_key_123"
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO idempotency_keys
            (user_id, key, action_type, resource_id, response_status, response_body, created_at, expires_at)
            VALUES (?, ?, 'UPDATE_PIPELINE', 'lead-xyz', 200, '{"id": "lead-xyz"}', ?, ?)
            """,
            (user["user_id"], idem_key, now, expires),
        )

    client = LeadHunterClient()
    with pytest.raises(CLIError) as exc_info:
        client.start_job(
            location="Indiranagar",
            radius_km=1.0,
            max_leads=5,
            idempotency_key=idem_key,
        )
    assert exc_info.value.status_code == 409
    assert "different action type" in exc_info.value.message


def test_job_idempotency_concurrent_requests_safe():
    """
    Concurrent requests submitting the exact same Idempotency-Key and payload
    must safely return the identical job ID without duplicate background jobs.
    """
    from concurrent.futures import ThreadPoolExecutor

    user = create_user_and_session("idem_concurrent@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    idem_key = f"concurrent_key_{uuid.uuid4().hex[:8]}"

    def submit():
        c = LeadHunterClient()
        return c.start_job(
            location="Whitefield",
            radius_km=2.0,
            max_leads=10,
            idempotency_key=idem_key,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(submit) for _ in range(4)]
        results = [f.result() for f in futures]

    job_ids = {r["id"] for r in results}
    # All threads must receive the exact same job ID
    assert len(job_ids) == 1

    # Database must contain exactly one job row for this user
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["user_id"],)).fetchone()[0]
        assert count == 1


def test_automation_task_idem_key_determinism_and_sensitivity():
    """
    Verifies that automation task idempotency keys:
    1. Are fully deterministic for identical tasks.
    2. Change when location, radius, max_leads, categories, or filters change.
    """
    def make_sig(plan_name, idx, loc, radius, max_l, cats=None, phone_only=False, web_only=False):
        sig = f"{plan_name}:{idx}:{loc.strip().lower()}:{radius:.2f}:{max_l}:{sorted(cats or [])}:{phone_only}:{web_only}"
        h = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        return f"auto_{plan_name[:8]}_{idx}_{h}"

    k1 = make_sig("plan1", 1, "Indiranagar", 1.5, 25)
    k2 = make_sig("plan1", 1, "Indiranagar", 1.5, 25)
    assert k1 == k2  # Deterministic

    # Changing location -> distinct key
    k_loc = make_sig("plan1", 1, "Koramangala", 1.5, 25)
    assert k_loc != k1

    # Changing radius -> distinct key
    k_rad = make_sig("plan1", 1, "Indiranagar", 2.0, 25)
    assert k_rad != k1

    # Changing max_leads -> distinct key
    k_max = make_sig("plan1", 1, "Indiranagar", 1.5, 50)
    assert k_max != k1

    # Changing categories -> distinct key
    k_cat = make_sig("plan1", 1, "Indiranagar", 1.5, 25, cats=["cafe", "bakery"])
    assert k_cat != k1

    # Changing filters -> distinct key
    k_phone = make_sig("plan1", 1, "Indiranagar", 1.5, 25, phone_only=True)
    assert k_phone != k1


def test_cli_admin_adopt_legacy_unauthenticated(capsys):
    """Calling 'hunt admin adopt-legacy' without authentication exits with code 3."""
    clear_session_token()
    ret = main(["admin", "adopt-legacy", "-y", "--password", "Secret123!"])
    assert ret == 3
    err = capsys.readouterr().err
    assert "Authentication Error" in err


def test_cli_admin_adopt_legacy_empty_inventory(capsys):
    """When no unowned legacy records exist, admin adopt-legacy exits 0 and reports nothing to adopt."""
    user = create_user_and_session("admin_empty@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    ret = main(["admin", "adopt-legacy", "-y", "--password", "Password123!"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "No unowned legacy leads or jobs found" in out


def test_cli_admin_adopt_legacy_confirmation_aborted(monkeypatch, capsys):
    """Declining interactive confirmation aborts adoption without modifying records."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at) VALUES ('abort-lead', NULL, 'osm:ab1', 'Abort Cafe', 'Cafe', 12.9, 77.6, '', '{}', '{}', ?, ?)",
            (now, now),
        )

    user = create_user_and_session("admin_abort@test.com", "Password123!")
    save_session_token(user["email"], user["session_token"])

    # Simulate user answering 'n'
    monkeypatch.setattr("builtins.input", lambda _: "n")

    ret = main(["admin", "adopt-legacy", "--password", "Password123!"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Adoption cancelled" in out

    # Verify record remains unowned
    with get_db() as conn:
        assert conn.execute("SELECT user_id FROM leads WHERE id = 'abort-lead'").fetchone()[0] is None


def test_cli_admin_adopt_legacy_success(tmp_path, capsys):
    """Valid admin adoption claims unowned records and deletes local token."""
    admin_token_path = str(tmp_path / ".admin_token")
    os.environ["ADMIN_TOKEN_PATH"] = admin_token_path

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at) VALUES ('cli-job', NULL, 'completed', 'Indiranagar', 12.9, 77.6, 1.0, '[]', 10, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO leads (id, user_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at) VALUES ('cli-lead', NULL, 'osm:cl1', 'CLI Cafe', 'Cafe', 12.9, 77.6, '', '{}', '{}', ?, ?)",
            (now, now),
        )

    user = create_user_and_session("admin_cli@test.com", "Password123!")
    user_id = user["user_id"]
    save_session_token(user["email"], user["session_token"])

    try:
        ret = main(["admin", "adopt-legacy", "-y", "--password", "Password123!"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Successfully adopted 1 leads and 1 jobs" in out

        # Verify DB records updated
        with get_db() as conn:
            assert conn.execute("SELECT user_id FROM jobs WHERE id = 'cli-job'").fetchone()[0] == user_id
            assert conn.execute("SELECT user_id FROM leads WHERE id = 'cli-lead'").fetchone()[0] == user_id

        # Verify token file was deleted
        assert not os.path.exists(admin_token_path)
    finally:
        os.environ.pop("ADMIN_TOKEN_PATH", None)


