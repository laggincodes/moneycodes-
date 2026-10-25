import React, { useState } from 'react';
import type { JobResponse } from '../types';
import { Play, Pause, XCircle, Terminal, AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';

interface JobStatusBannerProps {
  job: JobResponse | null;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}

export const JobStatusBanner: React.FC<JobStatusBannerProps> = ({
  job,
  onPause,
  onResume,
  onCancel,
}) => {
  const [showLogs, setShowLogs] = useState(false);

  if (!job) return null;

  const isRunning = job.status === 'running';
  const isPaused = job.status === 'paused';
  const isCompleted = job.status === 'completed';
  const isFailed = job.status === 'failed';
  const isCancelled = job.status === 'cancelled';

  const progressPercent =
    job.max_leads > 0 ? Math.min(100, Math.round((job.processed_count / job.max_leads) * 100)) : 0;

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl mb-6">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        {/* Status text & info */}
        <div className="flex items-center gap-3">
          {isRunning && <Loader2 className="w-5 h-5 text-indigo-400 animate-spin flex-shrink-0" />}
          {isPaused && <Pause className="w-5 h-5 text-amber-400 flex-shrink-0" />}
          {isCompleted && <CheckCircle2 className="w-5 h-5 text-emerald-400 flex-shrink-0" />}
          {isFailed && <AlertCircle className="w-5 h-5 text-rose-400 flex-shrink-0" />}
          {isCancelled && <XCircle className="w-5 h-5 text-slate-400 flex-shrink-0" />}

          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-slate-100">
                {isRunning ? 'Discovery & Audit Job in Progress' : `Job ${job.status.toUpperCase()}`}
              </span>
              <span className="text-xs uppercase px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
                {job.status}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Area: <span className="text-slate-300">{job.location_query}</span> (Radius: {job.radius_km}km) •
              Found: <span className="text-slate-300">{job.total_found}</span> •
              Audited: <span className="text-indigo-400 font-medium">{job.processed_count}/{job.max_leads}</span>
            </p>
          </div>
        </div>

        {/* Action Controls */}
        <div className="flex items-center gap-2 self-end md:self-auto">
          {isRunning && (
            <button
              onClick={onPause}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-medium transition cursor-pointer"
            >
              <Pause className="w-3.5 h-3.5" /> Pause
            </button>
          )}

          {isPaused && (
            <button
              onClick={onResume}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs font-medium transition cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" /> Resume
            </button>
          )}

          {(isRunning || isPaused) && (
            <button
              onClick={onCancel}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/30 text-xs font-medium transition cursor-pointer"
            >
              <XCircle className="w-3.5 h-3.5" /> Cancel
            </button>
          )}

          <button
            onClick={() => setShowLogs(!showLogs)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition cursor-pointer"
          >
            <Terminal className="w-3.5 h-3.5" /> {showLogs ? 'Hide Logs' : 'View Logs'}
          </button>
        </div>
      </div>

      {/* Progress bar */}
      <div className="mt-3 w-full bg-slate-800 rounded-full h-2 overflow-hidden">
        <div
          className={`h-full transition-all duration-300 ${
            isCompleted ? 'bg-emerald-500' : isFailed ? 'bg-rose-500' : 'bg-indigo-500'
          }`}
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      {/* Error message callout if any */}
      {job.error_message && (
        <div className="mt-3 p-2.5 rounded-lg bg-rose-950/50 border border-rose-800/60 text-rose-300 text-xs flex items-center gap-2">
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          <span>{job.error_message}</span>
        </div>
      )}

      {/* Collapsible log view */}
      {showLogs && (
        <div className="mt-4 bg-slate-950 border border-slate-800 rounded-lg p-3 max-h-48 overflow-y-auto font-mono text-xs">
          <div className="text-slate-500 mb-1 border-b border-slate-800 pb-1 flex justify-between">
            <span>Live Background Queue Activity</span>
            <span>{job.logs.length} entries</span>
          </div>
          {job.logs.length === 0 ? (
            <p className="text-slate-600 italic">No logs recorded yet.</p>
          ) : (
            job.logs.map((log, idx) => (
              <div key={idx} className="py-0.5 leading-relaxed flex gap-2">
                <span className="text-slate-600 select-none">
                  {log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : '--:--:--'}
                </span>
                <span
                  className={
                    log.level === 'error'
                      ? 'text-rose-400'
                      : log.level === 'warning'
                      ? 'text-amber-400'
                      : 'text-slate-300'
                  }
                >
                  {log.message}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
};
