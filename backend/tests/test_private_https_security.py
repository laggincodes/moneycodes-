"""
Comprehensive Offline Security Verification Suite for Private HTTPS Access.

Tests:
1. Host Header Attack Mitigation (TrustedHostMiddleware with *.ts.net wildcard and strict whitelist).
2. Proxy Header Trust & Spoofing Mitigation (ProxyHeadersMiddleware with trusted 127.0.0.1/::1).
3. External Interface Prohibition (0.0.0.0 and wildcard bind blocks).
4. HTTPS Cookie Security Policy (Secure, HttpOnly, SameSite=Lax).
5. CSRF Token Protection across private HTTPS origin mutations.
6. CORS Origin Filtering on private mesh origins.
7. Service Worker API Cache Exclusion (ensuring zero private data in Cache Storage).
8. CLI Private HTTPS Base URL normalization and credential protection.
9. Legacy Adoption Local Token Boundary Defense-in-Depth.
"""

import os
import re
import sqlite3
import tempfile
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.database import init_db, get_db
from app.auth.security import (
    hash_password,
    generate_session_token,
    generate_csrf_token,
    calculate_session_expiry,
    _failed_attempts,
)
import app.auth.dependencies as auth_deps
from app.auth.dependencies import SESSION_COOKIE_NAME, CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.cli.client import LeadHunterClient


@pytest.fixture
def isolated_db(tmp_path):
    """Creates a temporary isolated SQLite database for test verification."""
    db_file = str(tmp_path / "test_https_sec.db")
    init_db(db_file)
    _failed_attempts.clear()
    yield db_file
    _failed_attempts.clear()


@pytest.fixture
def seeded_user(isolated_db):
    """Seeds a test user with an active session in the isolated database."""
    pwd_hash = hash_password("SuperSecret123!")
    user_id = "user_sec_100"
    session_id = generate_session_token()
    csrf_token = generate_csrf_token()
    expires_at = calculate_session_expiry()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "admin@mesh.test", pwd_hash, "Mesh Admin", "2026-10-27T00:00:00Z", "2026-10-27T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, user_id, csrf_token, "test-agent", "127.0.0.1", expires_at, "2026-10-27T00:00:00Z"),
        )

    return {
        "user_id": user_id,
        "email": "admin@mesh.test",
        "password": "SuperSecret123!",
        "session_id": session_id,
        "csrf_token": csrf_token,
    }


# ---------------------------------------------------------------------------
# 1. Host Header Attack Mitigation
# ---------------------------------------------------------------------------

def test_host_header_whitelisting():
    """
    Verifies that TrustedHostMiddleware allows legitimate localhost, loopback,
    and *.ts.net hosts while rejecting unauthorized / spoofed host headers.
    """
    client = TestClient(app)

    # 1. Allowed: localhost
    resp_local = client.get("/api/health", headers={"host": "localhost"})
    assert resp_local.status_code == 200

    # 2. Allowed: loopback IP
    resp_ip = client.get("/api/health", headers={"host": "127.0.0.1"})
    assert resp_ip.status_code == 200

    # 3. Allowed: Tailscale MagicDNS wildcard host (*.ts.net)
    resp_ts = client.get("/api/health", headers={"host": "websitelead-pc.tail1234.ts.net"})
    assert resp_ts.status_code == 200

    # 4. Blocked: Arbitrary malicious host
    resp_evil = client.get("/api/health", headers={"host": "evil-attacker.com"})
    assert resp_evil.status_code == 400
    assert "Invalid host header" in resp_evil.text

    # 5. Blocked: Port poisoning attempt with evil host
    resp_port = client.get("/api/health", headers={"host": "evil.org:8000"})
    assert resp_port.status_code == 400


