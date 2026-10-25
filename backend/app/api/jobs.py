"""
Job management API endpoints: start, pause, resume, cancel, and status.
"""
import json
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.database import get_db
from app.schemas import StartJobRequest, JobResponse, JobLogEntry

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])

# Global worker reference set by app state
worker_instance = None


def set_worker(worker):
    global worker_instance
    worker_instance = worker


def _format_job_row(row) -> JobResponse:
    raw_logs = json.loads(row["logs"] or "[]")
    logs = [JobLogEntry(**item) if isinstance(item, dict) else JobLogEntry(timestamp="", level="info", message=str(item)) for item in raw_logs]
    return JobResponse(
        id=row["id"],
        status=row["status"],
        location_query=row["location_query"],
        lat=row["lat"],
        lon=row["lon"],
        radius_km=row["radius_km"],
        categories=json.loads(row["categories"] or "[]"),
        max_leads=row["max_leads"],
        total_found=row["total_found"],
        processed_count=row["processed_count"],
        error_message=row["error_message"],
        logs=logs,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("", response_model=JobResponse)
async def start_job(request: StartJobRequest):
    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    try:
        job_id = await worker_instance.start_job(
            location_query=request.location,
            radius_km=request.radius_km,
            categories=request.categories,
            max_leads=request.max_leads,
            filter_only_with_phone=request.filter_only_with_phone,
            filter_only_missing_website=request.filter_only_missing_website,
        )

        with get_db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Failed to fetch started job.")
            return _format_job_row(row)

    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/active", response_model=Optional[JobResponse])
async def get_active_job():
    with get_db() as conn:
        # Check if there is a running or paused job first
        row = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('running', 'paused') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            # Fall back to latest job
            row = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 1").fetchone()

        if row:
            return _format_job_row(row)
        return None


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
        return _format_job_row(row)


@router.post("/{job_id}/pause")
async def pause_job(job_id: str):
    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    success = await worker_instance.pause_job(job_id)
    if not success:
        # Check if job exists in db
        with get_db() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": "paused", "job_id": job_id}


@router.post("/{job_id}/resume")
async def resume_job(job_id: str):
    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    success = await worker_instance.resume_job(job_id)
    if not success:
        with get_db() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": "running", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    await worker_instance.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}
