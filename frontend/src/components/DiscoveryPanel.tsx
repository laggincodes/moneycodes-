import React, { useState } from 'react';
import { Search, MapPin, Sparkles, Phone, Globe, Info } from 'lucide-react';

interface DiscoveryPanelProps {
  onStartJob: (params: {
    location: string;
    radius_km: number;
    categories: string[];
    max_leads: number;
    filter_only_with_phone: boolean;
    filter_only_missing_website: boolean;
  }) => Promise<void>;
  isLoading: boolean;
}

const CATEGORIES = [
  { id: 'cafes', label: 'Cafés' },
  { id: 'restaurants', label: 'Restaurants' },
  { id: 'shops', label: 'Retail Shops' },
  { id: 'salons', label: 'Salons & Beauty' },
  { id: 'gyms', label: 'Gyms & Fitness' },
  { id: 'clinics', label: 'Clinics & Doctors' },
  { id: 'coaching', label: 'Coaching & Education' },
  { id: 'other', label: 'Local Services' },
];

const PRESETS = [
  'Connaught Place, New Delhi',
  'Indiranagar, Bengaluru',
  'Bandra West, Mumbai',
  'Koramangala, Bengaluru',
  'Sector 29, Gurgaon',
];

export const DiscoveryPanel: React.FC<DiscoveryPanelProps> = ({ onStartJob, isLoading }) => {
  const [location, setLocation] = useState('Indiranagar, Bengaluru');
  const [radiusKm, setRadiusKm] = useState(1.5);
  const [selectedCats, setSelectedCats] = useState<string[]>([
    'cafes',
    'restaurants',
    'salons',
    'gyms',
    'clinics',
  ]);
  const [maxLeads, setMaxLeads] = useState(25);
  const [onlyPhone, setOnlyPhone] = useState(false);
  const [onlyMissingWeb, setOnlyMissingWeb] = useState(false);

  const toggleCategory = (catId: string) => {
    setSelectedCats((prev) =>
      prev.includes(catId) ? prev.filter((id) => id !== catId) : [...prev, catId]
    );
  };

  const selectAllCategories = () => {
    setSelectedCats(CATEGORIES.map((c) => c.id));
  };

  const clearAllCategories = () => {
    setSelectedCats([]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!location.trim()) return;
    if (selectedCats.length === 0) {
      alert('Please select at least one business category.');
      return;
    }

    await onStartJob({
      location: location.trim(),
      radius_km: radiusKm,
      categories: selectedCats,
      max_leads: maxLeads,
      filter_only_with_phone: onlyPhone,
      filter_only_missing_website: onlyMissingWeb,
    });
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-2xl mb-8">
      <div className="flex items-center justify-between mb-4 pb-3 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <Sparkles className="w-5 h-5 text-indigo-400" />
          <h2 className="text-lg font-semibold text-slate-100">Client Acquisition Engine</h2>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <Info className="w-3.5 h-3.5 text-indigo-400" />
          <span>Real OSM Discovery & Automated Auditing</span>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Location Input and Quick Presets */}
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-slate-300 mb-1.5">
            Target Location / Market Area
          </label>
          <div className="relative">
            <MapPin className="absolute left-3.5 top-3 w-4 h-4 text-slate-500" />
            <input
              type="text"
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="e.g. Indiranagar, Bengaluru or Connaught Place, New Delhi"
              required
              className="w-full bg-slate-950 border border-slate-800 rounded-xl pl-10 pr-4 py-2.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition"
            />
          </div>

          {/* Quick presets */}
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <span className="text-xs text-slate-500">Quick presets:</span>
            {PRESETS.map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => setLocation(preset)}
                className="text-xs bg-slate-800/60 hover:bg-slate-800 text-slate-300 px-2 py-0.5 rounded transition cursor-pointer"
              >
                {preset.split(',')[0]}
              </button>
            ))}
          </div>
        </div>

        {/* Business Categories Multi-Select */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-300">
              Business Categories ({selectedCats.length} selected)
            </label>
            <div className="flex gap-2 text-xs">
              <button
                type="button"
                onClick={selectAllCategories}
                className="text-indigo-400 hover:text-indigo-300 transition cursor-pointer"
              >
                Select All
              </button>
              <span className="text-slate-600">•</span>
              <button
                type="button"
                onClick={clearAllCategories}
                className="text-slate-500 hover:text-slate-400 transition cursor-pointer"
              >
                Clear
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            {CATEGORIES.map((cat) => {
              const isSelected = selectedCats.includes(cat.id);
              return (
                <button
                  key={cat.id}
                  type="button"
                  onClick={() => toggleCategory(cat.id)}
                  className={`px-3 py-2 rounded-xl text-xs font-medium border text-left transition flex items-center justify-between cursor-pointer ${
                    isSelected
                      ? 'bg-indigo-600/15 border-indigo-500/50 text-indigo-300'
                      : 'bg-slate-950/60 border-slate-800 text-slate-400 hover:border-slate-700'
                  }`}
                >
                  <span>{cat.label}</span>
                  <span
                    className={`w-2 h-2 rounded-full ${
                      isSelected ? 'bg-indigo-400' : 'bg-slate-700'
                    }`}
                  />
                </button>
              );
            })}
          </div>
        </div>

        {/* Search Parameters: Radius & Max Leads */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
          {/* Radius Slider */}
          <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-3">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-slate-300 font-medium">Search Radius</span>
              <span className="text-xs font-mono font-bold text-indigo-400">{radiusKm} km</span>
            </div>
            <input
              type="range"
              min="0.5"
              max="8.0"
              step="0.5"
              value={radiusKm}
              onChange={(e) => setRadiusKm(parseFloat(e.target.value))}
              className="w-full accent-indigo-500 cursor-pointer"
            />
            <div className="flex justify-between text-[10px] text-slate-500 mt-1">
              <span>0.5 km (Walking)</span>
              <span>3.0 km (Neighborhood)</span>
              <span>8.0 km (District)</span>
            </div>
          </div>

          {/* Max Leads Limit */}
          <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-3">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-slate-300 font-medium">Max Qualified Leads</span>
              <span className="text-xs font-mono font-bold text-indigo-400">{maxLeads} leads</span>
            </div>
            <input
              type="range"
              min="5"
              max="60"
              step="5"
              value={maxLeads}
              onChange={(e) => setMaxLeads(parseInt(e.target.value))}
              className="w-full accent-indigo-500 cursor-pointer"
            />
            <div className="flex justify-between text-[10px] text-slate-500 mt-1">
              <span>5 quick leads</span>
              <span>25 recommended</span>
              <span>60 deep search</span>
            </div>
          </div>
        </div>

        {/* Lead Discovery Filters */}
        <div className="flex flex-wrap items-center gap-4 pt-1">
          <label className="flex items-center gap-2 cursor-pointer select-none text-xs text-slate-300 hover:text-slate-100">
            <input
              type="checkbox"
              checked={onlyPhone}
              onChange={(e) => setOnlyPhone(e.target.checked)}
              className="w-4 h-4 rounded border-slate-700 bg-slate-900 text-indigo-500 focus:ring-indigo-500"
            />
            <span className="flex items-center gap-1">
              <Phone className="w-3 h-3 text-slate-400" /> Only with verified public phone / WhatsApp
            </span>
          </label>

          <label className="flex items-center gap-2 cursor-pointer select-none text-xs text-slate-300 hover:text-slate-100">
            <input
              type="checkbox"
              checked={onlyMissingWeb}
              onChange={(e) => setOnlyMissingWeb(e.target.checked)}
              className="w-4 h-4 rounded border-slate-700 bg-slate-900 text-indigo-500 focus:ring-indigo-500"
            />
            <span className="flex items-center gap-1">
              <Globe className="w-3 h-3 text-slate-400" /> Prioritize businesses without listed website
            </span>
          </label>
        </div>

        {/* Submit Action & OSM Disclaimer */}
        <div className="pt-2 flex flex-col sm:flex-row items-center justify-between gap-4 border-t border-slate-800">
          <p className="text-[11px] text-slate-400 max-w-lg leading-relaxed">
            <span className="text-indigo-400 font-medium">Coverage Note:</span> OpenStreetMap is community-curated; coverage varies by city. Missing websites in OSM indicate no URL was tagged. Leads are audited in real time.
          </p>

          <button
            type="submit"
            disabled={isLoading || selectedCats.length === 0}
            className="w-full sm:w-auto inline-flex items-center justify-center gap-2 px-6 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 active:bg-indigo-700 disabled:opacity-50 text-white font-medium text-sm transition shadow-lg shadow-indigo-600/20 cursor-pointer"
          >
            <Search className="w-4 h-4" />
            <span>{isLoading ? 'Processing Queue...' : 'Start Discovery & Audits'}</span>
          </button>
        </div>
      </form>
    </div>
  );
};
