import React from 'react';
import type { Lead } from '../types';
import { ScoreBadge } from './ScoreBadge';
import {
  Search,
  SlidersHorizontal,
  Phone,
  Globe,
  ChevronLeft,
  ChevronRight,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Eye,
  Trash2,
} from 'lucide-react';

interface LeadTableProps {
  leads: Lead[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  search: string;
  onSearchChange: (val: string) => void;
  category: string;
  onCategoryChange: (val: string) => void;
  status: string;
  onStatusChange: (val: string) => void;
  minScore: number;
  onMinScoreChange: (val: number) => void;
  onlyMissingWeb: boolean;
  onOnlyMissingWebChange: (val: boolean) => void;
  sortBy: string;
  sortDir: string;
  onSortToggle: (field: string) => void;
  onPageChange: (newPage: number) => void;
  onSelectLead: (lead: Lead) => void;
  onDeleteLead: (leadId: string) => Promise<void>;
}

export const LeadTable: React.FC<LeadTableProps> = ({
  leads,
  total,
  page,
  totalPages,
  search,
  onSearchChange,
  category,
  onCategoryChange,
  status,
  onStatusChange,
  minScore,
  onMinScoreChange,
  onlyMissingWeb,
  onOnlyMissingWebChange,
  sortBy,
  sortDir,
  onSortToggle,
  onPageChange,
  onSelectLead,
  onDeleteLead,
}) => {
  const PIPELINE_STATUS_COLORS: Record<string, string> = {
    new: 'bg-slate-800 text-slate-300',
    qualified: 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/30',
    contacted: 'bg-amber-500/10 text-amber-400 border border-amber-500/30',
    replied: 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/30',
    meeting: 'bg-purple-500/10 text-purple-400 border border-purple-500/30',
    won: 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30',
    lost: 'bg-rose-500/10 text-rose-400 border border-rose-500/30',
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl shadow-xl overflow-hidden">
      {/* Table Filters Header */}
      <div className="p-4 border-b border-slate-800 flex flex-col md:flex-row gap-3 items-center justify-between bg-slate-950/40">
        <div className="relative w-full md:w-72">
          <Search className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
          <input
            type="text"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search leads by name or area..."
            className="w-full bg-slate-950 border border-slate-800 rounded-xl pl-9 pr-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
          />
        </div>

        <div className="flex flex-wrap items-center gap-2.5 w-full md:w-auto">
          {/* Category Filter */}
          <select
            value={category}
            onChange={(e) => onCategoryChange(e.target.value)}
            className="bg-slate-950 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-indigo-500 cursor-pointer"
          >
            <option value="">All Categories</option>
            <option value="Café">Cafés</option>
            <option value="Restaurant">Restaurants</option>
            <option value="Shop">Retail Shops</option>
            <option value="Salon">Salons & Beauty</option>
            <option value="Gym">Gyms & Fitness</option>
            <option value="Clinic">Clinics & Healthcare</option>
            <option value="Coaching">Coaching & Education</option>
          </select>

          {/* Pipeline Stage Filter */}
          <select
            value={status}
            onChange={(e) => onStatusChange(e.target.value)}
            className="bg-slate-950 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-indigo-500 cursor-pointer"
          >
            <option value="">All Stages</option>
            <option value="new">New</option>
            <option value="qualified">Qualified</option>
            <option value="contacted">Contacted</option>
            <option value="replied">Replied</option>
            <option value="meeting">Meeting</option>
            <option value="won">Won</option>
            <option value="lost">Lost</option>
          </select>

          {/* Min Score Filter */}
          <select
            value={minScore}
            onChange={(e) => onMinScoreChange(parseInt(e.target.value))}
            className="bg-slate-950 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-indigo-500 cursor-pointer"
          >
            <option value="0">Any Score</option>
            <option value="50">Score 50+</option>
            <option value="70">Score 70+ (High)</option>
          </select>

          {/* Only Missing Web */}
          <label className="flex items-center gap-1.5 text-xs text-slate-300 bg-slate-950 border border-slate-800 rounded-xl px-2.5 py-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={onlyMissingWeb}
              onChange={(e) => onOnlyMissingWebChange(e.target.checked)}
              className="w-3.5 h-3.5 rounded bg-slate-900 border-slate-700 text-indigo-500"
            />
            <span>No Website</span>
          </label>
        </div>
      </div>

      {/* Table Data */}
      <div className="overflow-x-auto -webkit-overflow-scrolling-touch">
        <table className="w-full min-w-[640px] text-left text-xs">
          <thead className="bg-slate-950/60 border-b border-slate-800 text-slate-400 uppercase tracking-wider font-semibold">
            <tr>
              <th
                onClick={() => onSortToggle('score')}
                className="py-3 px-4 cursor-pointer hover:text-slate-200 transition select-none"
              >
                <div className="flex items-center gap-1">
                  <span>Score</span>
                  {sortBy === 'score' ? (
                    sortDir === 'desc' ? <ArrowDown className="w-3 h-3 text-indigo-400" /> : <ArrowUp className="w-3 h-3 text-indigo-400" />
                  ) : (
                    <ArrowUpDown className="w-3 h-3 opacity-40" />
                  )}
                </div>
              </th>
              <th
                onClick={() => onSortToggle('name')}
                className="py-3 px-4 cursor-pointer hover:text-slate-200 transition select-none"
              >
                <div className="flex items-center gap-1">
                  <span>Business Name</span>
                  {sortBy === 'name' ? (
                    sortDir === 'desc' ? <ArrowDown className="w-3 h-3 text-indigo-400" /> : <ArrowUp className="w-3 h-3 text-indigo-400" />
                  ) : (
                    <ArrowUpDown className="w-3 h-3 opacity-40" />
                  )}
                </div>
              </th>
              <th className="py-3 px-4">Category</th>
              <th className="py-3 px-4">Contact</th>
              <th className="py-3 px-4">Website Audit</th>
              <th className="py-3 px-4">Pipeline Stage</th>
              <th className="py-3 px-4 text-right">Actions</th>
            </tr>
          </thead>

          <tbody className="divide-y divide-slate-800/60">
            {leads.length === 0 ? (
              <tr>
                <td colSpan={7} className="text-center py-12 text-slate-500">
                  <SlidersHorizontal className="w-8 h-8 mx-auto mb-2 opacity-40" />
                  <p className="text-sm font-medium">No leads match current filters.</p>
                  <p className="text-xs text-slate-600 mt-0.5">
                    Start a discovery search above or adjust your search keywords.
                  </p>
                </td>
              </tr>
            ) : (
              leads.map((lead) => {
                const audit = lead.audit_result;
                return (
                  <tr
                    key={lead.id}
                    className="hover:bg-slate-800/40 transition cursor-pointer group"
                    onClick={() => onSelectLead(lead)}
                  >
                    {/* Score */}
                    <td className="py-3 px-4 whitespace-nowrap">
                      <ScoreBadge score={lead.score} priority={lead.score_breakdown?.priority} size="sm" />
                    </td>

                    {/* Name & Address */}
                    <td className="py-3 px-4 max-w-xs">
                      <div className="font-semibold text-slate-100 group-hover:text-indigo-400 transition truncate">
                        {lead.name}
                      </div>
                      <div className="text-[11px] text-slate-400 truncate mt-0.5">
                        {lead.address || 'Address unrecorded in listing'}
                      </div>
                    </td>

                    {/* Category */}
                    <td className="py-3 px-4 whitespace-nowrap text-slate-300">
                      <span className="px-2 py-0.5 rounded bg-slate-800/80 text-[11px] font-medium border border-slate-700/60">
                        {lead.category}
                      </span>
                    </td>

                    {/* Contact Phone */}
                    <td className="py-3 px-4 whitespace-nowrap">
                      {lead.phone ? (
                        <div className="flex items-center gap-1.5 text-slate-200">
                          <Phone className="w-3 h-3 text-indigo-400 flex-shrink-0" />
                          <span className="font-mono text-[11px]">{lead.phone}</span>
                        </div>
                      ) : (
                        <span className="text-slate-600 text-[11px] italic">No phone</span>
                      )}
                    </td>

                    {/* Website Audit Status */}
                    <td className="py-3 px-4 whitespace-nowrap">
                      {audit?.has_website_url ? (
                        audit.is_reachable ? (
                          <div className="flex items-center gap-1.5">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                            <span className="text-emerald-400 font-medium">Reachable</span>
                            {!audit.has_viewport_meta && (
                              <span className="text-[10px] text-rose-400 bg-rose-500/10 px-1 rounded border border-rose-500/20">
                                No mobile viewport
                              </span>
                            )}
                          </div>
                        ) : (
                          <div className="flex items-center gap-1.5">
                            <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
                            <span className="text-rose-400 font-medium">Site Down / Error</span>
                          </div>
                        )
                      ) : (
                        <div className="flex items-center gap-1.5 text-amber-400">
                          <Globe className="w-3 h-3 text-amber-400 flex-shrink-0" />
                          <span className="font-medium">No Website Listed</span>
                        </div>
                      )}
                    </td>

                    {/* Pipeline Stage */}
                    <td className="py-3 px-4 whitespace-nowrap">
                      <span
                        className={`text-[11px] font-semibold uppercase px-2 py-0.5 rounded-full ${
                          PIPELINE_STATUS_COLORS[lead.pipeline_status] || 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        {lead.pipeline_status}
                      </span>
                    </td>

                    {/* Actions */}
                    <td className="py-3 px-4 whitespace-nowrap text-right" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => onSelectLead(lead)}
                          title="Open Pitch & Audit"
                          className="p-1.5 rounded-lg bg-slate-800 hover:bg-indigo-600 text-slate-300 hover:text-white transition cursor-pointer"
                        >
                          <Eye className="w-3.5 h-3.5" />
                        </button>

                        <button
                          onClick={async () => {
                            if (confirm(`Delete ${lead.name}?`)) {
                              await onDeleteLead(lead.id);
                            }
                          }}
                          title="Delete Lead"
                          className="p-1.5 rounded-lg bg-slate-800 hover:bg-rose-600 text-slate-400 hover:text-white transition cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination Footer */}
      <div className="p-4 border-t border-slate-800 flex items-center justify-between text-xs text-slate-400 bg-slate-950/40">
        <div>
          Showing <span className="text-slate-200 font-semibold">{leads.length}</span> of{' '}
          <span className="text-slate-200 font-semibold">{total}</span> leads
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => onPageChange(page - 1)}
            disabled={page <= 1}
            aria-label="Previous Page"
            className="p-2 min-h-[36px] min-w-[36px] flex items-center justify-center rounded-lg border border-slate-800 hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed transition cursor-pointer"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="px-1">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => onPageChange(page + 1)}
            disabled={page >= totalPages}
            aria-label="Next Page"
            className="p-2 min-h-[36px] min-w-[36px] flex items-center justify-center rounded-lg border border-slate-800 hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed transition cursor-pointer"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
};
