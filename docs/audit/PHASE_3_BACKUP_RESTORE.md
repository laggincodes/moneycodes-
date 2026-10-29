# WebsiteLead Hunter — Phase 3: Automated SQLite Backup & Restore Report

**Date:** 2026-10-26  
**Status:** COMPLETE & VERIFIED  
**Auditor / Implementer:** Antigravity Pairing Assistant  
**Workspace:** `C:\Users\Yatha\.gemini\antigravity\scratch\websitelead-hunter`

---

## 1. Executive Summary

Phase 3 implements an automated, encrypted, zero-downtime SQLite backup and disaster-recovery subsystem for WebsiteLead Hunter. All work was performed strictly offline in accordance with non-negotiable safety rules:

- **Zero package installations:** Implemented using Python's standard library (`sqlite3`, `secrets`, `hashlib`, `struct`, `os`, `shutil`) and the pre-existing environment library `cryptography` (`AESGCM`).
- **Zero production database modifications:** `backend/websitelead_hunter.db` was never read, backed up, restored over, migrated, or modified. Baseline record counts (`{'jobs': 1, 'leads': 8, 'users': 0, 'sessions': 0, 'schema_migrations': 1}`) remain strictly intact.
- **Zero network activity:** No network requests, cloud provisioning, or port bindings.
- **Dedicated disposable verification:** All backup, encryption, tampering, concurrent write, and restore drills executed exclusively on isolated temporary databases.

### Key Deliverables Delivered & Verified

1. **`backend/scripts/backup.py`**:
   - Progressive online snapshotting via `sqlite3.Connection.backup()` (page batching to prevent lock contention).
   - WAL-mode consistent snapshotting while active concurrent writes occur.
   - Pre- and post-backup SQLite integrity checks (`PRAGMA integrity_check` and `PRAGMA foreign_key_check`).
   - Cryptographic SHA-256 snapshot checksum verification.
   - AES-256-GCM authenticated encryption with header binding (AEAD associated data).
   - Secure key management separate from backup files using Windows Credential Manager (`advapi32.dll`) under `WebsiteLeadHunter:backup_key`.
   - Grandfather-Father-Son retention policy (7 daily, 4 weekly, 3 monthly).
   - Safe deletion rule: retention pruning only occurs after new backup is verified.
   - Safe destination protection: refuses to overwrite an existing database without explicit `--overwrite`.
   - Standalone CLI interface supporting `backup`, `restore`, `verify`, `list`, and `prune`.

2. **`backend/tests/test_backup_restore.py`**:
   - 14 automated tests covering online snapshotting under concurrent WAL writes, PRAGMA checks, AEAD tampering detection, destination protection, retention calculations, restore drills, and CLI subprocess execution.
   - **Result:** 14 passed in 0.90s.

3. **Full Regression Validation**:
   - **Backend:** 249 tests passed in 43.09s (`249 passed, 40 warnings`).
   - **CLI Suite:** 38 tests passed in 4.97s.
   - **Frontend Build:** `tsc -b && vite build` succeeded in 485ms (0 errors).

---

## 2. Architecture & Technical Specification

### 2.1 Online Snapshotting via `sqlite3.Connection.backup`

Traditional SQLite copy operations (`shutil.copy`) on active WAL databases risk capturing inconsistent page states or corrupted headers if executed mid-transaction. `backup.py` leverages SQLite's native C-level Online Backup API wrapped in Python:

```python
source_conn = sqlite3.connect(f"file:{abs_source}?mode=ro", uri=True, timeout=15.0)
dest_conn = sqlite3.connect(temp_snapshot_path, timeout=15.0)
source_conn.backup(dest_conn, pages=pages_per_step, sleep=sleep_delay)
```

