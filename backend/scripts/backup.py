"""
Automated SQLite Backup & Restore Utility for WebsiteLead Hunter.

Features:
- Progressive snapshotting via SQLite's native Online Backup API (sqlite3.Connection.backup).
- Zero downtime: snapshots active database safely, including WAL mode changes.
- Pre- and post-backup integrity validation (PRAGMA integrity_check and PRAGMA foreign_key_check).
- Cryptographic SHA-256 snapshot checksum verification.
- AES-256-GCM authenticated encryption with header binding (associated data).
- Secure key storage in Windows Credential Manager (advapi32.dll) separated from backup files.
- Grandfather-Father-Son retention policy (7 daily, 4 weekly, 3 monthly).
- Atomic file writes and safe destination protection (never overwrites without explicit flag).
- Safe deletion rule: retention pruning only occurs after new backup is verified.
"""

import argparse
import base64
import json
import logging
import os
import secrets
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Dict, List, Optional, Tuple, Any

# Ensure backend root is on sys.path for app imports
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.cli.credentials import (
    WindowsCredentialStore,
    InMemoryCredentialStore,
    get_credential_backend,
    is_credential_store_available,
)

logger = logging.getLogger("wlh_backup")

# Constants
MAGIC_BYTES = b"WLHBAK01"
HEADER_LEN_STRUCT = ">I"  # 4-byte unsigned big-endian integer
HEADER_LEN_SIZE = struct.calcsize(HEADER_LEN_STRUCT)
DEFAULT_KEY_TARGET = "WebsiteLeadHunter:backup_key"
DEFAULT_KEY_USERNAME = "backup_service"
DEFAULT_BACKUP_DIR = os.path.join(_BACKEND_DIR, "backups")
DEFAULT_RETENTION_DAILY = 7
DEFAULT_RETENTION_WEEKLY = 4
DEFAULT_RETENTION_MONTHLY = 3
DEFAULT_PAGES_PER_STEP = 100
DEFAULT_SLEEP_DELAY = 0.005


class BackupError(Exception):
    """Base exception for backup operations."""
    pass


class BackupIntegrityError(BackupError):
    """Raised when SQLite integrity check or foreign key check fails."""
    pass


class BackupVerificationError(BackupError):
    """Raised when backup file format, checksum, or authentication fails."""
    pass


class BackupDecryptionError(BackupError):
    """Raised when decryption fails due to wrong key or corrupted ciphertext."""
    pass


class BackupKeyError(BackupError):
    """Raised when encryption key cannot be retrieved, generated, or stored."""
    pass


# ---------------------------------------------------------------------------
# Key Management
# ---------------------------------------------------------------------------

def parse_key_input(key_input: str) -> bytes:
    """Parses a 32-byte encryption key from raw bytes, 64-char hex, or 44-char base64."""
    if not key_input:
        raise BackupKeyError("Encryption key input is empty.")
    # Try hex
    if len(key_input) == 64:
        try:
            k = bytes.fromhex(key_input)
            if len(k) == 32:
                return k
        except ValueError:
            pass
    # Try base64
    try:
        k = base64.b64decode(key_input)
        if len(k) == 32:
            return k
    except Exception:
        pass
    # Try raw bytes as utf-8 if exactly 32 bytes
    raw = key_input.encode("utf-8")
    if len(raw) == 32:
        return raw

    raise BackupKeyError(
        f"Invalid encryption key length ({len(key_input)} chars). "
        "Must be a 32-byte key encoded as 64 hex characters or 44 base64 characters."
    )


