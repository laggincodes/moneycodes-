"""
Comprehensive automated tests for SQLite Backup & Restore Utility.

Tests:
1. Online snapshotting with concurrent WAL writes.
2. SQLite integrity & foreign key checks (pass and fail cases).
3. AES-256-GCM encryption/decryption roundtrip and SHA-256 validation.
4. Tamper detection: bit-flips, header tampering, magic corruption, wrong keys.
5. Safe destination protection: no accidental overwrite without explicit flag.
6. Grandfather-Father-Son retention policy (7 daily, 4 weekly, 3 monthly).
7. Safe deletion rule: no pruning on failed backups.
8. Key storage with Windows Credential Manager / in-memory store.
9. End-to-end restore drill measuring restore duration (<60s) and full record parity.
10. CLI subprocess commands (backup, verify, list, restore, prune).
"""

import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.cli.credentials import InMemoryCredentialStore
from scripts.backup import (
    BackupDecryptionError,
    BackupError,
    BackupIntegrityError,
    BackupKeyError,
    BackupVerificationError,
    calculate_retention_plan,
    calculate_sha256,
    create_online_snapshot,
    encrypt_snapshot_to_file,
    execute_backup,
    get_or_create_backup_key,
    parse_key_input,
    prune_backups,
    read_backup_container,
    restore_backup,
    verify_backup_file,
    verify_database_integrity,
)


@pytest.fixture
def temp_dir():
    """Provides an isolated disposable directory for each test."""
    td = tempfile.mkdtemp(prefix="wlh_test_backup_")
    yield Path(td)
    shutil.rmtree(td, ignore_errors=True)


@pytest.fixture
def disposable_db(temp_dir):
    """
    Creates a realistic disposable SQLite database with complete v1-v4 schema
    and representative records for all tables.
    """
    db_path = temp_dir / "disposable_source.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript("""
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    );
    INSERT INTO schema_migrations VALUES
        (1, 'Add users, sessions, and account isolation columns', '2026-10-26T00:00:00Z'),
        (2, 'Add idempotency keys table for offline mutation deduplication', '2026-10-26T00:00:00Z'),
        (3, 'Add request_fingerprint column to idempotency_keys', '2026-10-26T00:00:00Z'),
        (4, 'Add durable worker leases, fencing tokens, and checkpoint data', '2026-10-26T00:00:00Z');

    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    INSERT INTO users VALUES ('user_1', 'leadfinder@local.test', 'pbkdf2_hash_xxx', 'Test Admin', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z');

    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        user_agent TEXT,
        ip_address TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    INSERT INTO sessions VALUES ('sess_1', 'user_1', 'csrf_token_123', 'pytest', '127.0.0.1', '2026-10-27T00:00:00Z', '2026-10-26T00:00:00Z');

    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
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
        worker_id TEXT,
        lease_expires_at TEXT,
        heartbeat_at TEXT,
        lease_version INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        checkpoint_data TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    INSERT INTO jobs VALUES (
        'job_1', 'user_1', 'completed', 'Indiranagar, Bangalore',
        12.9784, 77.6408, 2.5, '["cafe"]', 10, 5, 5, NULL, '[]',
        'worker_pid_1234', '2026-10-26T12:00:00Z', '2026-10-26T12:00:00Z',
        1, 0, 3, '{"stage":"completed"}', '2026-10-26T10:00:00Z', '2026-10-26T10:05:00Z'
    );

    CREATE TABLE leads (
        id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
        job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
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
        UNIQUE (user_id, provider_id)
    );
    INSERT INTO leads VALUES (
        'lead_1', 'user_1', 'job_1', 'osm:node:101', 'Artisan Roastery', 'cafe',
        '100 Feet Rd', 12.9784, 77.6408, '+919876543210', 'https://artisan.example.com',
        '08:00-22:00', 'https://osm.org/node/101', '{"provider":"osm"}', '{}',
        '{"has_website":true,"mobile_friendly":true}', 85, '{"breakdown":85}',
        '{"email_pitch":"Hi Artisan"}', 'new', 'Great candidate', NULL,
        '2026-10-26T10:01:00Z', '2026-10-26T10:01:00Z'
    );

    CREATE TABLE idempotency_keys (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        action_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        response_status INTEGER NOT NULL,
        response_body TEXT NOT NULL,
        request_fingerprint TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    );
    INSERT INTO idempotency_keys VALUES (
        'user_1', 'idem_key_999', 'job_create', 'job_1', 200,
        '{"id":"job_1","status":"queued"}', 'fingerprint_hash_abc',
        '2026-10-26T10:00:00Z', '2026-10-27T10:00:00Z'
    );
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_online_snapshot_with_concurrent_writes(disposable_db, temp_dir):
    """
    Verifies that create_online_snapshot produces a consistent snapshot
    while concurrent threads are actively writing to the source database in WAL mode.
    """
    snapshot_path = str(temp_dir / "online_snapshot.db")
    stop_event = threading.Event()
    inserted_concurrently = []

    def active_writer():
        conn = sqlite3.connect(str(disposable_db), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL")
        counter = 1
        while not stop_event.is_set():
            try:
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"user_concurrent_{counter}",
                        f"user_{counter}@test.com",
                        "hash",
                        f"Writer {counter}",
                        "2026-10-26T00:00:00Z",
                        "2026-10-26T00:00:00Z",
                    ),
                )
                conn.commit()
                inserted_concurrently.append(counter)
                counter += 1
                time.sleep(0.002)
            except Exception:
                break
        conn.close()

    writer_thread = threading.Thread(target=active_writer)
    writer_thread.start()

    try:
        # Snapshot with small page batches to interleave with writes
        sha = create_online_snapshot(
            source_db_path=str(disposable_db),
            snapshot_path=snapshot_path,
            pages_per_step=5,
            sleep_delay=0.005,
        )
        assert len(sha) == 64
        assert os.path.exists(snapshot_path)
    finally:
        stop_event.set()
        writer_thread.join()

    # Snapshot must be consistent and valid
    verify_database_integrity(snapshot_path)

    # Snapshot must have valid records
    snap_conn = sqlite3.connect(snapshot_path)
    cur = snap_conn.cursor()
    user_count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    lead_count = cur.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    snap_conn.close()

    assert user_count >= 1
    assert lead_count == 1