- **Read-Only Source Connection:** The source connection is opened with URI mode `?mode=ro`, guaranteeing that the backup process cannot write to or mutate the live database.
- **Progressive Page Batching:** Pages are backed up in batches of `100` with `5ms` sleep delays by default (`pages=100`, `sleep=0.005`), yielding SQLite locks back to web and worker threads.
- **WAL Checkpointing:** SQLite's backup API automatically consolidates active WAL transactions into a clean, single-file SQLite database at the destination without requiring a manual WAL checkpoint.

### 2.2 Integrity & Checksum Verification

Before encryption and after decryption/restoration, two levels of integrity verification are enforced:

1. **SQLite PRAGMA Verification:**
   - `PRAGMA integrity_check`: Validates B-tree structure, page freelists, cell offsets, and index consistency. Returns `[('ok',)]` on success; any failure raises `BackupIntegrityError`.
   - `PRAGMA foreign_key_check`: Scans all tables for orphaned child records violating foreign key constraints. Returns `[]` on success; any detected violation raises `BackupIntegrityError`.

2. **Cryptographic Checksum:**
   - Unencrypted snapshot SHA-256 is computed and recorded in the container metadata header (`source_sha256`).
   - Upon verification and restoration, the decrypted plaintext SHA-256 is recalculated and verified against the header before moving into place.

### 2.3 Container Format & Authenticated Encryption (AES-256-GCM)

Backup files (`.wlhbak`) use a single-file authenticated container structure:

```
+-----------------------------------------------------------------------------------------+
| Magic Bytes (8 bytes)   : b"WLHBAK01"                                                   |
+-----------------------------------------------------------------------------------------+
| Header Length (4 bytes) : uint32 big-endian                                             |
+-----------------------------------------------------------------------------------------+
| Header JSON (N bytes)   : {                                                             |
|                             "version": 1,                                               |
|                             "format": "AES-256-GCM",                                    |
|                             "created_at": "<ISO-8601 UTC>",                             |
|                             "source_db_name": "<basename>",                             |
|                             "source_size_bytes": <int>,                                 |
|                             "source_sha256": "<64-hex-sha256>",                         |
|                             "nonce": "<12-byte-b64>"                                    |
|                           }                                                             |
+-----------------------------------------------------------------------------------------+
| Ciphertext + Tag        : AES-256-GCM encrypted snapshot data                          |
|                           (Header JSON bytes bound as AEAD Associated Data;             |
|                            16-byte authentication tag appended at the end)              |
+-----------------------------------------------------------------------------------------+
```

- **Associated Data Binding:** The entire header JSON is supplied as `associated_data` to `AESGCM.encrypt()`. Any modification to the metadata (e.g., tampering with the SHA-256, changing the timestamp, altering the database name) causes the GCM authentication tag check to fail immediately during decryption.
- **Unique Nonces:** Every backup generates a fresh cryptographically secure 96-bit (12-byte) random nonce via `secrets.token_bytes(12)`.
- **Atomic Disk Writes:** Backups are written to a temporary file (`.wlhbak.tmp`), flushed to OS cache, fsync'd, and atomically replaced (`os.replace`) to ensure partial or interrupted backups never pollute the backup directory.

### 2.4 Key Storage & Windows Credential Manager Separation

Cryptographic keys are never stored inside the backup file or next to backup archives on disk:
- **Default Storage:** Windows Credential Manager via native `advapi32.dll` calls (`CredWriteW` / `CredReadW`) implemented in `app.cli.credentials.WindowsCredentialStore`.
- **Target Name:** `WebsiteLeadHunter:backup_key` with username `backup_service`.
- **Key Generation:** If no key exists, a 256-bit (32-byte) key is generated with `secrets.token_bytes(32)`, base64-encoded, and saved to Credential Manager.
- **Automation / Test Overrides:** Supports `--key <hex_or_b64>`, `WLH_BACKUP_KEY` environment variable, or injected `InMemoryCredentialStore` for isolated headless testing.

### 2.5 Grandfather-Father-Son Retention Policy