def get_or_create_backup_key(
    target: str = DEFAULT_KEY_TARGET,
    credential_store: Optional[Any] = None,
    key_override: Optional[str] = None,
) -> bytes:
    """
    Retrieves or generates a 256-bit (32-byte) AES backup key.
    Checks in order:
    1. key_override parameter
    2. WLH_BACKUP_KEY environment variable
    3. Credential store (Windows Credential Manager or injected store)
    4. If none exists, generates a fresh random key and saves it to credential store.
    """
    # 1. Parameter override
    if key_override:
        return parse_key_input(key_override)

    # 2. Environment variable override
    env_key = os.getenv("WLH_BACKUP_KEY")
    if env_key:
        return parse_key_input(env_key)

    # 3. Credential Store
    store = credential_store or get_credential_backend()
    if not getattr(store, "is_available", False):
        raise BackupKeyError(
            "Windows Credential Manager is unavailable and no manual key was provided. "
            "Pass --key or set WLH_BACKUP_KEY environment variable."
        )

    try:
        cred = store.read(target)
        if cred is not None:
            _, secret = cred
            return parse_key_input(secret)
    except BackupKeyError:
        raise
    except Exception as exc:
        logger.warning(f"Failed to read backup key from credential store: {exc}")

    # 4. Generate fresh 256-bit key
    fresh_key = secrets.token_bytes(32)
    b64_key = base64.b64encode(fresh_key).decode("ascii")

    try:
        saved = store.write(target, DEFAULT_KEY_USERNAME, b64_key)
        if not saved:
            raise BackupKeyError(f"Credential store rejected writing target '{target}'.")
        logger.info(f"Generated fresh 256-bit backup key and stored under target '{target}'.")
        return fresh_key
    except Exception as exc:
        raise BackupKeyError(f"Failed to securely persist new backup key: {exc}") from exc


# ---------------------------------------------------------------------------
# SQLite Integrity & Checksum Verification
# ---------------------------------------------------------------------------

def calculate_sha256(file_path: str, chunk_size: int = 65536) -> str:
    """Computes SHA-256 digest of a file on disk."""
    hasher = sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_database_integrity(db_path: str) -> None:
    """
    Validates SQLite database consistency:
    - Runs PRAGMA integrity_check (must return [('ok',)])
    - Runs PRAGMA foreign_key_check (must return empty list [])
    Raises BackupIntegrityError if any check fails.
    """
    if not os.path.exists(db_path):
        raise BackupIntegrityError(f"Database file does not exist: {db_path}")

    # Use read-only URI mode to prevent any writes during inspection
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.execute("PRAGMA foreign_keys = ON")

        # 1. Integrity check
        cursor = conn.execute("PRAGMA integrity_check")
        rows = cursor.fetchall()
        if not rows or rows[0][0] != "ok":
            errors = [row[0] for row in rows]
            raise BackupIntegrityError(f"SQLite PRAGMA integrity_check failed: {errors}")

        # 2. Foreign key check
        cursor = conn.execute("PRAGMA foreign_key_check")
        fk_violations = cursor.fetchall()
        if fk_violations:
            raise BackupIntegrityError(
                f"SQLite PRAGMA foreign_key_check detected violations: {fk_violations}"
            )
    except sqlite3.Error as exc:
        raise BackupIntegrityError(f"SQLite error during integrity verification: {exc}") from exc
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Online Snapshot API
# ---------------------------------------------------------------------------

def create_online_snapshot(
    source_db_path: str,
    snapshot_path: str,
    pages_per_step: int = DEFAULT_PAGES_PER_STEP,
    sleep_delay: float = DEFAULT_SLEEP_DELAY,
) -> str:
    """
    Uses SQLite's native Online Backup API to produce a consistent point-in-time snapshot.
    Ensures zero downtime while source database is actively serving reads and WAL writes.
    Validates the snapshot integrity and returns its unencrypted SHA-256 digest.
    """
    if not os.path.exists(source_db_path):
        raise BackupError(f"Source database file not found: {source_db_path}")

    # Ensure snapshot target directory exists
    os.makedirs(os.path.dirname(os.path.abspath(snapshot_path)), exist_ok=True)

    abs_source = os.path.abspath(source_db_path)
    source_uri = f"file:{abs_source}?mode=ro"

    source_conn = None
    dest_conn = None
    try:
        source_conn = sqlite3.connect(source_uri, uri=True, timeout=15.0)
        dest_conn = sqlite3.connect(snapshot_path, timeout=15.0)

        # Execute progressive backup
        # Pages=-1 would copy all at once; pages_per_step batches to minimize source locking
        source_conn.backup(dest_conn, pages=pages_per_step, sleep=sleep_delay)
    except sqlite3.Error as exc:
        raise BackupError(f"Failed to create online SQLite snapshot: {exc}") from exc
    finally:
        if dest_conn:
            dest_conn.close()
        if source_conn:
            source_conn.close()

    # Validate snapshot integrity
    verify_database_integrity(snapshot_path)

    # Compute unencrypted snapshot digest
    return calculate_sha256(snapshot_path)


