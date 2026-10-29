"""
Durable Background Job Queue and Standalone Worker for WebsiteLead Hunter.

Implements Milestone 9B & 9C requirements:
- Atomic SQLite conditional job claims with unique worker identities.
- Monotonically increasing lease_version fencing tokens.
- Fenced mutations: validates lease ownership in the same transaction as lead inserts and progress updates.
- Stale worker recovery and prevention of split-brain overwrites after lease expiry.
- Heartbeat renewal loop and cooperative cancellation / pause handling.
- Checkpointed resumption: skips already-persisted leads across restarts without duplicate external calls.
- Non-blocking external HTTP operations: no SQLite write transactions held open during network audits.
- Safe Windows sleep suppression via SetThreadExecutionState with robust fallback and finally restoration.
"""

import asyncio
import json
import logging
import os
import re
import socket
import sys
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Any

from app.database import get_db
from app.discovery.base import DiscoveryProvider, DiscoveryResult, DiscoveredBusiness
from app.discovery.geocoding import NominatimGeocoder
from app.discovery.overpass import OverpassDiscoveryProvider
from app.audit.website_auditor import WebsiteAuditor
from app.scoring.scorer import LeadScorer
from app.pitch.llm_provider import SalesPitchGenerator

logger = logging.getLogger(__name__)

SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_\-]?key\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]{4,}", re.IGNORECASE),
    re.compile(r"(?i)(session[_\-]?token\s*[:=]\s*)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
]


def sanitize_log_message(msg: str) -> str:
    """Masks tokens, bearer headers, and passwords from log and error strings."""
    if not msg:
        return ""
    cleaned = str(msg)
    for pat in SECRET_PATTERNS:
        cleaned = pat.sub(r"\1[REDACTED]", cleaned)
    return cleaned


class DurableWorkerError(Exception):
    """Base exception for durable worker errors."""
    pass


class StaleLeaseError(DurableWorkerError):
    """Raised when a worker has lost its lease due to expiration, preemption, or fencing rejection."""
    pass


class FencingRejectionError(StaleLeaseError):
    """Raised when an atomic database mutation is rejected because lease_version has advanced."""
    pass


class JobCancelledException(DurableWorkerError):
    """Raised when a job has been cancelled by the user."""
    pass


class JobPausedException(DurableWorkerError):
    """Raised when a job has been paused by the user."""
    pass


class WindowsSleepInhibitor:
    """
    Safely manages system execution state on Windows to prevent sleep while jobs run.
    Isolated with fallback for non-Windows platforms or permission errors.
    """
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_AWAYMODE_REQUIRED = 0x00000040

    def __init__(self):
        self._active = False
        self._kernel32 = None
        if sys.platform == "win32":
            try:
                import ctypes
                self._kernel32 = ctypes.windll.kernel32
            except Exception as exc:
                logger.warning(f"Could not load kernel32 for sleep prevention: {exc}")

    def inhibit(self):
        """Prevents sleep while active jobs are executing."""
        if self._kernel32 and not self._active:
            try:
                flags = self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_AWAYMODE_REQUIRED
                res = self._kernel32.SetThreadExecutionState(flags)
                if res != 0:
                    self._active = True
                    logger.debug("Windows sleep inhibitor activated.")
            except Exception as exc:
                logger.warning(f"Failed to set thread execution state: {exc}")

    def restore(self):
        """Restores normal sleep state when jobs complete or pause."""
        if self._kernel32 and self._active:
            try:
                self._kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
                self._active = False
                logger.debug("Windows sleep inhibitor restored.")
            except Exception as exc:
                logger.warning(f"Failed to restore thread execution state: {exc}")


