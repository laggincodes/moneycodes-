"""
Job management API endpoints: start, pause, resume, cancel, and status.
Enforces authentication, CSRF validation, and strict account isolation.
"""
import json
import sqlite3
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Depends, Header
from app.database import get_db
from app.schemas import StartJobRequest, JobResponse, JobLogEntry
from app.auth.dependencies import get_current_user, verify_csrf_token

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
        worker_id=row["worker_id"] if "worker_id" in row.keys() else None,
        lease_version=row["lease_version"] if "lease_version" in row.keys() else None,
        retry_count=row["retry_count"] if "retry_count" in row.keys() else None,
    )


def compute_job_request_fingerprint(request: StartJobRequest) -> str:
    """
    Calculates a deterministic, collision-resistant SHA-256 fingerprint
    from canonicalized request input parameters. Includes every behavior-affecting field.
    """
    canonical_dict = {
        "location": request.location.strip().lower(),
        "radius_km": round(float(request.radius_km), 4),
        "categories": sorted([c.strip().lower() for c in (request.categories or [])]),
        "max_leads": int(request.max_leads),
        "filter_only_with_phone": bool(request.filter_only_with_phone),
        "filter_only_missing_website": bool(request.filter_only_missing_website),
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@router.post("", response_model=JobResponse)
async def start_job(
    request: StartJobRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    clean_key = idempotency_key.strip() if idempotency_key else None
    if clean_key and len(clean_key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key exceeds maximum allowed length of 255 characters.")

    incoming_fingerprint = compute_job_request_fingerprint(request) if clean_key else None
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    reserved_key = False

    # 1. Idempotency replay & atomic reservation check
    if clean_key:
        for _ in range(50):  # Wait up to 2.5s if a concurrent request is currently in-flight
            try:
                with get_db() as conn:
                    existing_key = conn.execute(
                        "SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?",
                        (user["id"], clean_key),
                    ).fetchone()
                    if existing_key:
                        if existing_key["expires_at"] <= now:
                            # Expired key: clear it so it can be re-reserved
                            conn.execute("DELETE FROM idempotency_keys WHERE user_id = ? AND key = ?", (user["id"], clean_key))
                        elif existing_key["resource_id"] == "PENDING":
                            # In-flight concurrent request processing: early payload conflict check
                            stored_fp = existing_key["request_fingerprint"] if "request_fingerprint" in existing_key.keys() else None
                            if stored_fp and stored_fp != incoming_fingerprint:
                                raise HTTPException(
                                    status_code=409,
                                    detail="Idempotency key reuse conflict: Key was already used with a different request payload.",
                                )
                            # In-flight concurrent request processing: wait briefly for leader
                            time.sleep(0.05)
                            continue
                        else:
                            if existing_key["action_type"] != "START_JOB":
                                raise HTTPException(
                                    status_code=409,
                                    detail="Idempotency key was already used for a different action type.",
                                )

                            stored_fp = existing_key["request_fingerprint"] if "request_fingerprint" in existing_key.keys() else None
                            if stored_fp:
                                if stored_fp != incoming_fingerprint:
                                    raise HTTPException(
                                        status_code=409,
                                        detail="Idempotency key reuse conflict: Key was already used with a different request payload.",
                                    )
                            else:
                                # Legacy fallback for pre-v3 records where request_fingerprint is null
                                cached_data = json.loads(existing_key["response_body"])
                                req_cats = sorted(request.categories or [])
                                cached_cats = sorted(cached_data.get("categories") or [])
                                cached_loc = cached_data.get("location_query", "").strip().lower()
                                req_loc = request.location.strip().lower()
                                loc_matches = (cached_loc == req_loc) or (req_loc in cached_loc) or (cached_loc in req_loc)
                                if (
                                    not loc_matches
                                    or abs(float(cached_data.get("radius_km", 0.0)) - float(request.radius_km)) > 1e-4
                                    or int(cached_data.get("max_leads", 0)) != int(request.max_leads)
                                    or req_cats != cached_cats
                                ):
                                    raise HTTPException(
                                        status_code=409,
                                        detail="Idempotency key reuse conflict: Key was already used with a different request payload.",
                                    )

                            cached_data = json.loads(existing_key["response_body"])
                            return JobResponse(**cached_data)

                    # Atomically reserve key as PENDING with incoming_fingerprint
                    conn.execute(
                        """
                        INSERT INTO idempotency_keys
                        (user_id, key, action_type, resource_id, request_fingerprint, response_status, response_body, created_at, expires_at)
                        VALUES (?, ?, 'START_JOB', 'PENDING', ?, 0, '', ?, ?)
                        """,
                        (user["id"], clean_key, incoming_fingerprint, now, expires_at),
                    )
                    reserved_key = True
                    break
            except sqlite3.IntegrityError:
                # Concurrent thread just reserved or completed this key
                time.sleep(0.05)
                continue

    try:
        job_id = await worker_instance.start_job(
            location_query=request.location,
            radius_km=request.radius_km,
            categories=request.categories,
            max_leads=request.max_leads,
            filter_only_with_phone=request.filter_only_with_phone,
            filter_only_missing_website=request.filter_only_missing_website,
            user_id=user["id"],
        )

        with get_db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Failed to fetch started job.")
            resp = _format_job_row(row)

            # 2. Persist idempotency record with final completed job response and fingerprint
            if clean_key:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO idempotency_keys
                    (user_id, key, action_type, resource_id, request_fingerprint, response_status, response_body, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user["id"], clean_key, "START_JOB", job_id, incoming_fingerprint, 200, resp.model_dump_json(), now, expires_at),
                )

            return resp

    except Exception as exc:
        if clean_key and reserved_key:
            with get_db() as conn:
                conn.execute("DELETE FROM idempotency_keys WHERE user_id = ? AND key = ?", (user["id"], clean_key))
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=List[JobResponse])
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Lists recent discovery jobs owned by the authenticated user."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
        return [_format_job_row(r) for r in rows]


@router.get("/active", response_model=Optional[JobResponse])
async def get_active_job(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        # Check if there is a running or paused job for this user first
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE user_id = ? AND status IN ('running', 'paused')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (user["id"],),
        ).fetchone()

        if not row:
            # Fall back to latest job for this user
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
                (user["id"],),
            ).fetchone()

        if row:
            return _format_job_row(row)
        return None


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
        return _format_job_row(row)


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")

        current_status = row["status"]
        if current_status in TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} is in terminal state '{current_status}' and cannot be paused.",
            )
        if current_status == "paused":
            # Idempotent no-op
            return {"status": "paused", "job_id": job_id}

    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    success = await worker_instance.pause_job(job_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} could not be paused: Task is not actively executing in this worker process.",
        )
    return {"status": "paused", "job_id": job_id}


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")

        current_status = row["status"]
        if current_status in TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} is in terminal state '{current_status}' and cannot be resumed.",
            )
        if current_status == "running":
            # Idempotent no-op
            return {"status": "running", "job_id": job_id}

    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    success = await worker_instance.resume_job(job_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} cannot be resumed: Execution was interrupted by a previous server restart. To avoid duplicate external network probes, please start a new discovery job.",
        )
    return {"status": "running", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")

        current_status = row["status"]
        if current_status == "cancelled":
            # Idempotent no-op
            return {"status": "cancelled", "job_id": job_id}
        if current_status in ("completed", "failed"):
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} is already in terminal state '{current_status}' and cannot be cancelled.",
            )

    if not worker_instance:
        raise HTTPException(status_code=500, detail="Worker instance not initialized.")

    await worker_instance.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}