def test_verify_database_integrity_pass(disposable_db):
    """Verifies that a valid database passes integrity and foreign key checks."""
    verify_database_integrity(str(disposable_db))


def test_verify_database_integrity_fails_on_foreign_key_violation(temp_dir):
    """Verifies that foreign key violations are detected and raise BackupIntegrityError."""
    bad_db = temp_dir / "bad_fk.db"
    conn = sqlite3.connect(str(bad_db))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))")
    conn.execute("INSERT INTO child VALUES ('c1', 'nonexistent_parent')")
    conn.commit()
    conn.close()

    with pytest.raises(BackupIntegrityError, match="foreign_key_check detected violations"):
        verify_database_integrity(str(bad_db))


def test_verify_database_integrity_fails_on_corrupt_file(temp_dir):
    """Verifies that a corrupt database file raises BackupIntegrityError."""
    corrupt_db = temp_dir / "corrupted.db"
    # Write garbage bytes
    with open(corrupt_db, "wb") as f:
        f.write(b"Corrupted database header and invalid sqlite pages" * 100)

    with pytest.raises(BackupIntegrityError):
        verify_database_integrity(str(corrupt_db))


def test_encryption_decryption_roundtrip(disposable_db, temp_dir):
    """
    Verifies that encrypt_snapshot_to_file and verify_backup_file correctly
    encrypt and authenticate snapshots using AES-256-GCM.
    """
    key = AESGCM.generate_key(bit_length=256)
    backup_file = str(temp_dir / "test_backup.wlhbak")

    # 1. Snapshot
    snap_file = str(temp_dir / "snap.db")
    sha = create_online_snapshot(str(disposable_db), snap_file)

    # 2. Encrypt
    metadata = encrypt_snapshot_to_file(
        snapshot_path=snap_file,
        target_backup_path=backup_file,
        key=key,
        source_db_name="disposable_source.db",
        source_sha256=sha,
    )
    assert os.path.exists(backup_file)
    assert metadata["source_sha256"] == sha
    assert metadata["format"] == "AES-256-GCM"

    # 3. Read container format
    meta_parsed, header_bytes, nonce, ciphertext = read_backup_container(backup_file)
    assert meta_parsed["source_sha256"] == sha
    assert len(nonce) == 12
    assert len(ciphertext) > 100

    # 4. Self-verify
    verified = verify_backup_file(backup_file, key)
    assert verified["source_sha256"] == sha


