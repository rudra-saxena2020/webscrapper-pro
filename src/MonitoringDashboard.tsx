/**
 * MonitoringDashboard.tsx
 * Real-Time Product Monitoring & Inventory Sync Dashboard
 */
import React, { useState, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity, AlertTriangle, ArrowLeft, BarChart3, Bell, BellOff,
  CheckCircle2, ChevronRight, Clock, Database, Eye, Filter,
  Package, RefreshCw, Search, ShieldCheck, TrendingDown,
  TrendingUp, XCircle, Zap, ZapOff, Globe, AlertCircle,
  ArrowUpDown, CalendarDays, Tag
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)); }

const API_BASE = '';

// ─── Types ────────────────────────────────────────────────────────────────────
interface MonitorMetrics {
  total_changes: number;
  total_oos: number;
  total_back: number;
  total_new: number;
  total_deleted: number;
  total_sync_fail: number;
  today_changes: number;
  today_oos: number;
  today_back: number;
  today_new: number;
  today_deleted: number;
  today_sync_fail: number;
  last_monitor_run: string | null;
  total_monitored: number;
}

interface MonitorState {
  is_running: boolean;
  current_scraper: string | null;
  last_run: string | null;
}

interface ChangeEvent {
  id: number;
  scraper_id: string;
  change_type: string;
  product_title: string | null;
  product_handle: string | null;
  shopify_product_id: string | null;
  source_url: string | null;
  shopify_url: string | null;
  sku: string | null;
  previous_value: any;
  new_value: any;
  sync_status: string;
  sync_error: string | null;
  detected_at: string;
  synced_at: string | null;
}

interface Notification {
  id: number;
  event_type: string;
  scraper_id: string | null;
  product_title: string | null;
  message: string | null;
  severity: string;
  created_at: string;
}

// ─── Constants ────────────────────────────────────────────────────────────────
const CHANGE_META: Record<string, { label: string; color: string; bg: string; icon: any }> = {
  oos:            { label: 'Out of Stock',    color: 'text-amber-400',  bg: 'bg-amber-400/10 border-amber-400/20',  icon: ZapOff },
  back_in_stock:  { label: 'Back in Stock',   color: 'text-emerald-400',bg: 'bg-emerald-400/10 border-emerald-400/20', icon: Zap },
  new_product:    { label: 'New Product',     color: 'text-blue-400',   bg: 'bg-blue-400/10 border-blue-400/20',   icon: TrendingUp },
  deleted:        { label: 'Removed',         color: 'text-red-400',    bg: 'bg-red-400/10 border-red-400/20',     icon: XCircle },
  price_change:   { label: 'Price Change',    color: 'text-violet-400', bg: 'bg-violet-400/10 border-violet-400/20', icon: ArrowUpDown },
  variant_change: { label: 'Variant Change',  color: 'text-sky-400',    bg: 'bg-sky-400/10 border-sky-400/20',     icon: Tag },
};

const SYNC_META: Record<string, { label: string; color: string }> = {
  pending: { label: 'Pending',  color: 'text-amber-400' },
  synced:  { label: 'Synced',   color: 'text-emerald-400' },
  failed:  { label: 'Failed',   color: 'text-red-400' },
};

function timeAgo(iso: string): string {
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60)  return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// ─── MetricTile ───────────────────────────────────────────────────────────────
function MetricTile({ label, value, today, icon: Icon, color, delay = 0 }: {
  label: string; value: number; today: number; icon: any; color: string; delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.3 }}
      className="relative glass-card p-5 rounded-2xl overflow-hidden group"
    >
      <div className={cn('absolute top-0 left-0 w-full h-[2px]', color)} />
      <div className="flex items-start justify-between mb-3">
        <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-400">{label}</p>
        <Icon className={cn('w-4 h-4 opacity-60', color.replace('bg-', 'text-'))} />
      </div>
      <p className="text-3xl font-black text-white tabular-nums">{value.toLocaleString()}</p>
      <p className="text-[10px] text-slate-500 mt-1 font-medium">
        <span className={cn('font-black', today > 0 ? 'text-primary' : 'text-slate-600')}>+{today}</span> today
      </p>
    </motion.div>
  );
}

