import React from 'react';
import { Target, Download, LayoutGrid, List, Trash2, LogOut, User as UserIcon, Smartphone, Wifi, WifiOff } from 'lucide-react';
import { api } from '../services/api';
import type { User } from '../types';

interface NavbarProps {
  viewMode: 'table' | 'kanban';
  onViewModeChange: (mode: 'table' | 'kanban') => void;
  onClearLeads: () => void;
  leadCount: number;
  user?: User | null;
  onLogout?: () => void;
  isOnline?: boolean;
  canInstall?: boolean;
  onInstall?: () => void;
}

export const Navbar: React.FC<NavbarProps> = ({
  viewMode,
  onViewModeChange,
  onClearLeads,
  leadCount,
  user,
  onLogout,
  isOnline = true,
  canInstall = false,
  onInstall,
}) => {
  return (
    <header className="border-b border-slate-800 bg-slate-950/90 backdrop-blur-md sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-3 sm:px-6 py-2.5 sm:py-3.5 flex flex-wrap items-center justify-between gap-3">
        {/* Brand & Connectivity Indicator */}
        <div className="flex items-center gap-2.5 sm:gap-3">
          <div className="w-8 h-8 sm:w-9 sm:h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-600/30 shrink-0">
            <Target className="w-4 h-4 sm:w-5 sm:h-5 text-white" />
          </div>
          <div>
            <h1 className="font-bold text-sm sm:text-base text-slate-100 flex items-center gap-1.5 sm:gap-2">
              WebsiteLead Hunter
              <span className="hidden sm:inline text-[10px] font-mono px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/30">
                PWA v1
              </span>
              {/* Online / Offline Status Dot */}
              <span
                className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ${
                  isOnline
                    ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                    : 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                }`}
                title={isOnline ? 'Connected to local server' : 'Working offline'}
              >
                {isOnline ? <Wifi className="w-2.5 h-2.5" /> : <WifiOff className="w-2.5 h-2.5" />}
                <span className="hidden xs:inline">{isOnline ? 'Online' : 'Offline'}</span>
              </span>
            </h1>
            <p className="hidden md:block text-[11px] text-slate-400">Automated Client Acquisition Platform</p>
          </div>
        </div>

        {/* View Switcher and Actions */}
        <div className="flex items-center gap-1.5 sm:gap-3">
          {/* PWA Install Button (if browser triggered beforeinstallprompt) */}
          {canInstall && onInstall && (
            <button
              onClick={onInstall}
              title="Install WebsiteLead Hunter as a standalone app"
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium shadow-md shadow-indigo-600/20 transition cursor-pointer"
            >
              <Smartphone className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Install App</span>
            </button>
          )}

          {/* View Mode Toggle */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-0.5 sm:p-1 flex items-center gap-0.5 sm:gap-1">
            <button
              onClick={() => onViewModeChange('table')}
              className={`flex items-center gap-1 sm:gap-1.5 px-2.5 sm:px-3 py-1 rounded-lg text-xs font-medium transition cursor-pointer min-h-[36px] sm:min-h-0 ${
                viewMode === 'table'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <List className="w-3.5 h-3.5" />
              <span className="hidden xs:inline">Table</span>
            </button>

            <button
              onClick={() => onViewModeChange('kanban')}
              className={`flex items-center gap-1 sm:gap-1.5 px-2.5 sm:px-3 py-1 rounded-lg text-xs font-medium transition cursor-pointer min-h-[36px] sm:min-h-0 ${
                viewMode === 'kanban'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <LayoutGrid className="w-3.5 h-3.5" />
              <span className="hidden xs:inline">Pipeline</span>
            </button>
          </div>

          {/* Export CSV Button */}
          <a
            href={api.getExportUrl()}
            download
            className="inline-flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-200 border border-slate-800 text-xs font-medium transition cursor-pointer min-h-[36px] sm:min-h-0"
          >
            <Download className="w-3.5 h-3.5 text-indigo-400" />
            <span className="hidden sm:inline">Export</span>
          </a>

          {/* Clear Leads */}
          {leadCount > 0 && (
            <button
              onClick={onClearLeads}
              title="Delete all stored leads"
              className="p-2 sm:p-1.5 rounded-xl text-slate-500 hover:text-rose-400 hover:bg-rose-500/10 transition cursor-pointer min-h-[36px] min-w-[36px] flex items-center justify-center"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}

          {/* User Status & Logout */}
          {user && (
            <div className="flex items-center gap-1.5 sm:gap-2 pl-1.5 sm:pl-2 border-l border-slate-800">
              <div className="hidden lg:flex items-center gap-1.5 text-xs text-slate-300 bg-slate-900 px-2.5 py-1 rounded-lg border border-slate-800">
                <UserIcon className="w-3.5 h-3.5 text-indigo-400" />
                <span className="max-w-[120px] truncate">{user.email}</span>
              </div>
              {onLogout && (
                <button
                  onClick={onLogout}
                  title="Log out"
                  className="flex items-center gap-1 p-2 sm:px-2.5 sm:py-1.5 rounded-xl text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 border border-slate-800 transition text-xs cursor-pointer min-h-[36px] min-w-[36px] justify-center"
                >
                  <LogOut className="w-3.5 h-3.5" />
                  <span className="hidden sm:inline">Logout</span>
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </header>
  );
};