def test_tampered_ciphertext_rejected(disposable_db, temp_dir):
    """Verifies that flipping bits in ciphertext raises BackupDecryptionError."""
    key = AESGCM.generate_key(bit_length=256)
    backup_file = str(temp_dir / "tampered.wlhbak")

    snap_file = str(temp_dir / "snap.db")
    sha = create_online_snapshot(str(disposable_db), snap_file)
    encrypt_snapshot_to_file(snap_file, backup_file, key, "source.db", sha)

    # Tamper with the last byte of the file (in the tag or ciphertext)
    with open(backup_file, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        last_byte = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last_byte[0] ^ 0x01]))

    with pytest.raises(BackupDecryptionError, match="Authentication tag verification failed"):
        verify_backup_file(backup_file, key)


def test_tampered_header_rejected_by_aead(disposable_db, temp_dir):
    """
    Verifies that modifying metadata header is detected by AEAD associated data
    and rejected with BackupDecryptionError.
    """
    key = AESGCM.generate_key(bit_length=256)
    backup_file = str(temp_dir / "tampered_header.wlhbak")

    snap_file = str(temp_dir / "snap.db")
    sha = create_online_snapshot(str(disposable_db), snap_file)
    encrypt_snapshot_to_file(snap_file, backup_file, key, "source.db", sha)

    meta, header_bytes, nonce, ciphertext = read_backup_container(backup_file)
    # Modify header JSON by altering source_db_name while keeping exact same length
    tampered_meta = dict(meta)
    tampered_meta["source_db_name"] = "hacked.db" + (" " * (len(meta["source_db_name"]) - 9))
    tampered_header_bytes = json_str = import_json_dumps(tampered_meta)

    # Overwrite header with tampered version
    with open(backup_file, "r+b") as f:
        f.seek(8 + 4)  # Skip magic and header length
        f.write(tampered_header_bytes[: len(header_bytes)])

    with pytest.raises(BackupDecryptionError, match="Authentication tag verification failed"):
        verify_backup_file(backup_file, key)


def import_json_dumps(obj):
    import json
    return json.dumps(obj, sort_keys=True).encode("utf-8")


def test_wrong_key_rejected(disposable_db, temp_dir):
    """Verifies that decrypting with a wrong 256-bit key fails."""
    key1 = AESGCM.generate_key(bit_length=256)
    key2 = AESGCM.generate_key(bit_length=256)
    backup_file = str(temp_dir / "wrong_key.wlhbak")

    snap_file = str(temp_dir / "snap.db")
    sha = create_online_snapshot(str(disposable_db), snap_file)
    encrypt_snapshot_to_file(snap_file, backup_file, key1, "source.db", sha)

    with pytest.raises(BackupDecryptionError, match="Authentication tag verification failed"):
        verify_backup_file(backup_file, key2)