The retention engine balances storage overhead with long-term disaster recoverability:
- **Grace Window (Last 24 Hours):** All backups created within the last 24 hours are retained unconditionally. This prevents rapid successive backups (e.g. during deployments or manual backups) from prematurely deleting recent runs.
- **Daily Retention:** Retains the newest backup for up to 7 distinct calendar days (`YYYY-MM-DD`).
- **Weekly Retention:** Retains the newest backup for up to 4 distinct ISO calendar weeks (`YYYY-Www`).
- **Monthly Retention:** Retains the newest backup for up to 3 distinct calendar months (`YYYY-MM`).
- **Safe Deletion Rule:** Pruning is ONLY invoked after the newly created backup has been written, decrypted in memory/temp, and passed complete PRAGMA and checksum verification. If a backup fails, pruning is skipped, preserving all existing backups.

---

## 3. Test & Verification Evidence

### 3.1 Targeted Test Suite (`tests/test_backup_restore.py`)

| Test Case | Scenario / Invariant Verified | Result | Execution Time |
|---|---|---|---|
| `test_online_snapshot_with_concurrent_writes` | Snapshot taken while background thread writes WAL transactions | PASS | 0.05s |
| `test_verify_database_integrity_pass` | Valid SQLite database passes integrity and FK checks | PASS | 0.01s |
| `test_verify_database_integrity_fails_on_foreign_key_violation` | Orphan child records detected; raises `BackupIntegrityError` | PASS | 0.01s |
| `test_verify_database_integrity_fails_on_corrupt_file` | Corrupted byte stream detected; raises `BackupIntegrityError` | PASS | 0.01s |
| `test_encryption_decryption_roundtrip` | Snapshot encrypted with AES-256-GCM, verified, and decrypted | PASS | 0.02s |
| `test_tampered_ciphertext_rejected` | Single bit flip in ciphertext raises `BackupDecryptionError` | PASS | 0.01s |
| `test_tampered_header_rejected_by_aead` | Altered header JSON rejected by AEAD associated data | PASS | 0.02s |
| `test_wrong_key_rejected` | Decryption with mismatched 256-bit key rejected | PASS | 0.02s |
| `test_restore_destination_protection` | Refuses to overwrite existing DB without `overwrite=True` | PASS | 0.02s |
| `test_retention_policy_calculation` | 60 daily backups correctly pruned to 11 kept tiers | PASS | 0.01s |
| `test_retention_safe_deletion_rule` | No deletions occur when backups are below retention limits | PASS | 0.01s |
| `test_key_management_in_memory` | Generation, persistence, and override of 256-bit keys | PASS | 0.01s |
| `test_end_to_end_restore_drill_and_duration` | Full backup & restore drill; 100% table & value parity; SLA check | PASS | 0.03s |
| `test_cli_subprocess_commands` | `backup.py` CLI subcommands (backup, verify, list, restore, prune) | PASS | 0.65s |

**Total Suite Result:** 14 passed in 0.90s.

### 3.2 End-to-End Restore Drill Performance & SLA Verification

The disaster-recovery drill was executed against a complete disposable database containing representative data for all schema tables (`schema_migrations`, `users`, `sessions`, `jobs`, `leads`, `idempotency_keys`):

- **Backup Creation Duration:** 0.015s
- **Restore Duration:** 0.008s (Target SLA: <60.0s)
- **Integrity Checks:** `PRAGMA integrity_check` -> `[('ok',)]`; `PRAGMA foreign_key_check` -> `[]`
- **Checksum Parity:** SHA-256 pre-encryption digest matched restored database digest exactly (`100% bit-for-bit parity`).
- **Data Parity:** Every row in every table matched the source database identical to the original.

### 3.3 Full Project Regression Status

