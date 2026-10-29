"""
Milestone 9C.2: Comprehensive Retry Exhaustion & Crash Recovery Matrix.

Tests:
Part A:
1. Initial job claim (attempt 1, retry_count=0).
2. Retry limit zero (max_retries=0 -> initial attempt runs, 0 retries permitted).
3. Retry limit one (max_retries=1 -> initial attempt + 1 retry, fails on 2nd recovery).
4. Retry limit two (max_retries=2 -> initial attempt + 2 retries, fails on 3rd recovery).
5. Healthy lease renewal does not consume a retry.
6. An expired lease is counted once per recovery.
7. Concurrent workers cannot bypass the retry limit.
8. Stale workers cannot alter retry metadata or status.
9. Completed, cancelled, and failed jobs cannot be reclaimed.
10. Null, zero, negative, and malformed retry settings are handled safely.

Part B:
Six Crash-Recovery Boundaries tested separately:
Boundary 1: Crash after claim, before discovery.
Boundary 2: Crash during discovery before checkpoint persistence.
Boundary 3: Crash after checkpoint persistence, before first lead persisted.
Boundary 4: Crash after audit finishes, before lead persistence.
Boundary 5: Crash after lead persistence, before progress/checkpoint update.
Boundary 6: Crash during final completion.
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
from app.discovery.base import DiscoveryProvider, DiscoveryResult, DiscoveredBusiness, DiscoveryCoverageReport, FieldProvenance
from app.schemas import WebsiteAuditResult, LeadScore, SalesPitches
from app.queue.durable_worker import (
    DurableJobWorker,
    FencingRejectionError,
    StaleLeaseError,
    JobCancelledException,
    JobPausedException,
)


@pytest.fixture(autouse=True)
def setup_matrix_db(tmp_path):
    """Sets up an isolated SQLite database with migrations applied for each test."""
    db_file = str(tmp_path / "matrix_test.db")
    init_db(db_file)
    yield db_file


def seed_user(user_id: str = "u_matrix") -> str:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, 'hash', 'Matrix User', ?, ?)
            """,
            (user_id, f"{user_id}@test.com", now, now),
        )
    return user_id


def seed_job(
    job_id: str,
    user_id: str = "u_matrix",
    status: str = "pending",
    retry_count: int = 0,
    max_retries: int = 3,
    lease_version: int = 0,
    worker_id: Optional[str] = None,
    lease_expires_at: Optional[str] = None,
    checkpoint_data: Optional[str] = None,
) -> str:
    seed_user(user_id)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, checkpoint_data,
                worker_id, lease_expires_at, heartbeat_at, lease_version,
                retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, 'Bangalore', 12.97, 77.64, 1.5, '["cafe"]', 5, 0, 0, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, user_id, status, checkpoint_data, worker_id, lease_expires_at,
                lease_expires_at, lease_version, retry_count, max_retries, now, now
            ),
        )
    return job_id


# ==============================================================================
# PART A: RETRY SEMANTICS & LIMIT EXHAUSTION
# ==============================================================================

def test_part_a_1_initial_job_claim():
    """1. Initial job claim is attempt 1 with retry_count = 0."""
    seed_job("job_init", status="pending", retry_count=0, max_retries=3)
    worker = DurableJobWorker(worker_id="w_init")
    claimed = worker.claim_next_job()

    assert claimed is not None
    assert claimed["id"] == "job_init"
    assert claimed["status"] == "running"
    assert claimed["retry_count"] == 0
    assert claimed["lease_version"] == 1
    assert claimed["worker_id"] == "w_init"


def test_part_a_2_retry_limit_zero():
    """2. Retry limit zero (max_retries=0 permits initial attempt, 0 retries)."""
    seed_job("job_limit_0", status="pending", retry_count=0, max_retries=0)

    # Initial claim (attempt 1) succeeds
    w1 = DurableJobWorker(worker_id="w1")
    claimed_1 = w1.claim_next_job()
    assert claimed_1 is not None
    assert claimed_1["retry_count"] == 0

    # w1 crashes; lease expires
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_0'", (past,))

    # Recovery claim attempt by w2: must fail because max_retries=0 allows 0 retries
    w2 = DurableJobWorker(worker_id="w2")
    claimed_2 = w2.claim_next_job()
    assert claimed_2 is None

    # Verify job marked failed in DB
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = 'job_limit_0'").fetchone()
        assert row["status"] == "failed"
        assert "Exceeded maximum lease recovery retry attempts (0)" in (row["error_message"] or "")
        assert row["retry_count"] == 0