// ─── ChangeRow ────────────────────────────────────────────────────────────────
function ChangeRow({ event, expanded, onToggle }: {
  event: ChangeEvent; expanded: boolean; onToggle: () => void;
}) {
  const meta = CHANGE_META[event.change_type] ?? { label: event.change_type, color: 'text-slate-400', bg: 'bg-white/5 border-white/10', icon: Activity };
  const syncMeta = SYNC_META[event.sync_status] ?? { label: event.sync_status, color: 'text-slate-400' };
  const Icon = meta.icon;

  return (
    <>
      <motion.tr
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="border-b border-white/5 hover:bg-white/[0.02] cursor-pointer transition-colors"
        onClick={onToggle}
      >
        <td className="py-3 px-4 w-32">
          <span className={cn('inline-flex items-center gap-1.5 px-2 py-1 rounded-lg text-[10px] font-black uppercase tracking-wide border', meta.bg, meta.color)}>
            <Icon className="w-3 h-3" />
            {meta.label}
          </span>
        </td>
        <td className="py-3 px-3 max-w-[200px]">
          <p className="text-xs text-white font-medium truncate">{event.product_title || event.product_handle || '—'}</p>
          {event.sku && <p className="text-[10px] text-slate-500 font-mono">{event.sku}</p>}
        </td>
        <td className="py-3 px-3">
          <span className="text-[10px] font-mono bg-white/5 px-2 py-1 rounded text-slate-400">{event.scraper_id}</span>
        </td>
        <td className="py-3 px-3">
          <span className={cn('text-[10px] font-black', syncMeta.color)}>{syncMeta.label}</span>
        </td>
        <td className="py-3 px-4 text-right">
          <span className="text-[10px] text-slate-500">{timeAgo(event.detected_at)}</span>
        </td>
      </motion.tr>
      <AnimatePresence>
        {expanded && (
          <motion.tr
            key="expanded"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          >
            <td colSpan={5} className="px-4 pb-4 bg-white/[0.015]">
              <div className="grid grid-cols-3 gap-4 pt-3 text-[11px]">
                <div>
                  <p className="text-slate-500 font-bold mb-1 uppercase tracking-wide text-[9px]">Previous</p>
                  <pre className="text-slate-300 font-mono bg-black/30 rounded-lg p-2 text-[10px] overflow-auto max-h-20">
                    {event.previous_value ? JSON.stringify(event.previous_value, null, 2) : '—'}
                  </pre>
                </div>
                <div>
                  <p className="text-slate-500 font-bold mb-1 uppercase tracking-wide text-[9px]">New</p>
                  <pre className="text-slate-300 font-mono bg-black/30 rounded-lg p-2 text-[10px] overflow-auto max-h-20">
                    {event.new_value ? JSON.stringify(event.new_value, null, 2) : '—'}
                  </pre>
                </div>
                <div className="space-y-2">
                  {event.shopify_url && (
                    <a href={event.shopify_url} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-1.5 text-primary hover:underline text-[10px] font-medium">
                      <Globe className="w-3 h-3" /> View on Shopify
                    </a>
                  )}
                  {event.source_url && (
                    <a href={event.source_url} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-1.5 text-slate-400 hover:text-white text-[10px] font-medium">
                      <Eye className="w-3 h-3" /> Source URL
                    </a>
                  )}
                  {event.sync_error && (
                    <p className="text-red-400 text-[10px] font-mono">{event.sync_error}</p>
                  )}
                  {event.synced_at && (
                    <p className="text-slate-500 text-[10px]">Synced: {timeAgo(event.synced_at)}</p>
                  )}
                </div>
              </div>
            </td>
          </motion.tr>
        )}
      </AnimatePresence>
    </>
  );
}

