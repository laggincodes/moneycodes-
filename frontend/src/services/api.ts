import type { JobResponse, Lead, PaginatedLeads, PipelineStatus, User, AuthResponse } from '../types';

const BASE_URL = '/api';

let currentCsrfToken: string | null = null;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function extractError(res: Response, fallback: string): Promise<never> {
  let detail = fallback;
  try {
    const data = await res.json();
    if (data && data.detail) {
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    }
  } catch {
    detail = `${fallback} (HTTP ${res.status})`;
  }
  const err = new ApiError(detail, res.status);
  throw err;
}

function getHeaders(hasBody: boolean = false): Record<string, string> {
  const headers: Record<string, string> = {};
  if (hasBody) {
    headers['Content-Type'] = 'application/json';
  }
  if (currentCsrfToken) {
    headers['X-CSRF-Token'] = currentCsrfToken;
  }
  return headers;
}

export const api = {
  // Authentication
  async register(params: {
    email: string;
    password: string;
    full_name?: string;
  }): Promise<AuthResponse> {
    const res = await fetch(`${BASE_URL}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(params),
    });
    if (!res.ok) await extractError(res, 'Registration failed');
    const data: AuthResponse = await res.json();
    currentCsrfToken = data.csrf_token;
    return data;
  },

  async login(params: { email: string; password: string }): Promise<AuthResponse> {
    const res = await fetch(`${BASE_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(params),
    });
    if (!res.ok) await extractError(res, 'Invalid email or password');
    const data: AuthResponse = await res.json();
    currentCsrfToken = data.csrf_token;
    return data;
  },

  async logout(): Promise<void> {
    await fetch(`${BASE_URL}/auth/logout`, {
      method: 'POST',
      credentials: 'same-origin',
    });
    currentCsrfToken = null;
  },

  async getMe(): Promise<User | null> {
    try {
      const res = await fetch(`${BASE_URL}/auth/me`, {
        credentials: 'same-origin',
      });
      if (!res.ok) {
        currentCsrfToken = null;
        return null;
      }
      const data = await res.json();
      currentCsrfToken = data.csrf_token;
      return data.user;
    } catch {
      currentCsrfToken = null;
      return null;
    }
  },

  // Health
  async getHealth(): Promise<{ status: string; service: string }> {
    const res = await fetch(`${BASE_URL}/health`);
    if (!res.ok) await extractError(res, 'Backend unreachable');
    return res.json();
  },

  // Discovery / Categories
  async getCategories(): Promise<Array<{ id: string; label: string }>> {
    const res = await fetch(`${BASE_URL}/discovery/categories`, {
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to load categories');
    return res.json();
  },

  // Jobs
  async startJob(params: {
    location: string;
    radius_km: number;
    categories: string[];
    max_leads: number;
    filter_only_with_phone: boolean;
    filter_only_missing_website: boolean;
  }): Promise<JobResponse> {
    const res = await fetch(`${BASE_URL}/jobs`, {
      method: 'POST',
      headers: getHeaders(true),
      credentials: 'same-origin',
      body: JSON.stringify(params),
    });
    if (!res.ok) await extractError(res, 'Failed to start job');
    return res.json();
  },

  async getActiveJob(): Promise<JobResponse | null> {
    const res = await fetch(`${BASE_URL}/jobs/active`, {
      credentials: 'same-origin',
    });
    if (!res.ok) return null;
    return res.json();
  },

  async getJob(jobId: string): Promise<JobResponse> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}`, {
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Job not found');
    return res.json();
  },

  async pauseJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/pause`, {
      method: 'POST',
      headers: getHeaders(false),
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to pause job');
    return res.json();
  },

  async resumeJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/resume`, {
      method: 'POST',
      headers: getHeaders(false),
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to resume job');
    return res.json();
  },

  async cancelJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/cancel`, {
      method: 'POST',
      headers: getHeaders(false),
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to cancel job');
    return res.json();
  },

  // Leads
  async getLeads(params: {
    search?: string;
    category?: string;
    pipeline_status?: string;
    min_score?: number;
    has_phone?: boolean;
    missing_website?: boolean;
    sort_by?: string;
    sort_dir?: string;
    page?: number;
    page_size?: number;
  }): Promise<PaginatedLeads> {
    const query = new URLSearchParams();
    if (params.search) query.set('search', params.search);
    if (params.category) query.set('category', params.category);
    if (params.pipeline_status) query.set('pipeline_status', params.pipeline_status);
    if (params.min_score !== undefined) query.set('min_score', params.min_score.toString());
    if (params.has_phone !== undefined) query.set('has_phone', params.has_phone.toString());
    if (params.missing_website !== undefined) query.set('missing_website', params.missing_website.toString());
    if (params.sort_by) query.set('sort_by', params.sort_by);
    if (params.sort_dir) query.set('sort_dir', params.sort_dir);
    if (params.page) query.set('page', params.page.toString());
    if (params.page_size) query.set('page_size', params.page_size.toString());

    const res = await fetch(`${BASE_URL}/leads?${query.toString()}`, {
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to fetch leads');
    return res.json();
  },

  async getLead(leadId: string): Promise<Lead> {
    const res = await fetch(`${BASE_URL}/leads/${leadId}`, {
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Lead not found');
    return res.json();
  },

  async updateLeadPipeline(
    leadId: string,
    pipeline_status: PipelineStatus,
    notes?: string,
    idempotencyKey?: string,
  ): Promise<Lead> {
    const headers = getHeaders(true);
    if (idempotencyKey) {
      headers['Idempotency-Key'] = idempotencyKey;
    }
    const res = await fetch(`${BASE_URL}/leads/${leadId}/pipeline`, {
      method: 'PATCH',
      headers,
      credentials: 'same-origin',
      body: JSON.stringify({ pipeline_status, notes }),
    });
    if (!res.ok) await extractError(res, 'Failed to update pipeline stage');
    return res.json();
  },

  async deleteLead(leadId: string): Promise<void> {
    const res = await fetch(`${BASE_URL}/leads/${leadId}`, {
      method: 'DELETE',
      headers: getHeaders(false),
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to delete lead');
  },

  async clearAllLeads(): Promise<void> {
    const res = await fetch(`${BASE_URL}/leads?all_leads=true`, {
      method: 'DELETE',
      headers: getHeaders(false),
      credentials: 'same-origin',
    });
    if (!res.ok) await extractError(res, 'Failed to clear leads');
  },

  // Export CSV
  getExportUrl(pipeline_status?: string): string {
    return pipeline_status ? `${BASE_URL}/export/csv?pipeline_status=${pipeline_status}` : `${BASE_URL}/export/csv`;
  },
};