# ---------------------------------------------------------------------------
# AES-256-GCM Encryption & Container Format
# ---------------------------------------------------------------------------

def encrypt_snapshot_to_file(
    snapshot_path: str,
    target_backup_path: str,
    key: bytes,
    source_db_name: str,
    source_sha256: str,
) -> Dict[str, Any]:
    """
    Encrypts snapshot using AES-256-GCM.
    Cryptographically binds header metadata using AEAD associated data.
    Atomically writes to target_backup_path.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise BackupError("AES-256 encryption requires the 'cryptography' library.") from exc

    if len(key) != 32:
        raise BackupKeyError("AES-256-GCM requires a 32-byte key.")

    source_size = os.path.getsize(snapshot_path)
    now_utc = datetime.now(timezone.utc).isoformat()
    nonce = secrets.token_bytes(12)  # 96-bit standard GCM nonce

    metadata = {
        "version": 1,
        "format": "AES-256-GCM",
        "created_at": now_utc,
        "source_db_name": source_db_name,
        "source_size_bytes": source_size,
        "source_sha256": source_sha256,
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }

    header_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
    header_len = len(header_bytes)

    # Read unencrypted snapshot data
    with open(snapshot_path, "rb") as f:
        plaintext = f.read()

    # Encrypt snapshot with AEAD, binding header_bytes as associated data
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=header_bytes)

    # Write atomically to target_backup_path via temporary file
    temp_backup = target_backup_path + ".tmp"
    try:
        with open(temp_backup, "wb") as f:
            f.write(MAGIC_BYTES)
            f.write(struct.pack(HEADER_LEN_STRUCT, header_len))
            f.write(header_bytes)
            f.write(ciphertext)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_backup, target_backup_path)
    except Exception as exc:
        if os.path.exists(temp_backup):
            try:
                os.unlink(temp_backup)
            except OSError:
                pass
        raise BackupError(f"Failed writing encrypted backup container: {exc}") from exc

    return metadata


def read_backup_container(backup_file_path: str) -> Tuple[Dict[str, Any], bytes, bytes, bytes]:
    """
    Parses backup container file.
    Returns: (metadata_dict, header_bytes, nonce_bytes, ciphertext_and_tag)
    Raises BackupVerificationError on malformed file.
    """
    if not os.path.exists(backup_file_path):
        raise BackupVerificationError(f"Backup file does not exist: {backup_file_path}")

    file_size = os.path.getsize(backup_file_path)
    min_size = len(MAGIC_BYTES) + HEADER_LEN_SIZE + 2 + 16  # Magic + Len + Min JSON + Tag
    if file_size < min_size:
        raise BackupVerificationError(f"Backup file is truncated or too small ({file_size} bytes).")

    with open(backup_file_path, "rb") as f:
        magic = f.read(len(MAGIC_BYTES))
        if magic != MAGIC_BYTES:
            raise BackupVerificationError(f"Invalid backup file magic header: {magic!r}")

        header_len_bytes = f.read(HEADER_LEN_SIZE)
        if len(header_len_bytes) < HEADER_LEN_SIZE:
            raise BackupVerificationError("Truncated header length field.")
        (header_len,) = struct.unpack(HEADER_LEN_STRUCT, header_len_bytes)

        if header_len > 1_000_000:  # 1 MB sanity limit on header
            raise BackupVerificationError(f"Unreasonably large header length ({header_len} bytes).")

        header_bytes = f.read(header_len)
        if len(header_bytes) < header_len:
            raise BackupVerificationError("Truncated header data.")

        try:
            metadata = json.loads(header_bytes.decode("utf-8"))
        except Exception as exc:
            raise BackupVerificationError(f"Failed to parse backup metadata JSON: {exc}") from exc

        ciphertext = f.read()

    # Parse nonce
    nonce_b64 = metadata.get("nonce")
    if not nonce_b64:
        raise BackupVerificationError("Backup metadata missing required 'nonce' field.")
    try:
        nonce = base64.b64decode(nonce_b64)
    except Exception as exc:
        raise BackupVerificationError(f"Invalid base64 nonce in metadata: {exc}") from exc

    return metadata, header_bytes, nonce, ciphertext


def verify_backup_file(backup_file_path: str, key: bytes) -> Dict[str, Any]:
    """
    Verifies that a backup file is authentic, uncorrupted, and valid:
    1. Parses container and metadata.
    2. Decrypts AES-256-GCM ciphertext using key and validates authentication tag.
    3. Verifies SHA-256 checksum matches metadata.
    4. Writes temporary SQLite DB and executes PRAGMA integrity_check and PRAGMA foreign_key_check.
    5. Cleans up temporary file.
    Returns validated metadata dictionary.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise BackupError("AES-256 decryption requires the 'cryptography' library.") from exc

    metadata, header_bytes, nonce, ciphertext = read_backup_container(backup_file_path)

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=header_bytes)
    except InvalidTag as exc:
        raise BackupDecryptionError(
            "Authentication tag verification failed. "
            "Data is corrupted, tampered with, or the wrong encryption key was provided."
        ) from exc
    except Exception as exc:
        raise BackupDecryptionError(f"Decryption failed: {exc}") from exc

    # Check unencrypted SHA-256
    computed_sha = sha256(plaintext).hexdigest()
    expected_sha = metadata.get("source_sha256")
    if computed_sha != expected_sha:
        raise BackupVerificationError(
            f"SHA-256 digest mismatch! Expected {expected_sha}, computed {computed_sha}."
        )

    # Verify SQLite database consistency in a temporary file
    temp_dir = tempfile.mkdtemp(prefix="wlh_verify_")
    temp_db = os.path.join(temp_dir, "verified.db")
    try:
        with open(temp_db, "wb") as f:
            f.write(plaintext)
        verify_database_integrity(temp_db)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return metadata