def test_part_a_3_retry_limit_one():
    """3. Retry limit one (max_retries=1 permits 1 initial attempt + 1 retry = 2 executions)."""
    seed_job("job_limit_1", status="pending", retry_count=0, max_retries=1)

    # Initial claim: Execution 1 (attempt 1)
    w1 = DurableJobWorker(worker_id="w1")
    c1 = w1.claim_next_job()
    assert c1 is not None
    assert c1["retry_count"] == 0

    # Crash 1: lease expires
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_1'", (past,))

    # Recovery claim 1: Execution 2 (retry 1 of 1) -> SUCCEEDS!
    w2 = DurableJobWorker(worker_id="w2")
    c2 = w2.claim_next_job()
    assert c2 is not None
    assert c2["retry_count"] == 1
    assert c2["status"] == "running"

    # Crash 2: lease expires again
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_1'", (past,))

    # Recovery claim 2: Execution 3 (retry 2 > max_retries 1) -> FAILS!
    w3 = DurableJobWorker(worker_id="w3")
    c3 = w3.claim_next_job()
    assert c3 is None

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = 'job_limit_1'").fetchone()
        assert row["status"] == "failed"
        assert "Exceeded maximum lease recovery retry attempts (1)" in (row["error_message"] or "")
        assert row["retry_count"] == 1


def test_part_a_4_retry_limit_two():
    """4. Retry limit two (max_retries=2 permits initial + 2 retries = 3 executions)."""
    seed_job("job_limit_2", status="pending", retry_count=0, max_retries=2)
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    # Attempt 1 (initial)
    w1 = DurableJobWorker(worker_id="w1")
    c1 = w1.claim_next_job()
    assert c1["retry_count"] == 0

    # Crash 1 -> Recovery 1 (retry 1)
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_2'", (past,))
    w2 = DurableJobWorker(worker_id="w2")
    c2 = w2.claim_next_job()
    assert c2 is not None
    assert c2["retry_count"] == 1

    # Crash 2 -> Recovery 2 (retry 2)
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_2'", (past,))
    w3 = DurableJobWorker(worker_id="w3")
    c3 = w3.claim_next_job()
    assert c3 is not None
    assert c3["retry_count"] == 2

    # Crash 3 -> Recovery 3 (retry 3 > 2) -> FAILS!
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_limit_2'", (past,))
    w4 = DurableJobWorker(worker_id="w4")
    c4 = w4.claim_next_job()
    assert c4 is None

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = 'job_limit_2'").fetchone()
        assert row["status"] == "failed"
        assert row["retry_count"] == 2


def test_part_a_5_healthy_lease_renewal_does_not_consume_retry():
    """5. Healthy lease renewal does not consume a retry."""
    seed_job("job_renew", status="pending", retry_count=0, max_retries=3)
    worker = DurableJobWorker(worker_id="w_renew")
    claimed = worker.claim_next_job()
    assert claimed["retry_count"] == 0

    # Renew lease 5 times
    for _ in range(5):
        renewed = worker.renew_lease("job_renew", claimed["lease_version"])
        assert renewed is True

    # Check retry_count remains 0
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT retry_count FROM jobs WHERE id = 'job_renew'").fetchone()
        assert row["retry_count"] == 0


def test_part_a_6_expired_lease_counted_once_per_recovery():
    """6. An expired lease is counted once per recovery."""
    seed_job("job_once", status="pending", retry_count=0, max_retries=3)
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    w1 = DurableJobWorker(worker_id="w1")
    w1.claim_next_job()

    # Expire lease
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_once'", (past,))

    # Recover
    w2 = DurableJobWorker(worker_id="w2")
    c2 = w2.claim_next_job()
    assert c2["retry_count"] == 1

    # Worker 2 does multiple operations: renews lease, appends logs
    w2.renew_lease("job_once", c2["lease_version"])
    w2._append_job_log("job_once", "Auditing step", "info", lease_version=c2["lease_version"])

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT retry_count FROM jobs WHERE id = 'job_once'").fetchone()
        assert row["retry_count"] == 1


def test_part_a_7_concurrent_workers_cannot_bypass_limit():
    """7. Concurrent workers cannot bypass the retry limit."""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    # Job at limit: retry_count=1, max_retries=1, expired
    seed_job("job_conc_limit", status="running", retry_count=1, max_retries=1,
             lease_version=2, worker_id="w_dead", lease_expires_at=past)

    results = []

    def try_claim(worker_idx: int):
        w = DurableJobWorker(worker_id=f"w_concurrent_{worker_idx}")
        res = w.claim_next_job()
        results.append((worker_idx, res))

    threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # None of the workers should have claimed the job as 'running'
    for widx, res in results:
        assert res is None, f"Worker {widx} erroneously claimed exhausted job!"

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = 'job_conc_limit'").fetchone()
        assert row["status"] == "failed"
        assert row["retry_count"] == 1


