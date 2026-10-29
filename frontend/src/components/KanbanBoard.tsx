import React from 'react';
import type { Lead, PipelineStatus } from '../types';
import { ScoreBadge } from './ScoreBadge';
import { Phone, Globe, ChevronRight, Eye } from 'lucide-react';

interface KanbanBoardProps {
  leads: Lead[];
  onSelectLead: (lead: Lead) => void;
  onUpdatePipeline: (leadId: string, status: PipelineStatus) => Promise<void>;
}

const COLUMNS: Array<{ id: PipelineStatus; title: string; color: string; border: string }> = [
  { id: 'new', title: 'New', color: 'bg-slate-800/60', border: 'border-slate-700' },
  { id: 'qualified', title: 'Qualified', color: 'bg-indigo-950/40', border: 'border-indigo-800/40' },
  { id: 'contacted', title: 'Contacted', color: 'bg-amber-950/40', border: 'border-amber-800/40' },
  { id: 'replied', title: 'Replied', color: 'bg-cyan-950/40', border: 'border-cyan-800/40' },
  { id: 'meeting', title: 'Meeting Scheduled', color: 'bg-purple-950/40', border: 'border-purple-800/40' },
  { id: 'won', title: 'Won Deals 🎉', color: 'bg-emerald-950/40', border: 'border-emerald-800/40' },
  { id: 'lost', title: 'Lost / Closed', color: 'bg-rose-950/30', border: 'border-rose-900/40' },
];

const NEXT_STAGE: Record<PipelineStatus, PipelineStatus | null> = {
  new: 'qualified',
  qualified: 'contacted',
  contacted: 'replied',
  replied: 'meeting',
  meeting: 'won',
  won: null,
  lost: null,
};

export const KanbanBoard: React.FC<KanbanBoardProps> = ({
  leads,
  onSelectLead,
  onUpdatePipeline,
}) => {
  return (
    <div className="overflow-x-auto -webkit-overflow-scrolling-touch pb-4">
      <div className="flex gap-4 min-w-[1280px]">
        {COLUMNS.map((col) => {
          const colLeads = leads.filter((l) => l.pipeline_status === col.id);

          return (
            <div
              key={col.id}
              className={`flex-1 rounded-2xl border ${col.border} ${col.color} p-3 flex flex-col max-h-[750px] shadow-lg`}
            >
              {/* Column Header */}
              <div className="flex items-center justify-between pb-2 mb-3 border-b border-slate-800/80">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-xs uppercase tracking-wider text-slate-200">
                    {col.title}
                  </span>
                  <span className="text-[11px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-300">
                    {colLeads.length}
                  </span>
                </div>
              </div>

              {/* Cards List */}
              <div className="flex-1 overflow-y-auto space-y-2.5 pr-1">
                {colLeads.length === 0 ? (
                  <div className="text-center py-8 text-xs text-slate-600 italic">
                    No leads in this stage
                  </div>
                ) : (
                  colLeads.map((lead) => {
                    const nextStage = NEXT_STAGE[lead.pipeline_status];
                    return (
                      <div
                        key={lead.id}
                        onClick={() => onSelectLead(lead)}
                        className="bg-slate-900/90 border border-slate-800 hover:border-indigo-500/50 rounded-xl p-3 shadow-md hover:shadow-indigo-500/5 transition cursor-pointer group"
                      >
                        <div className="flex items-start justify-between gap-2 mb-1.5">
                          <h4 className="font-semibold text-xs text-slate-100 group-hover:text-indigo-400 transition line-clamp-1">
                            {lead.name}
                          </h4>
                          <ScoreBadge score={lead.score} priority={lead.score_breakdown?.priority} size="sm" />
                        </div>

                        <span className="text-[10px] text-slate-400 block mb-2 font-medium">
                          {lead.category}
                        </span>

                        <div className="space-y-1 mb-3 text-[11px] text-slate-400">
                          {lead.phone ? (
                            <div className="flex items-center gap-1 text-slate-300">
                              <Phone className="w-3 h-3 text-indigo-400" />
                              <span className="font-mono text-[10px]">{lead.phone}</span>
                            </div>
                          ) : (
                            <span className="text-slate-600 italic">No phone</span>
                          )}

                          {lead.website ? (
                            <div className="flex items-center gap-1 text-emerald-400">
                              <Globe className="w-3 h-3" />
                              <span className="truncate">Website Active</span>
                            </div>
                          ) : (
                            <div className="flex items-center gap-1 text-amber-400 font-medium">
                              <Globe className="w-3 h-3" />
                              <span>No Website</span>
                            </div>
                          )}
                        </div>

                        {/* Card Footer Actions */}
                        <div
                          className="pt-2 border-t border-slate-800/80 flex items-center justify-between"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <button
                            onClick={() => onSelectLead(lead)}
                            className="text-[11px] text-slate-400 hover:text-indigo-400 transition flex items-center gap-1 cursor-pointer"
                          >
                            <Eye className="w-3 h-3" /> View Pitch
                          </button>

                          {nextStage && (
                            <button
                              onClick={() => onUpdatePipeline(lead.id, nextStage)}
                              className="text-[10px] px-2 py-1 rounded bg-indigo-600/20 hover:bg-indigo-600/40 text-indigo-300 border border-indigo-500/30 flex items-center gap-1 transition cursor-pointer"
                            >
                              <span>Next</span>
                              <ChevronRight className="w-2.5 h-2.5" />
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
