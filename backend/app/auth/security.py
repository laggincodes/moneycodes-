"""
Authentication and Password Security Core for WebsiteLead Hunter.
Implements scrypt password hashing, timing-safe validation, email normalization,
rate limiting, session tokens, and CSRF protection.
"""
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Tuple, Optional

# Password policy bounds
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

# Standard scrypt parameters (memory-hard, GPU/ASIC resistant)
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_LEN = 32

# Session lifetime
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

# Rate limiter configuration: 5 failed attempts per 300 seconds (5 mins)
RATE_LIMIT_WINDOW_SECONDS = 300
RATE_LIMIT_MAX_ATTEMPTS = 5

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

# In-memory rate limiting store: key -> list of failed attempt timestamps
_failed_attempts: Dict[str, list[float]] = {}


def normalize_email(email: str) -> str:
    """Trims whitespace and lowercases email address."""
    return email.strip().lower()


def validate_email_format(email: str) -> bool:
    """Validates that email matches standard format and reasonable length."""
    norm = normalize_email(email)
    if len(norm) < 5 or len(norm) > 254:
        return False
    return bool(EMAIL_REGEX.match(norm))


def validate_password_policy(password: str) -> Tuple[bool, Optional[str]]:
    """Checks password against length constraints."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password cannot exceed {MAX_PASSWORD_LENGTH} characters."
    return True, None


def hash_password(password: str) -> str:
    """
    Hashes a password using scrypt with a cryptographically secure random salt.
    Format: scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=0,
        dklen=SCRYPT_KEY_LEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Verifies candidate password against stored hash using constant-time comparison.
    """
    try:
        parts = stored_hash.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False

        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected_key = bytes.fromhex(parts[5])

        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=0,
            dklen=len(expected_key),
        )
        return hmac.compare_digest(derived, expected_key)
    except Exception:
        return False


def dummy_password_hash_check():
    """
    Performs a dummy scrypt computation to match execution time when an account does not exist,
    preventing timing attacks from leaking whether an email is registered.
    """
    dummy_salt = b"0" * SCRYPT_SALT_BYTES
    hashlib.scrypt(
        b"dummy_password_timing_pad",
        salt=dummy_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=0,
        dklen=SCRYPT_KEY_LEN,
    )


def is_rate_limited(identifier: str) -> bool:
    """
    Checks if an IP or email identifier has exceeded maximum failed attempts in the time window.
    """
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    attempts = _failed_attempts.get(identifier, [])
    # Prune old attempts
    attempts = [t for t in attempts if t > cutoff]
    _failed_attempts[identifier] = attempts
    return len(attempts) >= RATE_LIMIT_MAX_ATTEMPTS


def record_failed_attempt(identifier: str):
    """Records a failed login or registration attempt."""
    now = time.time()
    if identifier not in _failed_attempts:
        _failed_attempts[identifier] = []
    _failed_attempts[identifier].append(now)


def clear_failed_attempts(identifier: str):
    """Clears failed attempts upon successful login."""
    _failed_attempts.pop(identifier, None)


def generate_session_token() -> str:
    """Generates a cryptographically secure 256-bit URL-safe session token."""
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    """Generates a cryptographically secure 128-bit hex CSRF token."""
    return secrets.token_hex(16)


def calculate_session_expiry() -> str:
    """Returns ISO format expiry timestamp 7 days in the future."""
    return (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()
