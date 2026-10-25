import React from 'react';
import { Target, Download, LayoutGrid, List, Trash2 } from 'lucide-react';
import { api } from '../services/api';

interface NavbarProps {
  viewMode: 'table' | 'kanban';
  onViewModeChange: (mode: 'table' | 'kanban') => void;
  onClearLeads: () => void;
  leadCount: number;
}

export const Navbar: React.FC<NavbarProps> = ({
  viewMode,
  onViewModeChange,
  onClearLeads,
  leadCount,
}) => {
  return (
    <header className="border-b border-slate-800 bg-slate-950/80 backdrop-blur-md sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3.5 flex items-center justify-between gap-4">
        {/* Logo and Brand */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-600/30">
            <Target className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="font-bold text-base text-slate-100 flex items-center gap-2">
              WebsiteLead Hunter
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/30">
                v1.0 Local
              </span>
            </h1>
            <p className="text-[11px] text-slate-400">Automated Client Acquisition for Freelance Developers</p>
          </div>
        </div>

        {/* View Switcher and Actions */}
        <div className="flex items-center gap-3">
          {/* View Mode Toggle */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-1 flex items-center gap-1">
            <button
              onClick={() => onViewModeChange('table')}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium transition cursor-pointer ${
                viewMode === 'table'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <List className="w-3.5 h-3.5" />
              <span>Table</span>
            </button>

            <button
              onClick={() => onViewModeChange('kanban')}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium transition cursor-pointer ${
                viewMode === 'kanban'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <LayoutGrid className="w-3.5 h-3.5" />
              <span>Pipeline</span>
            </button>
          </div>

          {/* Export CSV Button */}
          <a
            href={api.getExportUrl()}
            download
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-200 border border-slate-800 text-xs font-medium transition cursor-pointer"
          >
            <Download className="w-3.5 h-3.5 text-indigo-400" />
            <span className="hidden sm:inline">Export CSV</span>
          </a>

          {/* Clear Leads */}
          {leadCount > 0 && (
            <button
              onClick={onClearLeads}
              title="Delete all stored leads"
              className="p-1.5 rounded-xl text-slate-500 hover:text-rose-400 hover:bg-rose-500/10 transition cursor-pointer"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>
    </header>
  );
};
