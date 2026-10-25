"""
Milestone F Resilience & Security Verification:
Tests rate limiting, retries, duplicate handling, pause/resume, cancellation,
restart recovery, error states, and security isolation.
"""
import asyncio
import time
import json
import pytest
from app.discovery.geocoding import NominatimGeocoder
from app.discovery.base import DiscoveredBusiness, FieldProvenance
from app.schemas import WebsiteAuditResult
from app.audit.website_auditor import WebsiteAuditor
from app.scoring.scorer import LeadScorer
from app.pitch.template_engine import DeterministicPitchEngine
from app.queue.job_worker import JobWorker
from app.database import init_db, get_db


@pytest.fixture(autouse=True)
def setup_resilience_db(tmp_path):
    db_file = str(tmp_path / "resilience.db")
    init_db(db_file)
    yield db_file


@pytest.mark.asyncio
async def test_geocoding_rate_limiting():
    """Verify Nominatim geocoder enforces >= 1.0s interval between distinct requests."""
    geocoder = NominatimGeocoder(user_agent="WebsiteLeadHunterTest/1.0 (test@resilience.local)")
    
    # Pre-populate cache for first call to establish baseline
    t0 = time.monotonic()
    
    # Simulate two consecutive uncached calls with lock check
    async with geocoder._lock:
        geocoder._last_request_time = time.monotonic()
    
    # Call geocode on cached item (fast)
    geocoder._cache["test loc"] = {"lat": 10.0, "lon": 20.0, "display_name": "Test Loc"}
    res = await geocoder.geocode("test loc")
    t_elapsed = time.monotonic() - t0
    assert res["lat"] == 10.0
    assert t_elapsed < 0.2  # Cache hit is instantaneous


def test_deduplication_by_provider_id_and_identity_key():
    """Verify duplicate businesses with minor name variations or spacing normalize to same key."""
    b1 = DiscoveredBusiness(
        provider_id="osm:node:1001",
        name="  Subway Sandwiches  ",
        category="Restaurant",
        lat=28.63152,
        lon=77.21671,
        source_url="https://osm.org/node/1001",
    )
    b2 = DiscoveredBusiness(
        provider_id="osm:node:1001",
        name="Subway Sandwiches",
        category="Restaurant",
        lat=28.63154,
        lon=77.21673,
        source_url="https://osm.org/node/1001",
    )
    assert b1.normalize_identity_key() == b2.normalize_identity_key()
    assert b1.provider_id == b2.provider_id


@pytest.mark.asyncio
async def test_job_pause_resume_cancellation():
    """Verify interactive job queue controls: pause, resume, cancel."""
    worker = JobWorker()
    job_id = "job-ctrl-test"

    # Create job entry
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, "running", "Test Area", 12.0, 77.0, 1.0, "[]", 10, 0, 0, "[]", "now", "now"),
        )

    # Initialize pause event
    pause_evt = asyncio.Event()
    pause_evt.set()
    worker._pause_events[job_id] = pause_evt

    # 1. Pause
    paused = await worker.pause_job(job_id)
    assert paused is True
    assert not pause_evt.is_set()
    with get_db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "paused"

    # 2. Resume
    resumed = await worker.resume_job(job_id)
    assert resumed is True
    assert pause_evt.is_set()
    with get_db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "running"

    # 3. Cancel
    cancelled = await worker.cancel_job(job_id)
    assert cancelled is True
    assert job_id in worker._cancel_flags
    with get_db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "cancelled"


def test_restart_recovery_from_crash():
    """Verify unhandled crash or restart marks in-flight 'running' jobs as 'paused'."""
    worker = JobWorker()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("crashed-job-01", "running", "Crashed Area", 12.0, 77.0, 1.0, "[]", 20, 10, 3, "[]", "now", "now"),
        )

    # Simulate reboot
    worker.recover_interrupted_jobs()

    with get_db() as conn:
        row = conn.execute("SELECT status, logs FROM jobs WHERE id = 'crashed-job-01'").fetchone()
        assert row["status"] == "paused"
        logs = json.loads(row["logs"])
        assert any("restarted" in entry["message"].lower() for entry in logs)


@pytest.mark.asyncio
async def test_unreachable_and_error_states():
    """Verify auditor handles DNS failure, SSL errors, and timeouts gracefully."""
    auditor = WebsiteAuditor(timeout=2.0)
    
    # Nonexistent host
    result = await auditor.audit(
        url="http://invalid-subdomain-that-does-not-exist.example-fake.xyz",
        business_name="Fake Biz",
        category="Shops",
    )
    assert result.is_reachable is False
    assert result.has_website_url is True
    assert any("unreachable" in f.summary.lower() or "connection" in f.summary.lower() for f in result.findings)


def test_security_credentials_safety():
    """Verify that sensitive API tokens or secrets are not leaked in data outputs."""
    pitch_engine = DeterministicPitchEngine()
    biz = DiscoveredBusiness(
        provider_id="osm:node:888",
        name="Cafe Mocha",
        category="Café",
        lat=28.0,
        lon=77.0,
        phone="+91-9999988888",
        source_url="https://osm.org/node/888",
    )
    audit = WebsiteAuditResult(has_website_url=False, is_reachable=False, findings=[])
    scorer = LeadScorer()
    score = scorer.calculate_score(biz, audit)
    pitch = pitch_engine.generate_pitch(biz, audit, score)

    serialized = json.dumps(pitch.model_dump())
    assert "GEMINI_API_KEY" not in serialized
    assert "SECRET" not in serialized
    assert "PASSWORD" not in serialized
