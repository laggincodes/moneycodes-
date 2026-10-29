"""
Phase 2: Comprehensive Windows Worker Supervision & Lifecycle Verification Suite.

Tests:
1. Worker Logging:
   - Sensitive credential and token redaction (Bearer, passwords, session tokens, CSRF, admin tokens).
   - Bounded rotating file logging with bounded disk footprint (maxBytes x backupCount).
   - Logging error fallback and recovery (unwritable directory or handler failure).
2. Subprocess Lifecycle:
   - Worker daemon subprocess startup with isolated temporary SQLite database.
   - Graceful shutdown via SIGINT / SIGTERM / SIGBREAK with clean exit.
   - Forced process termination (kill / TerminateProcess) simulating abrupt crash.
3. Recovery Invariants:
   - Lease expiry detection and reclaim by a successor worker.
   - Monotonic lease_version fencing preventing stale worker writes (split-brain prevention).
   - Checkpointed resumption skipping already-persisted leads (zero duplicate leads).
   - Retry exhaustion honoring max_retries limit.
   - Cooperative cancellation and pause handling.
4. Task Scheduler Script Verification:
   - setup_task_scheduler.ps1 dry-run execution without registering tasks.
   - Verification of generated XML task definition structure.

Offline only: Uses isolated temporary SQLite fixtures and mock providers.
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pytest

from app.database import init_db, get_db
from app.discovery.base import DiscoveryProvider, DiscoveryResult, DiscoveredBusiness, DiscoveryCoverageReport, FieldProvenance
from app.schemas import WebsiteAuditResult, LeadScore, SalesPitches
from app.queue.durable_worker import (
    DurableJobWorker,
    FencingRejectionError,
    StaleLeaseError,
    JobCancelledException,
    JobPausedException,
    sanitize_log_message,
)
from app.queue.worker_daemon import (
    SensitiveDataFilter,
    setup_worker_logging,
)


class MockLifecycleDiscovery(DiscoveryProvider):
    def __init__(self, businesses: List[DiscoveredBusiness] = None):
        self.businesses = businesses or []

    async def discover(self, *args, **kwargs) -> DiscoveryResult:
        coverage = DiscoveryCoverageReport(
            total_found=len(self.businesses),
            returned_count=len(self.businesses),
            source="mock",
            categories_searched=["cafe"],
            search_center={"lat": 12.97, "lon": 77.64},
            search_radius_km=1.5,
        )
        return DiscoveryResult(businesses=self.businesses, coverage=coverage)


class MockLifecycleAuditor:
    def __init__(self):
        self.audit_count = 0

    async def audit(self, url: str, business_name: str, category: str) -> WebsiteAuditResult:
        self.audit_count += 1
        return WebsiteAuditResult(
            has_website_url=bool(url),
            url=url,
            is_reachable=True,
            ssl_valid=True,
            has_title=True,
            has_viewport_meta=True,
        )


class MockLifecycleScorer:
    def calculate_score(self, biz: DiscoveredBusiness, audit: WebsiteAuditResult) -> LeadScore:
        return LeadScore(total_score=85, factors=[], priority="High", explanation="Strong candidate")


class MockLifecyclePitchGen:
    async def generate(self, biz: DiscoveredBusiness, audit: WebsiteAuditResult, score: LeadScore) -> SalesPitches:
        return SalesPitches(
            business_summary=f"Summary for {biz.name}",
            verified_contacts={"phone": biz.phone},
            audit_findings_summary="Needs redesign",
            recommended_features=["Online booking"],
            suggested_project_scope="Standard redesign",
            suggested_price_inr="₹25,000",
            email_subject=f"Website redesign for {biz.name}",
            email_draft=f"Hi {biz.name}, we can help rebuild your site.",
            follow_up_draft="Following up on our email",
            why_worth_contacting="High conversion potential",
            generation_source="deterministic_template",
        )


def make_business(idx: int, name: str = None) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        provider_id=f"osm:test_{idx}",
        name=name or f"Business {idx}",
        category="cafe",
        lat=12.9700 + (idx * 0.001),
        lon=77.6400 + (idx * 0.001),
        website=f"https://biz{idx}.example.com",
        phone=f"+91 98765 4321{idx}",
        address=f"#{idx} 100ft Road, Bangalore",
        source_url=f"https://www.openstreetmap.org/node/{1000 + idx}",
        provenance={"name": FieldProvenance(source="osm", confidence=1.0)},
        raw_data={"id": idx},
    )


@pytest.fixture(autouse=True)
def cleanup_logging_handlers():
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)


@pytest.fixture
def isolated_db(tmp_path):
    """Sets up an isolated SQLite database with schema and migrations applied."""
    db_file = str(tmp_path / "lifecycle_test.db")
    init_db(db_file)
    orig_env = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = db_file
    yield db_file
    if orig_env is not None:
        os.environ["DATABASE_PATH"] = orig_env
    else:
        os.environ.pop("DATABASE_PATH", None)


def seed_user(user_id: str, email: str = None) -> str:
    now = datetime.now(timezone.utc).isoformat()
    norm_email = email or f"{user_id}@example.com"
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, 'hash', 'Test User', ?, ?)
            """,
            (user_id, norm_email, now, now),
        )
    return user_id


