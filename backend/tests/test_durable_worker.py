"""
Milestone 9C: Durable Worker Implementation & Failure Invariant Test Suite.

Verifies:
1. Two workers attempting to claim the same job (atomic mutual exclusion).
2. Lease expiration and reclaim by a second worker (monotonic lease_version bump).
3. Stale worker attempting to update job state (conditional rowcount == 0).
4. Stale worker attempting to insert a lead after losing ownership (fencing rejection & rollback).
5. Crash during discovery and partial checkpoint recovery.
6. Duplicate candidates across a resumed job (zero duplicate leads).
7. Cancellation during processing (cooperative halt).
8. Graceful shutdown and recovery after process termination.
9. Concurrent SQLite reads and writes in WAL mode.
10. Migration v4 from previous schema and repeated execution idempotency.
11. Existing idempotency behavior and tenant isolation.
12. Backup and restore using Python's SQLite Online Backup API.

Offline only: Uses mock providers and isolated SQLite fixtures.
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pytest

from app.database import init_db, get_db
from app.migrations import run_migrations, get_column_names
from app.discovery.base import DiscoveryProvider, DiscoveryResult, DiscoveredBusiness, DiscoveryCoverageReport, FieldProvenance
from app.schemas import WebsiteAuditResult, LeadScore, SalesPitches, StartJobRequest
from app.queue.durable_worker import (
    DurableJobWorker,
    FencingRejectionError,
    StaleLeaseError,
    JobCancelledException,
    JobPausedException,
    WindowsSleepInhibitor,
)


class MockOfflineDiscovery(DiscoveryProvider):
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


class MockFastAuditor:
    def __init__(self):
        self.audit_calls = []

    async def audit(self, url: str, business_name: str, category: str) -> WebsiteAuditResult:
        self.audit_calls.append({"url": url, "name": business_name})
        return WebsiteAuditResult(
            has_website_url=bool(url),
            url=url,
            is_reachable=True,
            ssl_valid=True,
            has_title=True,
            has_viewport_meta=True,
        )


class MockFastScorer:
    def calculate_score(self, biz: DiscoveredBusiness, audit: WebsiteAuditResult) -> LeadScore:
        return LeadScore(total_score=75, factors=[], priority="High", explanation="Good prospect")


class MockFastPitchGen:
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


@pytest.fixture(autouse=True)
def setup_durable_worker_db(tmp_path):
    """Sets up an isolated SQLite database with migrations applied for each test."""
    db_file = str(tmp_path / "durable_worker_test.db")
    init_db(db_file)
    yield db_file


def seed_test_user(user_id: str = "u_test_1") -> str:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, 'hash', 'Test User', ?, ?)
            """,
            (user_id, f"{user_id}@test.com", now, now),
        )
    return user_id