# ---------------------------------------------------------------------------
# Restore API
# ---------------------------------------------------------------------------

def restore_backup(
    backup_file_path: str,
    target_db_path: str,
    key: bytes,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """
    Restores an encrypted backup into target_db_path:
    1. Destination protection: fails if target exists and overwrite=False.
    2. Decrypts snapshot into an isolated temporary file.
    3. Verifies AEAD tag and SHA-256 checksum.
    4. Runs PRAGMA integrity_check and PRAGMA foreign_key_check on restored DB.
    5. Atomically replaces target_db_path.
    Returns restore summary dict.
    """
    start_time = time.monotonic()

    if os.path.exists(target_db_path) and not overwrite:
        raise BackupError(
            f"Destination database already exists at '{target_db_path}'. "
            "Specify overwrite=True (or pass --overwrite flag) to safely replace."
        )

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise BackupError("AES-256 decryption requires the 'cryptography' library.") from exc

    metadata, header_bytes, nonce, ciphertext = read_backup_container(backup_file_path)

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=header_bytes)
    except InvalidTag as exc:
        raise BackupDecryptionError(
            "Authentication tag verification failed. "
            "Ciphertext is corrupted or wrong key supplied."
        ) from exc
    except Exception as exc:
        raise BackupDecryptionError(f"Decryption failed: {exc}") from exc

    # Check unencrypted SHA-256
    computed_sha = sha256(plaintext).hexdigest()
    expected_sha = metadata.get("source_sha256")
    if computed_sha != expected_sha:
        raise BackupVerificationError(
            f"SHA-256 digest mismatch! Expected {expected_sha}, computed {computed_sha}."
        )

    # Ensure target directory exists
    target_dir = os.path.dirname(os.path.abspath(target_db_path))
    os.makedirs(target_dir, exist_ok=True)

    # Write to temporary file adjacent to target
    temp_restore = target_db_path + ".restoring.tmp"
    try:
        with open(temp_restore, "wb") as f:
            f.write(plaintext)
            f.flush()
            os.fsync(f.fileno())

        # Validate restored database integrity before committing to target path
        verify_database_integrity(temp_restore)

        # Atomic move to target
        os.replace(temp_restore, target_db_path)
    except Exception as exc:
        if os.path.exists(temp_restore):
            try:
                os.unlink(temp_restore)
            except OSError:
                pass
        raise BackupError(f"Restore failed: {exc}") from exc

    duration = time.monotonic() - start_time
    return {
        "status": "success",
        "target_db_path": target_db_path,
        "source_sha256": computed_sha,
        "restored_bytes": len(plaintext),
        "restore_duration_seconds": round(duration, 4),
        "source_metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Retention Policy (Grandfather-Father-Son)
# ---------------------------------------------------------------------------

def parse_backup_timestamp(file_path: str) -> Optional[datetime]:
    """
    Extracts UTC timestamp from backup metadata or filename.
    Filename pattern: wlh_backup_YYYYMMDD_HHMMSS[_microsec].wlhbak
    """
    basename = os.path.basename(file_path)
    # Try parsing from container header
    try:
        with open(file_path, "rb") as f:
            magic = f.read(len(MAGIC_BYTES))
            if magic == MAGIC_BYTES:
                len_bytes = f.read(HEADER_LEN_SIZE)
                if len(len_bytes) == HEADER_LEN_SIZE:
                    (hlen,) = struct.unpack(HEADER_LEN_STRUCT, len_bytes)
                    hbytes = f.read(hlen)
                    meta = json.loads(hbytes.decode("utf-8"))
                    dt_str = meta.get("created_at")
                    if dt_str:
                        return datetime.fromisoformat(dt_str)
    except Exception:
        pass

    # Fallback to filename parsing
    try:
        parts = basename.replace(".wlhbak", "").split("_")
        if len(parts) >= 3 and parts[0] == "wlh" and parts[1] == "backup":
            ts_str = f"{parts[2]}_{parts[3]}"
            dt = datetime.strptime(ts_str[:15], "%Y%m%d_%H%M%S")
            return dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # Fallback to file modification time
    try:
        mtime = os.path.getmtime(file_path)
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except Exception:
        return None


def calculate_retention_plan(
    backup_entries: List[Tuple[str, datetime]],
    max_daily: int = DEFAULT_RETENTION_DAILY,
    max_weekly: int = DEFAULT_RETENTION_WEEKLY,
    max_monthly: int = DEFAULT_RETENTION_MONTHLY,
    reference_time: Optional[datetime] = None,
) -> Tuple[List[str], List[str]]:
    """
    Implements Grandfather-Father-Son retention:
    - Retains all backups within the last 24 hours (grace window).
    - Retains the newest backup for up to max_daily distinct calendar days.
    - Retains the newest backup for up to max_weekly distinct ISO calendar weeks.
    - Retains the newest backup for up to max_monthly distinct calendar months.
    Returns: (kept_file_paths, pruned_file_paths)
    """
    if not backup_entries:
        return [], []

    # Sort descending by timestamp (newest first)
    sorted_entries = sorted(backup_entries, key=lambda x: x[1], reverse=True)
    if reference_time is None:
        reference_time = sorted_entries[0][1]

    keep_paths = set()

    # 1. Backups created within last 24 hours are kept unconditionally
    for path, dt in sorted_entries:
        age_seconds = (reference_time - dt).total_seconds()
        if 0 <= age_seconds <= 86400:
            keep_paths.add(path)

    # 2. Daily bucket: newest backup per calendar day (up to max_daily days)
    daily_buckets: Dict[str, str] = {}
    for path, dt in sorted_entries:
        day_key = dt.strftime("%Y-%m-%d")
        if day_key not in daily_buckets:
            daily_buckets[day_key] = path
            if len(daily_buckets) >= max_daily:
                break
    keep_paths.update(daily_buckets.values())

    # 3. Weekly bucket: newest backup per ISO week (up to max_weekly weeks)
    weekly_buckets: Dict[str, str] = {}
    for path, dt in sorted_entries:
        year, week, _ = dt.isocalendar()
        week_key = f"{year}-W{week:02d}"
        if week_key not in weekly_buckets:
            weekly_buckets[week_key] = path
            if len(weekly_buckets) >= max_weekly:
                break
    keep_paths.update(weekly_buckets.values())

    # 4. Monthly bucket: newest backup per calendar month (up to max_monthly months)
    monthly_buckets: Dict[str, str] = {}
    for path, dt in sorted_entries:
        month_key = dt.strftime("%Y-%m")
        if month_key not in monthly_buckets:
            monthly_buckets[month_key] = path
            if len(monthly_buckets) >= max_monthly:
                break
    keep_paths.update(monthly_buckets.values())

    kept = [p for p, _ in sorted_entries if p in keep_paths]
    pruned = [p for p, _ in sorted_entries if p not in keep_paths]
    return kept, pruned


def prune_backups(
    backup_dir: str,
    max_daily: int = DEFAULT_RETENTION_DAILY,
    max_weekly: int = DEFAULT_RETENTION_WEEKLY,
    max_monthly: int = DEFAULT_RETENTION_MONTHLY,
    dry_run: bool = False,
) -> List[str]:
    """
    Applies the retention policy to backup_dir.
    Safe deletion guarantee: Only deletes files identified as pruned.
    Returns list of pruned file paths.
    """
    if not os.path.isdir(backup_dir):
        return []

    entries: List[Tuple[str, datetime]] = []
    for fname in os.listdir(backup_dir):
        if fname.endswith(".wlhbak") and not fname.endswith(".tmp"):
            fpath = os.path.join(backup_dir, fname)
            dt = parse_backup_timestamp(fpath)
            if dt:
                entries.append((fpath, dt))

    if not entries:
        return []

    kept, to_prune = calculate_retention_plan(
        entries,
        max_daily=max_daily,
        max_weekly=max_weekly,
        max_monthly=max_monthly,
    )

    deleted = []
    for fpath in to_prune:
        if dry_run:
            logger.info(f"[DRY-RUN] Would prune old backup: {fpath}")
            deleted.append(fpath)
        else:
            try:
                os.unlink(fpath)
                logger.info(f"Pruned expired backup: {fpath}")
                deleted.append(fpath)
            except OSError as exc:
                logger.warning(f"Failed to prune {fpath}: {exc}")

    return deleted


# ---------------------------------------------------------------------------
# High-Level Backup Workflow
# ---------------------------------------------------------------------------

def execute_backup(
    source_db_path: str,
    backup_dir: str = DEFAULT_BACKUP_DIR,
    key: Optional[bytes] = None,
    key_target: str = DEFAULT_KEY_TARGET,
    credential_store: Optional[Any] = None,
    max_daily: int = DEFAULT_RETENTION_DAILY,
    max_weekly: int = DEFAULT_RETENTION_WEEKLY,
    max_monthly: int = DEFAULT_RETENTION_MONTHLY,
    pages_per_step: int = DEFAULT_PAGES_PER_STEP,
    sleep_delay: float = DEFAULT_SLEEP_DELAY,
) -> Dict[str, Any]:
    """
    Full backup workflow:
    1. Resolve / obtain 256-bit encryption key.
    2. Online snapshot via SQLite backup API.
    3. Verify snapshot integrity (PRAGMA integrity_check + foreign_key_check).
    4. Compute SHA-256 digest.
    5. Encrypt snapshot with AES-256-GCM.
    6. Verify created backup file (authenticity + integrity).
    7. ONLY AFTER SUCCESSFUL VERIFICATION: prune expired backups.
    Returns backup report dict.
    """
    if not os.path.exists(source_db_path):
        raise BackupError(f"Database file does not exist: {source_db_path}")

    os.makedirs(backup_dir, exist_ok=True)

    # 1. Resolve key
    encryption_key = key or get_or_create_backup_key(target=key_target, credential_store=credential_store)

    # 2. Online snapshot to temporary file
    temp_dir = tempfile.mkdtemp(prefix="wlh_snap_")
    snapshot_path = os.path.join(temp_dir, "snapshot.db")

    try:
        logger.info(f"Initiating online SQLite backup of {source_db_path}...")
        source_sha = create_online_snapshot(
            source_db_path=source_db_path,
            snapshot_path=snapshot_path,
            pages_per_step=pages_per_step,
            sleep_delay=sleep_delay,
        )
        logger.info(f"Snapshot created successfully. SHA-256: {source_sha}")

        # 3. Encrypt to target backup file
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y%m%d_%H%M%S_%f")
        backup_filename = f"wlh_backup_{ts_str}.wlhbak"
        target_backup_path = os.path.join(backup_dir, backup_filename)

        source_name = os.path.basename(source_db_path)
        metadata = encrypt_snapshot_to_file(
            snapshot_path=snapshot_path,
            target_backup_path=target_backup_path,
            key=encryption_key,
            source_db_name=source_name,
            source_sha256=source_sha,
        )
        logger.info(f"Encrypted backup written to {target_backup_path}.")

        # 4. Immediate self-verification
        logger.info("Executing post-backup verification drill...")
        verified_meta = verify_backup_file(target_backup_path, encryption_key)
        logger.info("Post-backup verification PASSED.")

        # 5. SAFE DELETION: Prune retention backups ONLY after verified success
        pruned_files = prune_backups(
            backup_dir=backup_dir,
            max_daily=max_daily,
            max_weekly=max_weekly,
            max_monthly=max_monthly,
        )

        return {
            "status": "success",
            "backup_file": target_backup_path,
            "source_db": source_db_path,
            "source_sha256": source_sha,
            "created_at": verified_meta["created_at"],
            "size_bytes": os.path.getsize(target_backup_path),
            "pruned_count": len(pruned_files),
            "pruned_files": pruned_files,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="Automated SQLite Backup & Restore Utility for WebsiteLead Hunter",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- backup ---
    p_backup = subparsers.add_parser("backup", help="Create an online encrypted SQLite backup")
    p_backup.add_argument("--db-path", default=os.getenv("DATABASE_PATH", "websitelead_hunter.db"), help="Path to source SQLite database")
    p_backup.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="Destination directory for backups")
    p_backup.add_argument("--key", help="Manual 32-byte encryption key (hex or base64)")
    p_backup.add_argument("--key-target", default=DEFAULT_KEY_TARGET, help="Windows Credential Manager target name")
    p_backup.add_argument("--max-daily", type=int, default=DEFAULT_RETENTION_DAILY, help="Daily retention count")
    p_backup.add_argument("--max-weekly", type=int, default=DEFAULT_RETENTION_WEEKLY, help="Weekly retention count")
    p_backup.add_argument("--max-monthly", type=int, default=DEFAULT_RETENTION_MONTHLY, help="Monthly retention count")
    p_backup.add_argument("--pages", type=int, default=DEFAULT_PAGES_PER_STEP, help="Pages per online backup step")
    p_backup.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_DELAY, help="Sleep between online backup steps (seconds)")

    # --- restore ---
    p_restore = subparsers.add_parser("restore", help="Restore and verify an encrypted backup")
    p_restore.add_argument("--backup-file", required=True, help="Path to .wlhbak backup file")
    p_restore.add_argument("--target-db", required=True, help="Destination path for restored SQLite database")
    p_restore.add_argument("--key", help="Manual 32-byte encryption key (hex or base64)")
    p_restore.add_argument("--key-target", default=DEFAULT_KEY_TARGET, help="Windows Credential Manager target name")
    p_restore.add_argument("--overwrite", action="store_true", help="Allow overwriting target database if it exists")

    # --- verify ---
    p_verify = subparsers.add_parser("verify", help="Verify backup integrity, checksum, and authenticity")
    p_verify.add_argument("--backup-file", required=True, help="Path to .wlhbak backup file")
    p_verify.add_argument("--key", help="Manual 32-byte encryption key (hex or base64)")
    p_verify.add_argument("--key-target", default=DEFAULT_KEY_TARGET, help="Windows Credential Manager target name")

    # --- list ---
    p_list = subparsers.add_parser("list", help="List available backups in backup directory")
    p_list.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="Directory containing backups")

    # --- prune ---
    p_prune = subparsers.add_parser("prune", help="Manually prune backups according to retention policy")
    p_prune.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="Directory containing backups")
    p_prune.add_argument("--max-daily", type=int, default=DEFAULT_RETENTION_DAILY, help="Daily retention count")
    p_prune.add_argument("--max-weekly", type=int, default=DEFAULT_RETENTION_WEEKLY, help="Weekly retention count")
    p_prune.add_argument("--max-monthly", type=int, default=DEFAULT_RETENTION_MONTHLY, help="Monthly retention count")
    p_prune.add_argument("--dry-run", action="store_true", help="Show files that would be pruned without deleting")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        if args.subcommand == "backup":
            key_bytes = parse_key_input(args.key) if args.key else None
            result = execute_backup(
                source_db_path=args.db_path,
                backup_dir=args.backup_dir,
                key=key_bytes,
                key_target=args.key_target,
                max_daily=args.max_daily,
                max_weekly=args.max_weekly,
                max_monthly=args.max_monthly,
                pages_per_step=args.pages,
                sleep_delay=args.sleep,
            )
            print(f"[OK] Backup created successfully: {result['backup_file']}")
            print(f"     Source SHA-256 : {result['source_sha256']}")
            print(f"     File Size      : {result['size_bytes']} bytes")
            print(f"     Pruned Old     : {result['pruned_count']} backups")
            return 0

        elif args.subcommand == "restore":
            key_bytes = parse_key_input(args.key) if args.key else get_or_create_backup_key(target=args.key_target)
            result = restore_backup(
                backup_file_path=args.backup_file,
                target_db_path=args.target_db,
                key=key_bytes,
                overwrite=args.overwrite,
            )
            print(f"[OK] Database restored successfully to: {result['target_db_path']}")
            print(f"     Restored Bytes : {result['restored_bytes']} bytes")
            print(f"     Restore Duration: {result['restore_duration_seconds']}s")
            print(f"     Verified SHA   : {result['source_sha256']}")
            return 0

        elif args.subcommand == "verify":
            key_bytes = parse_key_input(args.key) if args.key else get_or_create_backup_key(target=args.key_target)
            meta = verify_backup_file(backup_file_path=args.backup_file, key=key_bytes)
            print(f"[OK] Backup verification PASSED: {args.backup_file}")
            print(f"     Source Database: {meta.get('source_db_name')}")
            print(f"     Created At     : {meta.get('created_at')}")
            print(f"     Source SHA-256 : {meta.get('source_sha256')}")
            print(f"     Source Size    : {meta.get('source_size_bytes')} bytes")
            return 0

        elif args.subcommand == "list":
            if not os.path.isdir(args.backup_dir):
                print(f"Backup directory not found: {args.backup_dir}")
                return 0
            files = [f for f in os.listdir(args.backup_dir) if f.endswith(".wlhbak")]
            print(f"Backups in {args.backup_dir} ({len(files)} total):")
            for f in sorted(files):
                fpath = os.path.join(args.backup_dir, f)
                size = os.path.getsize(fpath)
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc).isoformat()
                print(f" - {f:<38} ({size:>8} bytes) [Modified: {mtime}]")
            return 0

        elif args.subcommand == "prune":
            deleted = prune_backups(
                backup_dir=args.backup_dir,
                max_daily=args.max_daily,
                max_weekly=args.max_weekly,
                max_monthly=args.max_monthly,
                dry_run=args.dry_run,
            )
            mode_str = "[DRY-RUN] Would prune" if args.dry_run else "Pruned"
            print(f"{mode_str} {len(deleted)} backup(s).")
            for p in deleted:
                print(f" - {p}")
            return 0

    except BackupError as exc:
        print(f"[ERROR] Backup operation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("Unexpected error during backup command execution")
        print(f"[CRITICAL] Unexpected error: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
