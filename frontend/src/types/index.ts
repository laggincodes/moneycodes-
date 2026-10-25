export interface JobLogEntry {
  timestamp: string;
  level: string;
  message: string;
}

export interface JobResponse {
  id: string;
  status: 'pending' | 'running' | 'paused' | 'cancelled' | 'completed' | 'failed';
  location_query: string;
  lat: float;
  lon: float;
  radius_km: number;
  categories: string[];
  max_leads: number;
  total_found: number;
  processed_count: number;
  error_message?: string | null;
  logs: JobLogEntry[];
  created_at: string;
  updated_at: string;
}

export type float = number;

export interface AuditFinding {
  check: string;
  status: 'verified' | 'inferred';
  passed: boolean;
  summary: string;
  evidence?: string | null;
}

export interface WebsiteAuditResult {
  has_website_url: boolean;
  url?: string | null;
  final_url?: string | null;
  status_code?: number | null;
  is_reachable: boolean;
  redirect_count: number;
  redirect_chain: string[];
  response_time_ms?: number | null;
  ssl_valid: boolean;
  has_title: boolean;
  title_text?: string | null;
  has_viewport_meta: boolean;
  has_description_meta: boolean;
  has_menu: boolean;
  has_contact_phone: boolean;
  has_contact_email: boolean;
  has_address_text: boolean;
  has_clear_cta: boolean;
  has_social_links: boolean;
  detected_socials: string[];
  mobile_friendly_indicator: boolean;
  mobile_issues: string[];
  findings: AuditFinding[];
}

export interface ScoreFactor {
  name: string;
  points: number;
  max_points: number;
  reason: string;
  evidence: string;
}

export interface LeadScore {
  total_score: number;
  factors: ScoreFactor[];
  priority: 'High' | 'Medium' | 'Low';
  explanation: string;
}

export interface SalesPitches {
  business_summary: string;
  verified_contacts: {
    phone?: string | null;
    website?: string | null;
    address?: string | null;
    source_listing?: string | null;
  };
  audit_findings_summary: string;
  recommended_features: string[];
  suggested_project_scope: string;
  suggested_price_inr: string;
  email_subject: string;
  email_draft: string;
  whatsapp_draft?: string | null;
  follow_up_draft: string;
  why_worth_contacting: string;
  generation_source: string;
}

export type PipelineStatus = 'new' | 'qualified' | 'contacted' | 'replied' | 'meeting' | 'won' | 'lost';

export interface Lead {
  id: string;
  job_id?: string | null;
  provider_id: string;
  name: string;
  category: string;
  address?: string | null;
  lat: number;
  lon: number;
  phone?: string | null;
  website?: string | null;
  opening_hours?: string | null;
  source_url: string;
  provenance: Record<string, any>;
  raw_data: Record<string, any>;
  audit_result?: WebsiteAuditResult | null;
  score: number;
  score_breakdown?: LeadScore | null;
  pitches?: SalesPitches | null;
  pipeline_status: PipelineStatus;
  notes?: string | null;
  contacted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface PaginatedLeads {
  items: Lead[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}