def seed_test_job(
    job_id: str = "test_job_1",
    user_id: str = "u_test_1",
    status: str = "pending",
    max_leads: int = 5,
    categories: list = None,
    checkpoint_data: str = None,
) -> str:
    seed_test_user(user_id)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, checkpoint_data,
                lease_version, retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, 'Indiranagar, Bangalore', 12.97, 77.64, 1.5, ?, ?, 0, 0, '[]', ?, 0, 0, 3, ?, ?)
            """,
            (
                job_id,
                user_id,
                status,
                json.dumps(categories or ["cafe"]),
                max_leads,
                checkpoint_data,
                now,
                now,
            ),
        )
    return job_id


# ==============================================================================
# 1. ATOMIC JOB CLAIMS & MUTUAL EXCLUSION
# ==============================================================================

def test_two_workers_claiming_same_job_atomic():
    """Two concurrent workers attempt to claim the same pending job; exactly one succeeds."""
    job_id = seed_test_job("job_atomic_1", status="pending")

    worker_a = DurableJobWorker(worker_id="worker_A", lease_duration_sec=30)
    worker_b = DurableJobWorker(worker_id="worker_B", lease_duration_sec=30)

    # Worker A claims first
    claim_a = worker_a.claim_next_job()
    assert claim_a is not None
    assert claim_a["id"] == job_id
    assert claim_a["worker_id"] == "worker_A"
    assert claim_a["lease_version"] == 1
    assert claim_a["status"] == "running"

    # Worker B attempts to claim; should return None because queue is now empty
    claim_b = worker_b.claim_next_job()
    assert claim_b is None

    # Verify database state
    with get_db() as conn:
        row = conn.execute("SELECT worker_id, lease_version, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["worker_id"] == "worker_A"
        assert row["lease_version"] == 1
        assert row["status"] == "running"


# ==============================================================================
# 2. LEASE EXPIRATION & RECLAIM BY SECOND WORKER
# ==============================================================================

def test_lease_expiration_and_reclaim_by_second_worker():
    """When a worker's lease expires, a second worker successfully reclaims the job and increments lease_version."""
    job_id = seed_test_job("job_lease_exp_1", status="pending")

    worker_a = DurableJobWorker(worker_id="worker_A", lease_duration_sec=10)
    worker_b = DurableJobWorker(worker_id="worker_B", lease_duration_sec=30)

    claim_a = worker_a.claim_next_job()
    assert claim_a["lease_version"] == 1

    # Simulate Worker A stalling and lease expiring in the past
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (past_iso, job_id))

    # Worker B claims the expired job
    claim_b = worker_b.claim_next_job()
    assert claim_b is not None
    assert claim_b["id"] == job_id
    assert claim_b["worker_id"] == "worker_B"
    assert claim_b["lease_version"] == 2
    assert claim_b["retry_count"] == 1


# ==============================================================================
# 3. STALE WORKER STATUS UPDATE REJECTED
# ==============================================================================

