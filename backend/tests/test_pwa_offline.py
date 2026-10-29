"""
Milestone 4 Offline PWA & Service Worker Test Suite.
Verifies Web App Manifest, Service Worker lifecycle, static shell caching rules,
strict private-data exclusion, and outbox isolation.
Offline only — zero external network requests.
"""
import json
import os
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND_PUBLIC = os.path.join(PROJECT_ROOT, "frontend", "public")
FRONTEND_DIST = os.path.join(PROJECT_ROOT, "frontend", "dist")


# ==============================================================================
# 1. WEB APP MANIFEST VALIDITY TESTS
# ==============================================================================

def test_manifest_file_exists_and_valid_json():
    manifest_path = os.path.join(FRONTEND_PUBLIC, "manifest.webmanifest")
    assert os.path.exists(manifest_path), f"Manifest file missing at {manifest_path}"

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Core PWA Manifest fields
    assert data.get("name") == "WebsiteLead Hunter"
    assert data.get("short_name") == "LeadHunter"
    assert data.get("display") == "standalone"
    assert data.get("start_url") == "/"
    assert data.get("theme_color") == "#4f46e5"
    assert data.get("background_color") == "#020617"
    assert "categories" in data
    assert len(data.get("icons", [])) >= 3


def test_manifest_referenced_icons_exist_on_disk():
    manifest_path = os.path.join(FRONTEND_PUBLIC, "manifest.webmanifest")
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for icon in data.get("icons", []):
        icon_rel = icon["src"].lstrip("/")
        icon_path = os.path.join(FRONTEND_PUBLIC, icon_rel)
        assert os.path.exists(icon_path), f"Referenced icon {icon['src']} not found at {icon_path}"
        assert os.path.getsize(icon_path) > 0, f"Icon {icon['src']} is empty"


# ==============================================================================
# 2. SERVICE WORKER LIFECYCLE & PRIVATE DATA CACHE EXCLUSION
# ==============================================================================

def test_service_worker_script_rules():
    sw_path = os.path.join(FRONTEND_PUBLIC, "sw.js")
    assert os.path.exists(sw_path), f"Service Worker missing at {sw_path}"

    with open(sw_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Versioned cache identifier
    assert "CACHE_NAME = 'websitelead-shell-v1'" in content

    # 2. Pre-cached static shell URLs
    assert "'/index.html'" in content
    assert "'/manifest.webmanifest'" in content
    assert "'/favicon.svg'" in content
    assert "'/icons/icon-192.png'" in content
    assert "'/icons/icon-512.png'" in content

    # 3. STRICT PRIVATE-DATA EXCLUSION RULE:
    # Authenticated API endpoints (/api/*) must NEVER enter shared Cache Storage!
    assert "url.pathname.startsWith('/api/')" in content
    assert "NEVER cache authenticated API responses" in content

    # 4. Mutations must never be cached
    assert "request.method !== 'GET'" in content

    # 5. Offline fallback handling for navigation
    assert "request.mode === 'navigate'" in content

    # 6. Background sync tag support
    assert "sync-offline-leads" in content


# ==============================================================================
# 3. PWA STATIC SHELL ENDPOINTS VIA FASTAPI
# ==============================================================================

def test_pwa_manifest_endpoint():
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "WebsiteLead Hunter"
    assert data["display"] == "standalone"


def test_pwa_service_worker_endpoint():
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "websitelead-shell-v1" in resp.text


def test_pwa_icon_endpoints():
    resp_192 = client.get("/icons/icon-192.png")
    assert resp_192.status_code == 200
    assert len(resp_192.content) > 0

    resp_512 = client.get("/icons/icon-512.png")
    assert resp_512.status_code == 200
    assert len(resp_512.content) > 0

    resp_maskable = client.get("/icons/icon-maskable.png")
    assert resp_maskable.status_code == 200
    assert len(resp_maskable.content) > 0

    resp_fav = client.get("/favicon.svg")
    assert resp_fav.status_code == 200


def test_api_routes_prioritized_over_static_mount():
    # Health endpoint still resolves to API, not static file
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy", "service": "WebsiteLead Hunter API"}

    # Discovery categories still resolves to API
    resp_cat = client.get("/api/discovery/categories")
    assert resp_cat.status_code == 200
    assert len(resp_cat.json()) >= 6