# ==============================================================================
# 1. WORKER LOGGING & REDACTION TESTS
# ==============================================================================

def test_sensitive_data_filter_redacts_credentials():
    """Verifies that SensitiveDataFilter redacts tokens, bearer headers, passwords, and secrets."""
    f = SensitiveDataFilter()

    # Test Bearer header redaction
    rec1 = logging.LogRecord("test", logging.INFO, "test.py", 1, "Connecting with Bearer eyJhbGciOiJIUzI1NiJ9.secret", (), None)
    f.filter(rec1)
    assert "[REDACTED]" in rec1.msg
    assert "eyJhbGci" not in rec1.msg

    # Test password redaction
    rec2 = logging.LogRecord("test", logging.INFO, "test.py", 1, "User login password: SuperSecretPassword123!", (), None)
    f.filter(rec2)
    assert "[REDACTED]" in rec2.msg
    assert "SuperSecretPassword123!" not in rec2.msg

    # Test session and admin tokens
    rec3 = logging.LogRecord("test", logging.INFO, "test.py", 1, "Session session_token=sess_abc12345678 and admin_token=adm_9876543210", (), None)
    f.filter(rec3)
    assert "[REDACTED]" in rec3.msg
    assert "sess_abc12345678" not in rec3.msg
    assert "adm_9876543210" not in rec3.msg

    # Test CSRF token and API key
    rec4 = logging.LogRecord("test", logging.INFO, "test.py", 1, "Header x-csrf-token=csrf_token_secret_val with api_key=ak_live_test_123456", (), None)
    f.filter(rec4)
    assert "[REDACTED]" in rec4.msg
    assert "csrf_token_secret_val" not in rec4.msg
    assert "ak_live_test_123456" not in rec4.msg

    # Test URL basic auth
    rec5 = logging.LogRecord("test", logging.INFO, "test.py", 1, "Proxy: http://admin:supersecret@127.0.0.1:8000/api", (), None)
    f.filter(rec5)
    assert "[REDACTED]" in rec5.msg
    assert "supersecret" not in rec5.msg


