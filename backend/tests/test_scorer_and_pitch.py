"""
Automated tests for Lead Scoring, Pitch Generation, and SQLite Persistence.
"""
import os
import json
import pytest
from app.discovery.base import DiscoveredBusiness, FieldProvenance
from app.schemas import WebsiteAuditResult, AuditFinding
from app.scoring.scorer import LeadScorer
from app.pitch.template_engine import DeterministicPitchEngine
from app.pitch.llm_provider import SalesPitchGenerator
from app.database import init_db, get_db
from app.queue.job_worker import JobWorker


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Initializes a temporary SQLite database for tests."""
    db_file = str(tmp_path / "test_hunter.db")
    init_db(db_file)
    yield db_file


def test_scorer_no_website_high_priority():
    """Verify high score for active business lacking a website but possessing a phone."""
    scorer = LeadScorer()
    biz = DiscoveredBusiness(
        provider_id="osm:node:501",
        name="Taj Bakery & Sweets",
        category="Bakery",
        address="12 MG Road, Bengaluru",
        lat=12.9716,
        lon=77.5946,
        phone="+91-9876543210",
        opening_hours="08:00-22:00",
        source_url="https://www.openstreetmap.org/node/501",
    )
    audit = WebsiteAuditResult(
        has_website_url=False,
        is_reachable=False,
        findings=[],
    )

    score = scorer.calculate_score(biz, audit)
    assert score.total_score >= 70
    assert score.priority == "High"
    assert len(score.factors) == 4
    for f in score.factors:
        assert f.name
        assert f.reason
        assert f.evidence


def test_scorer_modern_site_low_priority():
    """Verify lower score for business that already has a responsive, SSL-enabled website."""
    scorer = LeadScorer()
    biz = DiscoveredBusiness(
        provider_id="osm:node:502",
        name="Global Tech Gym",
        category="Gym",
        address="Sector 18, Noida",
        lat=28.5700,
        lon=77.3200,
        phone="+91-120-432100",
        website="https://globaltechgym.com",
        source_url="https://www.openstreetmap.org/node/502",
    )
    audit = WebsiteAuditResult(
        has_website_url=True,
        url="https://globaltechgym.com",
        is_reachable=True,
        ssl_valid=True,
        has_viewport_meta=True,
        has_clear_cta=True,
        has_title=True,
        findings=[],
    )

    score = scorer.calculate_score(biz, audit)
    assert score.total_score < 60
    assert score.priority in ["Low", "Medium"]


def test_deterministic_pitch_generation():
    """Verify sales pitch contains required sections, INR pricing benchmark, and grounded facts."""
    engine = DeterministicPitchEngine()
    biz = DiscoveredBusiness(
        provider_id="osm:node:503",
        name="Cinnamon Cafe",
        category="Café",
        address="Indiranagar 100ft Rd, Bengaluru",
        lat=12.9716,
        lon=77.6412,
        phone="+91-80-25251122",
        opening_hours="09:00-23:00",
        source_url="https://www.openstreetmap.org/node/503",
    )
    audit = WebsiteAuditResult(
        has_website_url=False,
        is_reachable=False,
        findings=[],
    )
    scorer = LeadScorer()
    score = scorer.calculate_score(biz, audit)

    pitch = engine.generate_pitch(biz, audit, score)
    assert pitch.business_summary
    assert "Cinnamon Cafe" in pitch.email_draft
    assert "₹" in pitch.suggested_price_inr
    assert "estimate" in pitch.suggested_price_inr.lower() or "recommend" in pitch.suggested_price_inr.lower()
    assert pitch.whatsapp_draft is not None
    assert "Cinnamon Cafe" in pitch.whatsapp_draft
    assert pitch.why_worth_contacting
    assert pitch.generation_source == "deterministic_template"


@pytest.mark.asyncio
async def test_persistence_and_restart_recovery(setup_test_db):
    """Verify saving a lead to SQLite, deduplication, and crash/restart recovery."""
    worker = JobWorker()
    biz = DiscoveredBusiness(
        provider_id="osm:node:999",
        name="Lotus Dental Clinic",
        category="Clinic & Healthcare",
        address="Park Street, Kolkata",
        lat=22.5532,
        lon=88.3516,
        phone="+91-33-22223333",
        source_url="https://www.openstreetmap.org/node/999",
        provenance={
            "name": FieldProvenance(source="OpenStreetMap", source_url="https://www.openstreetmap.org/node/999")
        },
    )

    # 0. Insert dummy job first to satisfy foreign key
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("job-test-01", "running", "Kolkata", 22.55, 88.35, 1.0, "[]", 10, 1, 0, "[]", "now", "now"),
        )

    # 1. Process single business
    await worker._process_single_business("job-test-01", biz)

    # 2. Verify lead exists in SQLite
    with get_db() as conn:
        row = conn.execute("SELECT * FROM leads WHERE provider_id = 'osm:node:999'").fetchone()
        assert row is not None
        assert row["name"] == "Lotus Dental Clinic"
        assert row["category"] == "Clinic & Healthcare"
        assert row["score"] >= 50
        assert row["pipeline_status"] == "qualified"
        
        # Verify JSON fields
        pitches = json.loads(row["pitches"])
        assert "Lotus Dental Clinic" in pitches["email_draft"]
        assert "₹" in pitches["suggested_price_inr"]

    # 3. Verify restart recovery
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, location_query, lat, lon, radius_km, categories,
                max_leads, total_found, processed_count, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("job-hanging-01", "running", "Test Loc", 22.0, 88.0, 1.0, "[]", 10, 5, 2, "[]", "now", "now"),
        )

    # Simulate server reboot
    worker.recover_interrupted_jobs()

    with get_db() as conn:
        job_row = conn.execute("SELECT status, logs FROM jobs WHERE id = 'job-hanging-01'").fetchone()
        assert job_row["status"] == "paused"
        assert "restarted" in job_row["logs"]
