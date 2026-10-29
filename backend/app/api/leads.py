"""
Leads API endpoints: Search, filter, pipeline update, and lead deletion.
Enforces authentication, CSRF validation, and strict account isolation.
"""
import json
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Depends, Header
from pydantic import BaseModel
from app.database import get_db
from app.schemas import (
    LeadResponse,
    UpdateLeadPipelineRequest,
    WebsiteAuditResult,
    LeadScore,
    SalesPitches,
)
from app.auth.dependencies import get_current_user, verify_csrf_token
from app.audit.website_auditor import WebsiteAuditor
from app.scoring.scorer import LeadScorer
from app.pitch.llm_provider import SalesPitchGenerator
from app.discovery.base import DiscoveredBusiness

router = APIRouter(prefix="/api/leads", tags=["Leads"])

ALLOWED_SORT_COLUMNS: Dict[str, str] = {
    "score": "score",
    "name": "name",
    "created_at": "created_at",
}

ALLOWED_SORT_DIRECTIONS: Dict[str, str] = {
    "asc": "ASC",
    "desc": "DESC",
}


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
    sort_by: str = Query(default="score"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    # Enforce account isolation: only leads owned by authenticated user
    where_clauses = ["user_id = ?"]
    params = [user["id"]]

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

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    # Validate sort parameters against fixed mapping
    clean_sort_by = ALLOWED_SORT_COLUMNS.get(sort_by.lower().strip())
    if not clean_sort_by:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort column '{sort_by}'. Allowed columns: {', '.join(ALLOWED_SORT_COLUMNS.keys())}",
        )

    clean_sort_dir = ALLOWED_SORT_DIRECTIONS.get(sort_dir.lower().strip())
    if not clean_sort_dir:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort direction '{sort_dir}'. Allowed directions: 'asc', 'desc'",
        )

    with get_db() as conn:
        # Count total owned leads matching criteria
        count_cursor = conn.execute(f"SELECT COUNT(*) as count FROM leads {where_sql}", params)
        total = count_cursor.fetchone()["count"]

        # Order and paginate using safe validated identifiers
        offset = (page - 1) * page_size
        query_sql = f"""
            SELECT * FROM leads
            {where_sql}
            ORDER BY {clean_sort_by} {clean_sort_dir}
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
async def get_lead(lead_id: str, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found.")
        return _format_lead_row(row)


@router.patch("/{lead_id}/pipeline", response_model=LeadResponse)
async def update_pipeline(
    lead_id: str,
    payload: UpdateLeadPipelineRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    allowed_statuses = ["new", "qualified", "contacted", "replied", "meeting", "won", "lost"]
    target_status = payload.pipeline_status.lower()
    if target_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pipeline status. Must be one of: {', '.join(allowed_statuses)}"
        )

    clean_key = idempotency_key.strip() if idempotency_key else None
    if clean_key and len(clean_key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key exceeds maximum allowed length of 255 characters.")

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        # 1. Idempotency replay check: return cached response if key exists and is valid
        if clean_key:
            existing_key = conn.execute(
                "SELECT * FROM idempotency_keys WHERE user_id = ? AND key = ?",
                (user["id"], clean_key),
            ).fetchone()
            if existing_key:
                if existing_key["expires_at"] > now:
                    cached_data = json.loads(existing_key["response_body"])
                    return LeadResponse(**cached_data)

        row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        ).fetchone()
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
            WHERE id = ? AND user_id = ?
            """,
            (target_status, notes, contacted_at, now, lead_id, user["id"]),
        )

        updated_row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        ).fetchone()
        resp = _format_lead_row(updated_row)

        # 2. Persist idempotency record
        if clean_key:
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys
                (user_id, key, action_type, resource_id, response_status, response_body, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], clean_key, "UPDATE_PIPELINE", lead_id, 200, resp.model_dump_json(), now, expires_at),
            )

        return resp


@router.delete("/{lead_id}")
async def delete_lead(
    lead_id: str,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Lead not found.")
    return {"message": "Lead successfully deleted.", "lead_id": lead_id}


@router.delete("")
async def bulk_delete_leads(
    all_leads: bool = Query(default=False),
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    if not all_leads:
        raise HTTPException(status_code=400, detail="Set all_leads=true to confirm deletion of all leads.")
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM leads WHERE user_id = ?", (user["id"],))
        count = cursor.rowcount
    return {"message": f"Successfully deleted {count} leads."}


class DirectAuditRequest(BaseModel):
    url: str
    business_name: Optional[str] = "Target Business"
    category: Optional[str] = "local_business"


@router.post("/audit-target", response_model=WebsiteAuditResult)
async def audit_target(
    request: DirectAuditRequest,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    """
    Performs on-demand website audit against a specified URL.
    Enforces SSRF prevention, IP pinning, redirect limits, and HTML analysis.
    """
    auditor = WebsiteAuditor()
    try:
        result = await auditor.audit(
            url=request.url,
            business_name=request.business_name,
            category=request.category,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Audit failed: {str(exc)}")


@router.post("/{lead_id}/audit", response_model=LeadResponse)
async def audit_lead(
    lead_id: str,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf_token),
):
    """
    Performs on-demand re-audit of an existing lead owned by the authenticated user.
    Updates the audit result, score, and pitches in the database.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found.")

    auditor = WebsiteAuditor()
    scorer = LeadScorer()
    pitch_gen = SalesPitchGenerator()

    biz = DiscoveredBusiness(
        provider_id=row["provider_id"],
        name=row["name"],
        category=row["category"],
        lat=row["lat"],
        lon=row["lon"],
        address=row["address"],
        phone=row["phone"],
        website=row["website"],
        opening_hours=row["opening_hours"],
        source_url=row["source_url"],
    )

    audit_result = await auditor.audit(
        url=row["website"],
        business_name=row["name"],
        category=row["category"],
    )
    score = scorer.calculate_score(biz, audit_result)
    pitches = await pitch_gen.generate(biz, audit_result, score)

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET audit_result = ?, score = ?, score_breakdown = ?, pitches = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                audit_result.model_dump_json(),
                score.total_score,
                score.model_dump_json(),
                pitches.model_dump_json(),
                now,
                lead_id,
                user["id"],
            ),
        )
        updated_row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user["id"]),
        ).fetchone()
        return _format_lead_row(updated_row)