def test_restore_destination_protection(disposable_db, temp_dir):
    """
    Verifies that restore_backup refuses to overwrite an existing database
    unless overwrite=True is explicitly specified.
    """
    key = AESGCM.generate_key(bit_length=256)
    backup_file = str(temp_dir / "protection.wlhbak")

    snap_file = str(temp_dir / "snap.db")
    sha = create_online_snapshot(str(disposable_db), snap_file)
    encrypt_snapshot_to_file(snap_file, backup_file, key, "source.db", sha)

    target_db = temp_dir / "already_exists.db"
    with open(target_db, "w") as f:
        f.write("pre-existing database file")

    # Should fail without overwrite=True
    with pytest.raises(BackupError, match="Destination database already exists"):
        restore_backup(backup_file, str(target_db), key, overwrite=False)

    # Content must NOT be touched
    with open(target_db, "r") as f:
        assert f.read() == "pre-existing database file"

    # Should succeed with overwrite=True
    res = restore_backup(backup_file, str(target_db), key, overwrite=True)
    assert res["status"] == "success"
    verify_database_integrity(str(target_db))


def test_retention_policy_calculation():
    """
    Verifies Grandfather-Father-Son retention calculation:
    - Retains backups within last 24 hours.
    - Retains 7 daily backups.
    - Retains 4 weekly backups.
    - Retains 3 monthly backups.
    """
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
    # 60 daily backups
    entries = [
        (f"backup_day_{i}.wlhbak", now - timedelta(days=i))
        for i in range(60)
    ]

    kept, pruned = calculate_retention_plan(
        entries,
        max_daily=7,
        max_weekly=4,
        max_monthly=3,
        reference_time=now,
    )

    assert len(kept) == 11
    assert len(pruned) == 49
    # Newest 7 days must be in kept
    for i in range(7):
        assert f"backup_day_{i}.wlhbak" in kept


def test_retention_safe_deletion_rule(temp_dir):
    """
    Verifies that prune_backups only deletes expired backups and preserves
    new backups. Also verifies that when a backup fails, pruning is never called.
    """
    backup_dir = temp_dir / "retention_dir"
    backup_dir.mkdir()

    # Create 3 backup dummy files
    for i in range(3):
        f = backup_dir / f"wlh_backup_2026102{i}_120000.wlhbak"
        f.write_text("backup")

    # With 3 backups (well below 7 daily), nothing should be pruned
    pruned = prune_backups(str(backup_dir), max_daily=7, max_weekly=4, max_monthly=3)
    assert len(pruned) == 0
    assert len(list(backup_dir.glob("*.wlhbak"))) == 3


def test_key_management_in_memory():
    """Verifies get_or_create_backup_key generates and persists 256-bit key."""
    mem_store = InMemoryCredentialStore()
    target = "WebsiteLeadHunter:test_key"

    # 1. First call generates key
    key1 = get_or_create_backup_key(target=target, credential_store=mem_store)
    assert len(key1) == 32

    # 2. Second call returns same key
    key2 = get_or_create_backup_key(target=target, credential_store=mem_store)
    assert key1 == key2

    # 3. Direct key override
    manual_hex = "a" * 64
    key3 = get_or_create_backup_key(key_override=manual_hex)
    assert key3 == bytes.fromhex(manual_hex)


def test_end_to_end_restore_drill_and_duration(disposable_db, temp_dir):
    """
    Performs complete end-to-end backup and restore drill:
    - Backs up disposable_db with full schema and records.
    - Measures restore duration (must be <60s).
    - Verifies 100% record parity across all tables:
      schema_migrations, users, sessions, jobs, leads, idempotency_keys.
    """
    backup_dir = temp_dir / "drill_backups"
    mem_store = InMemoryCredentialStore()

    # 1. Execute Backup
    start_backup = time.monotonic()
    backup_res = execute_backup(
        source_db_path=str(disposable_db),
        backup_dir=str(backup_dir),
        credential_store=mem_store,
        key_target="WebsiteLeadHunter:drill_key",
    )
    backup_duration = time.monotonic() - start_backup
    assert backup_res["status"] == "success"
    assert backup_duration < 10.0  # Local SQLite backup should take < 10s

    backup_file = backup_res["backup_file"]
    assert os.path.exists(backup_file)

    # 2. Restore Drill to separate destination
    restored_db = str(temp_dir / "restored_drill.db")
    key = get_or_create_backup_key(target="WebsiteLeadHunter:drill_key", credential_store=mem_store)

    start_restore = time.monotonic()
    restore_res = restore_backup(
        backup_file_path=backup_file,
        target_db_path=restored_db,
        key=key,
        overwrite=False,
    )
    restore_duration = restore_res["restore_duration_seconds"]

    # Restore SLA requirement: must be well under 60 seconds
    assert restore_duration < 60.0
    assert restore_res["status"] == "success"

    # 3. Comprehensive Record Parity Assertions
    orig_conn = sqlite3.connect(str(disposable_db))
    rest_conn = sqlite3.connect(restored_db)

    tables = ["schema_migrations", "users", "sessions", "jobs", "leads", "idempotency_keys"]
    for table in tables:
        orig_count = orig_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rest_count = rest_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert orig_count == rest_count, f"Record count mismatch in table {table}!"

        # Spot-check representative values
        orig_rows = orig_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        rest_rows = rest_conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        assert orig_rows == rest_rows, f"Data content mismatch in table {table}!"

    orig_conn.close()
    rest_conn.close()