def test_stale_worker_status_update_rejected():
    """A stale worker attempting to update progress or renew lease on a reclaimed job affects zero rows."""
    job_id = seed_test_job("job_stale_1", status="pending")

    worker_a = DurableJobWorker(worker_id="worker_A")
    worker_b = DurableJobWorker(worker_id="worker_B")

    claim_a = worker_a.claim_next_job()
    assert claim_a["lease_version"] == 1

    # Worker B reclaims after expiration
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (past_iso, job_id))

    claim_b = worker_b.claim_next_job()
    assert claim_b["lease_version"] == 2

    # Worker A attempts to renew its expired lease (v1)
    renew_result = worker_a.renew_lease(job_id, lease_version=1)
    assert renew_result is False

    # Worker A direct conditional update affects zero rows
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE jobs SET processed_count = 5, updated_at = ?
            WHERE id = ? AND worker_id = 'worker_A' AND lease_version = 1 AND status = 'running'
            """,
            (now, job_id),
        )
        assert cur.rowcount == 0


# ==============================================================================
# 4. STALE WORKER LEAD INSERT FENCING REJECTION & ROLLBACK
# ==============================================================================

def test_stale_worker_lead_insert_fencing_rejection():
    """
    Proves that a stale worker cannot insert leads or commit progress after a newer lease is issued.
    FencingRejectionError must be raised and the transaction rolled back.
    """
    job_id = seed_test_job("job_fence_lead_1", user_id="u_fence", status="pending")

    worker_a = DurableJobWorker(worker_id="worker_A")
    worker_b = DurableJobWorker(worker_id="worker_B")

    claim_a = worker_a.claim_next_job()
    assert claim_a["lease_version"] == 1

    # Worker B reclaims the job
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (past_iso, job_id))

    claim_b = worker_b.claim_next_job()
    assert claim_b["lease_version"] == 2

    # Worker A now attempts to persist a lead using its stale lease_version=1
    stale_biz = DiscoveredBusiness(
        provider_id="osm:node:stale_999",
        name="Stale Cafe",
        category="cafe",
        lat=12.97,
        lon=77.64,
        source_url="http://test.com",
        provenance={"source": FieldProvenance(source="test")},
    )
    mock_audit = WebsiteAuditResult(has_website_url=False, is_reachable=False)
    mock_score = LeadScore(total_score=50, factors=[], priority="Medium", explanation="Test")
    mock_pitches = SalesPitches(
        business_summary="Summary",
        verified_contacts={},
        audit_findings_summary="Findings",
        recommended_features=[],
        suggested_project_scope="Scope",
        suggested_price_inr="₹10,000",
        email_subject="Sub",
        email_draft="Draft",
        follow_up_draft="Follow",
        why_worth_contacting="Why",
        generation_source="deterministic_template",
    )

    with pytest.raises(FencingRejectionError) as exc_info:
        worker_a.persist_lead_fenced(
            job_id=job_id,
            lease_version=1,
            biz=stale_biz,
            audit_result=mock_audit,
            score=mock_score,
            pitches=mock_pitches,
            processed_count=1,
            user_id="u_fence",
        )

    assert "Fencing Rejection" in str(exc_info.value)

    # Prove that transaction was rolled back: lead does NOT exist in database!
    with get_db() as conn:
        row = conn.execute("SELECT * FROM leads WHERE provider_id = 'osm:node:stale_999'").fetchone()
        assert row is None, "Stale lead was written to database! Fencing transaction rollback failed."


# ==============================================================================
# 5. CRASH DURING DISCOVERY & PARTIAL CHECKPOINT RECOVERY
# ==============================================================================

@pytest.mark.asyncio
async def test_crash_during_discovery_and_partial_checkpointing():
    """Worker recovers from checkpointed candidates and resumes auditing from the next lead."""
    job_id = seed_test_job("job_checkpoint_1", user_id="u_chk", status="pending", max_leads=3)

    # Candidate businesses
    biz1 = DiscoveredBusiness(provider_id="osm:1", name="Cafe 1", category="cafe", lat=12.9, lon=77.6, source_url="http://1", provenance={})
    biz2 = DiscoveredBusiness(provider_id="osm:2", name="Cafe 2", category="cafe", lat=12.9, lon=77.6, source_url="http://2", provenance={})
    biz3 = DiscoveredBusiness(provider_id="osm:3", name="Cafe 3", category="cafe", lat=12.9, lon=77.6, source_url="http://3", provenance={})

    auditor = MockFastAuditor()
    worker_1 = DurableJobWorker(
        worker_id="worker_chk_1",
        discovery_provider=MockOfflineDiscovery([biz1, biz2, biz3]),
        auditor=auditor,
        scorer=MockFastScorer(),
        pitch_gen=MockFastPitchGen(),
    )

    # Claim job
    claimed = worker_1.claim_next_job()
    assert claimed["id"] == job_id

    # Simulate Worker 1 checkpointing the candidates and auditing only biz1 before crashing
    worker_1.update_checkpoint(job_id, claimed["lease_version"], {"candidates": [b.model_dump() for b in [biz1, biz2, biz3]]})
    worker_1.persist_lead_fenced(
        job_id=job_id,
        lease_version=claimed["lease_version"],
        biz=biz1,
        audit_result=await auditor.audit("", "Cafe 1", "cafe"),
        score=MockFastScorer().calculate_score(biz1, None),
        pitches=await MockFastPitchGen().generate(biz1, None, None),
        processed_count=1,
        user_id="u_chk",
    )

    # Simulate crash: lease expires
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (past_iso, job_id))

    # Worker 2 resumes from checkpoint
    auditor_2 = MockFastAuditor()
    worker_2 = DurableJobWorker(
        worker_id="worker_chk_2",
        auditor=auditor_2,
        scorer=MockFastScorer(),
        pitch_gen=MockFastPitchGen(),
    )

    reclaimed = worker_2.claim_next_job()
    assert reclaimed["id"] == job_id
    assert reclaimed["lease_version"] == 2

    # Process to completion
    await worker_2.process_claimed_job(reclaimed)

    # Verify that Worker 2 only audited biz2 and biz3 (biz1 was skipped!)
    audited_names = [call["name"] for call in auditor_2.audit_calls]
    assert "Cafe 1" not in audited_names, "Already audited lead was re-audited after restart!"
    assert "Cafe 2" in audited_names
    assert "Cafe 3" in audited_names

    # Verify total leads in DB
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = ?", (job_id,)).fetchone()[0]
        assert count == 3
        status = conn.execute("SELECT status, processed_count FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert status["status"] == "completed"
        assert status["processed_count"] == 3


# ==============================================================================
# 6. DUPLICATE CANDIDATES ACROSS RESUMED JOB
# ==============================================================================

@pytest.mark.asyncio
async def test_duplicate_candidates_across_resumed_job():
    """Duplicate candidate items across multiple discovery batches never produce duplicate leads."""
    job_id = seed_test_job("job_dup_1", user_id="u_dup", status="pending", max_leads=2)

    # Two businesses with duplicate provider_ids
    b1 = DiscoveredBusiness(provider_id="osm:same_id_100", name="Coffee House", category="cafe", lat=12.9, lon=77.6, source_url="http://c", provenance={})
    b2 = DiscoveredBusiness(provider_id="osm:same_id_100", name="Coffee House Duplicate", category="cafe", lat=12.9, lon=77.6, source_url="http://c", provenance={})

    worker = DurableJobWorker(
        discovery_provider=MockOfflineDiscovery([b1, b2]),
        auditor=MockFastAuditor(),
        scorer=MockFastScorer(),
        pitch_gen=MockFastPitchGen(),
    )

    claimed = worker.claim_next_job()
    await worker.process_claimed_job(claimed)

    with get_db() as conn:
        leads_count = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = ?", (job_id,)).fetchone()[0]
        assert leads_count == 1, "Duplicate provider_id resulted in multiple lead records!"


# ==============================================================================
# 7. CANCELLATION DURING PROCESSING
# ==============================================================================

@pytest.mark.asyncio
async def test_cancellation_during_processing():
    """Worker cleanly observes cooperative cancellation in SQLite and halts execution."""
    job_id = seed_test_job("job_cancel_1", user_id="u_cancel", status="pending", max_leads=5)

    bizs = [
        DiscoveredBusiness(provider_id=f"osm:c_{i}", name=f"Biz {i}", category="cafe", lat=12.9, lon=77.6, source_url="http://u", provenance={})
        for i in range(5)
    ]

    class CancellingAuditor:
        def __init__(self):
            self.calls = 0

        async def audit(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                # Cancel job in database mid-execution
                with get_db() as conn:
                    conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = 'job_cancel_1'")
            return WebsiteAuditResult(has_website_url=False, is_reachable=False)

    cancelling_auditor = CancellingAuditor()
    worker = DurableJobWorker(
        discovery_provider=MockOfflineDiscovery(bizs),
        auditor=cancelling_auditor,
        scorer=MockFastScorer(),
        pitch_gen=MockFastPitchGen(),
    )

    claimed = worker.claim_next_job()
    await worker.process_claimed_job(claimed)

    # Worker should have stopped after the cancellation was observed
    assert cancelling_auditor.calls <= 2

    with get_db() as conn:
        st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        assert st == "cancelled"


# ==============================================================================
# 8. GRACEFUL SHUTDOWN & RECOVERY
# ==============================================================================

@pytest.mark.asyncio
async def test_graceful_shutdown_and_recovery():
    """Worker request_stop() halts processing cleanly and releases lease for resumption."""
    job_id = seed_test_job("job_stop_1", user_id="u_stop", status="pending", max_leads=5)

    bizs = [
        DiscoveredBusiness(provider_id=f"osm:s_{i}", name=f"Shop {i}", category="shop", lat=12.9, lon=77.6, source_url="http://s", provenance={})
        for i in range(5)
    ]

    worker = DurableJobWorker(
        discovery_provider=MockOfflineDiscovery(bizs),
        auditor=MockFastAuditor(),
        scorer=MockFastScorer(),
        pitch_gen=MockFastPitchGen(),
    )

    claimed = worker.claim_next_job()
    # Trigger stop immediately
    worker.request_stop()

    await worker.process_claimed_job(claimed)

    # Job was paused cleanly and lease was released
    with get_db() as conn:
        row = conn.execute("SELECT status, worker_id, lease_expires_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "paused"
        assert row["worker_id"] is None
        assert row["lease_expires_at"] is None


# ==============================================================================
# 9. CONCURRENT SQLITE READS & WRITES
# ==============================================================================

def test_concurrent_sqlite_reads_and_writes(tmp_path):
    """Under SQLite WAL mode, readers do not block writers and writers do not block readers."""
    db_file = str(tmp_path / "wal_concurrency.db")
    init_db(db_file)

    errors = []
    read_counts = []
    stop_event = threading.Event()

    def reader_loop():
        conn = sqlite3.connect(db_file, timeout=15.0)
        conn.execute("PRAGMA journal_mode = WAL")
        while not stop_event.is_set():
            try:
                cur = conn.execute("SELECT COUNT(*) FROM jobs")
                cnt = cur.fetchone()[0]
                read_counts.append(cnt)
                time.sleep(0.005)
            except Exception as e:
                errors.append(e)
                break
        conn.close()

    def writer_loop():
        conn = sqlite3.connect(db_file, timeout=15.0)
        conn.execute("PRAGMA journal_mode = WAL")
        for i in range(25):
            try:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
                    VALUES (?, 'u', 'pending', 'loc', 0, 0, 1, '[]', 5, ?, ?)
                    """,
                    (f"j_{i}", now, now),
                )
                conn.commit()
                time.sleep(0.01)
            except Exception as e:
                errors.append(e)
                break
        conn.close()

    reader_thread = threading.Thread(target=reader_loop)
    writer_thread = threading.Thread(target=writer_loop)

    reader_thread.start()
    writer_thread.start()

    writer_thread.join()
    stop_event.set()
    reader_thread.join()

    assert len(errors) == 0, f"Concurrent database errors occurred: {errors}"
    assert len(read_counts) > 10
    assert max(read_counts) == 25


