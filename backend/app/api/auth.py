"""
Authentication API Endpoints for WebsiteLead Hunter.
Provides registration, login, logout, current user session status, and initial local bootstrap.
"""
import hmac
import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Response, Depends, Header
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.database import get_db
from app.auth.security import (
    normalize_email,
    validate_email_format,
    validate_password_policy,
    hash_password,
    verify_password,
    dummy_password_hash_check,
    is_rate_limited,
    record_failed_attempt,
    clear_failed_attempts,
    generate_session_token,
    generate_csrf_token,
    calculate_session_expiry,
    SESSION_MAX_AGE_SECONDS,
)
from app.auth.dependencies import (
    get_current_user,
    get_current_user_optional,
    verify_csrf_token,
    SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    COOKIE_SECURE,
    COOKIE_SAMESITE,
)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def get_admin_token_path() -> str:
    """Returns filesystem path for the ephemeral admin bootstrap token."""
    override = os.getenv("ADMIN_TOKEN_PATH")
    if override:
        return os.path.abspath(override)
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_dir, ".admin_token")


class RegisterRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., description="User password (min 8 chars, max 128 chars)")
    full_name: Optional[str] = Field(None, description="Optional display name")


class LoginRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., description="User password")


class AdminAdoptRequest(BaseModel):
    password: str = Field(..., description="Administrator password for re-verification")
    confirm: bool = Field(..., description="Explicit confirmation flag")


def _set_auth_cookies(response: Response, session_id: str, csrf_token: str):
    """Sets secure, HttpOnly session cookie and readable CSRF cookie."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # JavaScript can read to pass in X-CSRF-Token header
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
    )


def _clear_auth_cookies(response: Response):
    """Deletes session and CSRF cookies."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")


@router.post("/register")
async def register(request: Request, payload: RegisterRequest, response: Response):
    ip = request.client.host if request.client else "127.0.0.1"
    norm_email = normalize_email(payload.email)

    if is_rate_limited(ip) or is_rate_limited(norm_email):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again in a few minutes.",
        )

    if not validate_email_format(norm_email):
        record_failed_attempt(ip)
        raise HTTPException(status_code=400, detail="Invalid email format.")

    valid_pass, pass_err = validate_password_policy(payload.password)
    if not valid_pass:
        record_failed_attempt(ip)
        raise HTTPException(status_code=400, detail=pass_err)

    now = datetime.now(timezone.utc).isoformat()
    pwd_hash = hash_password(payload.password)
    user_id = str(uuid.uuid4())
    session_id = generate_session_token()
    csrf_token = generate_csrf_token()
    expires_at = calculate_session_expiry()

    with get_db() as conn:
        # Check if email is already taken
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (norm_email,)).fetchone()
        if existing:
            record_failed_attempt(ip)
            record_failed_attempt(norm_email)
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

        # Insert new user
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, norm_email, pwd_hash, payload.full_name or "", now, now),
        )

        # Note: Web registration NEVER adopts legacy data.
        # Legacy adoption is strictly restricted to authenticated local admin CLI.

        # Create session
        user_agent = request.headers.get("user-agent", "")
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, csrf_token, user_agent, ip, expires_at, now),
        )

    clear_failed_attempts(ip)
    clear_failed_attempts(norm_email)
    _set_auth_cookies(response, session_id, csrf_token)

    return {
        "message": "Account created successfully.",
        "user": {
            "id": user_id,
            "email": norm_email,
            "full_name": payload.full_name or "",
        },
        "csrf_token": csrf_token,
        "session_token": session_id,
    }


@router.post("/login")
async def login(request: Request, payload: LoginRequest, response: Response):
    ip = request.client.host if request.client else "127.0.0.1"
    norm_email = normalize_email(payload.email)

    if is_rate_limited(ip) or is_rate_limited(norm_email):
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please try again in 5 minutes.",
        )

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id, email, password_hash, full_name FROM users WHERE email = ?",
            (norm_email,),
        ).fetchone()

        if not user_row:
            record_failed_attempt(ip)
            record_failed_attempt(norm_email)
            dummy_password_hash_check()
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        if not verify_password(payload.password, user_row["password_hash"]):
            record_failed_attempt(ip)
            record_failed_attempt(norm_email)
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        user_id = user_row["id"]
        session_id = generate_session_token()
        csrf_token = generate_csrf_token()
        expires_at = calculate_session_expiry()
        now = datetime.now(timezone.utc).isoformat()
        user_agent = request.headers.get("user-agent", "")

        conn.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, user_agent, ip_address, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, csrf_token, user_agent, ip, expires_at, now),
        )

    clear_failed_attempts(ip)
    clear_failed_attempts(norm_email)
    _set_auth_cookies(response, session_id, csrf_token)

    return {
        "message": "Login successful.",
        "user": {
            "id": user_row["id"],
            "email": user_row["email"],
            "full_name": user_row["full_name"],
        },
        "csrf_token": csrf_token,
        "session_token": session_id,
    }


