"""
Output Formatting and Console Presentation for WebsiteLead Hunter CLI.
Provides clean table rendering, lead/job inspectors, and credential redaction.
Zero external dependencies.
"""
import sys
from typing import List, Dict, Any, Optional


def redact_secret(val: Optional[str]) -> str:
    """Masks secret or token strings."""
    if not val:
        return "<none>"
    if len(val) <= 8:
        return "***"
    return f"{val[:4]}...{val[-4:]}"


def format_table(headers: List[str], rows: List[List[str]], min_width: int = 10) -> str:
    """Renders an aligned ASCII table."""
    if not rows:
        return "No records found."

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(val)))
            else:
                col_widths.append(len(str(val)))

    # Header row
    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))

    # Data rows
    data_lines = []
    for row in rows:
        line = " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))
        data_lines.append(line)

    return f"{header_line}\n{separator_line}\n" + "\n".join(data_lines)


def format_lead_table(leads: List[Dict[str, Any]]) -> str:
    headers = ["ID", "Business Name", "Category", "Score", "Pipeline", "Phone", "Website"]
    rows = []
    for l in leads:
        lead_id = l.get("id", "")
        short_id = lead_id[:8] if len(lead_id) > 8 else lead_id
        name = l.get("name", "Unknown")[:25]
        cat = l.get("category", "")[:15]
        score = str(l.get("score", 0))
        status = l.get("pipeline_status", "new").upper()
        phone = l.get("phone") or "-"
        phone = phone[:15]
        website = l.get("website") or "<no website>"
        website = website[:25]
        rows.append([short_id, name, cat, score, status, phone, website])

    return format_table(headers, rows)


def format_job_table(jobs: List[Dict[str, Any]]) -> str:
    headers = ["Job ID", "Status", "Location", "Radius", "Found", "Processed", "Created At"]
    rows = []
    for j in jobs:
        job_id = j.get("id", "")
        short_id = job_id[:8] if len(job_id) > 8 else job_id
        status = j.get("status", "unknown").upper()
        loc = j.get("location_query", "")[:20]
        radius = f"{j.get('radius_km', 0)}km"
        found = str(j.get("total_found", 0))
        proc = str(j.get("processed_count", 0))
        created = j.get("created_at", "")[:16].replace("T", " ")
        rows.append([short_id, status, loc, radius, found, proc, created])

    return format_table(headers, rows)


def format_lead_detail(lead: Dict[str, Any]) -> str:
    lines = [
        "=" * 70,
        f"LEAD: {lead.get('name', 'Unknown')}",
        "=" * 70,
        f"ID:               {lead.get('id')}",
        f"Category:         {lead.get('category')}",
        f"Address:          {lead.get('address') or '<none>'}",
        f"Phone:            {lead.get('phone') or '<none>'}",
        f"Website:          {lead.get('website') or '<none>'}",
        f"Pipeline Stage:   {lead.get('pipeline_status', 'new').upper()}",
        f"Lead Score:       {lead.get('score', 0)}/100",
        f"Contacted At:     {lead.get('contacted_at') or '<not yet contacted>'}",
        f"Notes:            {lead.get('notes') or '<none>'}",
        f"Created:          {lead.get('created_at')}",
        f"Updated:          {lead.get('updated_at')}",
        "-" * 70,
    ]

    # Audit details
    audit = lead.get("audit_result")
    if audit:
        lines.append("WEBSITE AUDIT RESULT:")
        lines.append(f"  Reachable:        {audit.get('is_reachable')}")
        lines.append(f"  Status Code:      {audit.get('status_code')}")
        lines.append(f"  SSL Valid:        {audit.get('ssl_valid')}")
        lines.append(f"  Mobile Viewport:  {audit.get('has_viewport_meta')}")
        lines.append(f"  Missing CTAs:     {not audit.get('has_clear_cta')}")
        lines.append(f"  Page Title:       {audit.get('title') or '<none>'}")
        findings = audit.get("findings") or []
        if findings:
            lines.append("  Key Findings:")
            for f in findings:
                sev = f.get("severity", "info").upper()
                msg = f.get("message", "")
                lines.append(f"    [{sev}] {msg}")
    else:
        lines.append("WEBSITE AUDIT RESULT: <No audit data>")

    lines.append("-" * 70)

    # Pitch details
    pitches = lead.get("pitches")
    if pitches:
        lines.append("SALES PITCH PREVIEW:")
        quick_pitch = pitches.get("quick_pitch")
        if quick_pitch:
            lines.append(f"  Hook: {quick_pitch}")
        cold_email = pitches.get("cold_email")
        if cold_email:
            lines.append("  Cold Email:")
            for e_line in cold_email.split("\n"):
                lines.append(f"    {e_line}")
    lines.append("=" * 70)
    return "\n".join(lines)


def format_job_detail(job: Dict[str, Any]) -> str:
    lines = [
        "=" * 70,
        f"DISCOVERY JOB: {job.get('id')}",
        "=" * 70,
        f"Status:           {job.get('status', '').upper()}",
        f"Location:         {job.get('location_query')}",
        f"Radius:           {job.get('radius_km')} km (lat: {job.get('lat')}, lon: {job.get('lon')})",
        f"Categories:       {', '.join(job.get('categories') or [])}",
        f"Max Leads:        {job.get('max_leads')}",
        f"Total Found:      {job.get('total_found')}",
        f"Processed:        {job.get('processed_count')}",
        f"Created At:       {job.get('created_at')}",
        f"Updated At:       {job.get('updated_at')}",
    ]
    if job.get("error_message"):
        lines.append(f"ERROR:            {job.get('error_message')}")

    lines.append("-" * 70)
    lines.append("JOB LOGS:")
    logs = job.get("logs") or []
    if not logs:
        lines.append("  <No logs recorded>")
    for log in logs:
        ts = log.get("timestamp", "")[:19].replace("T", " ")
        lvl = log.get("level", "info").upper()
        msg = log.get("message", "")
        lines.append(f"  [{ts}] [{lvl}] {msg}")
    lines.append("=" * 70)
    return "\n".join(lines)
