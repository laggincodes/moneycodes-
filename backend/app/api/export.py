"""
CSV Export API Endpoint.
Exports discovered and audited leads into a clean, comprehensive CSV file.
Enforces authentication and strict account isolation.
"""
import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query, Response, Depends
from app.database import get_db
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/api/export", tags=["Export"])


@router.get("/csv")
async def export_leads_csv(
    pipeline_status: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    where_clauses = ["user_id = ?"]
    params = [user["id"]]

    if pipeline_status:
        where_clauses.append("pipeline_status = ?")
        params.append(pipeline_status.strip().lower())

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM leads {where_sql} ORDER BY score DESC, created_at DESC",
            params,
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "Lead ID",
        "Business Name",
        "Category",
        "Address",
        "Latitude",
        "Longitude",
        "Phone / WhatsApp",
        "Website",
        "Opening Hours",
        "Source Listing URL",
        "Lead Score",
        "Priority",
        "Website Reachable",
        "SSL Valid",
        "Mobile Viewport Present",
        "Missing CTAs",
        "Pipeline Status",
        "Suggested Scope",
        "Suggested INR Price Range",
        "Why Worth Contacting",
        "Personalized Email Subject",
        "Personalized Email Draft",
        "Personalized WhatsApp Draft",
        "Follow-Up Draft",
        "Notes",
        "Contacted Date",
        "Discovered Date",
    ])

    for r in rows:
        audit_data = json.loads(r["audit_result"]) if r["audit_result"] else {}
        score_data = json.loads(r["score_breakdown"]) if r["score_breakdown"] else {}
        pitch_data = json.loads(r["pitches"]) if r["pitches"] else {}

        writer.writerow([
            r["id"],
            r["name"],
            r["category"],
            r["address"] or "",
            r["lat"],
            r["lon"],
            r["phone"] or "",
            r["website"] or "",
            r["opening_hours"] or "",
            r["source_url"],
            r["score"],
            score_data.get("priority", ""),
            "Yes" if audit_data.get("is_reachable") else "No",
            "Yes" if audit_data.get("ssl_valid") else "No",
            "Yes" if audit_data.get("has_viewport_meta") else "No",
            "Yes" if not audit_data.get("has_clear_cta") else "No",
            r["pipeline_status"],
            pitch_data.get("suggested_project_scope", ""),
            pitch_data.get("suggested_price_inr", ""),
            pitch_data.get("why_worth_contacting", ""),
            pitch_data.get("email_subject", ""),
            pitch_data.get("email_draft", ""),
            pitch_data.get("whatsapp_draft", ""),
            pitch_data.get("follow_up_draft", ""),
            r["notes"] or "",
            r["contacted_at"] or "",
            r["created_at"],
        ])

    csv_content = output.getvalue()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"websitelead_hunter_export_{timestamp}.csv"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