def test_part_a_8_stale_workers_cannot_alter_retry_metadata_or_status():
    """8. Stale workers cannot alter retry metadata or status."""
    seed_job("job_stale_guard", status="pending", retry_count=0, max_retries=3)
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    w1 = DurableJobWorker(worker_id="w_stale_orig")
    c1 = w1.claim_next_job()
    v1 = c1["lease_version"]

    # Lease expires, w2 reclaims (bumps lease_version to 2, retry_count to 1)
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'job_stale_guard'", (past,))

    w2 = DurableJobWorker(worker_id="w_recovering")
    c2 = w2.claim_next_job()
    assert c2["lease_version"] == 2
    assert c2["retry_count"] == 1

    # Stale w1 attempts to renew lease
    renew_res = w1.renew_lease("job_stale_guard", v1)
    assert renew_res is False

    # Stale w1 attempts to update status or retry_count via conditional SQL
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'completed', retry_count = 99 WHERE id = 'job_stale_guard' AND worker_id = 'w_stale_orig' AND lease_version = ?",
            (v1,)
        )
        assert cur.rowcount == 0

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = 'job_stale_guard'").fetchone()
        assert row["status"] == "running"
        assert row["worker_id"] == "w_recovering"
        assert row["lease_version"] == 2
        assert row["retry_count"] == 1


def test_part_a_9_completed_cancelled_failed_jobs_not_reclaimed():
    """9. Completed, cancelled, and failed jobs cannot be reclaimed even if lease expired."""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    seed_job("j_comp", status="completed", lease_expires_at=past)
    seed_job("j_canc", status="cancelled", lease_expires_at=past)
    seed_job("j_fail", status="failed", lease_expires_at=past)

    worker = DurableJobWorker(worker_id="w_reclaim_check")
    claimed = worker.claim_next_job()
    assert claimed is None


def test_part_a_10_malformed_and_edge_case_retry_settings():
    """10. Null, zero, negative, and malformed retry settings are handled safely."""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    # Sub-case A: Negative max_retries (-1) -> treated as 0 retries
    seed_job("j_neg", status="running", retry_count=0, max_retries=-1, lease_expires_at=past)
    w = DurableJobWorker(worker_id="w_edge")
    assert w.claim_next_job() is None
    with get_db() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id = 'j_neg'").fetchone()[0] == "failed"

    # Sub-case B: Malformed string max_retries ("invalid") -> CAST yields 0, treated as 0 retries
    seed_job("j_malformed", status="running", retry_count=0, max_retries="invalid", lease_expires_at=past)
    assert w.claim_next_job() is None
    with get_db() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id = 'j_malformed'").fetchone()[0] == "failed"

    # Sub-case C: Zero max_retries (0) -> initial allowed, recovery fails
    seed_job("j_zero", status="running", retry_count=0, max_retries=0, lease_expires_at=past)
    assert w.claim_next_job() is None
    with get_db() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id = 'j_zero'").fetchone()[0] == "failed"


# ==============================================================================
# PART B: SIX CRASH-RECOVERY BOUNDARIES
# ==============================================================================

class MockProvider(DiscoveryProvider):
    def __init__(self, businesses: List[DiscoveredBusiness]):
        self.businesses = businesses
        self.call_count = 0

    async def discover(self, *args, **kwargs) -> DiscoveryResult:
        self.call_count += 1
        return DiscoveryResult(
            businesses=self.businesses,
            coverage=DiscoveryCoverageReport(
                total_found=len(self.businesses),
                returned_count=len(self.businesses),
                source="mock",
                categories_searched=["cafe"],
                search_center={"lat": 12.97, "lon": 77.64},
                search_radius_km=1.5,
            )
        )


class MockAuditorRecorder:
    def __init__(self):
        self.audited_urls = []

    async def audit(self, url: str, business_name: str, category: str) -> WebsiteAuditResult:
        self.audited_urls.append(url)
        return WebsiteAuditResult(
            has_website_url=bool(url),
            url=url,
            is_reachable=True,
            ssl_valid=True,
            has_title=True,
            has_viewport_meta=True,
        )