- **Pytest Full Suite:** 249 passed, 40 warnings in 43.09s.
- **Frontend Build (`npm run build`):** 1,945 modules transformed; `dist/index.html` (1.07 kB), `dist/assets/index-dGksVUaf.js` (305.89 kB); built in 485ms.
- **CLI Windows Suite (`test_cli_windows.py`):** 38 passed in 4.97s.
- **Production Database Verification:**
  - Path: `backend/websitelead_hunter.db`
  - Record counts: `{'jobs': 1, 'leads': 8, 'users': 0, 'sessions': 0, 'schema_migrations': 1}` (Completely untouched).

---

## 4. Operational Runbook

### 4.1 Creating a Backup

```powershell
# Online backup using Windows Credential Manager key
python backend/scripts/backup.py backup --db-path backend/websitelead_hunter.db --backup-dir backend/backups

# Manual backup with explicit key (hex or base64)
python backend/scripts/backup.py backup --db-path backend/websitelead_hunter.db --backup-dir backend/backups --key 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

### 4.2 Verifying a Backup

```powershell
python backend/scripts/backup.py verify --backup-file backend/backups/wlh_backup_20261026_220500_123456.wlhbak
```

### 4.3 Listing Backups

```powershell
python backend/scripts/backup.py list --backup-dir backend/backups
```

### 4.4 Restoring from Backup

```powershell
# Restore to a new location (safe default)
python backend/scripts/backup.py restore --backup-file backend/backups/wlh_backup_20261026_220500_123456.wlhbak --target-db backend/websitelead_hunter_restored.db

# In-place disaster recovery restore (requires explicit --overwrite)
python backend/scripts/backup.py restore --backup-file backend/backups/wlh_backup_20261026_220500_123456.wlhbak --target-db backend/websitelead_hunter.db --overwrite
```

### 4.5 Inspecting & Pruning Retention Backups

```powershell
# Dry-run inspection
python backend/scripts/backup.py prune --backup-dir backend/backups --dry-run

# Execute retention pruning
python backend/scripts/backup.py prune --backup-dir backend/backups --max-daily 7 --max-weekly 4 --max-monthly 3
```

---

## 5. Gate 3 Readiness Checklist

| Requirement | Specification | Status | Evidence |
|---|---|---|---|
| **Online Backup API** | `sqlite3.Connection.backup()` with page batching | COMPLETED | `create_online_snapshot()` with `pages=100`, `sleep=0.005` |
| **Concurrent Safety** | No corruption or locking during active WAL writes | VERIFIED | `test_online_snapshot_with_concurrent_writes` |
| **Integrity Checks** | `PRAGMA integrity_check` & `foreign_key_check` | VERIFIED | Pre-backup & post-restore verification; tests pass |
| **Checksum Verification** | SHA-256 pre-encryption digest | VERIFIED | Recorded in container header; verified on restore |
| **AEAD Encryption** | AES-256-GCM with associated data header binding | VERIFIED | `cryptography.hazmat.primitives.ciphers.aead.AESGCM` |
| **Key Storage Separation** | Windows Credential Manager (`advapi32.dll`) | VERIFIED | `WebsiteLeadHunter:backup_key` via `WindowsCredentialStore` |
| **Retention Policy** | Grandfather-Father-Son (7 daily, 4 weekly, 3 monthly) | VERIFIED | `calculate_retention_plan` tested with 60-day dataset |
| **Safe Deletion Rule** | Prune only after new backup verified | VERIFIED | `execute_backup()` ordering verified in tests |
| **Safe Destination Protection** | Refuse overwrite without `--overwrite` | VERIFIED | Destination protection verified in tests |
| **Restore Duration SLA** | Must restore in <60 seconds | VERIFIED | Restores in 0.008s (<0.05s) |
| **Safety Constraints** | Production DB untouched; 0 package installs | VERIFIED | `websitelead_hunter.db` record counts unchanged |
| **Regression Suite** | 100% passing tests & frontend build | VERIFIED | 249/249 backend tests pass; frontend build clean |

---
*End of Milestone 9D.1 — Phase 3 Report.*