class DurableJobWorker:
    """
    Standalone, crash-resilient background worker for WebsiteLead Hunter.
    Communicates strictly through the SQLite database using atomic claims,
    heartbeats, and lease-fenced mutations.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        lease_duration_sec: int = 30,
        heartbeat_interval_sec: float = 10.0,
        geocoder: Optional[NominatimGeocoder] = None,
        discovery_provider: Optional[DiscoveryProvider] = None,
        auditor: Optional[WebsiteAuditor] = None,
        scorer: Optional[LeadScorer] = None,
        pitch_gen: Optional[SalesPitchGenerator] = None,
        max_parallel_audits: int = 4,
    ):
        self.worker_id = worker_id or f"worker_{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.lease_duration_sec = lease_duration_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec

        self.geocoder = geocoder or NominatimGeocoder()
        self.discovery_provider = discovery_provider or OverpassDiscoveryProvider()
        self.auditor = auditor or WebsiteAuditor()
        self.scorer = scorer or LeadScorer()
        self.pitch_gen = pitch_gen or SalesPitchGenerator()
        self.semaphore = asyncio.Semaphore(max_parallel_audits)

        self.sleep_inhibitor = WindowsSleepInhibitor()
        self._stop_requested = False
        self._current_job_id: Optional[str] = None
        self._current_lease_version: int = 0

    def _append_job_log(self, job_id: str, message: str, level: str = "info", lease_version: Optional[int] = None):
        """Safely appends a sanitized log entry to the job's JSON logs array."""
        now = datetime.now(timezone.utc).isoformat()
        clean_msg = sanitize_log_message(message)
        try:
            with get_db() as conn:
                row = conn.execute("SELECT logs, lease_version FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row:
                    # If lease_version provided, verify ownership before logging
                    if lease_version is not None and row["lease_version"] != lease_version:
                        return
                    logs = json.loads(row["logs"] or "[]")
                    logs.append({"timestamp": now, "level": level, "message": clean_msg})
                    if len(logs) > 100:
                        logs = logs[-100:]
                    conn.execute(
                        "UPDATE jobs SET logs = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(logs), now, job_id),
                    )
        except Exception as exc:
            logger.error(f"Error logging to job {job_id}: {exc}")

    def claim_next_job(self) -> Optional[dict]:
        """
        Atomically claims the next pending job or abandoned job with an expired lease.
        Uses SQLite conditional UPDATE with RETURNING to guarantee single-worker ownership.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires = (now_dt + timedelta(seconds=self.lease_duration_sec)).isoformat()

        with get_db() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET 
                    status = CASE
                        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) > MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) THEN 'failed'
                        ELSE 'running'
                    END,
                    error_message = CASE
                        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) > MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) 
                            THEN 'Job failed: Exceeded maximum lease recovery retry attempts (' || MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) || ').'
                        WHEN status = 'pending' THEN NULL
                        ELSE error_message
                    END,
                    retry_count = CASE
                        WHEN status = 'running' AND (COALESCE(retry_count, 0) + 1) <= MAX(0, CAST(COALESCE(max_retries, 3) AS INTEGER)) 
                            THEN COALESCE(retry_count, 0) + 1
                        ELSE COALESCE(retry_count, 0)
                    END,
                    worker_id = ?,
                    lease_version = lease_version + 1,
                    heartbeat_at = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'pending'
                       OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                )
                RETURNING id, user_id, status, location_query, lat, lon, radius_km,
                          categories, max_leads, total_found, processed_count,
                          logs, worker_id, lease_version, lease_expires_at,
                          retry_count, max_retries, checkpoint_data, error_message;
                """,
                (self.worker_id, now, lease_expires, now, now),
            )
            row = cursor.fetchone()
            if not row:
                return None

            job = dict(row)

            # Safely parse max_retries for logging
            try:
                max_retries = max(0, int(job["max_retries"])) if job.get("max_retries") is not None else 3
            except (ValueError, TypeError):
                max_retries = 3

            # Check if job exceeded retry limits and was marked failed by the atomic claim query
            if job["status"] == "failed":
                self._append_job_log(
                    job["id"],
                    f"Job marked failed: exceeded maximum retries ({max_retries}).",
                    "error",
                    lease_version=job["lease_version"],
                )
                logger.warning(f"Job {job['id']} exceeded max retries ({max_retries}); marked failed.")
                return None

            # Distinguish recovery attempt from initial claim:
            # If retry_count > 0, this claim was an expired lease recovery attempt
            if job["retry_count"] > 0:
                self._append_job_log(
                    job["id"],
                    f"Job recovered by worker {self.worker_id} (lease v{job['lease_version']}, retry #{job['retry_count']}).",
                    "warning",
                    lease_version=job["lease_version"],
                )
            else:
                self._append_job_log(
                    job["id"],
                    f"Job claimed by worker {self.worker_id} (lease v{job['lease_version']}).",
                    "info",
                    lease_version=job["lease_version"],
                )

            return job

    def renew_lease(self, job_id: str, lease_version: int) -> bool:
        """
        Renews the lease and heartbeat timestamp for the claimed job.
        Strictly conditioned on worker_id, lease_version, and status == 'running'.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        new_lease = (now_dt + timedelta(seconds=self.lease_duration_sec)).isoformat()

        with get_db() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET heartbeat_at = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND worker_id = ?
                  AND lease_version = ?
                  AND status = 'running'
                """,
                (now, new_lease, now, job_id, self.worker_id, lease_version),
            )
            return cur.rowcount > 0

    def persist_lead_fenced(
        self,
        job_id: str,
        lease_version: int,
        biz: DiscoveredBusiness,
        audit_result,
        score,
        pitches,
        processed_count: int,
        user_id: Optional[str] = None,
    ):
        """
        Persists an audited lead and updates job processed count within a SINGLE transaction.
        Validates lease ownership at the start of the transaction.
        If the worker has lost ownership or lease_version changed, raises FencingRejectionError
        and rolls back the transaction, preventing stale writes.
        """
        now = datetime.now(timezone.utc).isoformat()
        lead_id = str(uuid.uuid4())
        pipeline_status = "qualified" if getattr(score, "total_score", 0) >= 50 else "new"

        with get_db() as conn:
            # 1. Atomic lease verification inside the transaction
            row = conn.execute(
                "SELECT lease_version, status, worker_id FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

            if not row or row["worker_id"] != self.worker_id or row["lease_version"] != lease_version or row["status"] != "running":
                cur_worker = row["worker_id"] if row else None
                cur_v = row["lease_version"] if row else None
                cur_status = row["status"] if row else None
                raise FencingRejectionError(
                    f"Fencing Rejection: Worker {self.worker_id} (v{lease_version}) attempted mutation for job {job_id}, "
                    f"but active state is worker={cur_worker}, lease_version={cur_v}, status={cur_status}."
                )

            # 2. Insert or update lead
            conn.execute(
                """
                INSERT INTO leads (
                    id, user_id, job_id, provider_id, name, category, address, lat, lon,
                    phone, website, opening_hours, source_url, provenance, raw_data,
                    audit_result, score, score_breakdown, pitches, pipeline_status,
                    notes, contacted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, provider_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    audit_result = excluded.audit_result,
                    score = excluded.score,
                    score_breakdown = excluded.score_breakdown,
                    pitches = excluded.pitches,
                    pipeline_status = excluded.pipeline_status,
                    updated_at = excluded.updated_at
                """,
                (
                    lead_id,
                    user_id,
                    job_id,
                    biz.provider_id,
                    biz.name,
                    biz.category,
                    biz.address,
                    biz.lat,
                    biz.lon,
                    biz.phone,
                    biz.website,
                    biz.opening_hours,
                    biz.source_url,
                    json.dumps({k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in (biz.provenance or {}).items()}),
                    json.dumps(biz.raw_data or {}),
                    json.dumps(audit_result.model_dump() if audit_result else {}),
                    getattr(score, "total_score", 0),
                    json.dumps(score.model_dump() if score else {}),
                    json.dumps(pitches.model_dump() if pitches else {}),
                    pipeline_status,
                    None,
                    None,
                    now,
                    now,
                ),
            )

            # 3. Update job progress conditioned on worker_id and lease_version
            cur = conn.execute(
                """
                UPDATE jobs
                SET processed_count = ?, updated_at = ?
                WHERE id = ? AND worker_id = ? AND lease_version = ? AND status = 'running'
                """,
                (processed_count, now, job_id, self.worker_id, lease_version),
            )
            if cur.rowcount == 0:
                raise FencingRejectionError(
                    f"Fencing Rejection: Progress update for job {job_id} rejected because lease_version changed."
                )

    def update_checkpoint(self, job_id: str, lease_version: int, checkpoint_dict: dict):
        """Updates checkpoint data JSON for a job, fenced by lease_version."""
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET checkpoint_data = ?, updated_at = ?
                WHERE id = ? AND worker_id = ? AND lease_version = ? AND status = 'running'
                """,
                (json.dumps(checkpoint_dict), now, job_id, self.worker_id, lease_version),
            )
            if cur.rowcount == 0:
                raise FencingRejectionError(f"Checkpoint update rejected for job {job_id}: lease expired or preempted.")

    async def _heartbeat_task(self, job_id: str, lease_version: int, stop_event: asyncio.Event, lease_lost_event: asyncio.Event):
        """Background coroutine that periodically sends heartbeats until stopped."""
        while not stop_event.is_set():
            try:
                await asyncio.sleep(self.heartbeat_interval_sec)
                if stop_event.is_set():
                    break
                renewed = self.renew_lease(job_id, lease_version)
                if not renewed:
                    logger.warning(f"Heartbeat failed: Worker {self.worker_id} lost lease on job {job_id} (v{lease_version})")
                    lease_lost_event.set()
                    break
            except Exception as exc:
                logger.error(f"Error in heartbeat loop for job {job_id}: {exc}")

    async def process_claimed_job(self, job: dict):
        """
        Executes a claimed job to completion or clean pause/cancellation/failure.
        Guarantees that external network operations occur outside write transactions.
        """
        job_id = job["id"]
        lease_version = job["lease_version"]
        user_id = job["user_id"]
        location_query = job["location_query"]
        radius_km = float(job["radius_km"])
        categories = json.loads(job["categories"] or "[]")
        max_leads = int(job["max_leads"])
        lat = float(job["lat"])
        lon = float(job["lon"])

        self._current_job_id = job_id
        self._current_lease_version = lease_version

        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        hb_task = asyncio.create_task(self._heartbeat_task(job_id, lease_version, stop_heartbeat, lease_lost))

        self.sleep_inhibitor.inhibit()
        try:
            # 1. Check for cancellation / pause before beginning discovery
            self._check_external_control(job_id, lease_version, lease_lost)

            # 2. Checkpoint recovery check: Did we already have candidates checkpointed?
            candidates: List[DiscoveredBusiness] = []
            checkpoint_raw = json.loads(job.get("checkpoint_data") or "{}")
            if isinstance(checkpoint_raw, list):
                checkpoint_data = {"candidates": checkpoint_raw}
            elif isinstance(checkpoint_raw, dict):
                checkpoint_data = checkpoint_raw
            else:
                checkpoint_data = {}

            if checkpoint_data.get("candidates"):
                # Recover candidates from checkpoint
                for raw in checkpoint_data["candidates"]:
                    candidates.append(DiscoveredBusiness(**raw))
                self._append_job_log(
                    job_id,
                    f"Resumed from checkpoint: loaded {len(candidates)} discovered businesses.",
                    "info",
                    lease_version=lease_version,
                )
            else:
                self._append_job_log(
                    job_id,
                    f"Querying OpenStreetMap Overpass for {', '.join(categories)} within {radius_km}km...",
                    "info",
                    lease_version=lease_version,
                )
                # Network discovery occurs strictly OUTSIDE any SQLite write transaction
                discovery_result = await self.discovery_provider.discover(
                    lat=lat,
                    lon=lon,
                    radius_km=radius_km,
                    categories=categories,
                    max_leads=max_leads * 2,
                )

                self._check_external_control(job_id, lease_version, lease_lost)

                if discovery_result.error_message:
                    safe_err = sanitize_log_message(discovery_result.error_message)
                    now = datetime.now(timezone.utc).isoformat()
                    with get_db() as conn:
                        conn.execute(
                            """
                            UPDATE jobs
                            SET status = 'failed', error_message = ?, updated_at = ?
                            WHERE id = ? AND worker_id = ? AND lease_version = ?
                            """,
                            (safe_err, now, job_id, self.worker_id, lease_version),
                        )
                    self._append_job_log(job_id, f"Discovery failed: {safe_err}", "error", lease_version=lease_version)
                    return

                candidates = discovery_result.businesses
                total_found = len(candidates)

                # Save candidates to checkpoint and update total_found
                candidate_dicts = [b.model_dump() for b in candidates]
                self.update_checkpoint(job_id, lease_version, {"candidates": candidate_dicts})

                now = datetime.now(timezone.utc).isoformat()
                with get_db() as conn:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET total_found = ?, updated_at = ?
                        WHERE id = ? AND worker_id = ? AND lease_version = ? AND status = 'running'
                        """,
                        (total_found, now, job_id, self.worker_id, lease_version),
                    )

                self._append_job_log(
                    job_id,
                    f"Discovered {total_found} businesses. Starting audits...",
                    "info",
                    lease_version=lease_version,
                )

            # 3. Check for already-persisted leads (Checkpoint Resumption & Duplicate Elimination)
            with get_db() as conn:
                existing_rows = conn.execute(
                    "SELECT provider_id FROM leads WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
                persisted_provider_ids = {r[0] for r in existing_rows}

            # Filter candidates up to max_leads
            processed_count = len(persisted_provider_ids)
            candidates_to_process = candidates[:max_leads]

            # 4. Audit loop
            for biz in candidates_to_process:
                # Cooperative control and fencing check
                self._check_external_control(job_id, lease_version, lease_lost)

                # Skip if already audited and saved in a previous attempt
                if biz.provider_id in persisted_provider_ids:
                    continue

                # External HTTP audit and pitch generation occurs OUTSIDE write transaction
                async with self.semaphore:
                    audit_result = await self.auditor.audit(
                        url=biz.website,
                        business_name=biz.name,
                        category=biz.category,
                    )
                    score = self.scorer.calculate_score(biz, audit_result)
                    pitches = await self.pitch_gen.generate(biz, audit_result, score)

                # Check fencing once more before writing
                self._check_external_control(job_id, lease_version, lease_lost)

                processed_count += 1

                # Persist lead and update processed_count in atomic, fenced transaction
                self.persist_lead_fenced(
                    job_id=job_id,
                    lease_version=lease_version,
                    biz=biz,
                    audit_result=audit_result,
                    score=score,
                    pitches=pitches,
                    processed_count=processed_count,
                    user_id=user_id,
                )
                persisted_provider_ids.add(biz.provider_id)

            # 5. Mark job completed
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'completed', updated_at = ?
                    WHERE id = ? AND worker_id = ? AND lease_version = ? AND status = 'running'
                    """,
                    (now, job_id, self.worker_id, lease_version),
                )
                if cur.rowcount == 0:
                    raise FencingRejectionError(f"Job {job_id} completion rejected: worker lost lease.")

            self._append_job_log(
                job_id,
                f"Job completed successfully. Audited and saved {processed_count} qualified leads.",
                "info",
                lease_version=lease_version,
            )

        except FencingRejectionError as f_err:
            logger.warning(f"Worker {self.worker_id} halted due to fencing rejection on job {job_id}: {f_err}")
            # Do NOT overwrite database status; a newer worker or user state already owns the job.
        except JobCancelledException:
            logger.info(f"Worker {self.worker_id} observed cancellation on job {job_id}.")
            self._append_job_log(job_id, "Processing cancelled by user.", "warning", lease_version=lease_version)
        except JobPausedException:
            logger.info(f"Worker {self.worker_id} observed pause on job {job_id}; releasing lease.")
            # Set status to paused if running, and clear worker lease so it can be resumed
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'paused', worker_id = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND worker_id = ? AND lease_version = ? AND status IN ('running', 'paused')
                    """,
                    (now, job_id, self.worker_id, lease_version),
                )
            self._append_job_log(job_id, "Job paused; lease released.", "warning", lease_version=lease_version)
        except Exception as exc:
            logger.exception(f"Unhandled error in worker {self.worker_id} for job {job_id}")
            safe_err = sanitize_log_message(str(exc))
            now = datetime.now(timezone.utc).isoformat()
            try:
                with get_db() as conn:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', error_message = ?, updated_at = ?
                        WHERE id = ? AND worker_id = ? AND lease_version = ? AND status = 'running'
                        """,
                        (safe_err, now, job_id, self.worker_id, lease_version),
                    )
                self._append_job_log(job_id, f"Fatal error: {safe_err}", "error", lease_version=lease_version)
            except Exception as db_exc:
                logger.error(f"Failed to record failure status for job {job_id}: {db_exc}")
        finally:
            stop_heartbeat.set()
            hb_task.cancel()
            self.sleep_inhibitor.restore()
            self._current_job_id = None
            self._current_lease_version = 0

    def _check_external_control(self, job_id: str, lease_version: int, lease_lost: asyncio.Event):
        """Checks for stop request, lost lease, or cooperative pause/cancel in database."""
        if self._stop_requested:
            raise JobPausedException("Worker shutdown requested; pausing execution.")

        if lease_lost.is_set():
            raise StaleLeaseError(f"Worker {self.worker_id} lost heartbeat lease on job {job_id}")

        with get_db() as conn:
            row = conn.execute("SELECT status, worker_id, lease_version FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise StaleLeaseError(f"Job {job_id} deleted from database.")
            if row["worker_id"] != self.worker_id or row["lease_version"] != lease_version:
                raise FencingRejectionError(f"Fencing check failed: active lease is v{row['lease_version']}, mine is v{lease_version}.")
            if row["status"] == "cancelled":
                raise JobCancelledException(f"Job {job_id} was cancelled.")
            if row["status"] == "paused":
                raise JobPausedException(f"Job {job_id} was paused.")

    def run_once(self) -> bool:
        """
        Executes a single polling iteration.
        Returns True if a job was claimed and executed; False if queue was empty.
        """
        job = self.claim_next_job()
        if not job:
            return False
        asyncio.run(self.process_claimed_job(job))
        return True

    async def run_daemon(self, poll_interval_sec: float = 2.0):
        """Continuous polling loop for the standalone worker daemon."""
        logger.info(f"DurableJobWorker daemon started [worker_id: {self.worker_id}]. Polling every {poll_interval_sec}s...")
        while not self._stop_requested:
            try:
                job = self.claim_next_job()
                if job:
                    await self.process_claimed_job(job)
                else:
                    await asyncio.sleep(poll_interval_sec)
            except Exception as exc:
                logger.error(f"Error in daemon polling loop: {exc}")
                await asyncio.sleep(poll_interval_sec)
        logger.info(f"DurableJobWorker daemon {self.worker_id} stopped cleanly.")

    def request_stop(self):
        """Signals the daemon to stop after completing or pausing the current iteration."""
        self._stop_requested = True
