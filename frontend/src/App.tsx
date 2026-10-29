import { useState, useEffect, useCallback, useRef } from 'react';
import type { JobResponse, Lead, PipelineStatus, User } from './types';
import { api } from './services/api';
import { offlineStore } from './services/offlineStore';
import { syncEngine, subscribeToSyncEvents, type SyncResult } from './services/syncEngine';
import { Navbar } from './components/Navbar';
import { OfflineBanner } from './components/OfflineBanner';
import { DiscoveryPanel } from './components/DiscoveryPanel';
import { JobStatusBanner } from './components/JobStatusBanner';
import { LeadTable } from './components/LeadTable';
import { KanbanBoard } from './components/KanbanBoard';
import { LeadDetailModal } from './components/LeadDetailModal';
import { AuthModal } from './components/AuthModal';

export function App() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [viewMode, setViewMode] = useState<'table' | 'kanban'>('table');
  const [activeJob, setActiveJob] = useState<JobResponse | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [totalLeads, setTotalLeads] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(25);
  const [totalPages, setTotalPages] = useState(1);

  // Network & PWA State
  const [isOnline, setIsOnline] = useState<boolean>(navigator.onLine);
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);
  const [isSyncing, setIsSyncing] = useState<boolean>(false);
  const [deferredPrompt, setDeferredPrompt] = useState<any>(null);
  const [canInstall, setCanInstall] = useState<boolean>(false);

  // Filters
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [pipelineStatus, setPipelineStatus] = useState('');
  const [minScore, setMinScore] = useState(0);
  const [onlyMissingWeb, setOnlyMissingWeb] = useState(false);
  const [sortBy, setSortBy] = useState('score');
  const [sortDir, setSortDir] = useState('desc');

  // Selected lead for detail modal
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);

  // Polling ref
  const pollIntervalRef = useRef<number | null>(null);

  // Load leads with offline cache fallback
  const fetchLeads = useCallback(async () => {
    if (!currentUser) return;
    try {
      const data = await api.getLeads({
        search,
        category,
        pipeline_status: pipelineStatus,
        min_score: minScore > 0 ? minScore : undefined,
        missing_website: onlyMissingWeb ? true : undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
        page,
        page_size: pageSize,
      });
      setLeads(data.items);
      setTotalLeads(data.total);
      setTotalPages(data.total_pages);

      // Cache leads locally for offline browsing
      await offlineStore.cacheUserLeads(currentUser.id, data.items);
    } catch (err) {
      console.warn('[App] Failed to fetch leads from server; checking local offline store:', err);
      // Fall back to account-isolated offline cache
      const cached = await offlineStore.getCachedUserLeads(currentUser.id);
      if (cached && cached.length > 0) {
        setLeads(cached);
        setTotalLeads(cached.length);
        setTotalPages(1);
      }
    }
  }, [currentUser, search, category, pipelineStatus, minScore, onlyMissingWeb, sortBy, sortDir, page, pageSize]);

  // Check auth session on startup
  useEffect(() => {
    api.getMe().then((user) => {
      setCurrentUser(user);
      setAuthChecked(true);
      if (user) {
        api.getActiveJob().then((job) => {
          if (job) setActiveJob(job);
        });
        // Check outbox status
        offlineStore.getPendingQueue(user.id).then((items) => {
          setSyncResult({
            syncedCount: 0,
            pendingCount: items.length,
            failedCount: items.filter((i) => i.status === 'failed').length,
            conflictCount: items.filter((i) => i.status === 'conflict').length,
            offline: !navigator.onLine,
          });
        });
        syncEngine.registerBackgroundSync();
      }
    });
  }, []);

  // Online / Offline and PWA Event Listeners
  useEffect(() => {
    const handleOnline = () => {
      setIsOnline(true);
      if (currentUser) {
        triggerSync();
      }
    };
    const handleOffline = () => {
      setIsOnline(false);
    };

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);

    // Listen for PWA installation prompt
    const handleBeforeInstall = (e: Event) => {
      e.preventDefault();
      setDeferredPrompt(e);
      setCanInstall(true);
    };
    window.addEventListener('beforeinstallprompt', handleBeforeInstall);

    // Subscribe to sync engine status broadcasts
    const unsubscribeSync = subscribeToSyncEvents((res) => {
      setSyncResult(res);
      setIsSyncing(false);
      // Refresh leads list after sync completes
      if (res.syncedCount > 0) {
        fetchLeads();
      }
    });

    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
      window.removeEventListener('beforeinstallprompt', handleBeforeInstall);
      unsubscribeSync();
    };
  }, [currentUser, fetchLeads]);

  // Fetch leads when user is authenticated and filters change
  useEffect(() => {
    if (currentUser) {
      fetchLeads();
    }
  }, [currentUser, fetchLeads]);

  // Job status polling
  useEffect(() => {
    if (currentUser && isOnline && activeJob && (activeJob.status === 'running' || activeJob.status === 'pending')) {
      pollIntervalRef.current = window.setInterval(async () => {
        try {
          const updatedJob = await api.getJob(activeJob.id);
          setActiveJob(updatedJob);

          if (updatedJob.processed_count !== activeJob.processed_count || updatedJob.status !== 'running') {
            fetchLeads();
          }

          if (updatedJob.status !== 'running' && updatedJob.status !== 'pending') {
            if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
          }
        } catch (err) {
          console.error('Error polling job:', err);
        }
      }, 1500);
    }

    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, [currentUser, isOnline, activeJob?.id, activeJob?.status, activeJob?.processed_count, fetchLeads]);

  // Trigger manual or automated sync
  const triggerSync = async () => {
    if (!currentUser || isSyncing) return;
    setIsSyncing(true);
    try {
      await syncEngine.syncQueue(currentUser.id);
    } finally {
      setIsSyncing(false);
    }
  };

  // Auth Handlers
  const handleAuthSuccess = (user: User) => {
    setCurrentUser(user);
    api.getActiveJob().then((job) => {
      if (job) setActiveJob(job);
    });
    triggerSync();
  };

  const handleLogout = async () => {
    let purgeFailed = false;
    if (currentUser) {
      try {
        // Purge private offline data for this account on logout
        await offlineStore.clearUserOfflineData(currentUser.id);
      } catch (err) {
        console.error('[App] Failed to purge offline user data on logout:', err);
        purgeFailed = true;
        alert(
          'Security Warning: Failed to purge local offline data during logout. To prevent cross-account exposure, browser storage must be cleared manually before another user logs in on this device.'
        );
      }
    }
    try {
      await api.logout();
    } catch (err) {
      console.warn('[App] Logout network request failed:', err);
    }
    setCurrentUser(null);
    setActiveJob(null);
    setLeads([]);
    setTotalLeads(0);
    setSelectedLead(null);
    if (purgeFailed) {
      setSyncResult({
        syncedCount: 0,
        pendingCount: 0,
        failedCount: 1,
        conflictCount: 0,
        offline: false,
      });
    } else {
      setSyncResult(null);
    }
  };

  // PWA Install Action
  const handleInstallApp = async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    console.log('[PWA] User install choice:', outcome);
    setDeferredPrompt(null);
    setCanInstall(false);
  };

  // Job & Lead Actions
  const handleStartJob = async (params: {
    location: string;
    radius_km: number;
    categories: string[];
    max_leads: number;
    filter_only_with_phone: boolean;
    filter_only_missing_website: boolean;
  }) => {
    if (!isOnline) {
      alert('Starting a new discovery & audit job requires an active network connection.');
      return;
    }
    try {
      const job = await api.startJob(params);
      setActiveJob(job);
      fetchLeads();
    } catch (err: any) {
      alert(`Error starting job: ${err.message}`);
    }
  };

  const handlePauseJob = async () => {
    if (!activeJob) return;
    try {
      await api.pauseJob(activeJob.id);
      const updated = await api.getJob(activeJob.id);
      setActiveJob(updated);
    } catch (err: any) {
      alert(`Could not pause job: ${err.message}`);
    }
  };

  const handleResumeJob = async () => {
    if (!activeJob) return;
    try {
      await api.resumeJob(activeJob.id);
      const updated = await api.getJob(activeJob.id);
      setActiveJob(updated);
    } catch (err: any) {
      alert(`Could not resume job: ${err.message}`);
    }
  };

  const handleCancelJob = async () => {
    if (!activeJob) return;
    if (confirm('Cancel active discovery and audit job?')) {
      try {
        await api.cancelJob(activeJob.id);
        const updated = await api.getJob(activeJob.id);
        setActiveJob(updated);
      } catch (err: any) {
        alert(`Could not cancel job: ${err.message}`);
      }
    }
  };

  const handleUpdatePipeline = async (leadId: string, status: PipelineStatus, notes?: string) => {
    if (!currentUser) return;

    // Optimistic local UI update
    setLeads((prev) =>
      prev.map((l) => (l.id === leadId ? { ...l, pipeline_status: status, notes: notes ?? l.notes } : l)),
    );
    if (selectedLead && selectedLead.id === leadId) {
      setSelectedLead((prev) => (prev ? { ...prev, pipeline_status: status, notes: notes ?? prev.notes } : null));
    }

    // If offline, queue operation in outbox
    if (!isOnline) {
      await offlineStore.queuePipelineUpdate(currentUser.id, leadId, status, notes);
      const pending = await offlineStore.getPendingQueue(currentUser.id);
      setSyncResult((prev) => ({
        syncedCount: prev?.syncedCount || 0,
        pendingCount: pending.length,
        failedCount: prev?.failedCount || 0,
        conflictCount: prev?.conflictCount || 0,
        offline: true,
      }));
      return;
    }

    try {
      const updated = await api.updateLeadPipeline(leadId, status, notes);
      setLeads((prev) => prev.map((l) => (l.id === leadId ? updated : l)));
      if (selectedLead && selectedLead.id === leadId) {
        setSelectedLead(updated);
      }
    } catch (err: any) {
      if (err.status === 404) {
        // Lead was deleted on server by another client (stale state)
        console.warn(`[App] Lead ${leadId} not found on server; purging from local state.`);
        setLeads((prev) => prev.filter((l) => l.id !== leadId));
        setTotalLeads((prev) => Math.max(0, prev - 1));
        if (selectedLead && selectedLead.id === leadId) {
          setSelectedLead(null);
        }
        alert('This lead was deleted or does not exist on the server.');
      } else if (err.status && err.status < 500) {
        // Non-retryable client error (e.g. 400 Bad Request, 403 Forbidden)
        alert(`Failed to update pipeline: ${err.message}`);
        fetchLeads();
      } else {
        // Network timeout / connection drop -> save to offline outbox
        console.warn('[App] Live update failed; saving to offline outbox:', err);
        await offlineStore.queuePipelineUpdate(currentUser.id, leadId, status, notes);
        const pending = await offlineStore.getPendingQueue(currentUser.id);
        setSyncResult((prev) => ({
          syncedCount: prev?.syncedCount || 0,
          pendingCount: pending.length,
          failedCount: prev?.failedCount || 0,
          conflictCount: prev?.conflictCount || 0,
          offline: false,
        }));
      }
    }
  };

  const handleDeleteLead = async (leadId: string) => {
    if (!isOnline) {
      alert('Lead deletion requires a live server connection.');
      return;
    }
    try {
      await api.deleteLead(leadId);
      setLeads((prev) => prev.filter((l) => l.id !== leadId));
      setTotalLeads((prev) => Math.max(0, prev - 1));
      if (selectedLead && selectedLead.id === leadId) {
        setSelectedLead(null);
      }
    } catch (err: any) {
      if (err.status === 404) {
        // Already deleted on server
        setLeads((prev) => prev.filter((l) => l.id !== leadId));
        setTotalLeads((prev) => Math.max(0, prev - 1));
        if (selectedLead && selectedLead.id === leadId) {
          setSelectedLead(null);
        }
      } else {
        alert(`Error deleting lead: ${err.message}`);
      }
    }
  };

  const handleClearAllLeads = async () => {
    if (!isOnline) {
      alert('Bulk clearing leads requires a live server connection.');
      return;
    }
    if (confirm('Are you sure you want to delete ALL stored leads from your account?')) {
      await api.clearAllLeads();
      setLeads([]);
      setTotalLeads(0);
      setSelectedLead(null);
    }
  };

  const handleSortToggle = (field: string) => {
    if (sortBy === field) {
      setSortDir((prev) => (prev === 'desc' ? 'asc' : 'desc'));
    } else {
      setSortBy(field);
      setSortDir('desc');
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      <Navbar
        viewMode={viewMode}
        onViewModeChange={setViewMode}
        onClearLeads={handleClearAllLeads}
        leadCount={totalLeads}
        user={currentUser}
        onLogout={handleLogout}
        isOnline={isOnline}
        canInstall={canInstall}
        onInstall={handleInstallApp}
      />

      {/* Offline Status & Sync Banner */}
      <OfflineBanner
        isOnline={isOnline}
        syncResult={syncResult}
        onManualSync={triggerSync}
        isSyncing={isSyncing}
      />

      <main className="flex-1 max-w-7xl w-full mx-auto px-3 sm:px-6 py-4 sm:py-6">
        {/* Discovery Panel */}
        <DiscoveryPanel
          onStartJob={handleStartJob}
          isLoading={activeJob?.status === 'running' || activeJob?.status === 'pending'}
        />

        {/* Job Status Banner */}
        <JobStatusBanner
          job={activeJob}
          onPause={handlePauseJob}
          onResume={handleResumeJob}
          onCancel={handleCancelJob}
        />

        {/* Views: Table or Kanban */}
        {viewMode === 'table' ? (
          <LeadTable
            leads={leads}
            total={totalLeads}
            page={page}
            pageSize={pageSize}
            totalPages={totalPages}
            search={search}
            onSearchChange={setSearch}
            category={category}
            onCategoryChange={setCategory}
            status={pipelineStatus}
            onStatusChange={setPipelineStatus}
            minScore={minScore}
            onMinScoreChange={setMinScore}
            onlyMissingWeb={onlyMissingWeb}
            onOnlyMissingWebChange={setOnlyMissingWeb}
            sortBy={sortBy}
            sortDir={sortDir}
            onSortToggle={handleSortToggle}
            onPageChange={setPage}
            onSelectLead={setSelectedLead}
            onDeleteLead={handleDeleteLead}
          />
        ) : (
          <KanbanBoard
            leads={leads}
            onSelectLead={setSelectedLead}
            onUpdatePipeline={handleUpdatePipeline}
          />
        )}
      </main>

      {/* Lead Detail & Pitch Modal */}
      <LeadDetailModal
        lead={selectedLead}
        onClose={() => setSelectedLead(null)}
        onUpdatePipeline={handleUpdatePipeline}
        onDeleteLead={handleDeleteLead}
      />

      {/* Authentication Modal */}
      {authChecked && !currentUser && (
        <AuthModal
          isOpen={true}
          onSuccess={handleAuthSuccess}
        />
      )}
    </div>
  );
}

export default App;