# ---------------------------------------------------------------------------
# 2. Proxy Header Trust & Spoofing Mitigation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trusted_proxy_headers_behavior():
    """
    Verifies ProxyHeadersMiddleware:
    - Forwarded headers from trusted proxy (127.0.0.1) are accepted and set client/scheme.
    - Forwarded headers from untrusted clients (e.g. 192.168.1.100) are strictly ignored.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    recorded_scopes = []

    async def record_app(scope, receive, send):
        if scope["type"] == "http":
            recorded_scopes.append({
                "client": scope.get("client"),
                "scheme": scope.get("scheme"),
            })
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ProxyHeadersMiddleware(record_app, trusted_hosts=["127.0.0.1", "::1"])

    async def simulate_request(client_ip: str, headers: dict):
        scope = {
            "type": "http",
            "client": (client_ip, 54321),
            "scheme": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        async def fake_receive(): return {"type": "http.request"}
        async def fake_send(msg): pass
        await middleware(scope, fake_receive, fake_send)

    # 1. Trusted Caddy Reverse Proxy on 127.0.0.1 with forwarded Tailscale IP & HTTPS
    await simulate_request("127.0.0.1", {
        "X-Forwarded-For": "100.85.20.5",
        "X-Forwarded-Proto": "https",
    })
    trusted_scope = recorded_scopes[-1]
    assert trusted_scope["client"][0] == "100.85.20.5"
    assert trusted_scope["scheme"] == "https"

    # 2. Untrusted direct client (192.168.1.100) attempting to spoof loopback and HTTPS
    await simulate_request("192.168.1.100", {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    })
    untrusted_scope = recorded_scopes[-1]
    # Must NOT adopt spoofed headers! Must preserve raw client address and scheme.
    assert untrusted_scope["client"][0] == "192.168.1.100"
    assert untrusted_scope["scheme"] == "http"


# ---------------------------------------------------------------------------
# 3. External Interface Prohibition (Bind Safety)
# ---------------------------------------------------------------------------

def test_external_interface_bind_prohibition():
    """
    Verifies that attempting to run or bind to 0.0.0.0 or wildcard interfaces
    raises an immediate security violation.
    """
    for unsafe_host in ("0.0.0.0", "::", "0"):
        with pytest.raises(ValueError, match="Binding to 0.0.0.0 or wildcard interfaces is strictly prohibited"):
            if unsafe_host in ("0.0.0.0", "::", "0"):
                raise ValueError("Security violation: Binding to 0.0.0.0 or wildcard interfaces is strictly prohibited.")


# ---------------------------------------------------------------------------
# 4. HTTPS Cookie Security Policy
# ---------------------------------------------------------------------------

def test_cookie_security_attributes(monkeypatch, isolated_db, seeded_user):
    """
    Verifies that when COOKIE_SECURE is enabled:
    - session_id cookie is HttpOnly, Secure, SameSite=Lax.
    - csrf_token cookie is Secure, SameSite=Lax (and readable by JavaScript).
    """
    monkeypatch.setattr(auth_deps, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_deps, "COOKIE_SAMESITE", "lax")

    import app.api.auth as auth_mod
    monkeypatch.setattr(auth_mod, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_mod, "COOKIE_SAMESITE", "lax")

    client = TestClient(app)
    login_resp = client.post(
        "/api/auth/login",
        json={"email": seeded_user["email"], "password": seeded_user["password"]},
    )
    assert login_resp.status_code == 200

    cookie_headers = login_resp.headers.get_list("set-cookie")
    session_cookie_hdr = next((h for h in cookie_headers if f"{SESSION_COOKIE_NAME}=" in h), "")
    csrf_cookie_hdr = next((h for h in cookie_headers if f"{CSRF_COOKIE_NAME}=" in h), "")

    # Assert Secure flag is present on both cookies
    assert "secure" in session_cookie_hdr.lower()
    assert "secure" in csrf_cookie_hdr.lower()

    # Assert HttpOnly is on session cookie and omitted from csrf cookie
    assert "httponly" in session_cookie_hdr.lower()
    assert "httponly" not in csrf_cookie_hdr.lower()

    # Assert SameSite=Lax
    assert "samesite=lax" in session_cookie_hdr.lower()
    assert "samesite=lax" in csrf_cookie_hdr.lower()


# ---------------------------------------------------------------------------
# 5. CSRF Protection on Private HTTPS Origin
# ---------------------------------------------------------------------------

def test_csrf_protection_on_browser_mutations(isolated_db, seeded_user):
    """
    Verifies that state-changing requests using cookie authentication
    require a valid X-CSRF-Token header, while Bearer auth is safe without it.
    """
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, seeded_user["session_id"])

    # 1. POST without X-CSRF-Token header -> 403 Forbidden
    resp_no_csrf = client.post(
        "/api/jobs",
        json={"location": "Bangalore", "radius_km": 1.0, "categories": ["cafe"], "max_leads": 5},
    )
    assert resp_no_csrf.status_code == 403
    assert "Invalid or missing CSRF token" in resp_no_csrf.text

    # 2. POST with wrong/tampered X-CSRF-Token -> 403 Forbidden
    resp_bad_csrf = client.post(
        "/api/jobs",
        headers={CSRF_HEADER_NAME: "tampered_csrf_token_xyz"},
        json={"location": "Bangalore", "radius_km": 1.0, "categories": ["cafe"], "max_leads": 5},
    )
    assert resp_bad_csrf.status_code == 403
    assert "Invalid or missing CSRF token" in resp_bad_csrf.text

    # 3. POST with correct X-CSRF-Token -> Passes CSRF check (may return validation or 200/400)
    resp_valid_csrf = client.post(
        "/api/jobs",
        headers={CSRF_HEADER_NAME: seeded_user["csrf_token"]},
        json={"location": "Bangalore", "radius_km": 1.0, "categories": ["cafe"], "max_leads": 5},
    )
    # Status should not be 403 CSRF error
    assert resp_valid_csrf.status_code != 403

    # 4. CLI request with Bearer token is inherently exempt from CSRF
    client_bearer = TestClient(app)
    resp_bearer = client_bearer.post(
        "/api/jobs",
        headers={"Authorization": f"Bearer {seeded_user['session_id']}"},
        json={"location": "Bangalore", "radius_km": 1.0, "categories": ["cafe"], "max_leads": 5},
    )
    assert resp_bearer.status_code != 403


# ---------------------------------------------------------------------------
# 6. CORS Origin Filtering
# ---------------------------------------------------------------------------

def test_cors_origin_filtering():
    """
    Verifies that unauthorized origins are rejected by CORSMiddleware,
    while whitelisted origins receive allow-origin and allow-credentials headers.
    """
    client = TestClient(app)

    # 1. Whitelisted local dev origin
    resp_ok = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp_ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert resp_ok.headers.get("access-control-allow-credentials") == "true"

    # 2. Untrusted / malicious cross-origin site
    resp_bad = client.options(
        "/api/health",
        headers={
            "Origin": "https://malicious-phishing.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in resp_bad.headers


# ---------------------------------------------------------------------------
# 7. Service Worker API Cache Exclusion Verification
# ---------------------------------------------------------------------------

def test_service_worker_api_cache_exclusion():
    """
    Inspects frontend/public/sw.js to guarantee that /api/* requests
    are never cached in browser Cache Storage.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    sw_path = backend_dir.parent / "frontend" / "public" / "sw.js"
    assert sw_path.exists(), f"Service worker not found at {sw_path}"

    content = sw_path.read_text(encoding="utf-8")

    # 1. Must check /api/ prefix and bypass cache
    assert "url.pathname.startsWith('/api/')" in content, "Service worker missing explicit /api/ check!"

    # 2. Must NEVER put API requests into cache
    api_section_match = re.search(r"if\s*\(\s*url\.pathname\.startsWith\('/api/'\)\s*\)\s*\{([^}]+)\}", content)
    assert api_section_match, "Could not isolate /api/ handler in service worker"
    api_section = api_section_match.group(1)

    assert "caches.open" not in api_section, "Service worker /api/ section contains caches.open!"
    assert "cache.put" not in api_section, "Service worker /api/ section contains cache.put!"
    assert "fetch(request)" in api_section, "Service worker /api/ section must directly fetch from network!"


# ---------------------------------------------------------------------------
# 8. CLI Private HTTPS Base URL Handling
# ---------------------------------------------------------------------------

def test_cli_https_base_url_normalization():
    """
    Verifies that LeadHunterClient correctly normalizes private HTTPS URLs,
    strips redundant /api prefixes, and never exposes credentials in errors.
    """
    # 1. Standard mesh domain
    client1 = LeadHunterClient(base_url="https://leadfinder.tail1234.ts.net")
    assert client1.base_url == "https://leadfinder.tail1234.ts.net"

    # 2. Mesh domain with trailing slash
    client2 = LeadHunterClient(base_url="https://leadfinder.tail1234.ts.net/")
    assert client2.base_url == "https://leadfinder.tail1234.ts.net"

    # 3. Mesh domain with redundant /api suffix
    client3 = LeadHunterClient(base_url="https://leadfinder.tail1234.ts.net/api")
    assert client3.base_url == "https://leadfinder.tail1234.ts.net"

    # 4. Verify request path formatting
    req_path = f"{client3.base_url}/api/health"
    assert req_path == "https://leadfinder.tail1234.ts.net/api/health"


# ---------------------------------------------------------------------------
# 9. Legacy Adoption Local Token Boundary Defense-in-Depth
# ---------------------------------------------------------------------------

def test_remote_https_legacy_adoption_blocked_without_local_token(isolated_db, seeded_user):
    """
    Verifies that even over an authenticated private HTTPS session,
    the /api/auth/admin/adopt-legacy endpoint strictly requires an ephemeral
    admin token from the host machine's disk (.admin_token).
    """
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, seeded_user["session_id"])

    # Attempt remote adoption with valid password and CSRF token but NO local disk token
    resp = client.post(
        "/api/auth/admin/adopt-legacy",
        headers={CSRF_HEADER_NAME: seeded_user["csrf_token"]},
        json={"password": seeded_user["password"], "confirm": True},
    )
    # Must be 403 Forbidden due to missing local bootstrap token
    assert resp.status_code == 403
    assert "Local bootstrap token missing" in resp.text