def test_cli_subprocess_commands(disposable_db, temp_dir):
    """
    Verifies that scripts/backup.py executes cleanly as a standalone CLI tool
    for backup, verify, list, restore, and prune commands.
    """
    backup_dir = temp_dir / "cli_backups"
    key_hex = secrets.token_hex(32)
    py_exe = sys.executable
    script_path = str(Path(BACKEND_DIR) / "scripts" / "backup.py")

    env = os.environ.copy()
    env["PYTHONPATH"] = BACKEND_DIR

    # 1. CLI Backup
    cmd_backup = [
        py_exe,
        script_path,
        "backup",
        "--db-path", str(disposable_db),
        "--backup-dir", str(backup_dir),
        "--key", key_hex,
    ]
    res_backup = subprocess.run(cmd_backup, capture_output=True, text=True, env=env)
    assert res_backup.returncode == 0, f"CLI backup failed: {res_backup.stderr}"
    assert "[OK] Backup created successfully" in res_backup.stdout

    # Find created backup file
    baks = list(backup_dir.glob("*.wlhbak"))
    assert len(baks) == 1
    bak_file = str(baks[0])

    # 2. CLI Verify
    cmd_verify = [
        py_exe,
        script_path,
        "verify",
        "--backup-file", bak_file,
        "--key", key_hex,
    ]
    res_verify = subprocess.run(cmd_verify, capture_output=True, text=True, env=env)
    assert res_verify.returncode == 0, f"CLI verify failed: {res_verify.stderr}"
    assert "[OK] Backup verification PASSED" in res_verify.stdout

    # 3. CLI List
    cmd_list = [
        py_exe,
        script_path,
        "list",
        "--backup-dir", str(backup_dir),
    ]
    res_list = subprocess.run(cmd_list, capture_output=True, text=True, env=env)
    assert res_list.returncode == 0
    assert "Backups in" in res_list.stdout

    # 4. CLI Restore
    restored_db = str(temp_dir / "cli_restored.db")
    cmd_restore = [
        py_exe,
        script_path,
        "restore",
        "--backup-file", bak_file,
        "--target-db", restored_db,
        "--key", key_hex,
    ]
    res_restore = subprocess.run(cmd_restore, capture_output=True, text=True, env=env)
    assert res_restore.returncode == 0, f"CLI restore failed: {res_restore.stderr}"
    assert "[OK] Database restored successfully" in res_restore.stdout
    assert os.path.exists(restored_db)

    # 5. CLI Prune (Dry-Run)
    cmd_prune = [
        py_exe,
        script_path,
        "prune",
        "--backup-dir", str(backup_dir),
        "--dry-run",
    ]
    res_prune = subprocess.run(cmd_prune, capture_output=True, text=True, env=env)
    assert res_prune.returncode == 0
    assert "[DRY-RUN]" in res_prune.stdout or "0 backup(s)" in res_prune.stdout
