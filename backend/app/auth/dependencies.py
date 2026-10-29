"""
FastAPI Authentication & CSRF Dependencies for WebsiteLead Hunter.
Manages server-side session lookup, expiration, user resolution, and CSRF protection.
"""
import hmac
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from fastapi import Request, HTTPException, Depends
from app.database import get_db

SESSION_COOKIE_NAME = "session_id"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("true", "1", "yes")
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")


def extract_token_from_request(request: Request) -> Optional[Tuple[str, str]]:
    """
    Extracts session token from request.
    Returns tuple of (token, source) where source is 'cookie' or 'bearer'.
    """
    # 1. Check HttpOnly cookie first (browser)
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return (cookie_token, "cookie")

    # 2. Check Authorization header (CLI / API clients)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return (token, "bearer")

    return None


async def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """
    Resolves the current authenticated user from session, or returns None if unauthenticated.
    """
    token_info = extract_token_from_request(request)
    if not token_info:
        return None

    token, source = token_info

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                s.id as session_id,
                s.csrf_token,
                s.expires_at,
                u.id as user_id,
                u.email,
                u.full_name,
                u.created_at
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.id = ?
            """,
            (token,),
        ).fetchone()

        if not row:
            return None

        # Check expiration
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            # Clean up expired session
            conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
            return None

        return {
            "id": row["user_id"],
            "email": row["email"],
            "full_name": row["full_name"],
            "created_at": row["created_at"],
            "session_id": row["session_id"],
            "csrf_token": row["csrf_token"],
            "auth_source": source,
        }


async def get_current_user(user: Optional[Dict[str, Any]] = Depends(get_current_user_optional)) -> Dict[str, Any]:
    """
    Enforces authentication. Raises HTTP 401 if request has no valid session.
    """
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def verify_csrf_token(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Verifies CSRF token on state-changing browser requests (POST, PATCH, DELETE, PUT).
    CLI / API requests using Bearer tokens are exempt because browsers cannot send
    custom Authorization headers cross-origin without CORS preflight approval.
    """
    # Safe methods do not require CSRF token
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    # Non-cookie requests (e.g. CLI using Bearer token) are inherently CSRF-safe
    if user.get("auth_source") == "bearer":
        return

    # Browser requests must supply matching X-CSRF-Token header
    csrf_header = request.headers.get(CSRF_HEADER_NAME)
    expected_token = user.get("csrf_token")

    if not csrf_header or not expected_token or not hmac.compare_digest(csrf_header, expected_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing CSRF token. State-changing requests require a valid X-CSRF-Token header.",
        )
