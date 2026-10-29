"""
Pydantic schemas for WebsiteLead Hunter API and services.
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class StartJobRequest(BaseModel):
    location: str = Field(..., description="Location name or address to search, e.g. 'Koramangala, Bengaluru'")
    radius_km: float = Field(default=1.5, ge=0.1, le=25.0, description="Search radius in kilometers")
    categories: List[str] = Field(
        default=["cafes", "restaurants", "shops", "salons", "gyms", "clinics"],
        description="Selected business categories"
    )
    max_leads: int = Field(default=25, ge=1, le=100, description="Maximum number of leads to discover and audit")
    filter_only_with_phone: bool = Field(default=False, description="Filter for businesses that have a contact phone")
    filter_only_missing_website: bool = Field(default=False, description="Filter for businesses without a website")


class JobLogEntry(BaseModel):
    timestamp: str
    level: str
    message: str


class JobResponse(BaseModel):
    id: str
    status: str  # pending, running, paused, cancelled, completed, failed
    location_query: str
    lat: float
    lon: float
    radius_km: float
    categories: List[str]
    max_leads: int
    total_found: int
    processed_count: int
    error_message: Optional[str] = None
    logs: List[JobLogEntry] = []
    created_at: str
    updated_at: str
    worker_id: Optional[str] = None
    lease_version: Optional[int] = None
    retry_count: Optional[int] = None


class AuditFinding(BaseModel):
    check: str
    status: str  # 'verified', 'inferred'
    passed: bool
    summary: str
    evidence: Optional[str] = None


class WebsiteAuditResult(BaseModel):
    has_website_url: bool
    url: Optional[str] = None
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    is_reachable: bool
    redirect_count: int = 0
    redirect_chain: List[str] = []
    response_time_ms: Optional[int] = None
    ssl_valid: bool = False
    
    # Structural checks
    has_title: bool = False
    title_text: Optional[str] = None
    has_viewport_meta: bool = False  # Critical for mobile responsiveness
    has_description_meta: bool = False
    
    # Feature heuristics
    has_menu: bool = False
    has_contact_phone: bool = False
    has_contact_email: bool = False
    has_address_text: bool = False
    has_clear_cta: bool = False
    has_social_links: bool = False
    detected_socials: List[str] = []
    
    # Mobile usability heuristics
    mobile_friendly_indicator: bool = False
    mobile_issues: List[str] = []
    
    # Findings list
    findings: List[AuditFinding] = []


class ScoreFactor(BaseModel):
    name: str
    points: int
    max_points: int
    reason: str
    evidence: str


class LeadScore(BaseModel):
    total_score: int  # 0 to 100
    factors: List[ScoreFactor]
    priority: str  # 'High', 'Medium', 'Low'
    explanation: str


class SalesPitches(BaseModel):
    business_summary: str
    verified_contacts: Dict[str, Optional[str]]
    audit_findings_summary: str
    recommended_features: List[str]
    suggested_project_scope: str
    suggested_price_inr: str  # Clearly labeled as recommended benchmark
    email_subject: str
    email_draft: str
    whatsapp_draft: Optional[str] = None
    follow_up_draft: str
    why_worth_contacting: str
    generation_source: str  # 'deterministic_template' or 'gemini_llm'


class LeadResponse(BaseModel):
    id: str
    job_id: Optional[str] = None
    provider_id: str
    name: str
    category: str
    address: Optional[str] = None
    lat: float
    lon: float
    phone: Optional[str] = None
    website: Optional[str] = None
    opening_hours: Optional[str] = None
    source_url: str
    provenance: Dict[str, Any]
    raw_data: Dict[str, Any]
    audit_result: Optional[WebsiteAuditResult] = None
    score: int
    score_breakdown: Optional[LeadScore] = None
    pitches: Optional[SalesPitches] = None
    pipeline_status: str  # new, qualified, contacted, replied, meeting, won, lost
    notes: Optional[str] = None
    contacted_at: Optional[str] = None
    created_at: str
    updated_at: str


class UpdateLeadPipelineRequest(BaseModel):
    pipeline_status: str
    notes: Optional[str] = None
    contacted_at: Optional[str] = None


class GeocodeResponse(BaseModel):
    lat: float
    lon: float
    display_name: str