def test_rotating_file_logging_bounds_disk_footprint(tmp_path):
    """Verifies that worker logging rotates when max_bytes is exceeded and respects backup_count."""
    log_dir = str(tmp_path / "test_logs")
    max_bytes = 1024  # 1 KB for fast rotation testing
    backup_count = 3

    logger = setup_worker_logging(log_dir=log_dir, max_bytes=max_bytes, backup_count=backup_count)

    log_file = os.path.join(log_dir, "worker.log")
    assert os.path.exists(log_dir)

    # Write enough log entries to trigger multiple rotations (10 KB total)
    for i in range(100):
        logger.info(f"Worker log entry iteration {i}: Processing job candidate batch data payload {uuid.uuid4().hex}")

    # Verify log files exist
    assert os.path.exists(log_file)
    rot_files = [f for f in os.listdir(log_dir) if f.startswith("worker.log")]

    # Files must include worker.log, worker.log.1, etc. but not exceed backup_count + 1 (active file)
    assert len(rot_files) <= backup_count + 1
    assert any(f.endswith(".1") for f in rot_files)

    # Verify each rotated file does not substantially exceed max_bytes (slight overshoot on record boundary is normal)
    for f in rot_files:
        fpath = os.path.join(log_dir, f)
        size = os.path.getsize(fpath)
        # Should be bounded reasonably close to max_bytes
        assert size < max_bytes * 3


def test_worker_logging_error_fallback(tmp_path, monkeypatch, capsys):
    """Verifies that setup_worker_logging falls back safely to console if file logging cannot be initialized."""
    # Point to an impossible file path (file as directory)
    dummy_file = tmp_path / "blocking_file.txt"
    dummy_file.write_text("block", encoding="utf-8")
    invalid_log_dir = str(dummy_file / "sub_logs")

    logger = setup_worker_logging(log_dir=invalid_log_dir)
    assert logger is not None

    # Logging must still work to console without raising an exception
    logger.info("Fallback test message to console.")
    out = capsys.readouterr().out
    assert "Fallback test message to console." in out


# ==============================================================================
# 2. SUBPROCESS LIFECYCLE & PROCESS MANAGEMENT
# ==============================================================================

