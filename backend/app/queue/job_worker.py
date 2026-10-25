"""
Persistent Background Job Queue and Worker.
Handles discovery, auditing, scoring, pitch generation, pause/resume/cancel controls,
and persists all state into SQLite.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from app.database import get_db
from app.discovery.base import DiscoveryProvider, DiscoveredBusiness
from app.discovery.geocoding import NominatimGeocoder
from app.discovery.overpass import OverpassDiscoveryProvider
from app.audit.website_auditor import WebsiteAuditor
from app.scoring.scorer import LeadScorer
from app.pitch.llm_provider import SalesPitchGenerator
from app.schemas import JobResponse, JobLogEntry

logger = logging.getLogger(__name__)


class JobWorker:
    """
    Manages background execution of client acquisition jobs.
    Supports pause, resume, cancel, real-time logging, and database recovery.
    """

    def __init__(
        self,
        geocoder: Optional[NominatimGeocoder] = None,
        discovery_provider: Optional[DiscoveryProvider] = None,
        auditor: Optional[WebsiteAuditor] = None,
        scorer: Optional[LeadScorer] = None,
        pitch_gen: Optional[SalesPitchGenerator] = None,
        max_parallel_audits: int = 4,
    ):
        self.geocoder = geocoder or NominatimGeocoder()
        self.discovery_provider = discovery_provider or OverpassDiscoveryProvider()
        self.auditor = auditor or WebsiteAuditor()
        self.scorer = scorer or LeadScorer()
        self.pitch_gen = pitch_gen or SalesPitchGenerator()
        self.semaphore = asyncio.Semaphore(max_parallel_audits)

        # Active tasks and cancellation/pause tokens
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._pause_events: Dict[str, asyncio.Event] = {}
        self._cancel_flags: Set[str] = set()

    def recover_interrupted_jobs(self):
        """
        Marks any previously 'running' jobs as 'paused' after application restart
        so they can be cleanly resumed without losing state.
        """
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            cursor = conn.execute(
                "SELECT id, logs FROM jobs WHERE status = 'running'"
            )
            interrupted = cursor.fetchall()
            for row in interrupted:
                job_id = row["id"]
                logs = json.loads(row["logs"] or "[]")
                logs.append({
                    "timestamp": now,
                    "level": "warning",
                    "message": "Server restarted while job was in progress. Job paused and ready to resume.",
                })
                conn.execute(
                    "UPDATE jobs SET status = 'paused', logs = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(logs), now, job_id),
                )
                logger.info(f"Recovered interrupted job {job_id} -> set to 'paused'")

    def _append_job_log(self, job_id: str, message: str, level: str = "info"):
        now = datetime.now(timezone.utc).isoformat()
        try:
            with get_db() as conn:
                row = conn.execute("SELECT logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row:
                    logs = json.loads(row["logs"] or "[]")
                    logs.append({"timestamp": now, "level": level, "message": message})
                    # Keep latest 100 log lines
                    if len(logs) > 100:
                        logs = logs[-100:]
                    conn.execute(
                        "UPDATE jobs SET logs = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(logs), now, job_id),
                    )
        except Exception as exc:
            logger.error(f"Error logging to job {job_id}: {exc}")

    async def start_job(
        self,
        location_query: str,
        radius_km: float,
        categories: List[str],
        max_leads: int = 30,
        filter_only_with_phone: bool = False,
        filter_only_missing_website: bool = False,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Geocode initial location
        geo_result = await self.geocoder.geocode(location_query)
        lat = geo_result["lat"]
        lon = geo_result["lon"]

        initial_logs = [
            {
                "timestamp": now,
                "level": "info",
                "message": f"Job initialized. Geocoded '{location_query}' -> ({lat:.4f}, {lon:.4f}).",
            }
        ]

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, location_query, lat, lon, radius_km, categories,
                    max_leads, total_found, processed_count, logs, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "running",
                    geo_result.get("display_name", location_query),
                    lat,
                    lon,
                    radius_km,
                    json.dumps(categories),
                    max_leads,
                    0,
                    0,
                    json.dumps(initial_logs),
                    now,
                    now,
                ),
            )

        # Set up control events
        pause_event = asyncio.Event()
        pause_event.set()  # Not paused initially
        self._pause_events[job_id] = pause_event
        if job_id in self._cancel_flags:
            self._cancel_flags.remove(job_id)

        # Launch background execution task
        task = asyncio.create_task(
            self._execute_job(
                job_id=job_id,
                lat=lat,
                lon=lon,
                radius_km=radius_km,
                categories=categories,
                max_leads=max_leads,
                filter_only_with_phone=filter_only_with_phone,
                filter_only_missing_website=filter_only_missing_website,
            )
        )
        self._running_tasks[job_id] = task
        return job_id

    async def pause_job(self, job_id: str) -> bool:
        if job_id in self._pause_events:
            self._pause_events[job_id].clear()
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'paused', updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
            self._append_job_log(job_id, "Job paused by user.", "warning")
            return True
        return False

    async def resume_job(self, job_id: str) -> bool:
        if job_id in self._pause_events:
            self._pause_events[job_id].set()
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
            self._append_job_log(job_id, "Job resumed by user.", "info")
            return True
        return False

    async def cancel_job(self, job_id: str) -> bool:
        self._cancel_flags.add(job_id)
        if job_id in self._pause_events:
            # Unblock if paused so it can exit
            self._pause_events[job_id].set()

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, job_id),
            )
        self._append_job_log(job_id, "Job cancelled by user.", "warning")
        return True

    async def _execute_job(
        self,
        job_id: str,
        lat: float,
        lon: float,
        radius_km: float,
        categories: List[str],
        max_leads: int,
        filter_only_with_phone: bool,
        filter_only_missing_website: bool,
    ):
        try:
            self._append_job_log(job_id, f"Querying OpenStreetMap Overpass for {', '.join(categories)} within {radius_km}km...")
            discovery_result = await self.discovery_provider.discover(
                lat=lat,
                lon=lon,
                radius_km=radius_km,
                categories=categories,
                max_leads=max_leads * 2,  # Fetch slightly more to allow client-side filters
            )

            if discovery_result.error_message:
                now = datetime.now(timezone.utc).isoformat()
                with get_db() as conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                        (discovery_result.error_message, now, job_id),
                    )
                self._append_job_log(job_id, f"Discovery failed: {discovery_result.error_message}", "error")
                return

            candidates = discovery_result.businesses
            total_found = len(candidates)
            self._append_job_log(
                job_id,
                f"Discovered {total_found} businesses. Applying filters and starting website audits..."
            )

            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET total_found = ? WHERE id = ?",
                    (total_found, job_id),
                )

            # Apply user filters
            filtered: List[DiscoveredBusiness] = []
            for b in candidates:
                if filter_only_with_phone and not b.phone:
                    continue
                if filter_only_missing_website and b.website:
                    continue
                filtered.append(b)
                if len(filtered) >= max_leads:
                    break

            processed_count = 0

            # Process candidates sequentially or bounded concurrency
            for biz in filtered:
                # Check for cancellation
                if job_id in self._cancel_flags:
                    self._append_job_log(job_id, "Processing stopped due to job cancellation.", "warning")
                    return

                # Check for pause
                pause_event = self._pause_events.get(job_id)
                if pause_event and not pause_event.is_set():
                    self._append_job_log(job_id, "Worker paused; waiting for resume signal...", "info")
                    await pause_event.wait()
                    if job_id in self._cancel_flags:
                        return

                # Audit business
                async with self.semaphore:
                    await self._process_single_business(job_id, biz)

                processed_count += 1
                now = datetime.now(timezone.utc).isoformat()
                with get_db() as conn:
                    conn.execute(
                        "UPDATE jobs SET processed_count = ?, updated_at = ? WHERE id = ?",
                        (processed_count, now, job_id),
                    )

            # Mark completed
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
            self._append_job_log(
                job_id,
                f"Job completed successfully. Audited and saved {processed_count} qualified leads.",
                "info",
            )

        except asyncio.CancelledError:
            self._append_job_log(job_id, "Job task was cancelled.", "warning")
        except Exception as exc:
            logger.exception(f"Unhandled error in job {job_id}")
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                    (str(exc), now, job_id),
                )
            self._append_job_log(job_id, f"Fatal error executing job: {str(exc)}", "error")
        finally:
            self._running_tasks.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            self._cancel_flags.discard(job_id)

    async def _process_single_business(self, job_id: str, biz: DiscoveredBusiness):
        """Audits, scores, generates sales pitch, and persists the lead into database."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            # 1. Audit website
            audit_result = await self.auditor.audit(
                url=biz.website,
                business_name=biz.name,
                category=biz.category,
            )

            # 2. Score lead
            score = self.scorer.calculate_score(biz, audit_result)

            # 3. Generate pitch package
            pitches = await self.pitch_gen.generate(biz, audit_result, score)

            lead_id = str(uuid.uuid4())
            pipeline_status = "qualified" if score.total_score >= 50 else "new"

            with get_db() as conn:
                # Upsert by provider_id
                conn.execute(
                    """
                    INSERT INTO leads (
                        id, job_id, provider_id, name, category, address, lat, lon,
                        phone, website, opening_hours, source_url, provenance, raw_data,
                        audit_result, score, score_breakdown, pitches, pipeline_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id) DO UPDATE SET
                        job_id = excluded.job_id,
                        name = excluded.name,
                        category = excluded.category,
                        address = excluded.address,
                        phone = excluded.phone,
                        website = excluded.website,
                        opening_hours = excluded.opening_hours,
                        provenance = excluded.provenance,
                        raw_data = excluded.raw_data,
                        audit_result = excluded.audit_result,
                        score = excluded.score,
                        score_breakdown = excluded.score_breakdown,
                        pitches = excluded.pitches,
                        updated_at = excluded.updated_at
                    """,
                    (
                        lead_id,
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
                        json.dumps({k: v.model_dump() for k, v in biz.provenance.items()}),
                        json.dumps(biz.raw_data),
                        json.dumps(audit_result.model_dump()),
                        score.total_score,
                        json.dumps(score.model_dump()),
                        json.dumps(pitches.model_dump()),
                        pipeline_status,
                        now,
                        now,
                    ),
                )
            self._append_job_log(
                job_id,
                f"Processed: {biz.name} ({biz.category}) -> Score {score.total_score}/100 [{score.priority}]"
            )
        except Exception as exc:
            logger.error(f"Error processing lead {biz.name}: {exc}")
            self._append_job_log(job_id, f"Error processing {biz.name}: {str(exc)[:60]}", "warning")
