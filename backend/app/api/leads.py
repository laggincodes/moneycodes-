"""
Leads API endpoints: Search, filter, pipeline update, and lead deletion.
"""
import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from app.database import get_db
from app.schemas import (
    LeadResponse,
    UpdateLeadPipelineRequest,
    WebsiteAuditResult,
    LeadScore,
    SalesPitches,
)

router = APIRouter(prefix="/api/leads", tags=["Leads"])


class PaginatedLeadsResponse(BaseModel):
    items: List[LeadResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


def _format_lead_row(row) -> LeadResponse:
    audit_data = json.loads(row["audit_result"]) if row["audit_result"] else None
    score_data = json.loads(row["score_breakdown"]) if row["score_breakdown"] else None
    pitch_data = json.loads(row["pitches"]) if row["pitches"] else None

    return LeadResponse(
        id=row["id"],
        job_id=row["job_id"],
        provider_id=row["provider_id"],
        name=row["name"],
        category=row["category"],
        address=row["address"],
        lat=row["lat"],
        lon=row["lon"],
        phone=row["phone"],
        website=row["website"],
        opening_hours=row["opening_hours"],
        source_url=row["source_url"],
        provenance=json.loads(row["provenance"] or "{}"),
        raw_data=json.loads(row["raw_data"] or "{}"),
        audit_result=WebsiteAuditResult(**audit_data) if audit_data else None,
        score=row["score"],
        score_breakdown=LeadScore(**score_data) if score_data else None,
        pitches=SalesPitches(**pitch_data) if pitch_data else None,
        pipeline_status=row["pipeline_status"] or "new",
        notes=row["notes"],
        contacted_at=row["contacted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("", response_model=PaginatedLeadsResponse)
async def list_leads(
    search: Optional[str] = None,
    category: Optional[str] = None,
    pipeline_status: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    has_phone: Optional[bool] = None,
    missing_website: Optional[bool] = None,
    job_id: Optional[str] = None,
    sort_by: str = Query(default="score", pattern="^(score|name|created_at)$"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    where_clauses = []
    params = []

    if search:
        search_like = f"%{search.strip()}%"
        where_clauses.append("(name LIKE ? OR address LIKE ?)")
        params.extend([search_like, search_like])

    if category:
        where_clauses.append("category LIKE ?")
        params.append(f"%{category.strip()}%")

    if pipeline_status:
        where_clauses.append("pipeline_status = ?")
        params.append(pipeline_status.strip().lower())

    if min_score is not None:
        where_clauses.append("score >= ?")
        params.append(min_score)

    if max_score is not None:
        where_clauses.append("score <= ?")
        params.append(max_score)

    if has_phone is True:
        where_clauses.append("(phone IS NOT NULL AND LENGTH(phone) >= 5)")
    elif has_phone is False:
        where_clauses.append("(phone IS NULL OR LENGTH(phone) < 5)")

    if missing_website is True:
        where_clauses.append("(website IS NULL OR website = '')")
    elif missing_website is False:
        where_clauses.append("(website IS NOT NULL AND website != '')")

    if job_id:
        where_clauses.append("job_id = ?")
        params.append(job_id)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with get_db() as conn:
        # Count total
        count_cursor = conn.execute(f"SELECT COUNT(*) as count FROM leads {where_sql}", params)
        total = count_cursor.fetchone()["count"]

        # Order and paginate
        offset = (page - 1) * page_size
        query_sql = f"""
            SELECT * FROM leads
            {where_sql}
            ORDER BY {sort_by} {sort_dir.upper()}
            LIMIT ? OFFSET ?
        """
        query_params = params + [page_size, offset]
        rows = conn.execute(query_sql, query_params).fetchall()

        items = [_format_lead_row(r) for r in rows]
        total_pages = max(1, (total + page_size - 1) // page_size)

        return PaginatedLeadsResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found.")
        return _format_lead_row(row)


@router.patch("/{lead_id}/pipeline", response_model=LeadResponse)
async def update_pipeline(lead_id: str, payload: UpdateLeadPipelineRequest):
    allowed_statuses = ["new", "qualified", "contacted", "replied", "meeting", "won", "lost"]
    target_status = payload.pipeline_status.lower()
    if target_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pipeline status. Must be one of: {', '.join(allowed_statuses)}"
        )

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found.")

        contacted_at = payload.contacted_at
        if target_status == "contacted" and not contacted_at and not row["contacted_at"]:
            contacted_at = now
        elif not contacted_at:
            contacted_at = row["contacted_at"]

        notes = payload.notes if payload.notes is not None else row["notes"]

        conn.execute(
            """
            UPDATE leads
            SET pipeline_status = ?, notes = ?, contacted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (target_status, notes, contacted_at, now, lead_id),
        )

        updated_row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _format_lead_row(updated_row)


@router.delete("/{lead_id}")
async def delete_lead(lead_id: str):
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Lead not found.")
    return {"message": "Lead successfully deleted.", "lead_id": lead_id}


@router.delete("")
async def bulk_delete_leads(all_leads: bool = Query(default=False)):
    if not all_leads:
        raise HTTPException(status_code=400, detail="Set all_leads=true to confirm deletion of all leads.")
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM leads")
        count = cursor.rowcount
    return {"message": f"Successfully deleted {count} leads."}