class MockScorerDummy:
    def calculate_score(self, biz: DiscoveredBusiness, audit: WebsiteAuditResult) -> LeadScore:
        return LeadScore(total_score=85, factors=[], priority="High", explanation="OK")


class MockPitchGenDummy:
    async def generate(self, biz: DiscoveredBusiness, audit: WebsiteAuditResult, score: LeadScore) -> SalesPitches:
        return SalesPitches(
            business_summary=f"Summary for {biz.name}",
            verified_contacts={"phone": biz.phone},
            audit_findings_summary="Audit OK",
            recommended_features=[],
            suggested_project_scope="Standard",
            suggested_price_inr="₹15,000",
            email_subject="Pitch",
            email_draft="Draft",
            follow_up_draft="Followup",
            why_worth_contacting="High",
            generation_source="deterministic_template",
        )


def make_biz(idx: int) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        provider_id=f"biz_osm_{idx}",
        name=f"Cafe Number {idx}",
        category="cafe",
        lat=12.97 + (idx * 0.001),
        lon=77.64 + (idx * 0.001),
        phone=f"+91 98000 000{idx:02d}",
        website=f"https://cafe{idx}.com",
        source="osm",
        source_url=f"https://www.openstreetmap.org/node/{idx}",
        provenance={"name": FieldProvenance(source="osm", confidence=1.0)},
    )


def test_boundary_1_crash_after_claim_before_discovery():
    """Boundary 1: Crash after claim, before discovery starts."""
    seed_job("j_b1", status="pending", retry_count=0, max_retries=2)
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    # Step 1: Worker 1 claims job
    w1 = DurableJobWorker(worker_id="w1_crash")
    claimed_1 = w1.claim_next_job()
    assert claimed_1["lease_version"] == 1
    assert claimed_1["retry_count"] == 0

    # Crash Simulation: w1 dies before calling discover(). Lease expires.
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'j_b1'", (past,))

    # Record state before recovery
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j_pre = conn.execute("SELECT * FROM jobs WHERE id = 'j_b1'").fetchone()
        assert j_pre["status"] == "running"
        assert j_pre["checkpoint_data"] is None
        assert j_pre["lease_version"] == 1
        assert j_pre["retry_count"] == 0

    # Step 2: Worker 2 recovers
    candidates = [make_biz(1), make_biz(2)]
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()

    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    assert mock_disc.call_count == 1
    assert len(auditor.audited_urls) == 2

    # Verify state after recovery
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j_post = conn.execute("SELECT * FROM jobs WHERE id = 'j_b1'").fetchone()
        assert j_post["status"] == "completed"
        assert j_post["lease_version"] == 2
        assert j_post["retry_count"] == 1
        leads_count = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b1'").fetchone()[0]
        assert leads_count == 2


def test_boundary_2_crash_during_discovery_before_checkpoint():
    """Boundary 2: Crash during discovery before checkpoint persistence."""
    seed_job("j_b2", status="pending", retry_count=0, max_retries=2)
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    w1 = DurableJobWorker(worker_id="w1_crash")
    claimed_1 = w1.claim_next_job()
    assert claimed_1["lease_version"] == 1

    # Crash Simulation: Worker starts discovery but process terminates before update_checkpoint().
    # Lease expires.
    with get_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = 'j_b2'", (past,))

    # Worker 2 recovers
    candidates = [make_biz(10), make_biz(11)]
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()
    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    assert mock_disc.call_count == 1 # discovery runs for w2 since checkpoint was not saved
    assert len(auditor.audited_urls) == 2

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE id = 'j_b2'").fetchone()
        assert j["status"] == "completed"
        assert j["lease_version"] == 2
        assert j["retry_count"] == 1
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b2'").fetchone()[0]
        assert leads == 2


def test_boundary_3_crash_after_checkpoint_before_first_lead():
    """Boundary 3: Crash after checkpoint persistence, before first lead persisted."""
    candidates = [make_biz(20), make_biz(21)]
    checkpoint_json = json.dumps({"candidates": [b.model_dump() for b in candidates]})
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    # Seed job that has checkpoint saved by dead worker
    seed_job("j_b3", status="running", retry_count=0, max_retries=2, lease_version=1,
             worker_id="w1_dead", lease_expires_at=past, checkpoint_data=checkpoint_json)

    # Worker 2 recovers
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()
    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    # Invariant: Discovery MUST NOT repeat when checkpoint exists!
    assert mock_disc.call_count == 0
    assert len(auditor.audited_urls) == 2

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE id = 'j_b3'").fetchone()
        assert j["status"] == "completed"
        assert j["lease_version"] == 2
        assert j["retry_count"] == 1
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b3'").fetchone()[0]
        assert leads == 2


