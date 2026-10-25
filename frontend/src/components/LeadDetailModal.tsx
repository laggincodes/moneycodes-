import React, { useState } from 'react';
import type { Lead, PipelineStatus } from '../types';
import { ScoreBadge } from './ScoreBadge';
import {
  X,
  ExternalLink,
  Phone,
  Globe,
  MapPin,
  Clock,
  Copy,
  Check,
  MessageSquare,
  Mail,
  ShieldCheck,
  AlertTriangle,
  Smartphone,
  CheckCircle2,
  Trash2,
  Send,
} from 'lucide-react';

interface LeadDetailModalProps {
  lead: Lead | null;
  onClose: () => void;
  onUpdatePipeline: (leadId: string, status: PipelineStatus, notes?: string) => Promise<void>;
  onDeleteLead: (leadId: string) => Promise<void>;
}

export const LeadDetailModal: React.FC<LeadDetailModalProps> = ({
  lead,
  onClose,
  onUpdatePipeline,
  onDeleteLead,
}) => {
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [notes, setNotes] = useState(lead?.notes || '');
  const [isSavingNotes, setIsSavingNotes] = useState(false);
  const [activeTab, setActiveTab] = useState<'pitch' | 'audit' | 'score'>('pitch');

  if (!lead) return null;

  const copyToClipboard = (text: string, key: string) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const handleStatusChange = async (newStatus: PipelineStatus) => {
    await onUpdatePipeline(lead.id, newStatus, notes);
  };

  const handleSaveNotes = async () => {
    setIsSavingNotes(true);
    await onUpdatePipeline(lead.id, lead.pipeline_status, notes);
    setIsSavingNotes(false);
  };

  const handleMarkContacted = async () => {
    await onUpdatePipeline(lead.id, 'contacted', notes);
  };

  const handleDelete = async () => {
    if (confirm(`Are you sure you want to delete lead "${lead.name}"?`)) {
      await onDeleteLead(lead.id);
      onClose();
    }
  };

  const audit = lead.audit_result;
  const score = lead.score_breakdown;
  const pitch = lead.pitches;

  // Clean phone for WhatsApp web
  const cleanPhone = lead.phone ? lead.phone.replace(/[^0-9]/g, '') : null;
  const waUrl =
    cleanPhone && pitch?.whatsapp_draft
      ? `https://wa.me/${cleanPhone}?text=${encodeURIComponent(pitch.whatsapp_draft)}`
      : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm overflow-y-auto">
      <div className="bg-slate-900 border border-slate-800 rounded-2xl w-full max-w-4xl max-h-[92vh] flex flex-col shadow-2xl overflow-hidden my-auto animate-in fade-in duration-200">
        {/* Header */}
        <div className="p-5 border-b border-slate-800 flex items-start justify-between gap-4 bg-slate-950/40">
          <div>
            <div className="flex items-center gap-2.5 flex-wrap">
              <h2 className="text-xl font-bold text-slate-100">{lead.name}</h2>
              <ScoreBadge score={lead.score} priority={score?.priority} size="md" />
              <span className="text-xs font-medium px-2.5 py-0.5 rounded-full bg-slate-800 text-slate-300 border border-slate-700">
                {lead.category}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-3 mt-2 text-xs text-slate-400">
              {lead.address && (
                <span className="flex items-center gap-1">
                  <MapPin className="w-3.5 h-3.5 text-slate-500" /> {lead.address}
                </span>
              )}
              {lead.phone && (
                <span className="flex items-center gap-1 text-slate-300">
                  <Phone className="w-3.5 h-3.5 text-indigo-400" /> {lead.phone}
                </span>
              )}
              {lead.website ? (
                <a
                  href={lead.website}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1 text-indigo-400 hover:underline"
                >
                  <Globe className="w-3.5 h-3.5" /> Visit Website <ExternalLink className="w-3 h-3" />
                </a>
              ) : (
                <span className="flex items-center gap-1 text-amber-400/90 font-medium">
                  <Globe className="w-3.5 h-3.5 text-amber-400" /> No Website Listed
                </span>
              )}
              {lead.opening_hours && (
                <span className="flex items-center gap-1 text-slate-400">
                  <Clock className="w-3.5 h-3.5 text-slate-500" /> {lead.opening_hours}
                </span>
              )}
              <a
                href={lead.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-slate-500 hover:text-slate-300 underline"
              >
                OSM Listing
              </a>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            {/* Pipeline Stage Select */}
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-slate-400 font-medium">Stage:</span>
              <select
                value={lead.pipeline_status}
                onChange={(e) => handleStatusChange(e.target.value as PipelineStatus)}
                className="bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 font-medium focus:outline-none focus:border-indigo-500 cursor-pointer"
              >
                <option value="new">New</option>
                <option value="qualified">Qualified</option>
                <option value="contacted">Contacted</option>
                <option value="replied">Replied</option>
                <option value="meeting">Meeting</option>
                <option value="won">Won 🎉</option>
                <option value="lost">Lost</option>
              </select>
            </div>

            <button
              onClick={handleDelete}
              title="Delete Lead"
              className="p-1.5 text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 rounded-lg transition cursor-pointer"
            >
              <Trash2 className="w-4 h-4" />
            </button>

            <button
              onClick={onClose}
              className="p-1.5 text-slate-400 hover:text-slate-200 hover:bg-slate-800 rounded-lg transition cursor-pointer"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* View Tabs */}
        <div className="flex border-b border-slate-800 px-5 bg-slate-950/20 text-xs font-semibold">
          <button
            onClick={() => setActiveTab('pitch')}
            className={`py-2.5 px-4 border-b-2 transition cursor-pointer ${
              activeTab === 'pitch'
                ? 'border-indigo-500 text-indigo-400'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            Personalized Sales Package & Pitches
          </button>
          <button
            onClick={() => setActiveTab('audit')}
            className={`py-2.5 px-4 border-b-2 transition cursor-pointer ${
              activeTab === 'audit'
                ? 'border-indigo-500 text-indigo-400'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            Website Audit & Findings ({audit?.findings.length || 0})
          </button>
          <button
            onClick={() => setActiveTab('score')}
            className={`py-2.5 px-4 border-b-2 transition cursor-pointer ${
              activeTab === 'score'
                ? 'border-indigo-500 text-indigo-400'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            Lead Score Breakdown ({lead.score}/100)
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 overflow-y-auto space-y-6 flex-1">
          {/* TAB 1: SALES PACKAGE */}
          {activeTab === 'pitch' && pitch && (
            <div className="space-y-6">
              {/* Value Proposition Callout */}
              <div className="p-4 rounded-xl bg-indigo-950/30 border border-indigo-800/40">
                <span className="text-[11px] font-bold uppercase tracking-wider text-indigo-400 block mb-1">
                  Why This Business is Worth Contacting
                </span>
                <p className="text-sm text-slate-200 leading-relaxed">{pitch.why_worth_contacting}</p>
              </div>

              {/* Recommended Project Scope and Price Range */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="bg-slate-950/50 border border-slate-800 rounded-xl p-4">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400 block mb-1.5">
                    Recommended Project Scope
                  </span>
                  <p className="text-xs text-slate-200 leading-relaxed">{pitch.suggested_project_scope}</p>
                </div>

                <div className="bg-slate-950/50 border border-slate-800 rounded-xl p-4">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                      Suggested INR Price Range
                    </span>
                    <span className="text-[10px] bg-indigo-900/40 text-indigo-300 border border-indigo-700/50 px-2 py-0.5 rounded">
                      Recommendation
                    </span>
                  </div>
                  <p className="text-base font-bold text-emerald-400">{pitch.suggested_price_inr}</p>
                  <p className="text-[10px] text-slate-400 mt-1">
                    Customizable baseline benchmark for local freelance acquisition.
                  </p>
                </div>
              </div>

              {/* Recommended Website Features */}
              <div className="bg-slate-950/50 border border-slate-800 rounded-xl p-4">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400 block mb-2">
                  Tailored Website Features ({lead.category})
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {pitch.recommended_features.map((feat, idx) => (
                    <div key={idx} className="flex items-start gap-2 text-xs text-slate-300">
                      <CheckCircle2 className="w-4 h-4 text-emerald-400 flex-shrink-0 mt-0.5" />
                      <span>{feat}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Personalized First-Contact Email Draft */}
              <div className="bg-slate-950/60 border border-slate-800 rounded-xl p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <Mail className="w-4 h-4 text-indigo-400" />
                    <span className="text-xs font-bold uppercase tracking-wider text-slate-200">
                      First-Contact Email Draft
                    </span>
                  </div>
                  <button
                    onClick={() => copyToClipboard(pitch.email_draft, 'email')}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 transition cursor-pointer"
                  >
                    {copiedKey === 'email' ? (
                      <>
                        <Check className="w-3.5 h-3.5 text-emerald-400" /> Copied!
                      </>
                    ) : (
                      <>
                        <Copy className="w-3.5 h-3.5" /> Copy Draft
                      </>
                    )}
                  </button>
                </div>
                <div className="mb-2 text-xs bg-slate-900 px-3 py-1.5 rounded border border-slate-800 font-mono text-slate-300">
                  <span className="text-slate-500 font-sans">Subject:</span> {pitch.email_subject}
                </div>
                <pre className="text-xs text-slate-300 font-sans whitespace-pre-wrap leading-relaxed bg-slate-900/60 p-3 rounded-lg border border-slate-800/80">
                  {pitch.email_draft}
                </pre>
              </div>

              {/* Personalized WhatsApp Draft */}
              {pitch.whatsapp_draft && (
                <div className="bg-slate-950/60 border border-emerald-900/30 rounded-xl p-4">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <MessageSquare className="w-4 h-4 text-emerald-400" />
                      <span className="text-xs font-bold uppercase tracking-wider text-emerald-300">
                        WhatsApp Outreach Draft
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => copyToClipboard(pitch.whatsapp_draft!, 'wa')}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 transition cursor-pointer"
                      >
                        {copiedKey === 'wa' ? (
                          <>
                            <Check className="w-3.5 h-3.5 text-emerald-400" /> Copied
                          </>
                        ) : (
                          <>
                            <Copy className="w-3.5 h-3.5" /> Copy
                          </>
                        )}
                      </button>

                      {waUrl && (
                        <a
                          href={waUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1.5 px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-xs text-white font-medium transition cursor-pointer shadow-sm"
                        >
                          <Send className="w-3 h-3" /> Chat on WhatsApp
                        </a>
                      )}
                    </div>
                  </div>
                  <pre className="text-xs text-slate-300 font-sans whitespace-pre-wrap leading-relaxed bg-slate-900/60 p-3 rounded-lg border border-slate-800/80">
                    {pitch.whatsapp_draft}
                  </pre>
                </div>
              )}

              {/* Follow-up Draft */}
              <div className="bg-slate-950/60 border border-slate-800 rounded-xl p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-400">
                    Polite Follow-Up Draft (Day 3–4)
                  </span>
                  <button
                    onClick={() => copyToClipboard(pitch.follow_up_draft, 'followup')}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 transition cursor-pointer"
                  >
                    {copiedKey === 'followup' ? (
                      <>
                        <Check className="w-3.5 h-3.5 text-emerald-400" /> Copied
                      </>
                    ) : (
                      <>
                        <Copy className="w-3.5 h-3.5" /> Copy
                      </>
                    )}
                  </button>
                </div>
                <pre className="text-xs text-slate-300 font-sans whitespace-pre-wrap leading-relaxed bg-slate-900/60 p-3 rounded-lg border border-slate-800/80">
                  {pitch.follow_up_draft}
                </pre>
              </div>

              {/* Action: Mark as Contacted */}
              {lead.pipeline_status !== 'contacted' && (
                <div className="flex justify-end pt-2">
                  <button
                    onClick={handleMarkContacted}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold shadow-lg shadow-indigo-600/20 transition cursor-pointer"
                  >
                    <CheckCircle2 className="w-4 h-4" /> Mark as Contacted in Pipeline
                  </button>
                </div>
              )}
            </div>
          )}

          {/* TAB 2: AUDIT FINDINGS */}
          {activeTab === 'audit' && audit && (
            <div className="space-y-4">
              {/* Core Audit Metrics Badges */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-3">
                  <span className="text-[10px] text-slate-400 uppercase tracking-wider block">Reachability</span>
                  <div className="flex items-center gap-1.5 mt-1">
                    {audit.is_reachable ? (
                      <>
                        <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                        <span className="text-xs font-semibold text-emerald-300">
                          HTTP {audit.status_code || 200}
                        </span>
                      </>
                    ) : (
                      <>
                        <AlertTriangle className="w-4 h-4 text-rose-400" />
                        <span className="text-xs font-semibold text-rose-300">
                          {audit.has_website_url ? 'Down / Error' : 'No Site'}
                        </span>
                      </>
                    )}
                  </div>
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-xl p-3">
                  <span className="text-[10px] text-slate-400 uppercase tracking-wider block">SSL Encryption</span>
                  <div className="flex items-center gap-1.5 mt-1">
                    {audit.ssl_valid ? (
                      <>
                        <ShieldCheck className="w-4 h-4 text-emerald-400" />
                        <span className="text-xs font-semibold text-emerald-300">HTTPS Valid</span>
                      </>
                    ) : (
                      <>
                        <AlertTriangle className="w-4 h-4 text-amber-400" />
                        <span className="text-xs font-semibold text-amber-300">No HTTPS</span>
                      </>
                    )}
                  </div>
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-xl p-3">
                  <span className="text-[10px] text-slate-400 uppercase tracking-wider block">Mobile Viewport</span>
                  <div className="flex items-center gap-1.5 mt-1">
                    {audit.has_viewport_meta ? (
                      <>
                        <Smartphone className="w-4 h-4 text-emerald-400" />
                        <span className="text-xs font-semibold text-emerald-300">Configured</span>
                      </>
                    ) : (
                      <>
                        <AlertTriangle className="w-4 h-4 text-rose-400" />
                        <span className="text-xs font-semibold text-rose-300">Missing Viewport</span>
                      </>
                    )}
                  </div>
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-xl p-3">
                  <span className="text-[10px] text-slate-400 uppercase tracking-wider block">Conversion CTAs</span>
                  <div className="flex items-center gap-1.5 mt-1">
                    {audit.has_clear_cta ? (
                      <>
                        <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                        <span className="text-xs font-semibold text-emerald-300">Action Found</span>
                      </>
                    ) : (
                      <>
                        <AlertTriangle className="w-4 h-4 text-amber-400" />
                        <span className="text-xs font-semibold text-amber-300">Missing CTAs</span>
                      </>
                    )}
                  </div>
                </div>
              </div>

              {/* Findings List with Verified vs Inferred Pills */}
              <div className="border border-slate-800 rounded-xl divide-y divide-slate-800 overflow-hidden bg-slate-950/40">
                {audit.findings.map((f, idx) => (
                  <div key={idx} className="p-3.5 flex items-start justify-between gap-3 text-xs">
                    <div className="flex items-start gap-2.5">
                      {f.passed ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-400 flex-shrink-0 mt-0.5" />
                      ) : (
                        <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
                      )}
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-slate-200">{f.check}</span>
                          <span
                            className={`text-[9px] uppercase px-1.5 py-0.2 rounded font-mono font-medium ${
                              f.status === 'verified'
                                ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30'
                                : 'bg-slate-800 text-slate-400'
                            }`}
                          >
                            {f.status}
                          </span>
                        </div>
                        <p className="text-slate-300 mt-0.5 leading-relaxed">{f.summary}</p>
                        {f.evidence && (
                          <p className="text-[11px] text-slate-400 mt-1 font-mono bg-slate-900 px-2 py-1 rounded inline-block">
                            Evidence: {f.evidence}
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* TAB 3: SCORE BREAKDOWN */}
          {activeTab === 'score' && score && (
            <div className="space-y-4">
              <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between">
                <div>
                  <span className="text-[11px] text-slate-400 uppercase font-semibold">Total Lead Score</span>
                  <div className="flex items-center gap-3 mt-1">
                    <ScoreBadge score={score.total_score} priority={score.priority} size="lg" />
                    <span className="text-xs text-slate-300">{score.explanation}</span>
                  </div>
                </div>
              </div>

              <div className="border border-slate-800 rounded-xl divide-y divide-slate-800 overflow-hidden bg-slate-950/40">
                {score.factors.map((factor, idx) => (
                  <div key={idx} className="p-4 text-xs space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-200 text-sm">{factor.name}</span>
                      <span className="font-mono font-bold text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded border border-indigo-500/20">
                        +{factor.points} / {factor.max_points} pts
                      </span>
                    </div>
                    <p className="text-slate-300">{factor.reason}</p>
                    <p className="text-[11px] text-slate-400 font-mono bg-slate-900 px-2 py-1 rounded mt-1">
                      Evidence: {factor.evidence}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* User Notes Section */}
          <div className="border-t border-slate-800 pt-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-semibold text-slate-300">Outreach Notes & Activity Log</span>
              {lead.contacted_at && (
                <span className="text-[11px] text-emerald-400">
                  Contacted on {new Date(lead.contacted_at).toLocaleDateString()}
                </span>
              )}
            </div>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Record phone notes, response status, customized quotes, or meeting dates here..."
              rows={3}
              className="w-full bg-slate-950 border border-slate-800 rounded-xl p-3 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
            />
            <div className="flex justify-end mt-2">
              <button
                onClick={handleSaveNotes}
                disabled={isSavingNotes}
                className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium transition cursor-pointer"
              >
                {isSavingNotes ? 'Saving...' : 'Save Notes'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