// ─── Changes Log Sub-page ─────────────────────────────────────────────────────
function ChangesLogPage({ onBack }: { onBack: () => void }) {
  const [scraperFilter, setScraperFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');
  const [syncFilter, setSyncFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [page, setPage] = useState(0);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const PAGE_SIZE = 50;

  const params = new URLSearchParams();
  if (scraperFilter !== 'all') params.set('scraper_id', scraperFilter);
  if (typeFilter !== 'all') params.set('change_type', typeFilter);
  if (syncFilter !== 'all') params.set('sync_status', syncFilter);
  if (search) params.set('search', search);
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  params.set('limit', String(PAGE_SIZE));
  params.set('offset', String(page * PAGE_SIZE));

  const { data, isFetching } = useQuery({
    queryKey: ['monitor-changes', params.toString()],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/changes?${params}`).then(r => r.json()),
    refetchInterval: 30000,
  });

  const events: ChangeEvent[] = data?.changes ?? [];
  const total: number = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);

  const selectCls = "px-3 py-2 rounded-xl bg-white/5 border border-white/10 text-xs text-slate-300 outline-none focus:border-white/25 transition-colors";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button onClick={onBack}
          className="p-2 rounded-xl border border-white/10 text-slate-400 hover:text-white hover:border-white/20 transition-all">
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div>
          <h2 className="text-sm font-black text-white uppercase tracking-widest">Inventory Change Log</h2>
          <p className="text-[10px] text-slate-500 mt-0.5">{total.toLocaleString()} total events</p>
        </div>
        {isFetching && <RefreshCw className="w-4 h-4 text-primary animate-spin ml-auto" />}
      </div>

      {/* Filters */}
      <div className="glass-panel rounded-2xl p-4 flex flex-wrap gap-3 items-center">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
          <input value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}
            placeholder="Search products, handles, SKUs…"
            className="w-full pl-9 pr-4 py-2 rounded-xl bg-white/5 border border-white/10 text-xs text-white placeholder-slate-600 outline-none focus:border-white/25 transition-colors"
          />
        </div>
        <select value={scraperFilter} onChange={e => { setScraperFilter(e.target.value); setPage(0); }} className={selectCls}>
          <option value="all">All Scrapers</option>
          {data?.scraper_ids?.map((s: string) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={typeFilter} onChange={e => { setTypeFilter(e.target.value); setPage(0); }} className={selectCls}>
          <option value="all">All Types</option>
          {Object.entries(CHANGE_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
        <select value={syncFilter} onChange={e => { setSyncFilter(e.target.value); setPage(0); }} className={selectCls}>
          <option value="all">All Sync Status</option>
          <option value="pending">Pending</option>
          <option value="synced">Synced</option>
          <option value="failed">Failed</option>
        </select>
        <div className="flex items-center gap-2">
          <CalendarDays className="w-3.5 h-3.5 text-slate-500" />
          <input type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); setPage(0); }}
            className={cn(selectCls, 'w-36')} />
          <span className="text-slate-500 text-xs">→</span>
          <input type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); setPage(0); }}
            className={cn(selectCls, 'w-36')} />
        </div>
        {(scraperFilter !== 'all' || typeFilter !== 'all' || syncFilter !== 'all' || search || dateFrom || dateTo) && (
          <button onClick={() => { setScraperFilter('all'); setTypeFilter('all'); setSyncFilter('all'); setSearch(''); setDateFrom(''); setDateTo(''); setPage(0); }}
            className="px-3 py-2 rounded-xl bg-white/5 border border-white/10 text-[10px] font-black uppercase tracking-widest text-slate-400 hover:text-white transition-colors">
            Clear
          </button>
        )}
      </div>

      {/* Table */}
      <div className="glass-panel rounded-2xl overflow-hidden">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-white/10">
              {['Change', 'Product', 'Scraper', 'Sync', 'Detected'].map(h => (
                <th key={h} className="px-4 py-3 text-[9px] font-black uppercase tracking-[0.2em] text-slate-500">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {events.length === 0 && (
              <tr><td colSpan={5} className="py-16 text-center text-slate-500 text-xs">No changes recorded yet.</td></tr>
            )}
            {events.map(e => (
              <ChangeRow
                key={e.id}
                event={e}
                expanded={expandedId === e.id}
                onToggle={() => setExpandedId(expandedId === e.id ? null : e.id)}
              />
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center gap-2 justify-center">
          <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
            className="px-4 py-2 rounded-xl border border-white/10 text-xs text-slate-400 hover:text-white disabled:opacity-30 transition-all">← Prev</button>
          <span className="text-[10px] text-slate-500 px-3">Page {page + 1} / {totalPages}</span>
          <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
            className="px-4 py-2 rounded-xl border border-white/10 text-xs text-slate-400 hover:text-white disabled:opacity-30 transition-all">Next →</button>
        </div>
      )}
    </div>
  );
}

// ─── Scraper Health Card ──────────────────────────────────────────────────────
function ScraperHealthCard({ scraperId, monitorStatus, snapshotCount, counts }: {
  scraperId: string; monitorStatus: any; snapshotCount: number; counts: any;
}) {
  const status = monitorStatus?.status ?? counts?.status ?? 'unknown';
  const statusMeta: Record<string, { color: string; label: string }> = {
    completed: { color: 'bg-emerald-500', label: 'OK' },
    running:   { color: 'bg-blue-500 animate-pulse', label: 'Running' },
    failed:    { color: 'bg-red-500', label: 'Error' },
    skipped:   { color: 'bg-slate-500', label: 'Skipped' },
    unknown:   { color: 'bg-slate-600', label: 'No data' },
  };
  const sm = statusMeta[status] ?? statusMeta.unknown;
  const mainCount = counts?.main_count;
  const sourceCount = counts?.source_count;
  const diff = (mainCount ?? 0) - (sourceCount ?? 0);

  return (
    <div className="glass-card rounded-xl p-4 hover:border-white/15 transition-all">
      <div className="flex items-start justify-between mb-3">
        <span className="text-[10px] font-mono bg-white/5 px-2 py-1 rounded text-slate-300">{scraperId}</span>
        <div className="flex items-center gap-1.5">
          <span className={cn('w-2 h-2 rounded-full', sm.color)} />
          <span className="text-[9px] font-black uppercase tracking-wide text-slate-400">{sm.label}</span>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        <div>
          <p className="text-slate-500">Main</p>
          <p className="font-black text-white">{mainCount?.toLocaleString() ?? '—'}</p>
        </div>
        <div>
          <p className="text-slate-500">Source</p>
          <p className="font-black text-white">{sourceCount?.toLocaleString() ?? '—'}</p>
        </div>
        <div>
          <p className="text-slate-500">Diff</p>
          <p className={cn('font-black', diff > 0 ? 'text-amber-400' : diff < 0 ? 'text-blue-400' : 'text-white')}>
            {mainCount != null && sourceCount != null ? (diff > 0 ? `+${diff}` : diff) : '—'}
          </p>
        </div>
        <div>
          <p className="text-slate-500">Last check</p>
          <p className="font-black text-slate-300">
            {counts?.last_check ? timeAgo(counts.last_check) : '—'}
          </p>
        </div>
      </div>
      {monitorStatus?.error_message && (
        <p className="mt-2 text-[10px] text-red-400 font-mono truncate">{monitorStatus.error_message}</p>
      )}
    </div>
  );
}

// ─── Scraper Counts Table (compact list view) ─────────────────────────────────
function ScraperCountsTable({ scrapers, counts, updatingScraper, onUpdate }: { scrapers: string[]; counts: Record<string, any>; updatingScraper: string | null; onUpdate: (sid: string) => void }) {
  return (
    <div className="glass-panel rounded-2xl overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-white/10">
              {['Scraper', 'Status', 'Main', 'Source', 'Diff', 'Last Check', 'Action'].map(h => (
                <th key={h} className="px-4 py-3 text-[9px] font-black uppercase tracking-[0.2em] text-slate-500">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scrapers.length === 0 && (
              <tr><td colSpan={7} className="py-12 text-center text-slate-500 text-xs">No scrapers found.</td></tr>
            )}
            {scrapers.map(sid => {
              const c = counts[sid] || {};
              const status = c.status || 'unknown';
              const statusMeta: Record<string, { color: string; label: string }> = {
                completed: { color: 'bg-emerald-500', label: 'OK' },
                running:   { color: 'bg-blue-500 animate-pulse', label: 'Running' },
                failed:    { color: 'bg-red-500', label: 'Error' },
                skipped:   { color: 'bg-slate-500', label: 'Skipped' },
                unknown:   { color: 'bg-slate-600', label: 'No data' },
              };
              const sm = statusMeta[status] ?? statusMeta.unknown;
              const main = c.main_count;
              const source = c.source_count;
              const diff = (main ?? 0) - (source ?? 0);
              return (
                <tr key={sid} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="px-4 py-3">
                    <span className="text-[10px] font-mono bg-white/5 px-2 py-1 rounded text-slate-300">{sid}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1.5">
                      <span className={cn('w-2 h-2 rounded-full', sm.color)} />
                      <span className="text-[10px] font-black uppercase tracking-wide text-slate-400">{sm.label}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-[11px] font-black text-white">{main?.toLocaleString() ?? '—'}</td>
                  <td className="px-4 py-3 text-[11px] font-black text-white">{source?.toLocaleString() ?? '—'}</td>
                  <td className="px-4 py-3">
                    <span className={cn('text-[11px] font-black', diff > 0 ? 'text-amber-400' : diff < 0 ? 'text-blue-400' : 'text-slate-400')}>
                      {main != null && source != null ? (diff > 0 ? `+${diff}` : diff) : '—'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-[10px] text-slate-500">
                    {c.last_check ? timeAgo(c.last_check) : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => onUpdate(sid)}
                      disabled={updatingScraper === sid}
                      className={cn(
                        'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[9px] font-black uppercase tracking-widest transition-all',
                        updatingScraper === sid
                          ? 'bg-white/5 text-slate-500 cursor-not-allowed'
                          : 'bg-orange-500/10 border border-orange-500/20 text-orange-400 hover:bg-orange-500/20 active:scale-95'
                      )}
                    >
                      <RefreshCw className={cn('w-3 h-3', updatingScraper === sid ? 'animate-spin' : '')} />
                      {updatingScraper === sid ? '…' : 'Update'}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Main MonitoringDashboard ─────────────────────────────────────────────────
export default function MonitoringDashboard({ onBack }: { onBack: () => void }) {
  const [subPage, setSubPage] = useState<'overview' | 'changes'>('overview');
  const [triggering, setTriggering] = useState(false);
  const [unseenDismissed, setUnseenDismissed] = useState(false);
  const [updatingAll, setUpdatingAll] = useState(false);
  const [updatingScraper, setUpdatingScraper] = useState<string | null>(null);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // Metrics
  const { data: metrics } = useQuery<MonitorMetrics>({
    queryKey: ['monitor-metrics'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/metrics`).then(r => r.json()),
    refetchInterval: 30000,
  });

  // Runtime state
  const { data: stateData } = useQuery<{ state: MonitorState }>({
    queryKey: ['monitor-state'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/status`).then(r => r.json()),
    refetchInterval: 5000,
  });
  const state = stateData?.state ?? { is_running: false, current_scraper: null, last_run: null };

  // Recent changes (feed)
  const { data: recentData } = useQuery({
    queryKey: ['monitor-recent'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/changes?limit=20`).then(r => r.json()),
    refetchInterval: 30000,
  });
  const recentChanges: ChangeEvent[] = recentData?.changes ?? [];

  // Scraper status + main-vs-source counts (live)
  const { data: scraperStatusData } = useQuery({
    queryKey: ['monitor-scraper-status'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/scraper-status`).then(r => r.json()),
    refetchInterval: 60000,
  });
  const scraperStatus: Record<string, any> = scraperStatusData?.scrapers ?? {};

  const { data: countsData } = useQuery({
    queryKey: ['monitor-scraper-counts'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/scraper-counts`).then(r => r.json()),
    refetchInterval: 30000,
  });
  const scraperCounts: Record<string, any> = countsData?.scrapers ?? {};
  const scraperIds = Object.keys(scraperCounts).length > 0
    ? Object.keys(scraperCounts)
    : Object.keys(scraperStatus);

  // Unseen notifications
  const { data: notifData } = useQuery({
    queryKey: ['monitor-notifications'],
    queryFn: () => fetch(`${API_BASE}/api/monitoring/notifications`).then(r => r.json()),
    refetchInterval: 15000,
  });
  const notifications: Notification[] = notifData?.notifications ?? [];

  const handleTrigger = useCallback(async () => {
    setTriggering(true);
    try {
      const res = await fetch(`${API_BASE}/api/monitoring/trigger`, { method: 'POST' });
      if (res.ok) {
        queryClient.invalidateQueries({ queryKey: ['monitor-state'] });
        queryClient.invalidateQueries({ queryKey: ['monitor-metrics'] });
        queryClient.invalidateQueries({ queryKey: ['monitor-recent'] });
        queryClient.invalidateQueries({ queryKey: ['monitor-scraper-status'] });
        queryClient.invalidateQueries({ queryKey: ['monitor-scraper-counts'] });
      }
    } catch { /* non-fatal */ }
    setTriggering(false);
  }, [queryClient]);

  const handleDismissNotifs = useCallback(async () => {
    await fetch(`${API_BASE}/api/monitoring/notifications/mark-seen`, { method: 'POST' });
    queryClient.invalidateQueries({ queryKey: ['monitor-notifications'] });
    setUnseenDismissed(true);
  }, [queryClient]);

  const handleUploadAll = useCallback(async () => {
    if (!window.confirm('Upload all MISSING products from source CSVs to the MAIN STORE for every active scraper?\n\nExisting products will be skipped. Quality gate must be 100%.')) return;
    setUpdatingAll(true);
    setUpdateMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/shopify/upload-all`, {
        method: 'POST',
        headers: {
          'X-Store-Key': 'main',
          'X-Confirm-Main': 'CONFIRM MAIN STORE ACTION',
        },
      });
      const data = await res.json().catch(() => ({}));
      setUpdateMessage(data.message || (res.ok ? 'Upload All Missing started' : 'Upload All Missing failed'));
    } catch (e) {
      setUpdateMessage('Upload All Missing failed: network error');
    }
    setUpdatingAll(false);
  }, []);

  const handleUploadScraper = useCallback(async (sid: string) => {
    setUpdatingScraper(sid);
    setUpdateMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/shopify/upload/${sid}`, {
        method: 'POST',
        headers: {
          'X-Store-Key': 'main',
          'X-Confirm-Main': 'CONFIRM MAIN STORE ACTION',
        },
      });
      const data = await res.json().catch(() => ({}));
      setUpdateMessage(data.message || (res.ok ? `Upload Missing started for ${sid}` : `Upload Missing failed for ${sid}`));
    } catch (e) {
      setUpdateMessage(`Upload Missing failed for ${sid}: network error`);
    }
    setUpdatingScraper(null);
  }, []);

  const handleUpdateAll = useCallback(async () => {
    if (!window.confirm('Update all existing products in the MAIN STORE for every active scraper?')) return;
    setUpdatingAll(true);
    setUpdateMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/shopify/update-all`, {
        method: 'POST',
        headers: {
          'X-Store-Key': 'main',
          'X-Confirm-Main': 'CONFIRM MAIN STORE ACTION',
        },
      });
      const data = await res.json().catch(() => ({}));
      setUpdateMessage(data.message || (res.ok ? 'Update All started' : 'Update All failed'));
    } catch (e) {
      setUpdateMessage('Update All failed: network error');
    }
    setUpdatingAll(false);
  }, []);

  const handleUpdateScraper = useCallback(async (sid: string) => {
    setUpdatingScraper(sid);
    setUpdateMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/shopify/update/${sid}`, {
        method: 'POST',
        headers: {
          'X-Store-Key': 'main',
          'X-Confirm-Main': 'CONFIRM MAIN STORE ACTION',
        },
      });
      const data = await res.json().catch(() => ({}));
      setUpdateMessage(data.message || (res.ok ? `Update started for ${sid}` : `Update failed for ${sid}`));
    } catch (e) {
      setUpdateMessage(`Update failed for ${sid}: network error`);
    }
    setUpdatingScraper(null);
  }, []);

  if (subPage === 'changes') {
    return <ChangesLogPage onBack={() => setSubPage('overview')} />;
  }

  const m = metrics;
  const unseenCount = notifications.filter(n => n.severity !== 'seen').length;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button onClick={onBack}
            className="p-2 rounded-xl border border-white/10 text-slate-400 hover:text-white hover:border-white/20 transition-all">
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-sm font-black text-white uppercase tracking-widest">Monitoring</h2>
              {state.is_running && (
                <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-blue-500/10 border border-blue-500/20 text-[9px] font-black uppercase tracking-widest text-blue-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
                  Running — {state.current_scraper ?? '…'}
                </span>
              )}
              {!state.is_running && (
                <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-[9px] font-black uppercase tracking-widest text-emerald-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                  Idle
                </span>
              )}
            </div>
            <p className="text-[10px] text-slate-500 mt-1">
              {m?.total_monitored?.toLocaleString() ?? '—'} products tracked ·{' '}
              {m?.last_monitor_run ? `last scan ${timeAgo(m.last_monitor_run)}` : 'no scans yet'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* Notification bell */}
          {notifications.length > 0 && !unseenDismissed && (
            <button onClick={handleDismissNotifs}
              className="relative flex items-center gap-2 px-3 py-2 rounded-xl bg-amber-500/10 border border-amber-500/20 text-amber-400 hover:bg-amber-500/20 transition-all text-[10px] font-black uppercase tracking-widest">
              <Bell className="w-4 h-4" />
              {notifications.length} alerts
              <span className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-amber-500 text-[8px] text-black font-black flex items-center justify-center">
                {notifications.length}
              </span>
            </button>
          )}
          {(unseenDismissed || notifications.length === 0) && (
            <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-white/5 border border-white/10 text-slate-500 text-[10px] font-black uppercase tracking-widest">
              <BellOff className="w-4 h-4" />
              No alerts
            </div>
          )}
          <button
            onClick={handleTrigger}
            disabled={state.is_running || triggering}
            className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-xl text-[10px] font-black uppercase tracking-widest transition-all',
              (state.is_running || triggering)
                ? 'bg-white/5 border border-white/10 text-slate-500 cursor-not-allowed'
                : 'bg-primary/10 border border-primary/20 text-primary hover:bg-primary/20 active:scale-95'
            )}
          >
            <RefreshCw className={cn('w-3.5 h-3.5', (state.is_running || triggering) ? 'animate-spin' : '')} />
            {state.is_running ? 'Scanning…' : 'Run Now'}
          </button>
          <button
            onClick={handleUpdateAll}
            disabled={updatingAll}
            className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-xl text-[10px] font-black uppercase tracking-widest transition-all',
              updatingAll
                ? 'bg-white/5 border border-white/10 text-slate-500 cursor-not-allowed'
                : 'bg-orange-500/10 border border-orange-500/20 text-orange-400 hover:bg-orange-500/20 active:scale-95'
            )}
          >
            <RefreshCw className={cn('w-3.5 h-3.5', updatingAll ? 'animate-spin' : '')} />
            {updatingAll ? 'Updating…' : 'Update All'}
          </button>
          <button
            onClick={() => setSubPage('changes')}
            className="flex items-center gap-2 px-4 py-2 rounded-xl bg-white/5 border border-white/10 text-slate-300 hover:text-white text-[10px] font-black uppercase tracking-widest transition-all"
          >
            <Database className="w-3.5 h-3.5" />
            Change Log
          </button>
        </div>
      </div>

      {/* Update action message */}
      <AnimatePresence>
        {updateMessage && (
          <motion.div
            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="glass-panel rounded-2xl p-3 border-l-2 border-orange-400/60 text-[11px] text-slate-300 flex items-center justify-between">
              <span>{updateMessage}</span>
              <button onClick={() => setUpdateMessage(null)} className="text-slate-500 hover:text-white"><XCircle className="w-4 h-4" /></button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Alert bar */}
      <AnimatePresence>
        {notifications.length > 0 && !unseenDismissed && (
          <motion.div
            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="glass-panel rounded-2xl p-4 border-l-2 border-amber-400/60 space-y-2">
              <div className="flex items-center justify-between mb-2">
                <p className="text-[9px] font-black uppercase tracking-[0.2em] text-amber-400 flex items-center gap-2">
                  <Bell className="w-3.5 h-3.5" /> {notifications.length} Unread Alert(s)
                </p>
                <button onClick={handleDismissNotifs} className="text-[10px] text-slate-500 hover:text-white transition-colors">
                  Mark all seen
                </button>
              </div>
              {notifications.slice(0, 5).map(n => {
                const meta = CHANGE_META[n.event_type] ?? { label: n.event_type, color: 'text-slate-400', bg: 'bg-white/5 border-white/10', icon: Activity };
                const Icon = meta.icon;
                return (
                  <div key={n.id} className={cn('flex items-center gap-3 px-3 py-2 rounded-xl border text-[11px]', meta.bg)}>
                    <Icon className={cn('w-3.5 h-3.5 shrink-0', meta.color)} />
                    <span className={cn('font-black', meta.color)}>{meta.label}</span>
                    <span className="text-slate-300 truncate">{n.product_title ?? n.message}</span>
                    <span className="text-slate-500 shrink-0 ml-auto font-mono text-[10px]">{n.scraper_id}</span>
                    <span className="text-slate-600 shrink-0 text-[10px]">{timeAgo(n.created_at)}</span>
                  </div>
                );
              })}
              {notifications.length > 5 && (
                <p className="text-[10px] text-slate-500 pl-3">+{notifications.length - 5} more alerts</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Automation status bar */}
      <div className="glass-panel rounded-2xl p-4 flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-widest text-emerald-400">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          Automation Active
        </div>
        <div className="h-4 w-px bg-white/10 hidden sm:block" />
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <Clock className="w-3.5 h-3.5" />
          <span>Inventory scan every hour</span>
        </div>
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <RefreshCw className="w-3.5 h-3.5" />
          <span>Baseline refreshes after each auto-sync</span>
        </div>
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <Activity className="w-3.5 h-3.5" />
          <span>Dashboard live-updates every 30s</span>
        </div>
      </div>

      {/* Metrics grid */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
        <MetricTile label="OOS"           value={m?.total_oos ?? 0}    today={m?.today_oos ?? 0}    icon={ZapOff}      color="bg-amber-500"   delay={0} />
        <MetricTile label="Back in Stock" value={m?.total_back ?? 0}   today={m?.today_back ?? 0}   icon={Zap}         color="bg-emerald-500" delay={0.05} />
        <MetricTile label="New Products"  value={m?.total_new ?? 0}    today={m?.today_new ?? 0}    icon={TrendingUp}  color="bg-blue-500"    delay={0.1} />
        <MetricTile label="Removed"       value={m?.total_deleted ?? 0}today={m?.today_deleted ?? 0}icon={XCircle}     color="bg-red-500"     delay={0.15} />
        <MetricTile label="Price Changes" value={(m?.total_changes ?? 0) - (m?.total_oos ?? 0) - (m?.total_back ?? 0) - (m?.total_new ?? 0) - (m?.total_deleted ?? 0)} today={0} icon={ArrowUpDown} color="bg-violet-500" delay={0.2} />
        <MetricTile label="Sync Failures" value={m?.total_sync_fail ?? 0} today={m?.today_sync_fail ?? 0} icon={AlertTriangle} color="bg-red-600" delay={0.25} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Recent activity feed */}
        <div className="lg:col-span-2 glass-panel rounded-2xl overflow-hidden">
          <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
            <p className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400 flex items-center gap-2">
              <Activity className="w-3.5 h-3.5" /> Recent Changes
            </p>
            <button onClick={() => setSubPage('changes')}
              className="flex items-center gap-1 text-[10px] text-primary hover:underline">
              View all <ChevronRight className="w-3 h-3" />
            </button>
          </div>
          <div className="divide-y divide-white/5 max-h-[420px] overflow-y-auto">
            {recentChanges.length === 0 ? (
              <div className="py-16 text-center text-slate-500 text-xs">
                <Activity className="w-8 h-8 mx-auto mb-3 opacity-20" />
                No changes detected yet. Run the first monitoring scan.
              </div>
            ) : recentChanges.map(e => {
              const meta = CHANGE_META[e.change_type] ?? { label: e.change_type, color: 'text-slate-400', bg: 'bg-white/5 border-white/10', icon: Activity };
              const Icon = meta.icon;
              return (
                <div key={e.id} className="flex items-center gap-3 px-6 py-3 hover:bg-white/[0.02] transition-colors">
                  <div className={cn('w-6 h-6 rounded-lg flex items-center justify-center border shrink-0', meta.bg)}>
                    <Icon className={cn('w-3 h-3', meta.color)} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-white truncate">{e.product_title || e.product_handle || '—'}</p>
                    <p className="text-[10px] text-slate-500">{meta.label} · <span className="font-mono">{e.scraper_id}</span></p>
                  </div>
                  <div className="text-right shrink-0">
                    <p className={cn('text-[10px] font-black', SYNC_META[e.sync_status]?.color ?? 'text-slate-400')}>
                      {SYNC_META[e.sync_status]?.label ?? e.sync_status}
                    </p>
                    <p className="text-[10px] text-slate-600">{timeAgo(e.detected_at)}</p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Scraper health grid */}
        <div className="glass-panel rounded-2xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-white/5">
            <p className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400 flex items-center gap-2">
              <ShieldCheck className="w-3.5 h-3.5" /> Scraper Health
            </p>
            <span className="text-[10px] text-slate-500">{scraperIds.length} scrapers</span>
          </div>
          <div className="p-4 space-y-3 max-h-[420px] overflow-y-auto">
            {scraperIds.length === 0 ? (
              <p className="text-center text-slate-500 text-xs py-12">No monitoring data yet.</p>
            ) : scraperIds.map(sid => (
              <ScraperHealthCard
                key={sid}
                scraperId={sid}
                monitorStatus={scraperStatus[sid]}
                snapshotCount={scraperStatusData?.snapshot_counts?.[sid] ?? 0}
                counts={scraperCounts[sid]}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Scraper counts table — only scrapers active on MAIN store (live) */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400 flex items-center gap-2">
            <Package className="w-3.5 h-3.5" /> Active on Main Store — Main vs Source
          </p>
          <span className="text-[10px] text-slate-500">Live · refreshes every 30s</span>
        </div>
        <ScraperCountsTable scrapers={scraperIds} counts={scraperCounts} updatingScraper={updatingScraper} onUpdate={handleUpdateScraper} />
      </div>
    </div>
  );
}