def test_boundary_4_crash_after_audit_before_lead_persistence():
    """Boundary 4: Crash after audit finishes, before lead persistence."""
    candidates = [make_biz(30), make_biz(31)]
    checkpoint_json = json.dumps({"candidates": [b.model_dump() for b in candidates]})
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    seed_job("j_b4", status="running", retry_count=0, max_retries=2, lease_version=1,
             worker_id="w1_dead", lease_expires_at=past, checkpoint_data=checkpoint_json)

    # Worker 2 recovers: Candidate 30 was not committed to DB before crash, so it is re-audited and saved
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()
    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    assert mock_disc.call_count == 0
    assert len(auditor.audited_urls) == 2

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE id = 'j_b4'").fetchone()
        assert j["status"] == "completed"
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b4'").fetchone()[0]
        assert leads == 2


def test_boundary_5_crash_after_lead_persistence_before_next_lead():
    """Boundary 5: Crash after lead persistence, before next lead."""
    candidates = [make_biz(40), make_biz(41)]
    checkpoint_json = json.dumps({"candidates": [b.model_dump() for b in candidates]})
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    seed_job("j_b5", status="running", retry_count=0, max_retries=2, lease_version=1,
             worker_id="w1_dead", lease_expires_at=past, checkpoint_data=checkpoint_json)

    # Pre-commit Lead 40 to SQLite (as if w1 persisted lead 40 and then died)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO leads (id, user_id, job_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
            VALUES ('lead_40', 'u_matrix', 'j_b5', 'biz_osm_40', 'Cafe Number 40', 'cafe', 12.97, 77.64, 'http://test', '{}', '{}', ?, ?)
            """,
            (now, now)
        )
        conn.execute("UPDATE jobs SET processed_count = 1 WHERE id = 'j_b5'")

    # Worker 2 recovers
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()
    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    assert mock_disc.call_count == 0
    # INVARIANT: Lead 40 was already in leads table -> MUST NOT BE RE-AUDITED!
    assert auditor.audited_urls == ["https://cafe41.com"]
    assert len(auditor.audited_urls) == 1

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE id = 'j_b5'").fetchone()
        assert j["status"] == "completed"
        assert j["lease_version"] == 2
        assert j["retry_count"] == 1
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b5'").fetchone()[0]
        assert leads == 2
        # Zero duplicate provider IDs
        p_ids = [r[0] for r in conn.execute("SELECT provider_id FROM leads WHERE job_id = 'j_b5'").fetchall()]
        assert len(p_ids) == len(set(p_ids)) == 2


def test_boundary_6_crash_during_final_completion():
    """Boundary 6: Crash during final completion (all leads persisted, status not yet completed)."""
    candidates = [make_biz(50), make_biz(51)]
    checkpoint_json = json.dumps({"candidates": [b.model_dump() for b in candidates]})
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    seed_job("j_b6", status="running", retry_count=0, max_retries=2, lease_version=1,
             worker_id="w1_dead", lease_expires_at=past, checkpoint_data=checkpoint_json)

    # Pre-commit both leads
    with get_db() as conn:
        for idx in [50, 51]:
            conn.execute(
                """
                INSERT INTO leads (id, user_id, job_id, provider_id, name, category, lat, lon, source_url, provenance, raw_data, created_at, updated_at)
                VALUES (?, 'u_matrix', 'j_b6', ?, ?, 'cafe', 12.97, 77.64, 'http://test', '{}', '{}', ?, ?)
                """,
                (f"lead_{idx}", f"biz_osm_{idx}", f"Cafe Number {idx}", now, now)
            )
        conn.execute("UPDATE jobs SET processed_count = 2 WHERE id = 'j_b6'")

    # Worker 2 recovers
    mock_disc = MockProvider(candidates)
    auditor = MockAuditorRecorder()
    w2 = DurableJobWorker(
        worker_id="w2_recovering",
        discovery_provider=mock_disc,
        auditor=auditor,
        scorer=MockScorerDummy(),
        pitch_gen=MockPitchGenDummy(),
    )
    res = w2.run_once()
    assert res is True
    # INVARIANT: Both leads exist -> Zero discovery calls, Zero audits!
    assert mock_disc.call_count == 0
    assert len(auditor.audited_urls) == 0

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE id = 'j_b6'").fetchone()
        assert j["status"] == "completed"
        assert j["lease_version"] == 2
        assert j["retry_count"] == 1
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE job_id = 'j_b6'").fetchone()[0]
        assert leads == 2