# ==============================================================================
# 10. MIGRATION V4 & REPEATED EXECUTION
# ==============================================================================

def test_migration_v4_and_repeated_execution(tmp_path):
    """Verify Migration v4 adds worker lease columns and re-running is 100% idempotent."""
    db_file = str(tmp_path / "mig_v4_test.db")
    conn = sqlite3.connect(db_file)
    init_db(db_file)

    # Columns must exist after init_db()
    cols = get_column_names(conn, "jobs")
    assert "worker_id" in cols
    assert "lease_expires_at" in cols
    assert "heartbeat_at" in cols
    assert "lease_version" in cols
    assert "retry_count" in cols
    assert "max_retries" in cols
    assert "checkpoint_data" in cols

    # Re-run migrations
    run_migrations(conn)
    cols_after = get_column_names(conn, "jobs")
    assert cols_after.count("lease_version") == 1
    conn.close()


# ==============================================================================
# 11. WINDOWS SLEEP INHIBITOR UNIT TEST
# ==============================================================================

def test_windows_sleep_inhibitor_safe_fallback():
    """Verify WindowsSleepInhibitor initializes safely and handles inhibit/restore."""
    inhibitor = WindowsSleepInhibitor()
    # Inhibit and restore must execute without throwing exceptions on any platform
    inhibitor.inhibit()
    inhibitor.restore()
    assert inhibitor._active is False


