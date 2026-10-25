import { useState, useEffect, useCallback, useRef } from 'react';
import type { JobResponse, Lead, PipelineStatus } from './types';
import { api } from './services/api';
import { Navbar } from './components/Navbar';
import { DiscoveryPanel } from './components/DiscoveryPanel';
import { JobStatusBanner } from './components/JobStatusBanner';
import { LeadTable } from './components/LeadTable';
import { KanbanBoard } from './components/KanbanBoard';
import { LeadDetailModal } from './components/LeadDetailModal';

export function App() {
  const [viewMode, setViewMode] = useState<'table' | 'kanban'>('table');
  const [activeJob, setActiveJob] = useState<JobResponse | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [totalLeads, setTotalLeads] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(25);
  const [totalPages, setTotalPages] = useState(1);

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

  // Load leads
  const fetchLeads = useCallback(async () => {
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
    } catch (err) {
      console.error('Error fetching leads:', err);
    }
  }, [search, category, pipelineStatus, minScore, onlyMissingWeb, sortBy, sortDir, page, pageSize]);

  // Initial load
  useEffect(() => {
    fetchLeads();

    // Check if any job was running or recently active
    api.getActiveJob().then((job) => {
      if (job) setActiveJob(job);
    });
  }, [fetchLeads]);

  // Job status polling
  useEffect(() => {
    if (activeJob && (activeJob.status === 'running' || activeJob.status === 'pending')) {
      pollIntervalRef.current = window.setInterval(async () => {
        try {
          const updatedJob = await api.getJob(activeJob.id);
          setActiveJob(updatedJob);

          // Refresh leads list as new leads are processed
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
  }, [activeJob?.id, activeJob?.status, activeJob?.processed_count, fetchLeads]);

  // Actions
  const handleStartJob = async (params: {
    location: string;
    radius_km: number;
    categories: string[];
    max_leads: number;
    filter_only_with_phone: boolean;
    filter_only_missing_website: boolean;
  }) => {
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
    await api.pauseJob(activeJob.id);
    const updated = await api.getJob(activeJob.id);
    setActiveJob(updated);
  };

  const handleResumeJob = async () => {
    if (!activeJob) return;
    await api.resumeJob(activeJob.id);
    const updated = await api.getJob(activeJob.id);
    setActiveJob(updated);
  };

  const handleCancelJob = async () => {
    if (!activeJob) return;
    if (confirm('Cancel active discovery and audit job?')) {
      await api.cancelJob(activeJob.id);
      const updated = await api.getJob(activeJob.id);
      setActiveJob(updated);
    }
  };

  const handleUpdatePipeline = async (leadId: string, status: PipelineStatus, notes?: string) => {
    try {
      const updated = await api.updateLeadPipeline(leadId, status, notes);
      setLeads((prev) => prev.map((l) => (l.id === leadId ? updated : l)));
      if (selectedLead && selectedLead.id === leadId) {
        setSelectedLead(updated);
      }
    } catch (err: any) {
      alert(`Error updating stage: ${err.message}`);
    }
  };

  const handleDeleteLead = async (leadId: string) => {
    try {
      await api.deleteLead(leadId);
      setLeads((prev) => prev.filter((l) => l.id !== leadId));
      setTotalLeads((prev) => Math.max(0, prev - 1));
      if (selectedLead && selectedLead.id === leadId) {
        setSelectedLead(null);
      }
    } catch (err: any) {
      alert(`Error deleting lead: ${err.message}`);
    }
  };

  const handleClearAllLeads = async () => {
    if (confirm('Are you sure you want to delete ALL stored leads from the database?')) {
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
      />

      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6">
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
    </div>
  );
}

export default App;
