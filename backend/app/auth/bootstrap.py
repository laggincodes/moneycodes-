"""
Local Account Bootstrap CLI for WebsiteLead Hunter.
Allows creating the initial administrator/user account securely from the command line.
"""
import sys
import uuid
from datetime import datetime, timezone

from app.database import init_db, get_db
from app.auth.security import (
    normalize_email,
    validate_email_format,
    validate_password_policy,
    hash_password,
)


def bootstrap_account(email: str, password: str, full_name: str = "Admin"):
    init_db()
    norm_email = normalize_email(email)

    if not validate_email_format(norm_email):
        print(f"Error: Invalid email format '{email}'", file=sys.stderr)
        sys.exit(1)

    valid_pass, pass_err = validate_password_policy(password)
    if not valid_pass:
        print(f"Error: {pass_err}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    pwd_hash = hash_password(password)
    user_id = str(uuid.uuid4())

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (norm_email,)).fetchone()
        if existing:
            print(f"User '{norm_email}' already exists with ID: {existing['id']}")
            return existing["id"]

        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, norm_email, pwd_hash, full_name, now, now),
        )

        # Note: Legacy unassigned data is NOT silently adopted.
        # Administrative adoption requires explicit confirmation via `python cli.py admin adopt-legacy`.
        print(f"Successfully created account for '{norm_email}' (User ID: {user_id}).")
        if user_count == 0:
            print("Note: Pre-existing legacy leads and jobs were not adopted automatically.")
            print("To securely adopt unowned legacy data, run: python cli.py admin adopt-legacy")
        return user_id


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m app.auth.bootstrap <email> <password> [full_name]")
        sys.exit(1)

    email_arg = sys.argv[1]
    pass_arg = sys.argv[2]
    name_arg = sys.argv[3] if len(sys.argv) > 3 else "Admin"
    bootstrap_account(email_arg, pass_arg, name_arg)
