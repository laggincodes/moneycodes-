"""
Database Migration Manager for WebsiteLead Hunter.
Applies versioned, non-destructive migrations to SQLite database.
"""
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Tuple

logger = logging.getLogger(__name__)


def get_column_names(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Returns list of column names for a given table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Checks whether a table exists in SQLite database."""
    cursor = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone()[0] > 0


def apply_migration_v1(conn: sqlite3.Connection):
    """
    Migration v1:
    - Adds `users` table for multi-tenant accounts.
    - Adds `sessions` table for server-side HttpOnly cookie sessions.
    - Adds `user_id` column to `jobs` table.
    - Adds `user_id` column and compound uniqueness `(user_id, provider_id)` to `leads` table.
    - Preserves all pre-existing records intact.
    """
    logger.info("Applying database migration v1: Authentication and Account Isolation...")

    # 1. Create users table
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);")

    # 2. Create sessions table
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        csrf_token TEXT NOT NULL,
        user_agent TEXT,
        ip_address TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at);")

    # 3. Add user_id to jobs if table exists and column is missing
    if table_exists(conn, "jobs"):
        jobs_cols = get_column_names(conn, "jobs")
        if "user_id" not in jobs_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;")
            logger.info("Added user_id column to jobs table.")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs (user_id);")

    # 4. Add user_id to leads and update constraint to (user_id, provider_id)
    if table_exists(conn, "leads"):
        leads_cols = get_column_names(conn, "leads")
        if "user_id" not in leads_cols:
            # Safely recreate leads table with user_id and composite uniqueness (user_id, provider_id)
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("""
            CREATE TABLE leads_migrated (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                job_id TEXT,
                provider_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                address TEXT,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                phone TEXT,
                website TEXT,
                opening_hours TEXT,
                source_url TEXT NOT NULL,
                provenance TEXT NOT NULL,
                raw_data TEXT NOT NULL,
                audit_result TEXT,
                score INTEGER DEFAULT 0,
                score_breakdown TEXT,
                pitches TEXT,
                pipeline_status TEXT DEFAULT 'new',
                notes TEXT,
                contacted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL,
                UNIQUE (user_id, provider_id)
            );
            """)

            # Copy existing columns from leads (user_id will be NULL for legacy leads)
            conn.execute("""
            INSERT INTO leads_migrated (
                id, user_id, job_id, provider_id, name, category, address, lat, lon,
                phone, website, opening_hours, source_url, provenance, raw_data,
                audit_result, score, score_breakdown, pitches, pipeline_status,
                notes, contacted_at, created_at, updated_at
            )
            SELECT
                id, NULL, job_id, provider_id, name, category, address, lat, lon,
                phone, website, opening_hours, source_url, provenance, raw_data,
                audit_result, score, score_breakdown, pitches, pipeline_status,
                notes, contacted_at, created_at, updated_at
            FROM leads;
            """)

            conn.execute("DROP TABLE leads;")
            conn.execute("ALTER TABLE leads_migrated RENAME TO leads;")
            conn.execute("PRAGMA foreign_keys = ON;")
            logger.info("Migrated leads table to support user_id and composite uniqueness.")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_user_id ON leads (user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_pipeline_status ON leads (pipeline_status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_score ON leads (score);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_job_id ON leads (job_id);")


def apply_migration_v2(conn: sqlite3.Connection):
    """
    Migration v2:
    - Adds `idempotency_keys` table for deduplicating mutations and guaranteeing idempotent replays.
    - Scoped by (user_id, key) compound primary key for account isolation.
    - Includes TTL expiration index.
    """
    logger.info("Applying database migration v2: Idempotency Keys table...")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        user_id TEXT NOT NULL,
        key TEXT NOT NULL,
        action_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        response_status INTEGER NOT NULL,
        response_body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (user_id, key),
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_user_id ON idempotency_keys (user_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_expires_at ON idempotency_keys (expires_at);")


def apply_migration_v3(conn: sqlite3.Connection):
    """
    Migration v3:
    - Adds `request_fingerprint` column to `idempotency_keys` table.
    - Enables collision-resistant canonical payload verification for job creation.
    """
    logger.info("Applying database migration v3: Adding request_fingerprint to idempotency_keys...")
    if table_exists(conn, "idempotency_keys"):
        cols = get_column_names(conn, "idempotency_keys")
        if "request_fingerprint" not in cols:
            conn.execute("ALTER TABLE idempotency_keys ADD COLUMN request_fingerprint TEXT;")
            logger.info("Added request_fingerprint column to idempotency_keys table.")


def apply_migration_v4(conn: sqlite3.Connection):
    """
    Migration v4:
    - Adds durable worker leases, monotonic fencing tokens, and checkpoint columns to `jobs` table.
    - Preserves all pre-existing records intact with default lease_version = 0.
    """
    logger.info("Applying database migration v4: Adding durable worker leases and fencing tokens...")
    if table_exists(conn, "jobs"):
        cols = get_column_names(conn, "jobs")
        if "worker_id" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT;")
            logger.info("Added worker_id column to jobs table.")
        if "lease_expires_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT;")
            logger.info("Added lease_expires_at column to jobs table.")
        if "heartbeat_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;")
            logger.info("Added heartbeat_at column to jobs table.")
        if "lease_version" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN lease_version INTEGER NOT NULL DEFAULT 0;")
            logger.info("Added lease_version column to jobs table.")
        if "retry_count" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;")
            logger.info("Added retry_count column to jobs table.")
        if "max_retries" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3;")
            logger.info("Added max_retries column to jobs table.")
        if "checkpoint_data" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint_data TEXT;")
            logger.info("Added checkpoint_data column to jobs table.")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (status, lease_expires_at, created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs (worker_id, lease_version);")


MIGRATIONS: List[Tuple[int, str, callable]] = [
    (1, "Add users, sessions, and account isolation columns", apply_migration_v1),
    (2, "Add idempotency keys table for offline mutation deduplication", apply_migration_v2),
    (3, "Add request_fingerprint column to idempotency_keys for canonical payload verification", apply_migration_v3),
    (4, "Add durable worker leases, fencing tokens, and checkpoint data", apply_migration_v4),
]


def run_migrations(conn: sqlite3.Connection):
    """Executes all pending migrations in order."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    );
    """)

    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
    applied_versions = {row[0] for row in cursor.fetchall()}

    for version, name, func in MIGRATIONS:
        if version not in applied_versions:
            logger.info(f"Executing migration {version}: {name}")
            func(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now),
            )
            logger.info(f"Migration {version} applied successfully.")