@router.post("/logout")
async def logout(
    response: Response,
    user: Optional[dict] = Depends(get_current_user_optional),
):
    if user and user.get("session_id"):
        with get_db() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (user["session_id"],))

    _clear_auth_cookies(response)
    return {"message": "Logged out successfully."}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "created_at": user["created_at"],
        },
        "csrf_token": user["csrf_token"],
    }


@router.get("/admin/legacy-summary")
async def get_legacy_summary(
    user: dict = Depends(get_current_user),
):
    """
    Returns count of unowned legacy leads and jobs for administrative review.
    Requires authenticated user session.
    """
    with get_db() as conn:
        unowned_leads = conn.execute("SELECT COUNT(*) FROM leads WHERE user_id IS NULL").fetchone()[0]
        unowned_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id IS NULL").fetchone()[0]
    return {
        "unowned_leads": unowned_leads,
        "unowned_jobs": unowned_jobs,
    }


@router.post("/admin/adopt-legacy")
async def adopt_legacy_leads(
    request: Request,
    payload: AdminAdoptRequest,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
    admin_bootstrap_token: Optional[str] = Header(None, alias="X-Admin-Bootstrap-Token"),
):
    """
    Secure host-local administrative endpoint to adopt unowned legacy leads and jobs.
    Requires:
    1. Authenticated user session.
    2. Explicit confirmation flag (confirm=true).
    3. Password re-verification against the user's scrypt hash.
    4. Valid ephemeral bootstrap token from server host disk (X-Admin-Bootstrap-Token).
       The token is immediately deleted on consumption (one-time use).
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Adoption requires explicit confirmation (confirm=true).",
        )

    # 1. Re-verify password
    with get_db() as conn:
        user_row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if not user_row or not verify_password(payload.password, user_row["password_hash"]):
            record_failed_attempt(user["email"])
            raise HTTPException(
                status_code=403,
                detail="Invalid administrative credentials. Re-verification failed.",
            )

    # 2. Verify ephemeral local bootstrap token from host filesystem
    token_path = get_admin_token_path()
    if not admin_bootstrap_token or not os.path.exists(token_path):
        logger.warning(
            f"Security: Blocked legacy adoption attempt by user {user['id']} - missing local bootstrap token."
        )
        raise HTTPException(
            status_code=403,
            detail="Local bootstrap token missing or expired. Administrative adoption must be initiated from the host console.",
        )

    try:
        with open(token_path, "r", encoding="utf-8") as f:
            expected_token = f.read().strip()
    except Exception as exc:
        logger.error(f"Failed to read local bootstrap token file: {exc}")
        raise HTTPException(status_code=500, detail="Failed to read local bootstrap token.")

    # Immediately consume/delete token to prevent replay
    try:
        os.remove(token_path)
    except OSError:
        pass

    if not expected_token or not hmac.compare_digest(admin_bootstrap_token.strip(), expected_token):
        logger.warning(
            f"Security: Blocked legacy adoption attempt by user {user['id']} - token mismatch."
        )
        raise HTTPException(
            status_code=403,
            detail="Invalid local bootstrap token. Authorization failed.",
        )

    # 3. Perform atomic adoption of unowned records only
    with get_db() as conn:
        cur_jobs = conn.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL", (user["id"],))
        cur_leads = conn.execute("UPDATE leads SET user_id = ? WHERE user_id IS NULL", (user["id"],))
        adopted_jobs = cur_jobs.rowcount
        adopted_leads = cur_leads.rowcount

    logger.info(
        f"Security Audit: Admin user {user['id']} ({user['email']}) adopted {adopted_leads} legacy leads and {adopted_jobs} jobs via verified local token."
    )
    return {
        "message": f"Successfully adopted {adopted_leads} leads and {adopted_jobs} jobs.",
        "adopted_leads": adopted_leads,
        "adopted_jobs": adopted_jobs,
    }
