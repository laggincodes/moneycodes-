import type { JobResponse, Lead, PaginatedLeads, PipelineStatus } from '../types';

const BASE_URL = '/api';

export const api = {
  // Health
  async getHealth(): Promise<{ status: string; service: string; llm_configured: boolean }> {
    const res = await fetch(`${BASE_URL}/health`);
    if (!res.ok) throw new Error('Backend unreachable');
    return res.json();
  },

  // Discovery / Categories
  async getCategories(): Promise<Array<{ id: string; label: string }>> {
    const res = await fetch(`${BASE_URL}/discovery/categories`);
    if (!res.ok) throw new Error('Failed to load categories');
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
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Failed to start job' }));
      throw new Error(err.detail || 'Failed to start job');
    }
    return res.json();
  },

  async getActiveJob(): Promise<JobResponse | null> {
    const res = await fetch(`${BASE_URL}/jobs/active`);
    if (!res.ok) return null;
    return res.json();
  },

  async getJob(jobId: string): Promise<JobResponse> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}`);
    if (!res.ok) throw new Error('Job not found');
    return res.json();
  },

  async pauseJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/pause`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to pause job');
    return res.json();
  },

  async resumeJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/resume`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to resume job');
    return res.json();
  },

  async cancelJob(jobId: string): Promise<{ status: string }> {
    const res = await fetch(`${BASE_URL}/jobs/${jobId}/cancel`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to cancel job');
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

    const res = await fetch(`${BASE_URL}/leads?${query.toString()}`);
    if (!res.ok) throw new Error('Failed to fetch leads');
    return res.json();
  },

  async getLead(leadId: string): Promise<Lead> {
    const res = await fetch(`${BASE_URL}/leads/${leadId}`);
    if (!res.ok) throw new Error('Lead not found');
    return res.json();
  },

  async updateLeadPipeline(leadId: string, pipeline_status: PipelineStatus, notes?: string): Promise<Lead> {
    const res = await fetch(`${BASE_URL}/leads/${leadId}/pipeline`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pipeline_status, notes }),
    });
    if (!res.ok) throw new Error('Failed to update pipeline stage');
    return res.json();
  },

  async deleteLead(leadId: string): Promise<void> {
    const res = await fetch(`${BASE_URL}/leads/${leadId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Failed to delete lead');
  },

  async clearAllLeads(): Promise<void> {
    const res = await fetch(`${BASE_URL}/leads?all_leads=true`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Failed to clear leads');
  },

  // Export CSV
  getExportUrl(pipeline_status?: string): string {
    return pipeline_status ? `${BASE_URL}/export/csv?pipeline_status=${pipeline_status}` : `${BASE_URL}/export/csv`;
  },
};