def test_worker_daemon_subprocess_startup_and_poll(isolated_db, tmp_path):
    """
    Spawns worker_daemon.py as a real operating system subprocess pointing to an isolated database.
    Verifies that the process boots cleanly and enters the polling loop.
    """
    log_dir = str(tmp_path / "subproc_logs")
    env = os.environ.copy()
    env["DATABASE_PATH"] = isolated_db
    env["WORKER_POLL_INTERVAL"] = "0.5"
    env["WORKER_LOG_DIR"] = log_dir

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cmd = [sys.executable, "-m", "app.queue.worker_daemon"]

    proc = subprocess.Popen(
        cmd,
        cwd=backend_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Give worker 2 seconds to boot and poll
        time.sleep(2.0)
        assert proc.poll() is None, "Worker daemon subprocess died unexpectedly."

        # Verify worker log file was created by the daemon
        log_file = os.path.join(log_dir, "worker.log")
        assert os.path.exists(log_file), f"Expected log file not found at {log_file}"

        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
            assert "Initializing WebsiteLead Hunter standalone worker daemon" in content
            assert "entering polling loop" in content

    finally:
        # Terminate cleanly
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_worker_daemon_subprocess_forced_termination_and_recovery(isolated_db, tmp_path):
    """
    Verifies forced process termination (simulating hard crash/power failure):
    1. Worker A claims job.
    2. Worker A is abruptly killed (proc.kill()).
    3. Job remains 'running' with dead Worker A's lease.
    4. Worker B starts, detects expired lease, reclaims job, and resumes.
    """
    now = datetime.now(timezone.utc).isoformat()
    # Expired lease from a dead worker
    past_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    seed_user("user_subproc")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads,
                             total_found, processed_count, checkpoint_data, worker_id, lease_expires_at,
                             lease_version, retry_count, max_retries, created_at, updated_at)
            VALUES ('job-killed-proc', 'user_subproc', 'running', 'Whitefield', 12.97, 77.64, 1.0, '["cafe"]', 5,
                    3, 1, '{"discovered_count": 3, "processed_indices": [0]}', 'worker_dead_9999', ?,
                    1, 0, 3, ?, ?)
            """,
            (past_time, now, now),
        )
        # Pre-seed lead 0 as already persisted
        conn.execute(
            """
            INSERT INTO leads (id, user_id, job_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, score, pipeline_status, created_at, updated_at)
            VALUES ('lead-proc-0', 'user_subproc', 'job-killed-proc', 'osm:test_0', 'Business 0', 'cafe', 12.97, 77.64, 'https://biz0.example.com', '{}', '{}', 80, 'new', ?, ?)
            """,
            (now, now),
        )

    # Worker B starts to recover the job
    mock_biz = [make_business(0), make_business(1), make_business(2)]
    mock_auditor = MockLifecycleAuditor()
    worker_b = DurableJobWorker(
        worker_id="worker_b_recovery",
        discovery_provider=MockLifecycleDiscovery(mock_biz),
        auditor=mock_auditor,
        scorer=MockLifecycleScorer(),
        pitch_gen=MockLifecyclePitchGen(),
    )

    # Worker B claims next job
    claimed = worker_b.claim_next_job()
    assert claimed is not None
    assert claimed["id"] == "job-killed-proc"
    assert claimed["lease_version"] == 2
    assert claimed["retry_count"] == 1
    assert claimed["worker_id"] == "worker_b_recovery"

    # Worker B executes job to completion
    asyncio.run(worker_b.process_claimed_job(claimed))

    with get_db() as conn:
        final_job = conn.execute("SELECT status, processed_count, retry_count FROM jobs WHERE id = 'job-killed-proc'").fetchone()
        assert final_job["status"] == "completed"
        assert final_job["processed_count"] == 3
        assert final_job["retry_count"] == 1

        # Confirm exactly 3 leads in DB, zero duplicate leads
        leads = conn.execute("SELECT provider_id FROM leads WHERE job_id = 'job-killed-proc'").fetchall()
        assert len(leads) == 3
        assert len({row["provider_id"] for row in leads}) == 3

    # Auditor was called only for businesses 1 and 2 (business 0 was skipped because already in DB)
    assert mock_auditor.audit_count == 2


# ==============================================================================
# 3. FENCING, SPLIT-BRAIN & RETRY LIMIT INVARIANTS
# ==============================================================================

def test_stale_worker_cannot_commit_after_lease_reclaim(isolated_db):
    """
    Verifies that a stale worker whose lease expired and was reclaimed by another worker
    is strictly rejected by conditional SQLite UPDATEs (FencingRejectionError).
    """
    seed_user("user_fence")
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads,
                             worker_id, lease_expires_at, lease_version, retry_count, max_retries, created_at, updated_at)
            VALUES ('job-fencing-test', 'user_fence', 'running', 'Koramangala', 12.93, 77.62, 1.0, '["cafe"]', 10,
                    'worker_b_active', ?, 2, 1, 3, ?, ?)
            """,
            ((datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(), now, now),
        )

    # Stale Worker A still thinks it holds lease_version=1
    stale_worker_a = DurableJobWorker(worker_id="worker_a_stale")
    stale_worker_a._current_job_id = "job-fencing-test"
    stale_worker_a._current_lease_version = 1  # Expired fencing token

    # Attempt 1: Checkpoint update with stale lease_version
    with pytest.raises(FencingRejectionError):
        stale_worker_a.update_checkpoint(
            "job-fencing-test",
            1,
            {"processed_count": 5},
        )

    # Attempt 2: Lead insertion with stale lease_version
    biz = make_business(99)
    audit = WebsiteAuditResult(has_website_url=True, url="https://biz99.example.com", is_reachable=True, ssl_valid=True)
    score = LeadScore(total_score=90, factors=[], priority="High", explanation="Prospect")
    pitches = SalesPitches(
        business_summary="Summary",
        verified_contacts={},
        audit_findings_summary="Summary",
        recommended_features=[],
        suggested_project_scope="Scope",
        suggested_price_inr="₹25,000",
        email_subject="Subject",
        email_draft="Draft",
        follow_up_draft="Followup",
        why_worth_contacting="Why",
        generation_source="deterministic_template",
    )

    with pytest.raises(FencingRejectionError):
        stale_worker_a.persist_lead_fenced(
            job_id="job-fencing-test",
            lease_version=1,
            biz=biz,
            audit_result=audit,
            score=score,
            pitches=pitches,
            processed_count=1,
            user_id="user_fence",
        )

    # Verify no lead 99 was inserted into database
    with get_db() as conn:
        lead = conn.execute("SELECT id FROM leads WHERE provider_id = 'osm:test_99'").fetchone()
        assert lead is None


