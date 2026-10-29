"""
Automated Integration Tests for WebsiteLead Hunter REST API.
"""
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db, get_db
from app.auth.security import hash_password

client = TestClient(app)


@pytest.fixture(autouse=True)
def init_api_db(tmp_path):
    db_file = str(tmp_path / "test_api_hunter.db")
    init_db(db_file)

    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    test_user_id = "test-api-user-id"
    session_id = "test-api-session-id"
    csrf_token = "test-api-csrf-token"

    # Seed test user, session, and sample leads
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, 'tester@example.com', ?, 'API Tester', ?, ?)
            """,
            (test_user_id, hash_password("TestPass123!"), now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, 'TestClient', '127.0.0.1', ?, ?)
            """,
            (session_id, test_user_id, csrf_token, expires_at, now),
        )
        conn.execute(
            """
            INSERT INTO leads (
                id, user_id, provider_id, name, category, address, lat, lon, phone, website,
                source_url, provenance, raw_data, score, pipeline_status, created_at, updated_at
            ) VALUES (
                'lead-01', ?, 'osm:node:11', 'Spice Route Bistro', 'Restaurant / Food',
                'Connaught Place, New Delhi', 28.6315, 77.2167, '+91-11-23344556',
                '', 'https://osm.org/node:11', '{}', '{}', 85, 'qualified', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z'
            ), (
                'lead-02', ?, 'osm:node:12', 'Elite Dental Care', 'Clinic & Healthcare',
                'South Extension, New Delhi', 28.5715, 77.2267, '+91-11-44556677',
                'https://elitedental.com', 'https://osm.org/node:12', '{}', '{}', 45, 'new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z'
            )
            """,
            (test_user_id, test_user_id),
        )

    client.cookies.set("session_id", session_id)
    client.headers["X-CSRF-Token"] = csrf_token
    yield
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_get_categories():
    response = client.get("/api/discovery/categories")
    assert response.status_code == 200
    categories = response.json()
    assert len(categories) >= 6
    assert any(c["id"] == "cafes" for c in categories)


def test_list_leads_and_filtering():
    # 1. List all
    response = client.get("/api/leads")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # 2. Filter by search
    resp_search = client.get("/api/leads?search=Spice")
    assert resp_search.status_code == 200
    assert resp_search.json()["total"] == 1
    assert resp_search.json()["items"][0]["name"] == "Spice Route Bistro"

    # 3. Filter by min_score
    resp_score = client.get("/api/leads?min_score=80")
    assert resp_score.status_code == 200
    assert resp_score.json()["total"] == 1
    assert resp_score.json()["items"][0]["score"] == 85

    # 4. Filter by missing_website
    resp_noweb = client.get("/api/leads?missing_website=true")
    assert resp_noweb.status_code == 200
    assert resp_noweb.json()["total"] == 1
    assert resp_noweb.json()["items"][0]["id"] == "lead-01"


def test_update_lead_pipeline():
    response = client.patch(
        "/api/leads/lead-01/pipeline",
        json={"pipeline_status": "contacted", "notes": "Left WhatsApp pitch message with owner"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["pipeline_status"] == "contacted"
    assert data["notes"] == "Left WhatsApp pitch message with owner"
    assert data["contacted_at"] is not None


def test_export_csv():
    response = client.get("/api/export/csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    content = response.text
    assert "Business Name,Category" in content
    assert "Spice Route Bistro" in content
    assert "Elite Dental Care" in content


def test_delete_lead():
    # Delete lead-02
    response = client.delete("/api/leads/lead-02")
    assert response.status_code == 200
    assert response.json()["lead_id"] == "lead-02"

    # Confirm remaining
    resp_list = client.get("/api/leads")
    assert resp_list.json()["total"] == 1
    assert resp_list.json()["items"][0]["id"] == "lead-01"
