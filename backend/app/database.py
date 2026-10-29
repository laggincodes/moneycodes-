"""
SQLite Database connection and initialization.
"""
import os
import sqlite3
from typing import Generator
from contextlib import contextmanager
from app.migrations import run_migrations

DATABASE_PATH = os.getenv("DATABASE_PATH", "websitelead_hunter.db")


def get_db_connection() -> sqlite3.Connection:
    """Creates a database connection with dictionary-like row factory and foreign keys enabled."""
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for safe database transactions."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = None):
    """Initializes tables and indexes."""
    global DATABASE_PATH
    if db_path:
        DATABASE_PATH = db_path

    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            location_query TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_km REAL NOT NULL,
            categories TEXT NOT NULL,
            max_leads INTEGER NOT NULL,
            total_found INTEGER DEFAULT 0,
            processed_count INTEGER DEFAULT 0,
            error_message TEXT,
            logs TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            provider_id TEXT UNIQUE NOT NULL,
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
            FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_leads_pipeline_status ON leads (pipeline_status);
        CREATE INDEX IF NOT EXISTS idx_leads_score ON leads (score);
        CREATE INDEX IF NOT EXISTS idx_leads_job_id ON leads (job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
        """)
        run_migrations(conn)
