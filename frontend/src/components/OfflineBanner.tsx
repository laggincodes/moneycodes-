import React from 'react';
import { WifiOff, RefreshCw, AlertTriangle, CheckCircle, CloudUpload } from 'lucide-react';
import type { SyncResult } from '../services/syncEngine';

interface OfflineBannerProps {
  isOnline: boolean;
  syncResult: SyncResult | null;
  onManualSync: () => void;
  isSyncing: boolean;
}

export const OfflineBanner: React.FC<OfflineBannerProps> = ({
  isOnline,
  syncResult,
  onManualSync,
  isSyncing,
}) => {
  const pendingCount = syncResult?.pendingCount || 0;
  const failedCount = syncResult?.failedCount || 0;
  const conflictCount = syncResult?.conflictCount || 0;

  // Nothing to report if fully online with zero pending or failed items
  if (isOnline && pendingCount === 0 && failedCount === 0 && conflictCount === 0 && !isSyncing) {
    return null;
  }

  return (
    <div className="w-full bg-slate-900/95 border-b border-slate-800 backdrop-blur-md px-4 py-2 text-xs transition-all animate-in fade-in duration-200">
      <div className="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-2">
        {/* Left Status Message */}
        <div className="flex items-center gap-2">
          {!isOnline ? (
            <div className="flex items-center gap-1.5 text-amber-400 font-medium">
              <WifiOff className="w-3.5 h-3.5 shrink-0" />
              <span>Offline Mode — App shell active. Changes will save to local outbox.</span>
            </div>
          ) : isSyncing ? (
            <div className="flex items-center gap-1.5 text-blue-400 font-medium">
              <RefreshCw className="w-3.5 h-3.5 animate-spin shrink-0" />
              <span>Syncing pending offline changes with server...</span>
            </div>
          ) : failedCount > 0 || conflictCount > 0 ? (
            <div className="flex items-center gap-1.5 text-rose-400 font-medium">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
              <span>
                {failedCount > 0 && `${failedCount} change(s) failed to sync. `}
                {conflictCount > 0 && `${conflictCount} conflict(s) detected.`}
              </span>
            </div>
          ) : pendingCount > 0 ? (
            <div className="flex items-center gap-1.5 text-indigo-400 font-medium">
              <CloudUpload className="w-3.5 h-3.5 shrink-0" />
              <span>{pendingCount} offline change(s) queued for synchronization.</span>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 text-emerald-400 font-medium">
              <CheckCircle className="w-3.5 h-3.5 shrink-0" />
              <span>All changes synced with server.</span>
            </div>
          )}
        </div>

        {/* Right Action Buttons */}
        {isOnline && (pendingCount > 0 || failedCount > 0) && (
          <button
            onClick={onManualSync}
            disabled={isSyncing}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 text-[11px] font-medium transition cursor-pointer disabled:opacity-50"
          >
            <RefreshCw className={`w-3 h-3 ${isSyncing ? 'animate-spin' : ''}`} />
            <span>{isSyncing ? 'Syncing...' : 'Sync Now'}</span>
          </button>
        )}
      </div>
    </div>
  );
};