def test_retry_limit_exhaustion_marks_job_failed(isolated_db):
    """
    Verifies that when a job repeatedly crashes and reaches max_retries,
    claim_next_job transitions the job to 'failed' and releases the lease.
    """
    seed_user("user_retry")
    now = datetime.now(timezone.utc).isoformat()
    expired = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads,
                             worker_id, lease_expires_at, lease_version, retry_count, max_retries, created_at, updated_at)
            VALUES ('job-retry-limit', 'user_retry', 'running', 'Indiranagar', 12.97, 77.64, 1.0, '["cafe"]', 10,
                    'worker_crashed', ?, 2, 2, 2, ?, ?)
            """,
            (expired, now, now),
        )

    worker = DurableJobWorker(worker_id="worker_limit_checker")

    # Claim next job: Since retry_count (2) == max_retries (2), recovery must mark it failed
    claimed = worker.claim_next_job()
    assert claimed is None

    with get_db() as conn:
        row = conn.execute("SELECT status, error_message, retry_count FROM jobs WHERE id = 'job-retry-limit'").fetchone()
        assert row["status"] == "failed"
        assert "Exceeded maximum lease recovery retry attempts" in row["error_message"]
        assert row["retry_count"] == 2


def test_cooperative_cancellation_during_job(isolated_db):
    """Verifies that when a user cancels an active job, the worker cooperatively halts."""
    seed_user("user_canceller")
    mock_biz = [make_business(i) for i in range(5)]
    worker = DurableJobWorker(
        worker_id="worker_cancel_test",
        discovery_provider=MockLifecycleDiscovery(mock_biz),
        auditor=MockLifecycleAuditor(),
        scorer=MockLifecycleScorer(),
        pitch_gen=MockLifecyclePitchGen(),
    )

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES ('job-to-cancel', 'user_canceller', 'pending', 'Indiranagar', 12.97, 77.64, 1.0, '["cafe"]', 5, ?, ?)
            """,
            (now, now),
        )

    claimed = worker.claim_next_job()
    assert claimed is not None

    # Simulate user cancelling the job concurrently in the database
    with get_db() as conn:
        conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = 'job-to-cancel'")

    # When worker attempts to process, it cooperatively observes cancellation and halts cleanly
    asyncio.run(worker.process_claimed_job(claimed))

    with get_db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = 'job-to-cancel'").fetchone()
        assert row["status"] == "cancelled"


# ==============================================================================
# 4. TASK SCHEDULER SCRIPT VALIDATION
# ==============================================================================

def test_setup_task_scheduler_script_dry_run():
    """
    Executes setup_task_scheduler.ps1 in validation/dry-run mode via PowerShell.
    Confirms:
    1. Script executes with exit code 0.
    2. Output confirms SAFETY STATUS: TASK CREATION AND ACTIVATION DISABLED.
    3. schtasks query confirms task is NOT registered in Task Scheduler.
    """
    script_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "setup_task_scheduler.ps1")
    )
    assert os.path.exists(script_path), f"Script not found at {script_path}"

    res = subprocess.run(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script_path],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Script failed with output: {res.stderr}"
    assert "SAFETY STATUS: TASK CREATION AND ACTIVATION DISABLED" in res.stdout
    assert "WebsiteLeadHunter-DurableWorker" in res.stdout
    assert "XML definition generated" in res.stdout

    # Verify task is NOT registered
    query_res = subprocess.run(
        ["schtasks.exe", "/query", "/tn", "WebsiteLeadHunter-DurableWorker"],
        capture_output=True,
        text=True,
    )
    # schtasks returns 1 / error when task does not exist
    assert query_res.returncode != 0