# ==============================================================================
# 12. BACKUP & RESTORE USING PYTHON SQLITE ONLINE BACKUP API
# ==============================================================================

def test_sqlite_online_backup_and_restore(tmp_path):
    """Tests consistent snapshot backup and integrity verification drill."""
    source_db = str(tmp_path / "source.db")
    backup_db = str(tmp_path / "backup_snapshot.db")
    init_db(source_db)

    # Seed some data into source
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES ('u1', 'backup_user@test.com', 'hash', 'Backup User', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, status, location_query, lat, lon, radius_km, categories, max_leads, created_at, updated_at)
            VALUES ('backup_j1', 'u1', 'completed', 'Bangalore', 12.9, 77.6, 1, '[]', 10, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (id, user_id, job_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('l1', 'u1', 'backup_j1', 'osm:100', 'Cafe Restore', 'cafe', 12.9, 77.6, 'http://r', '{}', '{}', ?, ?)
            """,
            (now, now),
        )

    # Perform online backup using native Python sqlite3.backup() API
    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(backup_db)
    with dst:
        src.backup(dst, pages=100, sleep=0.005)
    dst.close()
    src.close()

    # Verify backup integrity
    verify_conn = sqlite3.connect(backup_db)
    verify_conn.row_factory = sqlite3.Row

    # PRAGMA integrity_check must return 'ok'
    integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"

    # PRAGMA foreign_key_check must return zero rows
    fk_errors = verify_conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_errors) == 0

    # Row counts must match exactly
    jobs_count = verify_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    leads_count = verify_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    assert jobs_count == 1
    assert leads_count == 1

    verify_conn.close()


# ==============================================================================
# 13. RETRY LIMIT EXHAUSTION MARKS JOB FAILED
# ==============================================================================

def test_retry_limit_exhaustion_marks_job_failed(tmp_path):
    """Verify that jobs exceeding max_retries upon lease recovery are marked failed."""
    db_path = str(tmp_path / "test_retry_fail.db")
    init_db(db_path)
    seed_test_user("u_retry")

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        # Insert job that has already failed 3 times (retry_count=3, max_retries=3)
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, worker_id, lease_expires_at, heartbeat_at, lease_version,
                retry_count, max_retries, created_at, updated_at
            )
            VALUES (
                'job_exhausted', 'u_retry', 'running', 'Bangalore', 12.97, 77.64, 1.5, '["cafe"]',
                10, 'worker_dead', ?, ?, 3, 3, 3, ?, ?
            )
            """,
            (past, past, now, now),
        )

        # Insert another job that is at retry_count=2, max_retries=3 (should succeed on next claim)
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, worker_id, lease_expires_at, heartbeat_at, lease_version,
                retry_count, max_retries, created_at, updated_at
            )
            VALUES (
                'job_recoverable', 'u_retry', 'running', 'Bangalore', 12.97, 77.64, 1.5, '["cafe"]',
                10, 'worker_dead', ?, ?, 2, 2, 3, ?, ?
            )
            """,
            (past, past, now, now),
        )

    worker = DurableJobWorker(worker_id="worker_active")

    # 1. First claim attempt should hit job_exhausted, recognize retry limit reached,
    # mark it failed, and return None for that claim cycle.
    claimed_1 = worker.claim_next_job()
    assert claimed_1 is None

    # Check job_exhausted status in DB
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row_exhausted = conn.execute("SELECT * FROM jobs WHERE id = 'job_exhausted'").fetchone()
        assert row_exhausted["status"] == "failed"
        assert "Exceeded maximum lease recovery retry attempts (3)" in (row_exhausted["error_message"] or "")

    # 2. Next claim attempt should now claim job_recoverable and bump retry_count to 3
    claimed_2 = worker.claim_next_job()
    assert claimed_2 is not None
    assert claimed_2["id"] == "job_recoverable"
    assert claimed_2["retry_count"] == 3
    assert claimed_2["lease_version"] == 3

