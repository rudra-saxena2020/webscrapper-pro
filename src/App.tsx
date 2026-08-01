import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import AutoSyncCenter from './AutoSyncCenter';
import MonitoringDashboard from './MonitoringDashboard';
import {
  Upload,
  Download,
  FileJson,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  FileSpreadsheet,
  Trash2,
  Plus,
  Database,
  Activity,
  Clock,
  RotateCcw,
  Tag,
  ArrowLeft,
  XCircle,
  X,
  CheckCheck,
  Info,
  Zap,
  Globe,
  TrendingUp,
  ChevronRight,
  LayoutGrid,
  Cpu,
  Shield,
  History,
  Search,
  Filter,
  Calendar,
  BarChart3,
  Layers,
  ChevronLeft,
  FileDown,
  Play,
  ShieldCheck,
  ZapOff,
  ClipboardList,
  ClipboardCheck,
  ClipboardX,
  Eye,
  RotateCw,
  Image as ImageIcon,
  ScanLine,
  Store,
  ArrowUpToLine,
  AlertTriangle,
  GitCompare,
  RefreshCcw,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import Papa from 'papaparse';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ──────────────────────────────────────────────
// Toast System
// ──────────────────────────────────────────────
type ToastType = 'success' | 'error' | 'info' | 'warning';
interface Toast {
  id: number;
  type: ToastType;
  title: string;
  message?: string;
}

let toastCounter = 0;
let globalAddToast: ((t: Omit<Toast, 'id'>) => void) | null = null;
const API_BASE = '';

function toast(type: ToastType, title: string, message?: string) {
  globalAddToast?.({ type, title, message });
}

function ToastContainer() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const add = useCallback((t: Omit<Toast, 'id'>) => {
    const id = ++toastCounter;
    setToasts(prev => [...prev, { ...t, id }]);
    setTimeout(() => setToasts(prev => prev.filter(x => x.id !== id)), 4500);
  }, []);

  useEffect(() => { globalAddToast = add; return () => { globalAddToast = null; }; }, [add]);

  const accent: Record<ToastType, string> = {
    success: 'bg-primary',
    error: 'bg-red-600',
    info: 'bg-white',
    warning: 'bg-yellow-400',
  };
  const icons: Record<ToastType, React.ReactNode> = {
    success: <CheckCheck className="w-4 h-4 text-primary" />,
    error: <AlertCircle className="w-4 h-4 text-red-500" />,
    info: <Info className="w-4 h-4 text-white" />,
    warning: <AlertCircle className="w-4 h-4 text-yellow-400" />,
  };

  return (
    <div className="fixed top-6 right-6 z-[1000] flex flex-col gap-2 w-[360px] pointer-events-none">
      <AnimatePresence>
        {toasts.map(t => (
          <motion.div
            key={t.id}
            initial={{ opacity: 0, x: 80 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 80 }}
            transition={{ duration: 0.2 }}
            className="pointer-events-auto relative overflow-hidden glass-panel shadow-2xl rounded-xl"
          >
            <div className={cn('absolute top-0 left-0 h-[3px] w-full', accent[t.type])} />
            <div className="flex items-start gap-3 p-4 pt-5">
              <div className="shrink-0 mt-0.5">{icons[t.type]}</div>
              <div className="flex-1 min-w-0">
                <p className="font-bold text-xs text-white uppercase tracking-widest">{t.title}</p>
                {t.message && <p className="text-[10px] text-slate-400 mt-1 leading-relaxed">{t.message}</p>}
              </div>
              <button onClick={() => setToasts(prev => prev.filter(x => x.id !== t.id))} className="text-slate-500 hover:text-white transition-colors shrink-0">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

// ──────────────────────────────────────────────
// Multi-Store Confirm Modal
// ──────────────────────────────────────────────
function MainStoreConfirmModal({
  open, opLabel, onConfirm, onCancel,
}: { open: boolean; opLabel: string; onConfirm: () => void; onCancel: () => void }) {
  const [text, setText] = useState('');
  const REQUIRED = 'CONFIRM MAIN STORE ACTION';
  const match = text.trim() === REQUIRED;

  useEffect(() => { if (!open) setText(''); }, [open]);
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[9000] flex items-center justify-center p-6">
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onCancel}
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
      />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="relative w-full max-w-md glass-panel shadow-2xl rounded-3xl overflow-hidden"
      >
        <div className="absolute top-0 left-0 w-full h-[3px] bg-gradient-to-r from-red-600 to-red-400" />
        <div className="p-8">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-10 h-10 rounded-xl bg-red-500/10 flex items-center justify-center border border-red-500/20">
              <AlertTriangle className="w-5 h-5 text-red-400" />
            </div>
            <div>
              <p className="text-[9px] font-black uppercase tracking-[0.2em] text-red-400 mb-0.5">MAIN STORE — Live Action</p>
              <h4 className="text-sm font-black text-white uppercase tracking-widest">Confirm Write Operation</h4>
            </div>
            <span className="ml-auto px-2 py-1 rounded-md bg-red-600/20 border border-red-500/30 text-[9px] font-black text-red-400 uppercase tracking-widest">MAIN</span>
          </div>

          <div className="mb-5 p-4 rounded-xl bg-red-500/5 border border-red-500/15">
            <p className="text-xs text-slate-300 leading-relaxed">
              You are about to perform a <span className="text-white font-bold">live store write</span> on your <span className="text-red-400 font-bold">MAIN STORE</span>.
            </p>
            <p className="text-[11px] text-slate-400 mt-2">Operation: <span className="text-white font-bold">{opLabel}</span></p>
          </div>

          <div className="mb-6 space-y-2">
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Type to confirm:</p>
            <p className="text-[10px] font-mono text-slate-300 px-3 py-2 bg-white/5 rounded-lg border border-white/10 select-all">{REQUIRED}</p>
            <input
              autoFocus
              value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && match) { onConfirm(); } }}
              placeholder="Type the phrase above…"
              className={cn(
                'w-full px-4 py-3 rounded-xl bg-white/5 border text-sm text-white placeholder-slate-600 outline-none transition-all font-mono',
                match ? 'border-red-500/60 focus:border-red-500' : 'border-white/10 focus:border-white/25'
              )}
            />
          </div>

          <div className="flex gap-3">
            <button
              onClick={onCancel}
              className="flex-1 py-3 text-[10px] font-black uppercase tracking-widest border border-white/10 text-slate-400 hover:text-white hover:border-white/20 rounded-xl transition-all"
            >
              Cancel
            </button>
            <button
              onClick={() => { if (match) onConfirm(); }}
              disabled={!match}
              className={cn(
                'flex-1 py-3 text-[10px] font-black uppercase tracking-widest rounded-xl transition-all',
                match
                  ? 'bg-red-600 hover:bg-red-500 text-white active:scale-95'
                  : 'bg-red-600/10 text-red-900 cursor-not-allowed'
              )}
            >
              Confirm Main Store Action
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

// ──────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────
interface Variant {
  'Variant SKU': string;
  size: string;
  color?: string;
  'Variant Price': string;
  'Variant Compare At Price'?: string;
  images: string[];
}
interface Product {
  Handle?: string;
  Title: string;
  'Body (HTML)': string;
  Vendor: string;
  'Product Category'?: string;
  Type: string;
  Tags: string;
  variants: Variant[];
}
interface ShopifyCounts {
  created?: number;
  updated?: number;
  deleted?: number;
  reimaged?: number;
  skipped?: number;
  failed?: number;
  total?: number;
  processed?: number;
  estimated_variants?: number;
}

interface WebsiteStats {
  id: string;
  scraper_id: string;
  name: string;
  currency: string;
  lastUpdated: string;
  totalProducts: number;
  category?: string;
  progress?: number;
  status?: string;
  is_running?: boolean;
  stuck?: boolean;
  products_count?: number;
  shopify_op?: string;
  shopify_counts?: ShopifyCounts;
  shopify_result?: any;
  quality?: any;
  quota_sleeping?: boolean;
  quota_resume_at?: string;
  estimated_variants?: number;
}

const FALLBACK_SCRAPERS: WebsiteStats[] = [
  { id: '1', scraper_id: 'cruise_fashion', name: 'Cruise Fashion', currency: 'GBP', lastUpdated: 'Never', totalProducts: 0 },
];

const generateHandle = (title: string) =>
  title.toLowerCase().replace(/[^a-z0-9\s-]/g, '').replace(/[\s-]+/g, '-').trim();

// ──────────────────────────────────────────────
// Stat Card — Bold Minimal
// ──────────────────────────────────────────────
function StatCard({ label, value, icon: Icon, accent, delay }: {
  label: string; value: string | number; icon: any; accent: string; delay: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.3 }}
      className="relative glass-card p-6 group rounded-2xl overflow-hidden"
    >
      <div className={cn('absolute top-0 left-0 w-full h-[2px] opacity-50', accent)} />
      <div className="flex items-center justify-between mb-4">
        <p className="text-[10px] font-bold text-slate-400 uppercase tracking-[0.2em]">{label}</p>
        <div className="p-2 rounded-lg bg-white/5 text-slate-400 group-hover:text-primary group-hover:bg-primary/10 transition-all duration-300">
          <Icon className="w-4 h-4" />
        </div>
      </div>
      <p className="text-3xl font-black text-white tracking-tight tabular-nums">{value}</p>
      <div className="absolute -bottom-2 -right-2 opacity-[0.03] group-hover:opacity-[0.07] transition-opacity duration-500">
        <Icon className="w-20 h-20" />
      </div>
    </motion.div>
  );
}

// ──────────────────────────────────────────────
// Add Website Modal
// ──────────────────────────────────────────────
function AddWebsiteModal({ isOpen, onClose, onSuccess }: { isOpen: boolean; onClose: () => void; onSuccess: () => void }) {
  const [name, setName] = useState('');
  const [url, setPlainUrl] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name || !url) return;
    setIsSubmitting(true);
    try {
      const res = await fetch(`${API_BASE}/api/scrapers/add`, {
        method: 'POST',
        mode: 'cors',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, url })
      });
      if (res.ok) {
        toast('success', 'Website Registered', `${name} has been added to the engine.`);
        onSuccess();
        onClose();
        setName('');
        setPlainUrl('');
      } else {
        const err = await res.json();
        toast('error', 'Registration Failed', err.error);
      }
    } catch {
      toast('error', 'Connection Error', 'Backend server might be offline.');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[2000] flex items-center justify-center p-6">
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
      />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="relative w-full max-w-lg glass-panel p-8 shadow-2xl rounded-3xl"
      >
        <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-primary to-secondary opacity-50" />
        <button onClick={onClose} className="absolute top-5 right-5 text-slate-500 hover:text-white transition-colors">
          <X className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-4 mb-8">
          <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20">
            <Plus className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h4 className="text-lg font-black text-white uppercase tracking-widest">Register Website</h4>
            <p className="text-[11px] text-slate-400 mt-0.5">Connect a new target URL to the scraping engine.</p>
          </div>
        </div>
        <form onSubmit={handleSubmit} className="space-y-6">
          <div className="space-y-1.5">
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-[0.2em] ml-1">Display Name</label>
            <input
              type="text" value={name} onChange={e => setName(e.target.value)}
              placeholder="e.g. Flannels Shoes"
              className="w-full px-5 py-3.5 bg-white/5 border border-white/10 rounded-2xl focus:border-primary/50 focus:ring-1 focus:ring-primary/20 outline-none text-white text-sm transition-all placeholder:text-slate-600"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-[0.2em] ml-1">Target Base URL</label>
            <input
              type="text" value={url} onChange={e => setPlainUrl(e.target.value)}
              placeholder="https://www.flannels.com/outlet/..."
              className="w-full px-5 py-3.5 bg-white/5 border border-white/10 rounded-2xl focus:border-primary/50 focus:ring-1 focus:ring-primary/20 outline-none text-white text-sm transition-all placeholder:text-slate-600"
            />
          </div>
          <div className="pt-3">
            <button
              type="submit" disabled={!name || !url || isSubmitting}
              className="w-full py-4 bg-primary hover:bg-primary-hover text-white font-black text-xs uppercase tracking-widest rounded-2xl shadow-lg shadow-primary/20 transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
            >
              {isSubmitting ? <RefreshCw className="w-4 h-4 animate-spin" /> : 'CONFIRM & ADD WEBSITE'}
            </button>
          </div>
        </form>
      </motion.div>
    </div>
  );
}

// ──────────────────────────────────────────────
// Quota countdown — live ticking display
// ──────────────────────────────────────────────
function QuotaCountdown({ resumeAt }: { resumeAt: string }) {
  const [remaining, setRemaining] = useState('');

  useEffect(() => {
    const compute = () => {
      // resumeAt is like "00:02 UTC" — parse as today or tomorrow's UTC time
      const [hhmm] = resumeAt.split(' ');
      const [hh, mm] = hhmm.split(':').map(Number);
      const now = new Date();
      const reset = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hh, mm, 0));
      if (reset.getTime() <= Date.now()) reset.setUTCDate(reset.getUTCDate() + 1);
      const diff = Math.max(0, Math.floor((reset.getTime() - Date.now()) / 1000));
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      const s = diff % 60;
      setRemaining(h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`);
    };
    compute();
    const iv = setInterval(compute, 1000);
    return () => clearInterval(iv);
  }, [resumeAt]);

  return <span className="font-mono tabular-nums">{remaining}</span>;
}

// ──────────────────────────────────────────────
// Shopify Progress Panel — inline per-card
// ──────────────────────────────────────────────
function ShopifyProgressPanel({
  op, counts, progress, isRunning, hasFailed, onCancel, quotaSleeping, resumeAt, estimatedVariants,
}: {
  op: string;
  counts: ShopifyCounts;
  progress: number;
  isRunning: boolean;
  hasFailed?: boolean;
  onCancel?: () => void;
  quotaSleeping?: boolean;
  resumeAt?: string;
  estimatedVariants?: number;
}) {
  const opLabels: Record<string, string> = {
    upload: 'Upload', update: 'Update', reimage: 'Fix Images', 'delete-oos': 'Delete OOS', nuke: 'Clear Store', dedup: 'Dedup',
  };
  const opLabel = opLabels[op] ?? op;

  const mainKey = op === 'upload' ? 'created' : op === 'update' ? 'updated' : op === 'reimage' ? 'reimaged' : 'deleted';
  const mainCount = counts[mainKey as keyof ShopifyCounts] ?? 0;
  const skipped = counts.skipped ?? 0;
  const failed = counts.failed ?? 0;
  const total = counts.total ?? 0;
  const processed = counts.processed ?? 0;
  const estVariants = estimatedVariants ?? counts.estimated_variants;

  const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : progress;

  const actionVerb = op === 'upload' ? 'Uploaded' : op === 'update' ? 'Updated' : op === 'reimage' ? 'Reimaged' : 'Deleted';
  const skipLabel = op === 'upload' ? 'already exist' : op === 'update' ? 'not matched' : op === 'reimage' ? 'no CSV match' : 'safety-skipped';

  const accentClass = quotaSleeping
    ? 'border-amber-500/30 bg-amber-500/5'
    : hasFailed
      ? 'border-red-500/20 bg-red-500/5'
      : isRunning
        ? 'border-primary/20 bg-primary/5'
        : 'border-emerald-500/20 bg-emerald-500/5';

  const barClass = quotaSleeping
    ? 'from-amber-500 to-amber-400'
    : hasFailed ? 'from-red-500 to-red-400' : isRunning ? 'from-primary to-secondary' : 'from-emerald-500 to-emerald-400';

  return (
    <motion.div
      key="shopify-progress"
      initial={{ opacity: 0, y: -6, height: 0 }}
      animate={{ opacity: 1, y: 0, height: 'auto' }}
      exit={{ opacity: 0, y: -6, height: 0 }}
      transition={{ duration: 0.25 }}
      className={cn('rounded-xl border p-3 space-y-2 overflow-hidden', accentClass)}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {quotaSleeping && (
            <span className="px-1.5 py-0.5 rounded text-[8px] font-black uppercase tracking-widest bg-amber-500/20 text-amber-400 animate-pulse border border-amber-500/30">
              QUOTA
            </span>
          )}
          {!quotaSleeping && isRunning && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />}
          {!quotaSleeping && !isRunning && !hasFailed && <CheckCircle2 className="w-3 h-3 text-emerald-400" />}
          {!quotaSleeping && !isRunning && hasFailed && <AlertCircle className="w-3 h-3 text-red-400" />}
          <span className={cn('text-[10px] font-black uppercase tracking-widest', quotaSleeping ? 'text-amber-300' : 'text-white')}>
            {quotaSleeping ? 'Paused — Variant Quota' : isRunning ? `${opLabel}…` : `${opLabel} ${hasFailed ? 'Partial' : 'Complete'}`}
          </span>
        </div>
        {isRunning && onCancel && (
          <button
            onClick={onCancel}
            className="text-[9px] text-slate-500 hover:text-red-400 transition-colors font-black uppercase tracking-widest flex items-center gap-1"
          >
            <XCircle className="w-3 h-3" /> Stop
          </button>
        )}
      </div>

      {/* Quota sleep countdown */}
      {quotaSleeping && resumeAt && (
        <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/20">
          <Clock className="w-3 h-3 text-amber-400 shrink-0" />
          <span className="text-[9px] text-amber-300">
            Auto-resumes at <span className="font-black">{resumeAt}</span> — in{' '}
            <QuotaCountdown resumeAt={resumeAt} />
          </span>
        </div>
      )}

      <div className="relative h-1.5 bg-white/5 rounded-full overflow-hidden">
        <motion.div
          className={cn('absolute inset-y-0 left-0 bg-gradient-to-r rounded-full', barClass)}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
        />
      </div>

      <div className="flex items-center justify-between text-[9px] font-mono font-bold">
        <span className="text-white">
          {actionVerb} <span className="text-emerald-400 tabular-nums">{mainCount.toLocaleString()}</span>
          {total > 0 && <span className="text-slate-500"> / {total.toLocaleString()}</span>}
          {estVariants && total > 0 && mainCount === 0 && (
            <span className="text-slate-500"> · ~{estVariants.toLocaleString()} variants</span>
          )}
          {skipped > 0 && <span className="text-slate-400"> — <span className="text-amber-400">{skipped.toLocaleString()}</span> {skipLabel}</span>}
        </span>
        <div className="flex items-center gap-2">
          {failed > 0 && <span className="text-red-400">{failed} failed</span>}
          <span className="text-slate-500 tabular-nums">{pct}%</span>
        </div>
      </div>

      {/* Pre-upload variant estimate subtitle */}
      {op === 'upload' && estVariants && total > 0 && processed === 0 && !quotaSleeping && (
        <p className="text-[9px] text-slate-500 font-mono">
          {total.toLocaleString()} products · ~{estVariants.toLocaleString()} variants to create
        </p>
      )}
    </motion.div>
  );
}

// ──────────────────────────────────────────────
// Validation Modal
// ──────────────────────────────────────────────
const QC_CATEGORIES: { key: string; label: string; icon: string }[] = [
  { key: 'images',        label: 'Images',      icon: '🖼' },
  { key: 'variants',      label: 'Variants',    icon: '🔀' },
  { key: 'description',   label: 'Description', icon: '📝' },
  { key: 'tags',          label: 'Tags',        icon: '🏷' },
  { key: 'sizes',         label: 'Sizes',       icon: '📐' },
  { key: 'pricing',       label: 'Pricing',     icon: '₹'  },
  { key: 'category',      label: 'Category',    icon: '📦' },
  { key: 'configuration', label: 'Config',      icon: '⚙'  },
];

function ValidationModal({
  data,
  onClose,
  onUpload,
  onUploadAll,
}: {
  data: any;
  onClose: () => void;
  onUpload?: (scraperId: string) => void;
  onUploadAll?: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const [uploading, setUploading] = useState(false);
  if (!data) return null;

  const products: any[] = data.products || [];
  const issues  = products.filter((p: any) => p.severity !== 'ok');
  const display = showAll ? issues : issues.slice(0, 30);
  const isAllScrapers = !data.scraper_id || data.scraper_id === 'All Scrapers';
  const canUpload = data.ready_to_upload && !data.upload_blocked;

  const perCat: Record<string, { ok: number; warnings: number; errors: number }> =
    data.per_category_summary ?? {};

  const handleUpload = () => {
    if (uploading) return;
    setUploading(true);
    if (isAllScrapers) {
      onUploadAll?.();
    } else {
      onUpload?.(data.scraper_id);
    }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        className="w-full max-w-2xl max-h-[90vh] flex flex-col glass-panel rounded-3xl shadow-2xl border border-white/10 overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-7 py-5 border-b border-white/5 bg-white/[0.02] shrink-0">
          <div>
            <h3 className="text-sm font-black text-white uppercase tracking-widest flex items-center gap-2">
              <CheckCheck className="w-4 h-4 text-primary" />
              Quality Gate — {data.scraper_id ?? 'All Scrapers'}
            </h3>
            <p className="text-[10px] text-slate-500 mt-0.5 font-mono">
              {data.csv_path ?? ''}{data.error ? ` — ${data.error}` : ''}
            </p>
          </div>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-xl border border-white/10 text-slate-500 hover:text-white hover:border-white/30 transition-all">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Summary row */}
        <div className="grid grid-cols-4 divide-x divide-white/5 shrink-0 bg-black/20">
          {[
            { label: 'Total',    value: data.total    ?? 0, color: 'text-white' },
            { label: 'OK',       value: data.ok       ?? 0, color: 'text-emerald-400' },
            { label: 'Warnings', value: data.warnings ?? 0, color: 'text-amber-400'   },
            { label: 'Errors',   value: data.errors   ?? 0, color: 'text-rose-400'    },
          ].map(({ label, value, color }) => (
            <div key={label} className="px-5 py-4 text-center">
              <p className={`text-2xl font-black tabular-nums ${color}`}>{value.toLocaleString()}</p>
              <p className="text-[9px] text-slate-600 font-bold uppercase tracking-[0.2em] mt-1">{label}</p>
            </div>
          ))}
        </div>

        {/* Pass-rate bar */}
        <div className="px-7 py-3 border-b border-white/5 shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${
                  data.errors > 0 ? 'bg-rose-500' : data.warnings > 0 ? 'bg-amber-500' : 'bg-emerald-500'
                }`}
                style={{ width: `${data.pass_rate ?? 0}%` }}
              />
            </div>
            <span className={`text-[10px] font-black tabular-nums ${
              data.errors > 0 ? 'text-rose-400' : data.warnings > 0 ? 'text-amber-400' : 'text-emerald-400'
            }`}>
              {data.pass_rate ?? 0}% pass
            </span>
            {canUpload
              ? <span className="text-[9px] font-black text-emerald-400 bg-emerald-400/10 border border-emerald-400/20 px-2 py-0.5 rounded-full uppercase tracking-widest">Ready to upload</span>
              : <span className="text-[9px] font-black text-rose-400 bg-rose-400/10 border border-rose-400/20 px-2 py-0.5 rounded-full uppercase tracking-widest">{data.errors} fix required</span>
            }
          </div>
        </div>

        {/* Per-category breakdown */}
        {Object.keys(perCat).length > 0 && (
          <div className="grid grid-cols-4 divide-x divide-white/5 shrink-0 bg-white/[0.01] border-b border-white/5">
            {QC_CATEGORIES.map(({ key, label, icon }) => {
              const c = perCat[key] ?? { ok: 0, warnings: 0, errors: 0 };
              const total = (c.ok || 0) + (c.warnings || 0) + (c.errors || 0);
              const pct = total > 0 ? Math.round(((c.ok + c.warnings) / total) * 100) : 100;
              const state = c.errors > 0 ? 'error' : c.warnings > 0 ? 'warn' : 'ok';
              return (
                <div key={key} className="px-2 py-2.5 text-center space-y-1">
                  <p className="text-sm leading-none">{icon}</p>
                  <p className="text-[8px] font-black text-slate-500 uppercase tracking-widest">{label}</p>
                  <p className={`text-[11px] font-black tabular-nums ${
                    state === 'error' ? 'text-rose-400' : state === 'warn' ? 'text-amber-400' : 'text-emerald-400'
                  }`}>{pct}%</p>
                  <div className="h-1 rounded-full bg-white/5 overflow-hidden mx-1">
                    <div className={`h-full rounded-full transition-all ${
                      state === 'error' ? 'bg-rose-500' : state === 'warn' ? 'bg-amber-500' : 'bg-emerald-500'
                    }`} style={{ width: `${pct}%` }} />
                  </div>
                  {c.errors > 0 && (
                    <p className="text-[7px] text-rose-400/80 tabular-nums">✗ {c.errors} errors</p>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Upload message if blocked */}
        {data.upload_message && (
          <div className={`px-7 py-2.5 shrink-0 text-[10px] font-mono ${
            data.upload_blocked ? 'text-rose-300 bg-rose-500/5 border-b border-rose-500/10'
                                : 'text-emerald-300 bg-emerald-500/5 border-b border-emerald-500/10'
          }`}>
            {data.upload_blocked ? '✗ ' : '✓ '}{data.upload_message}
          </div>
        )}

        {/* Issues list */}
        <div className="overflow-y-auto flex-1 px-7 py-4 space-y-2">
          {issues.length === 0 ? (
            <div className="py-10 text-center">
              <CheckCircle2 className="w-8 h-8 text-emerald-400 mx-auto mb-3" />
              <p className="text-sm font-black text-white uppercase tracking-widest">All products passed</p>
              <p className="text-[10px] text-slate-500 mt-1">No issues found — safe to upload.</p>
            </div>
          ) : (
            <>
              <p className="text-[9px] font-bold text-slate-600 uppercase tracking-[0.2em] pb-1">
                Showing {display.length} of {issues.length} products with issues
              </p>
              {display.map((p: any, i: number) => (
                <div key={i} className={`rounded-xl p-3 border text-[10px] font-mono space-y-1 ${
                  p.severity === 'error' ? 'border-rose-500/20 bg-rose-500/5' : 'border-amber-500/15 bg-amber-500/5'
                }`}>
                  <div className="flex items-start gap-2">
                    {p.severity === 'error'
                      ? <AlertCircle className="w-3 h-3 text-rose-400 shrink-0 mt-0.5" />
                      : <Info className="w-3 h-3 text-amber-400 shrink-0 mt-0.5" />}
                    <span className="text-white font-bold truncate">{p.title}</span>
                    <span className="ml-auto text-slate-600 shrink-0">{p.sku}</span>
                  </div>
                  {/* Per-category check badges */}
                  {p.checks && (
                    <div className="flex flex-wrap gap-1 pl-5 pt-0.5">
                      {QC_CATEGORIES.map(({ key, icon }) => {
                        const c = p.checks[key];
                        if (!c) return null;
                        const st = c.issues?.length ? 'error' : c.warnings?.length ? 'warn' : 'ok';
                        return (
                          <span key={key} className={`text-[7px] font-black px-1.5 py-0.5 rounded ${
                            st === 'error' ? 'bg-rose-500/20 text-rose-300'
                            : st === 'warn' ? 'bg-amber-500/20 text-amber-300'
                            : 'bg-emerald-500/10 text-emerald-500/60'
                          }`}>
                            {icon} {key}
                          </span>
                        );
                      })}
                    </div>
                  )}
                  {p.issues.map((iss: string, j: number) => (
                    <p key={j} className="text-rose-300 pl-5">✗ {iss}</p>
                  ))}
                  {p.warnings.map((w: string, j: number) => (
                    <p key={j} className="text-amber-300 pl-5">⚠ {w}</p>
                  ))}
                </div>
              ))}
              {issues.length > 30 && !showAll && (
                <button
                  onClick={() => setShowAll(true)}
                  className="w-full py-2.5 text-[10px] font-black text-primary uppercase tracking-widest border border-primary/20 rounded-xl hover:bg-primary/10 transition-all"
                >
                  Show all {issues.length} issues
                </button>
              )}
            </>
          )}
        </div>

        {/* Footer actions */}
        <div className="px-7 py-4 border-t border-white/5 shrink-0 flex gap-3">
          {(onUpload || onUploadAll) && (
            <button
              onClick={handleUpload}
              disabled={!canUpload || uploading}
              className={cn(
                'flex-1 flex items-center justify-center gap-2 py-2.5 text-[10px] font-black uppercase tracking-widest rounded-xl border transition-all',
                canUpload && !uploading
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 active:scale-95'
                  : 'border-white/5 text-slate-700 cursor-not-allowed'
              )}
            >
              <Upload className="w-3.5 h-3.5" />
              {uploading ? 'Starting…' : canUpload
                ? isAllScrapers ? 'Upload All Passing Scrapers' : `Upload ${data.total ?? ''} Products`
                : `${data.errors} Errors — Fix First`}
            </button>
          )}
          <button
            onClick={onClose}
            className={cn(
              'py-2.5 text-[10px] font-black text-white uppercase tracking-widest border border-white/10 rounded-xl hover:bg-white/5 transition-all',
              (onUpload || onUploadAll) ? 'px-6' : 'flex-1'
            )}
          >
            Close
          </button>
        </div>
      </motion.div>
    </div>
  );
}

// ──────────────────────────────────────────────
// Store Comparison Panel
// ──────────────────────────────────────────────
interface CompareProductSide {
  image_count: number;
  variant_count: number;
  price: string;
  tags: string;
  skus?: string[];
}
interface CompareProduct {
  handle: string;
  title: string;
  status: 'identical' | 'differs' | 'only_test' | 'only_main';
  test: CompareProductSide | null;
  main: CompareProductSide | null;
}
interface CompareData {
  scraper_id: string;
  test_total: number;
  main_total: number;
  matched: number;
  identical: number;
  differs: number;
  only_test: number;
  only_main: number;
  test_error: string | null;
  main_error: string | null;
  products: CompareProduct[];
}

function StoreComparisonPanel({
  scraperId,
  scraperName,
  loading,
  data,
  error,
  onClose,
}: {
  scraperId: string;
  scraperName: string;
  loading: boolean;
  data: CompareData | null;
  error: string | null;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState<'all' | 'differs' | 'only_test' | 'only_main' | 'identical'>('all');
  const [search, setSearch] = useState('');

  const filtered = (data?.products ?? []).filter(p => {
    if (filter !== 'all' && p.status !== filter) return false;
    if (search && !p.title.toLowerCase().includes(search.toLowerCase()) && !p.handle.includes(search.toLowerCase())) return false;
    return true;
  });

  const statusStyle: Record<string, string> = {
    differs:   'bg-amber-500/10 border-amber-500/20',
    only_test: 'bg-emerald-500/8 border-emerald-500/15',
    only_main: 'bg-red-500/8 border-red-500/15',
    identical: 'bg-white/2 border-white/5',
  };
  const statusBadge: Record<string, string> = {
    differs:   'bg-amber-500/15 text-amber-400 border-amber-500/25',
    only_test: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/25',
    only_main: 'bg-red-500/15 text-red-400 border-red-500/25',
    identical: 'bg-white/5 text-slate-500 border-white/10',
  };
  const statusLabel: Record<string, string> = {
    differs:   'Differs',
    only_test: 'TEST only',
    only_main: 'MAIN only',
    identical: 'Identical',
  };

  const filters: { key: typeof filter; label: string; count: number; color: string }[] = [
    { key: 'all',       label: 'All',       count: data?.products.length ?? 0,  color: 'border-white/20 text-white' },
    { key: 'differs',   label: 'Differs',   count: data?.differs ?? 0,          color: 'border-amber-500/30 text-amber-400' },
    { key: 'only_test', label: 'TEST only', count: data?.only_test ?? 0,        color: 'border-emerald-500/30 text-emerald-400' },
    { key: 'only_main', label: 'MAIN only', count: data?.only_main ?? 0,        color: 'border-red-500/30 text-red-400' },
    { key: 'identical', label: 'Identical', count: data?.identical ?? 0,        color: 'border-white/10 text-slate-500' },
  ];

  return (
    <div className="fixed inset-0 z-[8000] flex items-start justify-center p-4 overflow-hidden">
      <motion.div
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose}
        className="absolute inset-0 bg-black/75 backdrop-blur-sm"
      />
      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.22 }}
        className="relative w-full max-w-6xl mt-8 glass-panel rounded-3xl overflow-hidden shadow-2xl flex flex-col"
        style={{ maxHeight: 'calc(100vh - 4rem)' }}
      >
        {/* Top accent */}
        <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-primary via-secondary to-primary opacity-60" />

        {/* Header */}
        <div className="flex items-center gap-4 px-8 py-6 border-b border-white/8 shrink-0">
          <div className="w-10 h-10 rounded-xl bg-primary/10 border border-primary/20 flex items-center justify-center">
            <GitCompare className="w-5 h-5 text-primary" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 mb-0.5">Store Comparison</p>
            <h3 className="text-sm font-black text-white uppercase tracking-widest truncate">{scraperName}</h3>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="px-2 py-0.5 rounded bg-primary/15 border border-primary/25 text-[9px] font-black text-primary uppercase tracking-widest">TEST</span>
            <span className="text-slate-600 text-xs">vs</span>
            <span className="px-2 py-0.5 rounded bg-red-500/15 border border-red-500/25 text-[9px] font-black text-red-400 uppercase tracking-widest">MAIN</span>
          </div>
          <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-xl border border-white/10 text-slate-500 hover:text-white hover:border-white/25 transition-all ml-2">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Loading state */}
        {loading && (
          <div className="flex-1 flex items-center justify-center py-24">
            <div className="text-center space-y-4">
              <RefreshCw className="w-8 h-8 text-primary animate-spin mx-auto" />
              <p className="text-sm font-bold text-white uppercase tracking-widest">Fetching from both stores…</p>
              <p className="text-xs text-slate-500">This may take 30–60 s for large catalogues</p>
            </div>
          </div>
        )}

        {/* Error state */}
        {!loading && error && (
          <div className="flex-1 flex items-center justify-center py-24 px-8">
            <div className="text-center space-y-3 max-w-md">
              <AlertCircle className="w-8 h-8 text-red-400 mx-auto" />
              <p className="text-sm font-bold text-white uppercase tracking-widest">Comparison Failed</p>
              <p className="text-xs text-slate-400 leading-relaxed">{error}</p>
            </div>
          </div>
        )}

        {/* Data state */}
        {!loading && !error && data && (
          <>
            {/* Credential errors */}
            {(data.test_error || data.main_error) && (
              <div className="px-8 pt-4 space-y-2 shrink-0">
                {data.test_error && (
                  <div className="flex items-start gap-2 px-4 py-2.5 rounded-xl bg-amber-500/8 border border-amber-500/20">
                    <AlertCircle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                    <p className="text-[10px] text-amber-300"><span className="font-black">TEST store error:</span> {data.test_error}</p>
                  </div>
                )}
                {data.main_error && (
                  <div className="flex items-start gap-2 px-4 py-2.5 rounded-xl bg-red-500/8 border border-red-500/20">
                    <AlertCircle className="w-3.5 h-3.5 text-red-400 shrink-0 mt-0.5" />
                    <p className="text-[10px] text-red-300"><span className="font-black">MAIN store error:</span> {data.main_error}</p>
                  </div>
                )}
              </div>
            )}

            {/* Summary stat strip */}
            <div className="grid grid-cols-3 sm:grid-cols-6 gap-3 px-8 py-5 border-b border-white/8 shrink-0">
              {[
                { label: 'TEST Total',  value: data.test_total,  color: 'text-primary' },
                { label: 'MAIN Total',  value: data.main_total,  color: 'text-red-400' },
                { label: 'Identical',   value: data.identical,   color: 'text-slate-400' },
                { label: 'Differs',     value: data.differs,     color: 'text-amber-400' },
                { label: 'TEST Only',   value: data.only_test,   color: 'text-emerald-400' },
                { label: 'MAIN Only',   value: data.only_main,   color: 'text-red-400' },
              ].map(s => (
                <div key={s.label} className="text-center">
                  <p className={cn('text-2xl font-black tabular-nums', s.color)}>{s.value.toLocaleString()}</p>
                  <p className="text-[9px] font-bold text-slate-600 uppercase tracking-widest mt-0.5">{s.label}</p>
                </div>
              ))}
            </div>

            {/* Filter bar + search */}
            <div className="flex flex-wrap items-center gap-2 px-8 py-4 border-b border-white/8 shrink-0">
              <div className="flex gap-1.5 flex-wrap">
                {filters.map(f => (
                  <button
                    key={f.key}
                    onClick={() => setFilter(f.key)}
                    className={cn(
                      'px-3 py-1.5 rounded-lg text-[9px] font-black uppercase tracking-widest border transition-all',
                      filter === f.key ? `${f.color} bg-white/5` : 'border-white/8 text-slate-600 hover:text-slate-400'
                    )}
                  >
                    {f.label} <span className="ml-1 opacity-60">{f.count}</span>
                  </button>
                ))}
              </div>
              <div className="relative ml-auto">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3 h-3 text-slate-600" />
                <input
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  placeholder="Filter products…"
                  className="pl-8 pr-4 py-1.5 bg-white/4 border border-white/8 rounded-lg text-[10px] text-white placeholder-slate-700 outline-none focus:border-white/20 w-48 transition-all"
                />
              </div>
            </div>

            {/* Table */}
            <div className="flex-1 overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="flex items-center justify-center py-16 text-slate-600">
                  <p className="text-xs font-bold uppercase tracking-widest">No products match</p>
                </div>
              ) : (
                <table className="w-full text-[10px]">
                  <thead className="sticky top-0 bg-[#0a0a0a] border-b border-white/8 z-10">
                    <tr>
                      <th className="text-left px-4 py-3 text-[9px] font-black text-slate-500 uppercase tracking-widest w-[22%]">Product</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-slate-500 uppercase tracking-widest">Status</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-primary uppercase tracking-widest">T·Img</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-primary uppercase tracking-widest">T·Var</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-primary uppercase tracking-widest">T·Price</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-red-400 uppercase tracking-widest">M·Img</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-red-400 uppercase tracking-widest">M·Var</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-red-400 uppercase tracking-widest">M·Price</th>
                      <th className="text-center px-2 py-3 text-[9px] font-black text-slate-500 uppercase tracking-widest w-[18%]">Tags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((p, i) => {
                      const rowClass = statusStyle[p.status] ?? '';
                      const testImgDiff   = p.test && p.main && p.test.image_count    !== p.main.image_count;
                      const testVarDiff   = p.test && p.main && p.test.variant_count   !== p.main.variant_count;
                      const testPriceDiff = p.test && p.main && p.test.price           !== p.main.price;
                      const normTags = (s: string) => new Set((s || '').split(',').map(t => t.trim().toLowerCase()).filter(Boolean));
                      const testTagSet  = normTags(p.test?.tags ?? '');
                      const mainTagSet  = normTags(p.main?.tags ?? '');
                      const tagsDiff = p.test && p.main && (
                        testTagSet.size !== mainTagSet.size ||
                        ![...testTagSet].every(t => mainTagSet.has(t))
                      );
                      return (
                        <tr key={p.handle} className={cn('border-b transition-colors', rowClass, i % 2 === 0 ? '' : 'brightness-110')}>
                          <td className="px-4 py-2.5">
                            <p className="font-bold text-white truncate max-w-[200px]" title={p.title}>{p.title}</p>
                            <p className="text-slate-600 text-[9px] font-mono truncate">{p.handle}</p>
                          </td>
                          <td className="px-2 py-2.5 text-center">
                            <span className={cn('px-2 py-0.5 rounded text-[8px] font-black uppercase tracking-widest border', statusBadge[p.status])}>
                              {statusLabel[p.status]}
                            </span>
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testImgDiff ? 'text-amber-400' : 'text-slate-300')}>
                            {p.test?.image_count ?? '—'}
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testVarDiff ? 'text-amber-400' : 'text-slate-300')}>
                            {p.test?.variant_count ?? '—'}
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testPriceDiff ? 'text-amber-400' : 'text-slate-300')}>
                            {p.test?.price ?? '—'}
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testImgDiff ? 'text-amber-400' : 'text-slate-500')}>
                            {p.main?.image_count ?? '—'}
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testVarDiff ? 'text-amber-400' : 'text-slate-500')}>
                            {p.main?.variant_count ?? '—'}
                          </td>
                          <td className={cn('px-2 py-2.5 text-center font-mono font-bold', testPriceDiff ? 'text-amber-400' : 'text-slate-500')}>
                            {p.main?.price ?? '—'}
                          </td>
                          <td className="px-2 py-2.5 text-[9px] max-w-[160px]">
                            {!p.test && !p.main ? '—' : tagsDiff ? (
                              <div className="space-y-0.5">
                                <div className="flex items-center gap-1 mb-0.5">
                                  <span className="px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/20 text-[8px] font-black text-amber-400 uppercase tracking-widest">≠ Differs</span>
                                </div>
                                {p.test?.tags && <p className="text-primary/80 truncate" title={p.test.tags}>T: {p.test.tags}</p>}
                                {p.main?.tags && <p className="text-red-400/80 truncate" title={p.main.tags}>M: {p.main.tags}</p>}
                              </div>
                            ) : (
                              <p className="text-slate-600 truncate" title={p.test?.tags || p.main?.tags || ''}>
                                {(p.test?.tags || p.main?.tags) ? (p.test?.tags || p.main?.tags) : <span className="text-slate-700">—</span>}
                              </p>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>

            {/* Footer */}
            <div className="px-8 py-3 border-t border-white/8 shrink-0 flex items-center justify-between">
              <p className="text-[9px] text-slate-600 font-bold uppercase tracking-widest">
                Showing {filtered.length.toLocaleString()} of {data.products.length.toLocaleString()} products
              </p>
              <p className="text-[9px] text-slate-600">Amber = changed field · Green row = TEST only · Red row = MAIN only</p>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}

function ScraperCard({ site, onRun, onRestart, onCancel, onShopifyCancel, onDownload, onShopifyUpload, onShopifyUpdate, onShopifyUpdateImages, onShopifyCheckImages, onShopifyCheckOos, onShopifyDeleteOos, onShopifyNuke, onShopifyDedup, onDeleteProducts, onValidate, onQCUpload, onPromote, onApprove, isApproved, activeShopifyOp, activeStore, onCompare }: {
  site: WebsiteStats;
  onRun: () => void;
  onRestart: () => void;
  onCancel: () => void;
  onShopifyCancel: () => void;
  onDownload: () => void;
  onShopifyUpload: () => void;
  onShopifyUpdate: () => void;
  onShopifyUpdateImages: () => void;
  onShopifyCheckImages: () => void;
  onShopifyCheckOos: () => void;
  onShopifyDeleteOos: () => void;
  onShopifyNuke: () => void;
  onShopifyDedup: () => void;
  onDeleteProducts: () => void;
  onValidate: () => void;
  onQCUpload: () => void;
  onPromote?: () => void;
  onApprove?: () => void;
  isApproved?: boolean;
  activeShopifyOp?: string | null;
  activeStore?: 'test' | 'main';
  onCompare?: () => void;
}) {
  const [prevCount, setPrevCount] = useState(site.totalProducts);
  const countChanged = site.is_running && site.products_count !== undefined && site.products_count !== prevCount;

  const [completedShopifyResult, setCompletedShopifyResult] = useState<{ op: string; counts: ShopifyCounts; failed: boolean } | null>(null);
  const completedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevShopifyRunningRef = useRef<boolean>(false);

  useEffect(() => {
    if (site.products_count !== undefined) setPrevCount(site.products_count);
  }, [site.products_count]);

  useEffect(() => {
    const wasRunning = prevShopifyRunningRef.current;
    const isShopifyRunning = !!(site.is_running && site.shopify_op);
    const justFinished = wasRunning && !isShopifyRunning && site.progress === 100 && site.shopify_op && site.shopify_counts;

    if (justFinished && site.shopify_op && site.shopify_counts) {
      const hasFailed = (site.shopify_counts.failed ?? 0) > 0;
      setCompletedShopifyResult({ op: site.shopify_op, counts: site.shopify_counts, failed: hasFailed });
      if (completedTimerRef.current) clearTimeout(completedTimerRef.current);
      completedTimerRef.current = setTimeout(() => setCompletedShopifyResult(null), 12000);
    }

    prevShopifyRunningRef.current = isShopifyRunning;

    if (activeShopifyOp) setCompletedShopifyResult(null);
  }, [site.is_running, site.progress, site.shopify_op, site.shopify_counts, activeShopifyOp]);

  useEffect(() => () => { if (completedTimerRef.current) clearTimeout(completedTimerRef.current); }, []);

  // Quota sleep toast transitions
  const prevQuotaSleepingRef = useRef<boolean>(false);
  useEffect(() => {
    const wasAsleep = prevQuotaSleepingRef.current;
    const isAsleep = !!(site.quota_sleeping);
    if (!wasAsleep && isAsleep && site.quota_resume_at) {
      // Transitioned into quota sleep
      const utcStr = site.quota_resume_at;
      // Convert UTC time to IST for display (UTC+5:30)
      const [hh, mm] = utcStr.replace(' UTC', '').split(':').map(Number);
      const istH = (hh + 5) % 24;
      const istM = mm + 30 >= 60 ? (mm + 30 - 60) : (mm + 30);
      const istHFinal = mm + 30 >= 60 ? (istH + 1) % 24 : istH;
      const istStr = `${String(istHFinal).padStart(2, '0')}:${String(istM).padStart(2, '0')} IST`;
      toast('warning', 'Variant Quota Hit', `Upload paused — auto-resumes at ${utcStr} (${istStr})`);
    } else if (wasAsleep && !isAsleep && site.is_running) {
      // Transitioned out of quota sleep (upload still running = resumed)
      toast('success', 'Quota Reset', 'Daily variant limit reset — upload resuming automatically');
    }
    prevQuotaSleepingRef.current = isAsleep;
  }, [site.quota_sleeping, site.quota_resume_at, site.is_running]);

  const isPageScanPhase = site.is_running && (
    site.status?.includes('Scanning pages') ||
    site.status?.includes('Analyzing') ||
    site.status?.includes('Splitting') ||
    site.status?.includes('Initializing')
  );
  const isGraphQLPhase = site.is_running && (
    site.status?.includes('Syncing batch') ||
    site.status?.includes('sync') ||
    site.status?.includes('Starting sync')
  );
  const isFinalPhase = site.is_running && (
    site.status?.includes('Cleaning') ||
    site.status?.includes('Success') ||
    site.status?.includes('Saving')
  );

  const displayCount = site.is_running && site.products_count !== undefined
    ? site.products_count
    : site.totalProducts;

  const countLabel = !site.is_running
    ? 'TOTAL PRODUCTS'
    : isPageScanPhase
      ? 'COLOR CODES FOUND'
      : isGraphQLPhase
        ? 'PRODUCTS SYNCED'
        : isFinalPhase
          ? 'PRODUCTS SAVED'
          : 'PRODUCTS FETCHED';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.97 }}
      animate={{ opacity: 1, scale: 1 }}
      className={cn(
        'relative glass-card flex flex-col transition-all duration-300 rounded-3xl overflow-hidden group/card',
        site.is_running ? 'ring-2 ring-primary/50 bg-primary/5' :
        site.stuck ? 'ring-2 ring-red-500/40 bg-red-500/5' : ''
      )}
    >
      {/* Running top bar */}
      {site.is_running && (
        <div className="h-[3px] w-full bg-gradient-to-r from-primary to-secondary" />
      )}
      {/* Stuck top bar */}
      {site.stuck && !site.is_running && (
        <div className="h-[3px] w-full bg-gradient-to-r from-red-500 to-orange-500" />
      )}

      <div className="p-6 flex-1 flex flex-col sm:flex-row gap-6">
        {/* Header & Meta */}
        <div className="flex-1 space-y-5">
          <div className="flex justify-between items-start">
            <div>
              <div className="flex items-center gap-2.5 mb-1">
                {site.is_running && (
                  <span className="relative flex h-2.5 w-2.5">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-primary opacity-75" />
                    <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-primary" />
                  </span>
                )}
                {site.stuck && !site.is_running && (
                  <span className="px-2 py-0.5 rounded-full bg-red-500/20 border border-red-500/30 text-red-400 text-[8px] font-black uppercase tracking-wider">
                    Stuck
                  </span>
                )}
                <h4 className="text-sm font-black text-white uppercase tracking-widest group-hover/card:text-primary transition-colors">{site.name}</h4>
              </div>
              <div className="flex items-center gap-1.5 text-slate-500 text-[10px] font-mono">
                <Clock className="w-3 h-3" />
                <span>Updated {site.lastUpdated}</span>
              </div>
            </div>
            <div className={cn(
              'px-3 py-1 rounded-full border text-[9px] font-black tracking-widest uppercase transition-all',
              site.is_running ? 'border-primary/30 text-primary bg-primary/10' : 'border-white/10 text-slate-500 bg-white/5'
            )}>
              {site.currency}
            </div>
          </div>
        </div>

        {/* Product Count */}
        <div className="bg-white/5 rounded-2xl py-6 px-4 sm:px-6 w-full sm:w-auto min-w-[140px] text-center border border-white/5 group-hover/card:border-primary/20 transition-all flex flex-col justify-center">
          <AnimatePresence mode="popLayout">
            <motion.p
              key={displayCount}
              initial={countChanged ? { y: -16, opacity: 0 } : false}
              animate={{ y: 0, opacity: 1 }}
              className="text-4xl sm:text-5xl font-black text-white tabular-nums tracking-tight"
            >
              {displayCount.toLocaleString()}
            </motion.p>
          </AnimatePresence>
          <p className="text-[9px] text-slate-500 font-bold uppercase tracking-[0.2em] mt-2">
            {countLabel}
          </p>
          {site.quality && !site.is_running && (
            <div className={cn(
              'mt-2.5 px-2 py-0.5 rounded-full text-[8px] font-black uppercase tracking-wider self-center',
              (site.quality.pass_rate ?? 0) >= 95
                ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20'
                : (site.quality.pass_rate ?? 0) >= 80
                  ? 'bg-amber-500/15 text-amber-400 border border-amber-500/20'
                  : 'bg-rose-500/15 text-rose-400 border border-rose-500/20'
            )}>
              QG {site.quality.pass_rate ?? 0}%
            </div>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="p-5 space-y-4 bg-white/[0.02] border-t border-white/5">
        {site.is_running ? (
          <div className="space-y-4">
            <div className="flex justify-between text-[10px] font-bold font-mono">
              <span className="text-primary uppercase tracking-widest truncate max-w-[70%]">{site.status || 'SCRAPING…'}</span>
              <span className="text-slate-400">{site.progress || 0}%</span>
            </div>
            <div className="relative h-2 bg-white/5 rounded-full w-full overflow-hidden border border-white/5">
              <motion.div
                className="absolute inset-y-0 left-0 bg-gradient-to-r from-primary to-secondary"
                animate={{ width: `${site.progress || 0}%` }}
                transition={{ duration: 0.8, ease: 'easeOut' }}
              />
            </div>
            <div className="grid grid-cols-2 gap-3 pt-1">
              <button
                onClick={onRestart}
                className="flex items-center justify-center gap-1.5 py-3 text-[10px] font-black text-white uppercase tracking-widest border border-white/10 rounded-xl hover:bg-white/10 transition-all"
              >
                <RotateCcw className="w-3.5 h-3.5" /> Restart
              </button>
              <button
                onClick={activeShopifyOp ? onShopifyCancel : onCancel}
                className="flex items-center justify-center gap-1.5 py-3 text-[10px] font-black text-accent uppercase tracking-widest border border-accent/20 rounded-xl hover:bg-accent/10 transition-all"
              >
                <XCircle className="w-3.5 h-3.5" /> Stop
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {site.stuck && (
              <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-red-500/10 border border-red-500/20">
                <span className="text-red-400 text-[9px] font-bold uppercase tracking-wider truncate">
                  ⚠ {site.status || 'Stuck — no progress detected'}
                </span>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <button
                onClick={onRun}
                className="flex items-center justify-center gap-2 py-3.5 bg-primary hover:bg-primary-hover text-white text-[10px] font-black uppercase tracking-widest rounded-xl transition-all shadow-lg shadow-primary/20 active:scale-95"
              >
                <Zap className="w-3.5 h-3.5 fill-current" /> Run
              </button>
              <button
                onClick={onRestart}
                className={cn(
                  "flex items-center justify-center gap-2 py-3.5 text-[10px] font-black uppercase tracking-widest rounded-xl transition-all active:scale-95",
                  site.stuck
                    ? "border border-red-500/30 text-red-400 hover:bg-red-500/10"
                    : "border border-white/10 text-slate-400 hover:bg-white/10 hover:text-white"
                )}
              >
                <RotateCcw className="w-3.5 h-3.5" /> Restart
              </button>
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <button
            onClick={onDownload}
            className="flex items-center justify-center gap-2 py-3 text-[10px] font-black text-slate-400 uppercase tracking-widest border border-white/10 rounded-xl hover:bg-white/10 hover:text-white transition-all active:scale-95"
          >
            <Download className="w-4 h-4" /> CSV
          </button>
          <button
            onClick={onDeleteProducts}
            className="flex items-center justify-center gap-2 py-3 text-[10px] font-black text-rose-500/60 uppercase tracking-widest border border-rose-500/10 rounded-xl hover:bg-rose-500/10 hover:text-rose-500 transition-all active:scale-95"
          >
            <Trash2 className="w-3.5 h-3.5" /> Clear Local
          </button>
        </div>

        {/* Shopify actions */}
        <div className="pt-2 space-y-3">
          <div className="flex items-center gap-3">
            <div className="flex-1 h-px bg-white/5" />
            <p className="text-[9px] font-bold text-slate-600 uppercase tracking-[0.2em] flex items-center gap-1.5">
              Shopify{activeShopifyOp ? <span className="text-primary flex items-center gap-1"><span className="w-1 h-1 bg-primary rounded-full animate-pulse" /> RUNNING</span> : null}
            </p>
            <div className="flex-1 h-px bg-white/5" />
          </div>
          {/* QC buttons — always available */}
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={onValidate}
              className="flex items-center justify-center gap-1.5 py-2.5 text-[9px] font-black uppercase tracking-widest border border-primary/20 rounded-lg text-primary hover:bg-primary/10 transition-all active:scale-95"
            >
              <CheckCheck className="w-3 h-3" /> QC Check
            </button>
            <button
              onClick={onQCUpload}
              disabled={Boolean(activeShopifyOp)}
              className={cn(
                'flex items-center justify-center gap-1.5 py-2.5 text-[9px] font-black uppercase tracking-widest border rounded-lg transition-all',
                'border-violet-500/25 text-violet-400 hover:bg-violet-500/10',
                activeShopifyOp ? 'opacity-30 cursor-not-allowed' : 'active:scale-95'
              )}
            >
              <ShieldCheck className="w-3 h-3" /> QC &amp; Upload
            </button>
          </div>

          {/* Live Shopify progress panel — backend fields take priority; activeShopifyOp is local fallback */}
          <AnimatePresence>
            {(site.shopify_op && site.shopify_counts && (site.is_running || activeShopifyOp)) && (
              <ShopifyProgressPanel
                op={site.shopify_op}
                counts={site.shopify_counts}
                progress={site.progress ?? 0}
                isRunning={true}
                onCancel={onShopifyCancel}
                quotaSleeping={site.quota_sleeping}
                resumeAt={site.quota_resume_at}
                estimatedVariants={site.estimated_variants}
              />
            )}
            {(!site.is_running && !activeShopifyOp && completedShopifyResult) && (
              <ShopifyProgressPanel
                op={completedShopifyResult.op}
                counts={completedShopifyResult.counts}
                progress={100}
                isRunning={false}
                hasFailed={completedShopifyResult.failed}
              />
            )}
          </AnimatePresence>

          {/* Store badge */}
          {activeStore === 'main' && (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-red-500/8 border border-red-500/20">
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-red-500" />
              </span>
              <span className="text-[9px] font-black text-red-400 uppercase tracking-widest">Live — MAIN STORE</span>
            </div>
          )}

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {([
              { op: 'upload',     label: 'Upload',     icon: <Upload className="w-3 h-3" />,        onClick: onShopifyUpload,       border: 'border-emerald-500/20 hover:bg-emerald-500/10 text-emerald-400' },
              { op: 'update',     label: 'Update',     icon: <RefreshCw className="w-3 h-3" />,     onClick: onShopifyUpdate,       border: 'border-blue-500/20 hover:bg-blue-500/10 text-blue-400' },
              { op: 'reimage',       label: 'Fix Images', icon: <ImageIcon className="w-3 h-3" />,     onClick: onShopifyUpdateImages,  border: 'border-violet-500/20 hover:bg-violet-500/10 text-violet-400' },
              { op: 'check-images',  label: 'Img QC',     icon: <ScanLine className="w-3 h-3" />,      onClick: onShopifyCheckImages,   border: 'border-cyan-500/20 hover:bg-cyan-500/10 text-cyan-400' },
              { op: 'check-oos',     label: 'Check OOS',  icon: <CheckCircle2 className="w-3 h-3" />,  onClick: onShopifyCheckOos,      border: 'border-amber-500/20 hover:bg-amber-500/10 text-amber-400' },
              { op: 'delete-oos', label: 'Delete OOS', icon: <Trash2 className="w-3 h-3" />,        onClick: onShopifyDeleteOos,    border: 'border-rose-500/20 hover:bg-rose-500/10 text-rose-400' },
              { op: 'dedup',      label: 'Dedup',       icon: <GitCompare className="w-3 h-3" />,   onClick: onShopifyDedup,        border: 'border-orange-500/20 hover:bg-orange-500/10 text-orange-400' },
              { op: 'nuke',       label: 'Clear Store', icon: <ZapOff className="w-3 h-3" />,       onClick: onShopifyNuke,         border: 'border-red-600/30 hover:bg-red-600/10 text-red-500 col-span-2 sm:col-span-1' },
            ] as const).map(({ op, label, icon, onClick, border }) => {
              const isActive = activeShopifyOp === op;
              const isDisabled = Boolean(activeShopifyOp);
              return (
                <button
                  key={op}
                  onClick={onClick}
                  disabled={isDisabled}
                  className={cn(
                    'flex items-center justify-center gap-1.5 py-2.5 text-[9px] font-black uppercase tracking-widest border rounded-lg transition-all',
                    border,
                    isDisabled ? 'opacity-30 cursor-not-allowed' : 'active:scale-95'
                  )}
                >
                  {isActive ? <><RefreshCw className="w-3 h-3 animate-spin" />{label}…</> : <>{icon}{label}</>}
                </button>
              );
            })}
          </div>

          {/* Approve for MAIN + Promote to MAIN */}
          {onApprove && (() => {
            const qcPct = site.quality?.pass_rate ?? 0;
            const qcReady = qcPct >= 100;
            const approveDisabled = Boolean(activeShopifyOp) || isApproved || !qcReady;
            const approveTitle = isApproved
              ? 'Already approved — click Promote to proceed'
              : !qcReady
                ? `Quality gate must be 100% before approving (currently ${qcPct}%) — run Validate first`
                : 'Approve for MAIN store — requires 100% quality gate pass rate';
            return (
              <button
                onClick={onApprove}
                disabled={approveDisabled}
                title={approveTitle}
                className={cn(
                  'w-full flex items-center justify-center gap-2 py-2.5 text-[9px] font-black uppercase tracking-widest border rounded-lg transition-all',
                  isApproved
                    ? 'border-emerald-500/40 text-emerald-400 bg-emerald-500/10 cursor-default'
                    : qcReady
                      ? 'border-amber-500/30 text-amber-400 hover:bg-amber-500/10'
                      : 'border-white/10 text-slate-600 cursor-not-allowed',
                  approveDisabled && !isApproved ? 'opacity-40' : ''
                )}
              >
                <ShieldCheck className="w-3 h-3" />
                {isApproved ? '✓ Approved for MAIN' : 'Approve for MAIN'}
              </button>
            );
          })()}
          {onPromote && (
            <button
              onClick={onPromote}
              disabled={Boolean(activeShopifyOp) || !isApproved}
              title={!isApproved ? 'Approve for MAIN first before promoting' : 'Upload to MAIN store'}
              className={cn(
                'w-full flex items-center justify-center gap-2 py-2.5 text-[9px] font-black uppercase tracking-widest border rounded-lg transition-all',
                'border-red-500/30 text-red-400 hover:bg-red-500/10',
                (activeShopifyOp || !isApproved) ? 'opacity-30 cursor-not-allowed' : 'active:scale-95'
              )}
            >
              <ArrowUpToLine className="w-3 h-3" />
              Promote → MAIN Store
            </button>
          )}
          {onCompare && (
            <button
              onClick={onCompare}
              className="w-full flex items-center justify-center gap-2 py-2.5 text-[9px] font-black uppercase tracking-widest border border-primary/20 text-primary hover:bg-primary/10 rounded-lg transition-all active:scale-95"
            >
              <GitCompare className="w-3 h-3" />
              Compare Stores
            </button>
          )}
        </div>
      </div>
    </motion.div>
  );
}

// ──────────────────────────────────────────────
// Global Shopify Command Center — Bold Minimal
// ──────────────────────────────────────────────
const GLOBAL_SCRAPER_IDS = [
  'coach', 'cruise_fashion', 'michael_kors', 'karl',
  'marcjacobs', 'tory', 'gymshark', 'aloyoga', 'mytheresa', 'thedesignerboxuk', 'uk_polene', 'hoka',
  'drmartens', 'ugg', 'skims', 'thereformation', 'underarmour', 'organicbasics',
];

interface GlobalPanelProps {
  globalOp: string | null;
  globalProgress: any;
  allScrapersProgress?: Record<string, any>;
  onUploadAll: () => void;
  onUpdateAll: () => void;
  onCheckOosAll: () => void;
  onDeleteOosAll: () => void;
  onSyncAll: () => void;
  onNukeAll: () => void;
  onFullPipeline: () => void;
  onValidateAll: () => void;
  onQCUploadAll: () => void;
  onViewLogs: () => void;
}

function GlobalShopifyPanel({
  globalOp, globalProgress, allScrapersProgress,
  onUploadAll, onUpdateAll, onCheckOosAll, onDeleteOosAll, onSyncAll, onNukeAll,
  onFullPipeline, onValidateAll, onQCUploadAll, onViewLogs
}: GlobalPanelProps) {
  const isRunning = globalOp !== null;
  const pct = globalProgress?.progress ?? 0;
  const status = globalProgress?.status ?? '';
  const r = globalProgress?.shopify_result;
  const shopifyCounts: ShopifyCounts | null = globalProgress?.shopify_counts ?? null;
  const shopifyOp: string | null = globalProgress?.shopify_op ?? null;
  const showLiveCounts = !!(shopifyOp && shopifyCounts && (isRunning || pct === 100));

  const [breakdownVisible, setBreakdownVisible] = useState(false);
  const breakdownTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevIsRunningRef = useRef(false);
  const lastOpKeyRef = useRef<string | null>(null);

  // Derive op key from globalOp (e.g. 'upload-all' → 'upload')
  const opKey = globalOp?.includes('upload') ? 'upload'
    : globalOp?.includes('update') ? 'update'
    : globalOp?.includes('delete') ? 'delete-oos'
    : globalOp?.includes('nuke') ? 'nuke'
    : null;
  if (opKey) lastOpKeyRef.current = opKey;
  const effectiveOpKey = opKey ?? lastOpKeyRef.current;

  useEffect(() => {
    if (isRunning && !prevIsRunningRef.current) {
      setBreakdownVisible(true);
      if (breakdownTimerRef.current) clearTimeout(breakdownTimerRef.current);
    }
    if (!isRunning && prevIsRunningRef.current) {
      breakdownTimerRef.current = setTimeout(() => setBreakdownVisible(false), 12000);
    }
    prevIsRunningRef.current = isRunning;
  }, [isRunning]);

  useEffect(() => () => { if (breakdownTimerRef.current) clearTimeout(breakdownTimerRef.current); }, []);

  const mainCountKey = effectiveOpKey === 'upload' ? 'created'
    : effectiveOpKey === 'update' ? 'updated'
    : 'deleted';

  const scraperRows = GLOBAL_SCRAPER_IDS.map(sid => {
    const prog = allScrapersProgress?.[sid];
    const name = (SCRAPER_NAMES as Record<string, string>)[sid] ?? sid;
    if (!prog || prog.progress === undefined) {
      return { sid, name, state: 'pending' as const, count: 0, skipped: 0, failed: 0, total: 0, pct: 0 };
    }
    const state: 'running' | 'done' | 'started' | 'pending' = prog.is_running
      ? 'running'
      : prog.progress === 100
        ? 'done'
        : 'started';
    const counts = prog.shopify_counts ?? {};
    const count = counts[mainCountKey] ?? 0;
    return {
      sid, name, state,
      count,
      skipped: counts.skipped ?? 0,
      failed: counts.failed ?? 0,
      total: counts.total ?? 0,
      pct: prog.progress ?? 0,
    };
  });

  const actions = [
    { key: 'full-pipeline',  label: 'FULL PIPELINE',   icon: <Play className="w-4 h-4" />,        onClick: onFullPipeline,  highlight: true  },
    { key: 'validate-all',   label: 'VALIDATE ALL',    icon: <CheckCheck className="w-4 h-4" />,  onClick: onValidateAll,   highlight: false },
    { key: 'qc-upload-all',  label: 'QC & UPLOAD ALL', icon: <ShieldCheck className="w-4 h-4" />, onClick: onQCUploadAll,   highlight: false },
    { key: 'upload-all',     label: 'UPLOAD ALL',      icon: <Upload className="w-4 h-4" />,      onClick: onUploadAll,     highlight: false },
    { key: 'update-all',     label: 'UPDATE ALL',      icon: <RefreshCw className="w-4 h-4" />,   onClick: onUpdateAll,     highlight: false },
    { key: 'check-oos-all',  label: 'CHECK OOS',       icon: <ShieldCheck className="w-4 h-4" />, onClick: onCheckOosAll,   highlight: false },
    { key: 'delete-oos-all', label: 'DELETE OOS',      icon: <Trash2 className="w-4 h-4" />,      onClick: onDeleteOosAll,  highlight: false },
    { key: 'nuke-all',       label: 'CLEAR ALL',       icon: <ZapOff className="w-4 h-4" />,      onClick: onNukeAll,       highlight: false },
  ] as const;

  return (
    <motion.section
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="relative glass-panel rounded-3xl overflow-hidden shadow-2xl ring-2 ring-primary/20"
    >
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center items-start justify-between px-6 sm:px-8 py-5 border-b border-white/5 bg-white/[0.02] gap-4">
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20 shadow-inner">
            <Shield className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h3 className="text-sm font-black text-white uppercase tracking-widest">Shopify Command Center</h3>
            <p className="text-[11px] text-slate-500 mt-0.5">Global ops across all scrapers — safety-guarded</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {isRunning && (
            <div className="flex flex-col items-end gap-1">
              <span className="flex items-center gap-2 px-4 py-2 rounded-full border border-primary/30 bg-primary/10 text-primary text-[10px] font-black uppercase tracking-widest shadow-lg shadow-primary/10">
                <span className="w-1.5 h-1.5 bg-primary rounded-full animate-pulse" />
                {status || 'RUNNING…'} ({pct}%)
              </span>
              <div className="w-full bg-white/10 rounded-full h-1 mt-1 overflow-hidden">
                <div className="bg-primary h-full transition-all duration-300" style={{ width: `${pct}%` }} />
              </div>
            </div>
          )}
          <button
            onClick={onViewLogs}
            className="flex items-center gap-2 px-4 py-2 rounded-xl border border-white/10 text-slate-400 hover:border-white/20 hover:text-white text-[10px] font-black uppercase tracking-widest transition-all bg-white/5"
          >
            <History className="w-3.5 h-3.5" /> Logs
          </button>
        </div>
      </div>

      {/* Action Buttons */}
      <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 bg-white/[0.01]">
        {actions.map(({ key, label, icon, onClick, highlight }) => {
          const active = globalOp === key;
          const isValidateAction = key === 'validate-all';
          const disabledBtn = isRunning && !isValidateAction;
          return (
            <button
              key={key}
              onClick={onClick}
              disabled={disabledBtn}
              className={cn(
                'flex flex-col items-center gap-3 px-4 py-5 border-r border-white/5 last:border-r-0 text-[9px] font-black uppercase tracking-widest transition-all group',
                active
                  ? 'bg-primary text-white shadow-inner'
                  : disabledBtn
                    ? 'text-slate-700 cursor-not-allowed'
                    : key === 'nuke-all'
                      ? 'text-rose-500/60 hover:bg-rose-500/10 hover:text-rose-500'
                      : highlight
                        ? 'text-emerald-400 hover:bg-emerald-500/10 hover:text-emerald-300'
                        : 'text-slate-400 hover:bg-white/5 hover:text-primary'
              )}
            >
              <div className={cn(
                "p-2.5 rounded-xl transition-all",
                active ? "bg-white/20"
                  : highlight ? "bg-emerald-500/10 group-hover:bg-emerald-500/20"
                  : "bg-white/5 group-hover:bg-primary/10"
              )}>
                {active ? <RefreshCw className="w-4 h-4 animate-spin" /> : icon}
              </div>
              <span className="text-center leading-tight">{active ? 'RUNNING…' : label}</span>
            </button>
          );
        })}
      </div>

      {/* Progress */}
      <AnimatePresence>
        {(isRunning || pct > 0) && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="px-8 py-5 border-b border-white/5 space-y-3 bg-primary/[0.02]"
          >
            {/* Status line + overall bar */}
            <div className="flex justify-between text-[11px] font-mono font-bold">
              <span className="text-primary uppercase truncate max-w-[80%]">{status || 'PROCESSING…'}</span>
              <span className="text-slate-400">{pct}%</span>
            </div>
            <div className="h-2 bg-white/5 rounded-full w-full overflow-hidden border border-white/5">
              <motion.div
                className="h-full bg-gradient-to-r from-primary to-secondary"
                animate={{ width: `${pct}%` }}
                transition={{ duration: 0.6, ease: 'easeOut' }}
              />
            </div>

            {/* Live per-op count breakdown (same panel used on scraper cards) */}
            {showLiveCounts && shopifyOp && shopifyCounts && (
              <ShopifyProgressPanel
                op={shopifyOp}
                counts={shopifyCounts}
                progress={pct}
                isRunning={isRunning}
                hasFailed={(shopifyCounts.failed ?? 0) > 0 && !isRunning}
              />
            )}

            {/* Fallback final summary when no counts available */}
            {!showLiveCounts && r && pct === 100 && (
              <div className="flex flex-wrap gap-5 pt-1">
                {r.created > 0  && <span className="text-[10px] font-black text-emerald-400 uppercase flex items-center gap-1.5"><Upload className="w-3 h-3"/> {r.created} CREATED</span>}
                {r.updated > 0  && <span className="text-[10px] font-black text-blue-400 uppercase flex items-center gap-1.5"><RefreshCw className="w-3 h-3"/> {r.updated} UPDATED</span>}
                {r.deleted > 0  && <span className="text-[10px] font-black text-rose-400 uppercase flex items-center gap-1.5"><Trash2 className="w-3 h-3"/> {r.deleted} DELETED</span>}
                {r.skipped > 0  && <span className="text-[10px] font-bold text-slate-600 uppercase">{r.skipped} SKIPPED</span>}
                {r.failed > 0   && <span className="text-[10px] font-black text-red-500 uppercase flex items-center gap-1.5"><AlertCircle className="w-3 h-3"/> {r.failed} FAILED</span>}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Per-scraper live breakdown */}
      <AnimatePresence>
        {breakdownVisible && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3 }}
            className="border-b border-white/5 overflow-hidden"
          >
            <div className="px-8 pt-4 pb-1 flex items-center justify-between">
              <span className="text-[9px] font-black uppercase tracking-widest text-slate-500">
                {isRunning ? 'Live Scraper Progress' : 'Run Summary'}
              </span>
              {!isRunning && (
                <span className="text-[9px] text-slate-600 font-mono">Auto-dismissing…</span>
              )}
            </div>
            <div className="px-6 pb-4">
              <div className="rounded-xl border border-white/5 overflow-hidden bg-white/[0.015]">
                {scraperRows.map((row, i) => {
                  const isPending = row.state === 'pending';
                  const isRowRunning = row.state === 'running';
                  const isDone = row.state === 'done';
                  const hasStarted = !isPending;
                  const hasFailed = row.failed > 0;

                  const actionVerb = effectiveOpKey === 'upload' ? 'created'
                    : effectiveOpKey === 'update' ? 'updated'
                    : 'deleted';

                  return (
                    <div
                      key={row.sid}
                      className={cn(
                        'flex items-center gap-3 px-4 py-2.5 border-b border-white/[0.04] last:border-b-0 transition-colors',
                        isRowRunning ? 'bg-primary/5' : isDone && hasFailed ? 'bg-red-500/5' : ''
                      )}
                    >
                      {/* State icon */}
                      <div className="w-4 shrink-0 flex items-center justify-center">
                        {isPending && <span className="w-1.5 h-1.5 rounded-full bg-white/10" />}
                        {isRowRunning && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />}
                        {isDone && !hasFailed && <CheckCircle2 className="w-3 h-3 text-emerald-400" />}
                        {isDone && hasFailed && <AlertCircle className="w-3 h-3 text-red-400" />}
                        {row.state === 'started' && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />}
                      </div>

                      {/* Name */}
                      <span className={cn(
                        'text-[10px] font-bold flex-1 min-w-0 truncate',
                        isPending ? 'text-slate-600' : isRowRunning ? 'text-white' : isDone ? 'text-slate-300' : 'text-slate-400'
                      )}>
                        {row.name}
                      </span>

                      {/* Counts */}
                      {hasStarted && (
                        <div className="flex items-center gap-3 shrink-0">
                          <span className={cn(
                            'text-[10px] font-black tabular-nums',
                            isDone && !hasFailed ? 'text-emerald-400' : isRowRunning ? 'text-primary' : 'text-slate-400'
                          )}>
                            {row.count.toLocaleString()}
                            {row.total > 0 && (
                              <span className="text-slate-600 font-normal">/{row.total.toLocaleString()}</span>
                            )}
                            <span className="text-slate-600 font-normal ml-1">{actionVerb}</span>
                          </span>
                          {row.skipped > 0 && (
                            <span className="text-[9px] text-slate-600 tabular-nums">{row.skipped.toLocaleString()} skip</span>
                          )}
                          {row.failed > 0 && (
                            <span className="text-[9px] text-red-400 tabular-nums font-black">{row.failed} fail</span>
                          )}
                          {isRowRunning && (
                            <span className="text-[9px] text-slate-500 tabular-nums font-mono">{row.pct}%</span>
                          )}
                        </div>
                      )}
                      {isPending && (
                        <span className="text-[9px] text-slate-700 font-mono shrink-0">pending</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Safety Badge */}
      <div className="flex items-center gap-3 px-8 py-4 bg-black/20">
        <ShieldCheck className="w-4 h-4 text-primary shrink-0 opacity-80" />
        <p className="text-[10px] text-slate-500 font-mono italic">
          All operations safety-restricted to <code className="text-primary/70 bg-primary/5 px-1.5 py-0.5 rounded border border-primary/10">RudraScrapper-&#123;id&#125;</code> tagged products
        </p>
      </div>
    </motion.section>
  );
}

// ──────────────────────────────────────────────
// Activity Logs Page
// ──────────────────────────────────────────────
const ACTION_LABELS: Record<string, string> = {
  upload: 'Upload', update: 'Update', delete_oos: 'Delete OOS', check_oos: 'Check OOS',
  upload_all: 'Upload All', update_all: 'Update All', delete_oos_all: 'Delete OOS All',
  check_oos_all: 'Check OOS All', sync_all: 'Full Sync',
  upload_all_: 'Upload All', update_all_: 'Update All',
};
const SCRAPER_NAMES: Record<string, string> = {
  coach: 'Coach', cruise_fashion: 'Cruise Fashion', michael_kors: 'Michael Kors',
  karl: 'Karl Lagerfeld', marcjacobs: 'Marc Jacobs', tory: 'Tory Burch',
  gymshark: 'Gymshark', aloyoga: 'Alo Yoga',
  mytheresa: 'Mytheresa', thedesignerboxuk: 'The Designer Box UK',
  uk_polene: 'UK Polene', hoka: 'Hoka', skims: 'SKIMS Body',
  drmartens: 'Dr. Martens', ugg: 'UGG', thereformation: 'The Reformation',
  underarmour: 'Under Armour', organicbasics: 'Organic Basics', __global__: 'Global',
};

function ActivityLogsPage({ onBack }: { onBack: () => void }) {
  const [scraperFilter, setScraperFilter] = useState('all');
  const [actionFilter, setActionFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [storeFilter, setStoreFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [page, setPage] = useState(0);
  const pageSize = 50;

  const params = new URLSearchParams();
  if (scraperFilter !== 'all') params.set('scraper_id', scraperFilter);
  if (actionFilter !== 'all') params.set('action_type', actionFilter);
  if (statusFilter !== 'all') params.set('status', statusFilter);
  if (storeFilter !== 'all') params.set('store', storeFilter);
  if (search) params.set('search', search);
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  params.set('limit', String(pageSize));
  params.set('offset', String(page * pageSize));

  const { data: logsData, isLoading, refetch } = useQuery({
    queryKey: ['shopify-logs', params.toString()],
    queryFn: async () => {
      const res = await fetch(`/api/shopify/logs?${params}`);
      if (!res.ok) throw new Error('Failed to load logs');
      return res.json();
    },
    refetchInterval: 10000,
  });

  const { data: statsData } = useQuery({
    queryKey: ['shopify-log-stats'],
    queryFn: async () => {
      const res = await fetch('/api/shopify/logs/stats');
      if (!res.ok) return {};
      return res.json();
    },
    refetchInterval: 15000,
  });

  const logs: any[] = logsData?.logs ?? [];
  const total: number = logsData?.total ?? 0;
  const totalPages = Math.ceil(total / pageSize);
  const stats = statsData ?? {};

  const handleExport = () => {
    const exportParams = new URLSearchParams(params);
    exportParams.delete('limit'); exportParams.delete('offset');
    window.open(`/api/shopify/logs/export?${exportParams}`, '_blank');
  };

  const resetFilters = () => {
    setScraperFilter('all'); setActionFilter('all'); setStatusFilter('all');
    setStoreFilter('all'); setSearch(''); setDateFrom(''); setDateTo(''); setPage(0);
  };

  const selectCls = "px-4 py-2.5 bg-white/5 border border-white/10 rounded-xl focus:border-primary/50 text-sm text-slate-300 outline-none transition-all font-mono [color-scheme:dark]";

  const statusColor = (s: string) => ({
    success: 'text-emerald-400 border-emerald-500/20 bg-emerald-500/5',
    failed:  'text-rose-400 border-rose-500/20 bg-rose-500/5',
    partial: 'text-amber-400 border-amber-500/20 bg-amber-500/5',
    skipped: 'text-slate-500 border-white/5 bg-white/5',
  }[s] ?? 'text-slate-500 border-white/5');

  const actionColor = (a: string) => ({
    upload: 'text-emerald-400', update: 'text-blue-400',
    delete_oos: 'text-rose-400', check_oos: 'text-amber-400',
    sync_all: 'text-primary', upload_all: 'text-emerald-400',
    update_all: 'text-blue-400', delete_oos_all: 'text-rose-400',
  }[a] ?? 'text-slate-500');

  return (
    <motion.div
      key="logs"
      initial={{ opacity: 0, x: 20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -20 }}
      transition={{ duration: 0.2 }}
      className="space-y-6 font-mono"
    >
      {/* Header */}
      <div className="flex items-center gap-4">
        <button onClick={onBack} className="p-2.5 border border-[#333] hover:border-[#555] text-[#666] hover:text-white transition-colors">
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div className="flex-1">
          <h2 className="text-2xl font-black text-white uppercase tracking-widest flex items-center gap-3">
            <History className="w-6 h-6 text-[#ff4d00]" /> Activity History
          </h2>
          <p className="text-[#555] text-[11px] mt-0.5 uppercase tracking-widest">Immutable audit trail — every Shopify action logged</p>
        </div>
        <button
          onClick={handleExport}
          className="flex items-center gap-2 px-4 py-2.5 border border-[#333] text-[#666] hover:border-[#555] hover:text-white text-[10px] font-black uppercase tracking-widest transition-colors active:scale-95"
        >
          <FileDown className="w-3.5 h-3.5" /> Export CSV
        </button>
        <button
          onClick={() => refetch()}
          className="p-2.5 border border-[#333] text-[#666] hover:border-[#555] hover:text-white transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Stats Strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 border border-white/10 rounded-2xl overflow-hidden glass-panel">
        {[
          { label: 'Total Uploaded', value: (stats.total_uploaded ?? 0).toLocaleString(), icon: Upload,    accent: 'bg-emerald-500' },
          { label: 'Total Updated',  value: (stats.total_updated  ?? 0).toLocaleString(), icon: RefreshCw, accent: 'bg-blue-500' },
          { label: 'Total Deleted',  value: (stats.total_deleted  ?? 0).toLocaleString(), icon: Trash2,    accent: 'bg-rose-500' },
          { label: 'Total Ops',      value: (stats.total_ops      ?? 0).toLocaleString(), icon: BarChart3, accent: 'bg-primary' },
        ].map(({ label, value, icon: Icon, accent }, i) => (
          <div key={label} className={cn('p-6 flex items-center gap-5', i < 3 ? 'border-r border-white/5' : '')}>
            <div className={cn('w-1 h-12 rounded-full', accent)} />
            <div>
              <p className="text-3xl font-black text-white tabular-nums tracking-tight">{value}</p>
              <p className="text-[10px] text-slate-500 font-bold uppercase tracking-[0.2em] mt-1">{label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="glass-panel border border-white/10 p-6 rounded-3xl space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 text-slate-400">
            <Filter className="w-4 h-4 text-primary" />
            <span className="text-[11px] font-black uppercase tracking-widest">Filter System</span>
          </div>
          <button onClick={resetFilters} className="text-[10px] text-slate-500 hover:text-primary transition-colors font-black uppercase tracking-widest">
            Reset All
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <div className="lg:col-span-2 relative group">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500 group-focus-within:text-primary transition-colors" />
            <input
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(0); }}
              placeholder="Search notes or scraper…"
              className="w-full pl-11 pr-4 py-3 bg-white/5 border border-white/10 focus:border-primary/50 rounded-xl text-sm text-white placeholder:text-slate-600 outline-none transition-all font-mono"
            />
          </div>
          <select value={scraperFilter} onChange={e => { setScraperFilter(e.target.value); setPage(0); }} className={selectCls}>
            <option value="all">All Scrapers</option>
            <option value="__global__">Global</option>
            {Object.entries(SCRAPER_NAMES).filter(([k]) => k !== '__global__').map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select value={actionFilter} onChange={e => { setActionFilter(e.target.value); setPage(0); }} className={selectCls}>
            <option value="all">All Actions</option>
            <option value="upload">Upload</option>
            <option value="update">Update</option>
            <option value="delete_oos">Delete OOS</option>
            <option value="check_oos">Check OOS</option>
            <option value="upload_all">Upload All</option>
            <option value="update_all">Update All</option>
            <option value="delete_oos_all">Delete OOS All</option>
            <option value="sync_all">Full Sync</option>
          </select>
          <select value={statusFilter} onChange={e => { setStatusFilter(e.target.value); setPage(0); }} className={selectCls}>
            <option value="all">All Statuses</option>
            <option value="success">Success</option>
            <option value="partial">Partial</option>
            <option value="failed">Failed</option>
            <option value="skipped">Skipped</option>
          </select>
          <select value={storeFilter} onChange={e => { setStoreFilter(e.target.value); setPage(0); }} className={selectCls}>
            <option value="all">All Stores</option>
            <option value="test">TEST Store</option>
            <option value="main">MAIN Store</option>
          </select>
          <div className="relative">
            <Calendar className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500 pointer-events-none" />
            <input type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); setPage(0); }}
              className={cn(selectCls, 'w-full pl-11')} />
          </div>
        </div>

        {(dateTo || dateFrom) && (
          <div className="flex items-center gap-3 pt-2">
            <span className="text-[10px] text-slate-500 font-black uppercase tracking-widest">Date Range End:</span>
            <input type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); setPage(0); }}
              className={cn(selectCls, 'px-4 py-2')} />
          </div>
        )}
      </div>


      {/* Logs Table */}
      <div className="glass-panel rounded-3xl overflow-hidden shadow-xl">
        <div className="px-6 py-4 border-b border-white/5 flex items-center justify-between bg-white/[0.02]">
          <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">
            {total.toLocaleString()} {total === 1 ? 'ENTRY' : 'ENTRIES'}
          </span>
          <span className="text-[10px] text-slate-500 font-mono">
            Page {page + 1} / {Math.max(totalPages, 1)}
          </span>
        </div>

        {isLoading ? (
          <div className="flex flex-col items-center justify-center py-24 text-slate-500">
            <RefreshCw className="w-8 h-8 animate-spin mb-4 text-primary opacity-50" />
            <span className="text-[11px] uppercase tracking-widest font-black">Syncing Audit Logs…</span>
          </div>
        ) : logs.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-slate-500 bg-white/[0.01]">
            <div className="w-16 h-16 rounded-2xl bg-white/5 flex items-center justify-center mb-6">
              <History className="w-8 h-8 opacity-20" />
            </div>
            <p className="font-black text-sm text-white uppercase tracking-widest">No Activity Logs Yet</p>
            <p className="text-[10px] mt-2 text-slate-500 uppercase tracking-widest">Run a Shopify operation to start recording</p>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin scrollbar-thumb-white/10">
            <table className="w-full text-left font-mono">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.03]">
                  {['Time', 'Scraper', 'Store', 'Action', 'Status', 'Created', 'Updated', 'Deleted', 'Skipped', 'Failed', 'Notes'].map(h => (
                    <th key={h} className="px-5 py-4 text-[10px] font-black text-slate-500 uppercase tracking-widest whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {logs.map((log, i) => (
                  <motion.tr
                    key={log.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: i * 0.01 }}
                    className="border-b border-white/[0.02] hover:bg-white/[0.02] transition-colors group/row"
                  >
                    <td className="px-5 py-4 text-[11px] text-slate-500 whitespace-nowrap group-hover/row:text-slate-300">
                      {log.created_at ? new Date(log.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}
                    </td>
                    <td className="px-5 py-4 text-[11px] font-black text-slate-400 whitespace-nowrap uppercase group-hover/row:text-primary">
                      {SCRAPER_NAMES[log.scraper_id] ?? log.scraper_id}
                    </td>
                    <td className="px-5 py-4 whitespace-nowrap">
                      {log.store === 'main'
                        ? <span className="px-2 py-0.5 rounded-md text-[9px] font-black uppercase tracking-widest bg-red-600/20 border border-red-500/30 text-red-400">MAIN</span>
                        : <span className="px-2 py-0.5 rounded-md text-[9px] font-black uppercase tracking-widest bg-emerald-600/20 border border-emerald-500/30 text-emerald-400">TEST</span>
                      }
                    </td>
                    <td className={cn('px-5 py-4 text-[11px] font-black whitespace-nowrap uppercase', actionColor(log.action_type))}>
                      {ACTION_LABELS[log.action_type] ?? log.action_type}
                    </td>
                    <td className="px-5 py-4">
                      <span className={cn('px-2.5 py-1 rounded-lg border text-[10px] font-black uppercase tracking-tight', statusColor(log.status))}>
                        {log.status}
                      </span>
                    </td>
                    <td className="px-5 py-4 text-[11px] text-emerald-400 font-bold tabular-nums">{log.products_created || '—'}</td>
                    <td className="px-5 py-4 text-[11px] text-blue-400 font-bold tabular-nums">{log.products_updated || '—'}</td>
                    <td className="px-5 py-4 text-[11px] text-rose-400 font-bold tabular-nums">{log.products_deleted || '—'}</td>
                    <td className="px-5 py-4 text-[11px] text-slate-500 tabular-nums">{log.products_skipped || '—'}</td>
                    <td className="px-5 py-4 text-[11px] text-red-500 font-bold tabular-nums">{log.products_failed || '—'}</td>
                    <td className="px-5 py-4 text-[11px] text-slate-400 max-w-[240px] truncate" title={log.notes ?? ''}>
                      {log.error_message
                        ? <span className="text-rose-500 flex items-center gap-1.5" title={log.error_message}><AlertCircle className="w-3.5 h-3.5" /> {log.error_message.slice(0, 80)}</span>
                        : log.notes ?? <span className="opacity-20">—</span>}
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>

        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-6 py-4 border-t border-white/5 bg-white/[0.01]">
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="flex items-center gap-2 px-4 py-2 rounded-xl border border-white/10 text-[10px] font-black text-slate-400 hover:border-white/20 hover:text-white uppercase tracking-widest disabled:opacity-20 disabled:cursor-not-allowed transition-all"
            >
              <ChevronLeft className="w-4 h-4" /> Prev
            </button>
            <div className="flex items-center gap-2">
              {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                const pg = totalPages <= 7 ? i : (page < 4 ? i : (page > totalPages - 4 ? totalPages - 7 + i : page - 3 + i));
                return (
                  <button key={pg} onClick={() => setPage(pg)}
                    className={cn(
                      'w-8 h-8 rounded-lg text-[10px] font-black uppercase transition-all border flex items-center justify-center',
                      pg === page
                        ? 'bg-primary border-primary text-white shadow-lg shadow-primary/20'
                        : 'bg-white/5 border-white/10 text-slate-500 hover:border-primary/50 hover:text-primary'
                    )}>
                    {pg + 1}
                  </button>
                );
              })}
            </div>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="flex items-center gap-2 px-4 py-2 rounded-xl border border-white/10 text-[10px] font-black text-slate-400 hover:border-white/20 hover:text-white uppercase tracking-widest disabled:opacity-20 disabled:cursor-not-allowed transition-all"
            >
              Next <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

// ──────────────────────────────────────────────
// Quality Gate Panel
// ──────────────────────────────────────────────
const QC_CATS = ['images', 'variants', 'description', 'tags', 'sizes', 'pricing', 'category'] as const;
const QC_CAT_ICONS: Record<string, string> = {
  images: '🖼', variants: '🔀', description: '📝', tags: '🏷', sizes: '📐', pricing: '₹', category: '📦',
};

function ScraperQualityRow({ scraper, isExpanded, onExpand }: {
  scraper: WebsiteStats & { quality: any };
  isExpanded: boolean;
  onExpand: () => void;
}) {
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const q = scraper.quality;

  // Use in-memory products if available (live data); fall back to on-demand /api/validate
  const inlineProducts: any[] | null = Array.isArray(q?.products) ? q.products : null;
  const needsFetch = isExpanded && !inlineProducts && !detail && !loading;

  useEffect(() => {
    if (needsFetch) {
      setLoading(true);
      fetch(`/api/validate/${scraper.scraper_id}`)
        .then(r => r.json())
        .then(d => { setDetail(d); setLoading(false); })
        .catch(() => setLoading(false));
    }
  }, [needsFetch]);

  const score = q.pass_rate ?? 0;
  const scoreColor = score >= 95 ? 'text-emerald-400' : score >= 80 ? 'text-amber-400' : 'text-rose-400';
  const barColor = score >= 95 ? 'bg-emerald-500' : score >= 80 ? 'bg-amber-500' : 'bg-rose-500';
  const allProducts = inlineProducts ?? detail?.products ?? [];
  const issues = allProducts.filter((p: any) => p.severity !== 'ok');

  return (
    <div className="border border-white/5 rounded-2xl overflow-hidden">
      <button
        onClick={onExpand}
        className="w-full flex items-center gap-4 px-5 py-4 hover:bg-white/[0.03] transition-colors text-left"
      >
        <div className="flex-1 min-w-0">
          <p className="text-[11px] font-black text-white uppercase tracking-widest">{scraper.name}</p>
          <p className="text-[9px] text-slate-500 font-mono mt-0.5">
            {q.total ?? 0} products · {q.errors ?? 0} errors · {q.warnings ?? 0} warnings
          </p>
        </div>
        <div className="flex items-center gap-4 shrink-0">
          <div className="w-24">
            <div className="h-1 bg-white/5 rounded-full overflow-hidden">
              <div className={cn('h-full rounded-full transition-all', barColor)} style={{ width: `${score}%` }} />
            </div>
          </div>
          <span className={cn('text-sm font-black tabular-nums', scoreColor)}>{score}%</span>
          <ChevronRight className={cn('w-4 h-4 text-slate-500 transition-transform duration-200', isExpanded && 'rotate-90')} />
        </div>
      </button>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden border-t border-white/5"
          >
            {loading && !inlineProducts ? (
              <div className="flex items-center justify-center py-8 gap-2">
                <RefreshCw className="w-4 h-4 animate-spin text-slate-500" />
                <span className="text-[10px] text-slate-500 uppercase tracking-widest">Loading product details…</span>
              </div>
            ) : issues.length === 0 ? (
              <div className="px-5 py-6 text-center">
                <CheckCircle2 className="w-6 h-6 text-emerald-400 mx-auto mb-2" />
                <p className="text-[10px] text-slate-400 font-bold uppercase tracking-widest">All products passed or have warnings only</p>
              </div>
            ) : (
              <div className="divide-y divide-white/[0.03] max-h-[320px] overflow-y-auto">
                {issues.map((p: any, i: number) => (
                  <div key={i} className="px-5 py-3 space-y-1">
                    <div className="flex items-start gap-3">
                      <span className={cn(
                        'shrink-0 mt-0.5 text-[8px] font-black px-1.5 py-0.5 rounded uppercase tracking-wider',
                        p.severity === 'error' ? 'bg-rose-500/20 text-rose-400' : 'bg-amber-500/20 text-amber-400'
                      )}>
                        {p.severity}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className="text-[10px] font-bold text-white truncate">{p.title}</p>
                        <p className="text-[9px] text-slate-500 font-mono">SKU: {p.sku}</p>
                      </div>
                    </div>
                    {(p.issues?.length ?? 0) > 0 && (
                      <ul className="space-y-0.5 pl-8">
                        {p.issues.map((err: string, ei: number) => (
                          <li key={ei} className="text-[9px] text-rose-400 font-mono leading-snug">• {err}</li>
                        ))}
                      </ul>
                    )}
                    {(p.warnings?.length ?? 0) > 0 && (
                      <ul className="space-y-0.5 pl-8">
                        {p.warnings.slice(0, 2).map((w: string, wi: number) => (
                          <li key={wi} className="text-[9px] text-amber-400/70 font-mono leading-snug">• {w}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function QualityGatePanel({ qualityData, scrapers, onClose }: {
  qualityData: Record<string, any>;
  scrapers: WebsiteStats[];
  onClose: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const scrapersWithQuality = scrapers
    .map(s => ({ ...s, quality: qualityData[s.scraper_id] ?? s.quality ?? null }))
    .filter(s => s.quality);

  const totalProducts = scrapersWithQuality.reduce((a, s) => a + (s.quality?.total ?? 0), 0);
  const totalOk       = scrapersWithQuality.reduce((a, s) => a + (s.quality?.ok ?? 0), 0);
  const totalWarnings = scrapersWithQuality.reduce((a, s) => a + (s.quality?.warnings ?? 0), 0);
  const totalErrors   = scrapersWithQuality.reduce((a, s) => a + (s.quality?.errors ?? 0), 0);
  const overallScore  = totalProducts > 0 ? Math.round((totalOk + totalWarnings) / totalProducts * 100) : 0;

  const catTotals: Record<string, { ok: number; warnings: number; errors: number }> = {};
  for (const cat of QC_CATS) {
    catTotals[cat] = { ok: 0, warnings: 0, errors: 0 };
    for (const s of scrapersWithQuality) {
      const pc = s.quality?.per_category_summary?.[cat];
      if (pc) {
        catTotals[cat].ok       += pc.ok       ?? 0;
        catTotals[cat].warnings += pc.warnings ?? 0;
        catTotals[cat].errors   += pc.errors   ?? 0;
      }
    }
  }

  const handleCopyErrors = () => {
    const lines: string[] = [];
    for (const s of scrapersWithQuality) {
      // Prefer full products list (live data); fall back to condensed failed_products (DB historical)
      const products: any[] = s.quality?.products ?? [];
      const failed = products.length > 0
        ? products.filter((p: any) => p.severity === 'error')
        : (s.quality?.failed_products ?? []);
      if (!failed.length) continue;
      for (const p of failed) {
        lines.push(`Product: ${p.title}`);
        lines.push(`SKU: ${p.sku}`);
        lines.push('Errors:');
        for (const e of (p.issues ?? [])) lines.push(`  • ${e}`);
        lines.push('---');
      }
    }
    navigator.clipboard.writeText(lines.join('\n') || 'No errors found.').then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    });
  };

  return (
    <motion.div
      key="quality"
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -16 }}
      transition={{ duration: 0.25 }}
      className="space-y-8"
    >
      {/* Header */}
      <div className="flex items-center gap-6 border-b border-white/10 pb-8">
        <button
          onClick={onClose}
          className="w-12 h-12 flex items-center justify-center rounded-2xl border border-white/10 text-slate-400 hover:border-white/30 hover:text-white hover:bg-white/5 transition-all active:scale-90 shadow-lg"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1">
          <h2 className="text-3xl font-black text-white uppercase tracking-tight flex items-center gap-3">
            <ShieldCheck className="w-7 h-7 text-primary" />
            Quality Gate
          </h2>
          <p className="text-slate-500 text-[11px] mt-1 uppercase tracking-widest font-bold">
            Pre-upload product validation · {scrapersWithQuality.length} scraper{scrapersWithQuality.length !== 1 ? 's' : ''} with data
          </p>
        </div>
        <button
          onClick={handleCopyErrors}
          disabled={totalErrors === 0}
          className={cn(
            'flex items-center gap-2 px-5 py-2.5 rounded-xl text-[10px] font-black uppercase tracking-widest transition-all border',
            totalErrors === 0
              ? 'border-white/5 text-slate-600 cursor-not-allowed'
              : copied
                ? 'border-emerald-500/30 text-emerald-400 bg-emerald-500/10'
                : 'border-rose-500/30 text-rose-400 hover:bg-rose-500/10 active:scale-95'
          )}
        >
          {copied
            ? <><CheckCheck className="w-3.5 h-3.5" /> Copied!</>
            : <><FileDown className="w-3.5 h-3.5" /> Copy All Errors ({totalErrors})</>
          }
        </button>
      </div>

      {scrapersWithQuality.length === 0 ? (
        <div className="glass-panel rounded-3xl p-16 text-center">
          <ShieldCheck className="w-12 h-12 text-slate-700 mx-auto mb-6" />
          <p className="text-lg font-black text-white uppercase tracking-widest mb-3">No Quality Data Yet</p>
          <p className="text-slate-500 text-sm max-w-sm mx-auto leading-relaxed">
            Run scrapers to generate quality reports. Results appear automatically after each completed scrape.
          </p>
        </div>
      ) : (
        <>
          {/* Summary grid */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            {[
              { label: 'Total', value: totalProducts.toLocaleString(), color: 'text-white' },
              { label: 'Passed', value: totalOk.toLocaleString(), color: 'text-emerald-400' },
              { label: 'Warnings', value: totalWarnings.toLocaleString(), color: 'text-amber-400' },
              { label: 'Errors', value: totalErrors.toLocaleString(), color: 'text-rose-400' },
              {
                label: 'QG Score',
                value: `${overallScore}%`,
                color: overallScore >= 95 ? 'text-emerald-400' : overallScore >= 80 ? 'text-amber-400' : 'text-rose-400',
              },
            ].map(({ label, value, color }) => (
              <div key={label} className="glass-card rounded-2xl p-5 text-center">
                <p className={cn('text-3xl font-black tabular-nums', color)}>{value}</p>
                <p className="text-[9px] text-slate-600 font-bold uppercase tracking-[0.2em] mt-2">{label}</p>
              </div>
            ))}
          </div>

          {/* Category breakdown */}
          <div className="glass-panel rounded-3xl p-6 space-y-4">
            <h3 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em]">Breakdown by Category</h3>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {QC_CATS.map(cat => {
                const ct = catTotals[cat];
                const total = ct.ok + ct.warnings + ct.errors;
                const passRate = total > 0 ? Math.round((ct.ok + ct.warnings) / total * 100) : 100;
                const barCol = ct.errors > 0 ? 'bg-rose-500' : ct.warnings > 0 ? 'bg-amber-500' : 'bg-emerald-500';
                return (
                  <div key={cat} className="bg-white/[0.03] rounded-xl p-4 space-y-2.5 border border-white/5">
                    <div className="flex items-center gap-2">
                      <span className="text-base">{QC_CAT_ICONS[cat]}</span>
                      <p className="text-[10px] font-black text-white uppercase tracking-wider">{cat}</p>
                    </div>
                    <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                      <div className={cn('h-full rounded-full transition-all', barCol)} style={{ width: `${passRate}%` }} />
                    </div>
                    <div className="flex items-center justify-between text-[9px] font-mono">
                      <span className="text-emerald-400">{ct.ok} ok</span>
                      <span className="text-slate-600">{passRate}%</span>
                      {ct.errors > 0 && <span className="text-rose-400">{ct.errors} err</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Per-scraper expandable rows */}
          <div className="space-y-4">
            <h3 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em]">Per-Scraper Detail</h3>
            <div className="glass-panel rounded-3xl overflow-hidden divide-y divide-white/[0.03]">
              {scrapersWithQuality.map(s => (
                <ScraperQualityRow
                  key={s.scraper_id}
                  scraper={s as WebsiteStats & { quality: any }}
                  isExpanded={expanded === s.scraper_id}
                  onExpand={() => setExpanded(prev => prev === s.scraper_id ? null : s.scraper_id)}
                />
              ))}
            </div>
          </div>
        </>
      )}
    </motion.div>
  );
}

// ──────────────────────────────────────────────
// QA Review Page
// ──────────────────────────────────────────────
function QAReviewPage({ onBack, initialData, shopifyDomain }: { onBack: () => void; initialData?: any; shopifyDomain?: string }) {
  const [filterScraper, setFilterScraper] = useState<string>('all');
  // expandMode: 'report' shows quality report, 'errors' shows error list + qa events
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandMode, setExpandMode] = useState<'report' | 'errors'>('errors');
  const [reworkTarget, setReworkTarget] = useState<any | null>(null);
  const [reworkReason, setReworkReason] = useState('');
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['qa'],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/qa`);
      if (!res.ok) throw new Error('Failed to load QA data');
      return res.json();
    },
    initialData,
    refetchInterval: 20000,
    retry: false,
  });

  const products: any[] = data?.products ?? [];
  const pending: number = data?.pending ?? 0;
  const approved: number = data?.approved ?? 0;
  const rework: number = data?.rework ?? 0;

  const scraperIds = [...new Set(products.map((p: any) => p.scraper_id))].sort() as string[];

  const byStatus = (qs: string) => products.filter((p: any) => {
    const match = p.qa_status === qs;
    const scraperMatch = filterScraper === 'all' || p.scraper_id === filterScraper;
    return match && scraperMatch;
  });

  const pendingList   = byStatus('QA_PENDING_REVIEW');
  const approvedList  = byStatus('APPROVED');
  const reworkList    = byStatus('REWORK_REQUIRED');

  const handleApprove = async (p: any) => {
    setActionLoading(`approve-${p.shopify_product_id}`);
    try {
      const res = await fetch(`${API_BASE}/api/qa/${p.scraper_id}/${p.shopify_product_id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: '' }),
      });
      if (res.ok) {
        toast('success', 'Approved', `${p.title || p.sku} marked as APPROVED`);
        queryClient.invalidateQueries({ queryKey: ['qa'] });
      } else {
        toast('error', 'Failed', 'Could not approve product');
      }
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
    finally { setActionLoading(null); }
  };

  const handleReworkConfirm = async () => {
    if (!reworkTarget) return;
    const p = reworkTarget;
    setActionLoading(`rework-${p.shopify_product_id}`);
    try {
      const res = await fetch(`${API_BASE}/api/qa/${p.scraper_id}/${p.shopify_product_id}/rework`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reworkReason }),
      });
      if (res.ok) {
        toast('warning', 'Rework Required', `${p.title || p.sku} flagged for rework`);
        queryClient.invalidateQueries({ queryKey: ['qa'] });
        setReworkTarget(null); setReworkReason('');
      } else { toast('error', 'Failed', 'Could not flag product'); }
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
    finally { setActionLoading(null); }
  };

  const handleCopyErrors = (p: any) => {
    const errors: string[] = p.quality_error_list ?? [];
    const text = errors.length > 0
      ? `Quality Gate Errors — ${p.scraper_id} / ${p.title || p.sku}\n\n${errors.map((e, i) => `${i + 1}. ${e}`).join('\n')}`
      : `No quality gate errors for: ${p.title || p.sku} (${p.scraper_id})`;
    navigator.clipboard.writeText(text);
    toast('info', 'Copied', errors.length > 0 ? `${errors.length} error(s) copied` : 'No errors to copy');
  };

  const handleReRun = async (p: any) => {
    setActionLoading(`rerun-${p.shopify_product_id}`);
    try {
      const res = await fetch(`${API_BASE}/api/scrape`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scraper_id: p.scraper_id }),
      });
      if (res.ok) toast('success', 'Scraper Started', `${p.scraper_id} re-run triggered`);
      else toast('error', 'Failed', `Could not start ${p.scraper_id}`);
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
    finally { setActionLoading(null); }
  };

  const handleFixProductImages = async (p: any) => {
    setActionLoading(`fiximg-${p.shopify_product_id}`);
    try {
      const res = await fetch(
        `${API_BASE}/api/shopify/fix-product-images/${p.scraper_id}/${p.shopify_product_id}`,
        { method: 'POST' }
      );
      const data = await res.json();
      if (res.ok && data.ok) {
        toast('success', 'Images Fixed', `${data.reimaged ? 'Re-linked' : 'No colour images'} for "${p.title || p.sku}". ${data.image_link_failures > 0 ? data.image_link_failures + ' link failure(s).' : ''}`);
      } else {
        toast('error', 'Fix Images Failed', data.error || 'Could not re-link images.');
      }
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
    finally { setActionLoading(null); }
  };

  const [recheckLoading, setRecheckLoading] = useState<string | null>(null);

  const handleRecheckQuality = async (scraperId: string) => {
    const label = scraperId === 'all' ? 'all scrapers' : scraperId;
    setRecheckLoading(scraperId);
    try {
      const url = scraperId === 'all'
        ? `${API_BASE}/api/qa/recheck-all`
        : `${API_BASE}/api/qa/recheck/${scraperId}`;
      const res = await fetch(url, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        const errs = scraperId === 'all'
          ? data.total_errors
          : data.errors;
        if (errs === 0) {
          toast('success', 'Quality Gate Clear ✓', `${label} — 0 errors. QA list updated.`);
        } else {
          toast('warning', `${errs} error(s) found`, `${label} quality report refreshed in QA view.`);
        }
        queryClient.invalidateQueries({ queryKey: ['qa'] });
      } else {
        toast('error', 'Recheck Failed', data.error || 'Could not re-run quality gate.');
      }
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
    finally { setRecheckLoading(null); }
  };

  const handleQAImgQC = async (p: any) => {
    toast('info', 'Image QC Running', `Auditing Shopify variant image links for ${p.scraper_id}…`);
    try {
      const res = await fetch(`${API_BASE}/api/shopify/check-images/${p.scraper_id}`);
      if (res.ok) {
        const data = await res.json();
        const { total, issues_count, no_images_count, missing_variant_img_count, pass_rate } = data;
        if (issues_count === 0) {
          toast('success', 'Image QC Passed ✓', `All ${total} ${p.scraper_id} products OK (${pass_rate}% pass rate).`);
        } else {
          toast('warning', `Image QC: ${issues_count} issue(s)`, `${total} total — ${no_images_count} no images, ${missing_variant_img_count} missing variant links. Use Fix Images on the card.`);
        }
      } else {
        const err = await res.json();
        toast('error', 'Image QC Failed', err.error || 'Could not run image audit.');
      }
    } catch { toast('error', 'Network Error', 'Backend unreachable'); }
  };

  const toggleExpand = (pid: string, mode: 'report' | 'errors') => {
    if (expandedId === pid && expandMode === mode) {
      setExpandedId(null);
    } else {
      setExpandedId(pid);
      setExpandMode(mode);
    }
  };

  // Scraper color avatars — deterministic from first char
  const scraperColors = ['bg-primary', 'bg-secondary', 'bg-amber-500', 'bg-rose-500', 'bg-violet-500', 'bg-cyan-500', 'bg-emerald-500', 'bg-orange-500', 'bg-pink-500', 'bg-teal-500'];
  const scraperColor = (sid: string) => scraperColors[(sid.charCodeAt(0) ?? 0) % scraperColors.length];

  const qsBadgeClass = (qs: string) => {
    if (qs === 'APPROVED') return 'text-emerald-400 bg-emerald-400/10 border border-emerald-400/20';
    if (qs === 'REWORK_REQUIRED') return 'text-rose-400 bg-rose-400/10 border border-rose-400/20';
    return 'text-amber-400 bg-amber-400/10 border border-amber-400/20';
  };
  const qsIcon = (qs: string) => {
    if (qs === 'APPROVED') return <ClipboardCheck className="w-3 h-3" />;
    if (qs === 'REWORK_REQUIRED') return <ClipboardX className="w-3 h-3" />;
    return <ClipboardList className="w-3 h-3" />;
  };

  // Shared product row renderer
  const ProductRow = ({ p }: { p: any }) => {
    const isExpanded = expandedId === p.shopify_product_id;
    const isActing = actionLoading?.includes(p.shopify_product_id);
    const score = p.quality_score != null ? Math.round(p.quality_score) : null;
    const scoreColor = score == null ? 'text-slate-700'
      : score >= 90 ? 'text-emerald-400'
      : score >= 70 ? 'text-amber-400'
      : 'text-rose-400';

    return (
      <div>
        <div className="grid grid-cols-[auto_2.5fr_1fr_1fr_1fr_auto] gap-3 items-center px-5 py-3.5 hover:bg-white/[0.025] transition-colors group">
          {/* Thumbnail — scraper letter avatar */}
          <div className={cn('w-8 h-8 rounded-xl flex items-center justify-center text-[11px] font-black text-white shrink-0', scraperColor(p.scraper_id))}>
            {(p.scraper_id ?? '?')[0].toUpperCase()}
          </div>
          {/* Title */}
          <div className="min-w-0">
            <p className="text-[12px] font-bold text-white truncate leading-snug" title={p.title}>{p.title || '—'}</p>
            <p className="text-[9px] text-slate-600 font-mono mt-0.5 truncate">{p.shopify_product_id} · {p.sku || 'no-sku'}</p>
          </div>
          {/* Scraper */}
          <div className="text-[10px] font-black text-slate-500 uppercase tracking-widest truncate">{p.scraper_id}</div>
          {/* Quality Gate */}
          <div>
            {score != null ? (
              <div className="flex flex-col gap-0.5">
                <span className={cn('text-[13px] font-black leading-none', scoreColor)}>{score}%</span>
                {(p.quality_errors ?? 0) > 0 && <span className="text-[9px] text-rose-400 font-bold">{p.quality_errors}e</span>}
                {(p.quality_errors ?? 0) === 0 && (p.quality_warnings ?? 0) > 0 && <span className="text-[9px] text-amber-400 font-bold">{p.quality_warnings}w</span>}
              </div>
            ) : (
              <span className="text-[9px] text-slate-700 font-black">—</span>
            )}
          </div>
          {/* QA Status */}
          <div>
            <span className={cn('inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-[9px] font-black uppercase tracking-widest', qsBadgeClass(p.qa_status))}>
              {qsIcon(p.qa_status)}
              {p.qa_status === 'APPROVED' ? 'OK' : p.qa_status === 'REWORK_REQUIRED' ? 'Fix' : 'Pending'}
            </span>
          </div>
          {/* Actions — always visible */}
          <div className="flex items-center gap-1 flex-wrap">
            {/* QA: Approve */}
            {p.qa_status !== 'APPROVED' && (
              <button onClick={() => handleApprove(p)} disabled={!!isActing} title="Mark Approved"
                className="w-6 h-6 flex items-center justify-center rounded-md bg-emerald-400/10 border border-emerald-400/20 text-emerald-400 hover:bg-emerald-400/20 transition-colors active:scale-90 disabled:opacity-40">
                {isActing && actionLoading === `approve-${p.shopify_product_id}` ? <RotateCw className="w-2.5 h-2.5 animate-spin" /> : <ClipboardCheck className="w-2.5 h-2.5" />}
              </button>
            )}
            {/* QA: Rework */}
            {p.qa_status !== 'REWORK_REQUIRED' && (
              <button onClick={() => { setReworkTarget(p); setReworkReason(''); }} disabled={!!isActing} title="Flag for Rework"
                className="w-6 h-6 flex items-center justify-center rounded-md bg-rose-400/10 border border-rose-400/20 text-rose-400 hover:bg-rose-400/20 transition-colors active:scale-90 disabled:opacity-40">
                <ClipboardX className="w-2.5 h-2.5" />
              </button>
            )}
            {/* Fix Images — per product */}
            <button onClick={() => handleFixProductImages(p)} disabled={!!isActing} title="Re-link variant images for this product"
              className="w-6 h-6 flex items-center justify-center rounded-md bg-violet-500/10 border border-violet-500/20 text-violet-400 hover:bg-violet-500/20 transition-colors active:scale-90 disabled:opacity-40">
              {isActing && actionLoading === `fiximg-${p.shopify_product_id}` ? <RotateCw className="w-2.5 h-2.5 animate-spin" /> : <ImageIcon className="w-2.5 h-2.5" />}
            </button>
            {/* Img QC — scraper-level audit */}
            <button onClick={() => handleQAImgQC(p)} title="Audit image links for this scraper"
              className="w-6 h-6 flex items-center justify-center rounded-md bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 hover:bg-cyan-500/20 transition-colors active:scale-90">
              <ScanLine className="w-2.5 h-2.5" />
            </button>
            {/* View on Shopify */}
            {shopifyDomain && (
              <a href={`https://${shopifyDomain}/admin/products/${p.shopify_product_id}`} target="_blank" rel="noopener noreferrer"
                title="View in Shopify Admin"
                className="w-6 h-6 flex items-center justify-center rounded-md bg-white/5 border border-white/10 text-slate-400 hover:border-emerald-400/30 hover:text-emerald-400 transition-colors active:scale-90">
                <Globe className="w-2.5 h-2.5" />
              </a>
            )}
            {/* Quality Report */}
            <button onClick={() => toggleExpand(p.shopify_product_id, 'report')} title="View Quality Report"
              className={cn('w-6 h-6 flex items-center justify-center rounded-md border transition-colors active:scale-90',
                isExpanded && expandMode === 'report' ? 'bg-primary/10 border-primary/30 text-primary' : 'bg-white/5 border-white/10 text-slate-400 hover:border-primary/30 hover:text-primary')}>
              <ShieldCheck className="w-2.5 h-2.5" />
            </button>
            {/* Error Report */}
            <button onClick={() => toggleExpand(p.shopify_product_id, 'errors')} title="View Error Report"
              className={cn('w-6 h-6 flex items-center justify-center rounded-md border transition-colors active:scale-90',
                isExpanded && expandMode === 'errors' ? 'bg-rose-400/10 border-rose-400/20 text-rose-400' : 'bg-white/5 border-white/10 text-slate-400 hover:border-rose-400/20 hover:text-rose-400')}>
              <Eye className="w-2.5 h-2.5" />
            </button>
            {/* Copy Errors */}
            <button onClick={() => handleCopyErrors(p)} title="Copy All Errors"
              className="w-6 h-6 flex items-center justify-center rounded-md bg-white/5 border border-white/10 text-slate-400 hover:border-white/20 hover:text-white transition-colors active:scale-90">
              <CheckCheck className="w-2.5 h-2.5" />
            </button>
            {/* Re-run scraper */}
            <button onClick={() => handleReRun(p)} disabled={!!isActing} title="Re-run Product Scraper"
              className="w-6 h-6 flex items-center justify-center rounded-md bg-white/5 border border-white/10 text-slate-400 hover:border-amber-400/30 hover:text-amber-400 transition-colors active:scale-90 disabled:opacity-40">
              {isActing && actionLoading === `rerun-${p.shopify_product_id}` ? <RotateCw className="w-2.5 h-2.5 animate-spin" /> : <RotateCw className="w-2.5 h-2.5" />}
            </button>
          </div>
        </div>

        {/* Inline expansion: quality report OR error report + QA events */}
        <AnimatePresence>
          {isExpanded && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.18 }} className="overflow-hidden">
              <div className="px-5 pb-5 pt-2 bg-white/[0.015] border-t border-white/5">
                {expandMode === 'report' ? (
                  /* Quality Report panel */
                  <div className="space-y-3">
                    <p className="text-[9px] font-black text-primary/70 uppercase tracking-[0.2em] flex items-center gap-2">
                      <ShieldCheck className="w-3 h-3" /> Quality Report — {p.scraper_id}
                    </p>
                    {p.quality_report_full ? (
                      <>
                        <div className="grid grid-cols-4 gap-3">
                          {[
                            { label: 'Pass Rate', value: p.quality_report_full.pass_rate != null ? `${Math.round(p.quality_report_full.pass_rate)}%` : '—', color: (p.quality_report_full.pass_rate ?? 0) >= 90 ? 'text-emerald-400' : 'text-amber-400' },
                            { label: 'Total', value: (p.quality_report_full.total ?? 0).toLocaleString(), color: 'text-white' },
                            { label: 'Errors', value: (p.quality_report_full.errors ?? 0).toLocaleString(), color: (p.quality_report_full.errors ?? 0) > 0 ? 'text-rose-400' : 'text-emerald-400' },
                            { label: 'Warnings', value: (p.quality_report_full.warnings ?? 0).toLocaleString(), color: (p.quality_report_full.warnings ?? 0) > 0 ? 'text-amber-400' : 'text-emerald-400' },
                          ].map(s => (
                            <div key={s.label} className="bg-white/5 rounded-xl p-3">
                              <p className={cn('text-[16px] font-black', s.color)}>{s.value}</p>
                              <p className="text-[8px] font-black text-slate-600 uppercase tracking-widest mt-0.5">{s.label}</p>
                            </div>
                          ))}
                        </div>
                        {Object.keys(p.quality_report_full.categories ?? {}).length > 0 && (
                          <div className="space-y-1">
                            <p className="text-[9px] font-black text-slate-600 uppercase tracking-[0.15em] mb-1.5">Category Breakdown</p>
                            {Object.entries(p.quality_report_full.categories).map(([cat, cd]: [string, any]) => (
                              <div key={cat} className="flex items-center gap-3 py-1 border-b border-white/[0.03]">
                                <span className="text-[10px] font-black text-slate-400 w-28 capitalize">{cat.replace(/_/g, ' ')}</span>
                                <div className="flex-1 h-1 bg-white/5 rounded-full overflow-hidden">
                                  <div className="h-full bg-primary/60 rounded-full" style={{ width: `${cd.pass_rate ?? 0}%` }} />
                                </div>
                                <span className="text-[9px] font-black text-slate-500 w-10 text-right">{cd.pass_rate != null ? `${Math.round(cd.pass_rate)}%` : '—'}</span>
                                {(cd.errors ?? 0) > 0 && <span className="text-[9px] text-rose-400 font-bold">{cd.errors}e</span>}
                                {(cd.warnings ?? 0) > 0 && <span className="text-[9px] text-amber-400 font-bold">{cd.warnings}w</span>}
                              </div>
                            ))}
                          </div>
                        )}
                        {p.quality_report_full.ready_to_upload === true && (
                          <div className="flex items-center gap-2 text-emerald-400 text-[10px] font-black">
                            <ClipboardCheck className="w-3.5 h-3.5" /> Ready to upload — no blocking errors
                          </div>
                        )}
                        {p.quality_report_full.ready_to_upload === false && (
                          <div className="flex items-center gap-2 text-rose-400 text-[10px] font-black">
                            <ClipboardX className="w-3.5 h-3.5" /> Upload blocked — critical errors present
                          </div>
                        )}
                      </>
                    ) : (
                      <p className="text-slate-600 text-[11px] font-bold italic">No quality report available — run the scraper to generate one.</p>
                    )}
                  </div>
                ) : (
                  /* Error Report + QA Events panel */
                  <div className="space-y-4">
                    {(p.quality_error_list?.length ?? 0) > 0 && (
                      <div>
                        <p className="text-[9px] font-black text-rose-400/70 uppercase tracking-[0.2em] mb-2 flex items-center gap-2">
                          <Eye className="w-3 h-3" /> Quality Gate Errors ({p.quality_error_list.length})
                        </p>
                        <div className="space-y-1">
                          {p.quality_error_list.map((err: string, i: number) => (
                            <div key={i} className="flex items-start gap-2 text-[11px]">
                              <span className="text-rose-500 font-black shrink-0">{i + 1}.</span>
                              <span className="text-slate-300 font-medium leading-relaxed">{err}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    <div>
                      <p className="text-[9px] font-black text-slate-600 uppercase tracking-[0.2em] mb-2">QA Event History</p>
                      {p.qa_events?.length > 0 ? (
                        <div className="space-y-1.5">
                          {p.qa_events.map((ev: any, i: number) => (
                            <div key={i} className="flex items-start gap-3 text-[11px]">
                              <span className={cn('px-2 py-0.5 rounded text-[9px] font-black uppercase tracking-widest border shrink-0',
                                ev.action === 'APPROVED' ? 'text-emerald-400 border-emerald-400/20 bg-emerald-400/5' :
                                ev.action === 'REWORK_REQUIRED' ? 'text-rose-400 border-rose-400/20 bg-rose-400/5' :
                                'text-slate-400 border-white/10 bg-white/5')}>
                                {ev.action === 'APPROVED' ? 'Approved' : ev.action === 'REWORK_REQUIRED' ? 'Rework' : ev.action}
                              </span>
                              <div className="flex-1 min-w-0">
                                {ev.reason && <p className="text-slate-300 font-medium leading-relaxed">{ev.reason}</p>}
                                <p className="text-slate-600 font-mono text-[9px] mt-0.5">{ev.created_at ? new Date(ev.created_at).toLocaleString() : '—'}</p>
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="text-slate-700 text-[11px] font-bold italic">No review events yet.</p>
                      )}
                    </div>
                    <div className="pt-2 border-t border-white/5 flex items-center gap-4 text-[9px] text-slate-700 font-mono flex-wrap">
                      <span>Uploaded: {p.uploaded_at ? new Date(p.uploaded_at).toLocaleDateString() : '—'}</span>
                      <span>Synced: {p.last_synced_at ? new Date(p.last_synced_at).toLocaleDateString() : '—'}</span>
                      <span>Handle: {p.handle || '—'}</span>
                    </div>
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  };

  // Grouped section component
  const GroupSection = ({
    title, items, statusKey, headerColor, emptyMsg, defaultOpen,
  }: {
    title: string; items: any[]; statusKey: string;
    headerColor: string; emptyMsg: string; defaultOpen: boolean;
  }) => {
    const [open, setOpen] = useState(defaultOpen);
    return (
      <div className="glass-panel rounded-3xl overflow-hidden border border-white/5">
        <button
          onClick={() => setOpen(o => !o)}
          className="w-full flex items-center justify-between px-6 py-4 hover:bg-white/[0.02] transition-colors"
        >
          <div className="flex items-center gap-3">
            <span className={cn('text-[11px] font-black uppercase tracking-widest', headerColor)}>{title}</span>
            <span className={cn('px-2 py-0.5 rounded-lg text-[10px] font-black border', headerColor.replace('text-', 'border-').replace('400', '400/20'), 'bg-white/5')}>
              {items.length}
            </span>
          </div>
          <ChevronRight className={cn('w-4 h-4 text-slate-600 transition-transform', open && 'rotate-90')} />
        </button>
        <AnimatePresence>
          {open && (
            <motion.div initial={{ height: 0 }} animate={{ height: 'auto' }} exit={{ height: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden">
              {items.length === 0 ? (
                <div className="px-6 pb-6 pt-1 text-center text-slate-700 text-[11px] font-bold italic border-t border-white/5">
                  {emptyMsg}
                </div>
              ) : (
                <div className="border-t border-white/5">
                  {/* Column headers */}
                  <div className="grid grid-cols-[auto_2.5fr_1fr_1fr_1fr_auto] gap-3 px-5 py-2.5 bg-white/[0.01]">
                    {['', 'Product', 'Scraper', 'Quality', 'Status', 'Actions'].map(h => (
                      <div key={h} className="text-[8px] font-black text-slate-700 uppercase tracking-[0.15em]">{h}</div>
                    ))}
                  </div>
                  <div className="divide-y divide-white/[0.03]">
                    {items.map((p: any) => <ProductRow key={p.shopify_product_id} p={p} />)}
                  </div>
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  };

  return (
    <motion.div key="qa" initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -16 }} transition={{ duration: 0.2 }} className="space-y-8">
      {/* Header */}
      <div className="flex items-center gap-6 border-b border-white/10 pb-8">
        <button onClick={onBack} className="w-12 h-12 flex items-center justify-center rounded-2xl border border-white/10 text-slate-400 hover:border-white/30 hover:text-white hover:bg-white/5 transition-all active:scale-90 shadow-lg">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1">
          <h2 className="text-3xl font-black text-white uppercase tracking-tight flex items-center gap-3">
            <ClipboardList className="w-7 h-7 text-amber-400" /> QA Review
          </h2>
          <p className="text-slate-500 text-[11px] mt-1 uppercase tracking-widest font-bold">Manual approval workflow for uploaded Shopify products</p>
        </div>
        <div className="flex items-center gap-3">
          {pending > 0 && (
            <div className="flex items-center gap-2 px-4 py-2 rounded-xl bg-amber-400/10 border border-amber-400/20">
              <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              <span className="text-amber-400 text-[11px] font-black uppercase tracking-widest">{pending} pending</span>
            </div>
          )}
          <button onClick={() => refetch()} className="w-10 h-10 flex items-center justify-center rounded-xl border border-white/10 text-slate-400 hover:border-white/30 hover:text-white transition-all active:scale-90">
            <RotateCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Stat Strip */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'Pending Review', count: pending, icon: ClipboardList, color: 'border-amber-400/30 text-amber-400', bg: 'bg-amber-400/5' },
          { label: 'Approved', count: approved, icon: ClipboardCheck, color: 'border-emerald-400/30 text-emerald-400', bg: 'bg-emerald-400/5' },
          { label: 'Rework Required', count: rework, icon: ClipboardX, color: 'border-rose-400/30 text-rose-400', bg: 'bg-rose-400/5' },
        ].map(({ label, count, icon: Icon, color, bg }) => (
          <div key={label} className={cn('glass-panel rounded-2xl p-6 border flex items-center gap-4', color, bg)}>
            <div className={cn('w-10 h-10 rounded-xl flex items-center justify-center border', bg, color)}>
              <Icon className="w-5 h-5" />
            </div>
            <div>
              <p className="text-2xl font-black text-white">{count.toLocaleString()}</p>
              <p className="text-[10px] font-black uppercase tracking-widest text-slate-500 mt-0.5">{label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Scraper filter + Recheck Quality */}
      {scraperIds.length > 1 && (
        <div className="flex items-center gap-3 flex-wrap">
          <select value={filterScraper} onChange={e => setFilterScraper(e.target.value)}
            className="px-4 py-2 rounded-xl bg-white/5 border border-white/10 text-slate-400 text-[10px] font-black uppercase tracking-widest outline-none hover:border-white/20 transition-colors cursor-pointer">
            <option value="all">All Scrapers</option>
            {scraperIds.map(sid => <option key={sid} value={sid}>{sid}</option>)}
          </select>
          {filterScraper !== 'all' && (
            <button onClick={() => setFilterScraper('all')} className="text-[10px] font-black text-slate-600 hover:text-slate-400 uppercase tracking-widest transition-colors">
              × Clear
            </button>
          )}
          {/* Recheck Quality Gate — re-runs validate_csv + persists result so QA errors refresh */}
          <button
            onClick={() => handleRecheckQuality(filterScraper)}
            disabled={!!recheckLoading}
            title={filterScraper === 'all' ? 'Re-run quality gate for all scrapers and refresh error list' : `Re-run quality gate for ${filterScraper}`}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary/10 border border-primary/20 text-primary text-[10px] font-black uppercase tracking-widest hover:bg-primary/20 transition-colors disabled:opacity-40 active:scale-95">
            {recheckLoading ? <RotateCw className="w-3 h-3 animate-spin" /> : <ShieldCheck className="w-3 h-3" />}
            {recheckLoading ? 'Rechecking…' : filterScraper === 'all' ? 'Recheck All' : `Recheck ${filterScraper}`}
          </button>
        </div>
      )}

      {/* Three grouped sections */}
      {isLoading ? (
        <div className="flex items-center justify-center py-24 text-slate-600">
          <RotateCw className="w-6 h-6 animate-spin mr-3" />
          <span className="text-[11px] font-black uppercase tracking-widest">Loading QA data…</span>
        </div>
      ) : (
        <div className="space-y-4">
          <GroupSection title="Pending Review" items={pendingList} statusKey="QA_PENDING_REVIEW"
            headerColor="text-amber-400" emptyMsg="All caught up — no pending reviews." defaultOpen={true} />
          <GroupSection title="Rework Required" items={reworkList} statusKey="REWORK_REQUIRED"
            headerColor="text-rose-400" emptyMsg="No products flagged for rework." defaultOpen={true} />
          <GroupSection title="Approved" items={approvedList} statusKey="APPROVED"
            headerColor="text-emerald-400" emptyMsg="No approved products yet." defaultOpen={false} />
        </div>
      )}

      {/* Rework dialog */}
      <AnimatePresence>
        {reworkTarget && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-8">
            <motion.div initial={{ scale: 0.95, opacity: 0, y: 20 }} animate={{ scale: 1, opacity: 1, y: 0 }} exit={{ scale: 0.95, opacity: 0 }} transition={{ duration: 0.2 }}
              className="glass-panel rounded-3xl p-8 w-full max-w-lg border border-rose-400/20 shadow-2xl shadow-rose-400/5">
              <div className="flex items-center gap-3 mb-6">
                <div className="w-10 h-10 rounded-xl bg-rose-400/10 border border-rose-400/20 flex items-center justify-center">
                  <ClipboardX className="w-5 h-5 text-rose-400" />
                </div>
                <div>
                  <h3 className="text-sm font-black text-white uppercase tracking-widest">Request Fix</h3>
                  <p className="text-[10px] text-slate-500 font-bold mt-0.5 uppercase tracking-widest">Describe what needs to be corrected</p>
                </div>
              </div>
              <p className="text-[11px] text-slate-400 mb-4 font-bold truncate">
                <span className="text-white">{reworkTarget.title || reworkTarget.sku}</span>
              </p>
              <textarea value={reworkReason} onChange={e => setReworkReason(e.target.value)}
                placeholder="e.g. Wrong pricing — should be ₹12,500 not ₹1,25,000. Missing product image. Tag mismatch: should be womens-bags."
                autoFocus rows={4}
                className="w-full px-5 py-4 rounded-2xl bg-white/5 border border-white/10 text-[11px] text-slate-300 placeholder:text-slate-700 font-medium outline-none focus:border-rose-400/30 transition-colors resize-none" />
              <div className="flex gap-3 mt-6">
                <button onClick={() => { setReworkTarget(null); setReworkReason(''); }}
                  className="flex-1 px-5 py-3 rounded-2xl border border-white/10 text-slate-400 hover:text-white hover:border-white/20 text-[10px] font-black uppercase tracking-widest transition-all">
                  Cancel
                </button>
                <button onClick={handleReworkConfirm} disabled={actionLoading !== null}
                  className="flex-1 px-5 py-3 rounded-2xl bg-rose-500 hover:bg-rose-400 text-white text-[10px] font-black uppercase tracking-widest transition-all shadow-lg shadow-rose-500/20 active:scale-95 disabled:opacity-50 flex items-center justify-center gap-2">
                  {actionLoading ? <RotateCw className="w-3.5 h-3.5 animate-spin" /> : <ClipboardX className="w-3.5 h-3.5" />}
                  Flag for Rework
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}


// ──────────────────────────────────────────────
// Main App
// ──────────────────────────────────────────────
export default function App() {
  const [view, setView] = useState<'dashboard' | 'scraper' | 'logs' | 'quality' | 'qa' | 'auto_sync' | 'monitoring'>('dashboard');
  const [inputData, setInputData] = useState('');
  const [products, setProducts] = useState<Product[]>([]);
  const [transformedRows, setTransformedRows] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [scrapeUrl, setScrapeUrl] = useState('');
  const [isScraping, setIsScraping] = useState(false);
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [shopifyOps, setShopifyOps] = useState<Record<string, string | null>>({});
  const [globalShopifyOp, setGlobalShopifyOp] = useState<string | null>(null);
  const [validationModal, setValidationModal] = useState<any>(null);
  const [comparePanel, setComparePanel] = useState<{
    scraperId: string;
    scraperName: string;
    loading: boolean;
    data: CompareData | null;
    error: string | null;
  } | null>(null);
  const [appUrl, setAppUrl] = useState('');
  const [urlCopied, setUrlCopied] = useState(false);
  const queryClient = useQueryClient();

  // ── Multi-store state ─────────────────────────────────────────
  const [activeStore, setActiveStore] = useState<'test' | 'main'>(() =>
    (localStorage.getItem('activeStore') as 'test' | 'main') || 'test'
  );

  // ── MAIN store approval registry (in-memory, polled from backend) ──
  const [approvedScrapers, setApprovedScrapers] = useState<Set<string>>(new Set());
  useEffect(() => {
    const fetchApprovals = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/approve`);
        if (res.ok) {
          const data = await res.json();
          setApprovedScrapers(new Set(data.approved ?? []));
        }
      } catch { /* ignore — backend may not be up yet */ }
    };
    fetchApprovals();
    const interval = setInterval(fetchApprovals, 15000);
    return () => clearInterval(interval);
  }, []);
  const [confirmModal, setConfirmModal] = useState<{
    open: boolean; label: string; action: (() => void) | null;
  }>({ open: false, label: '', action: null });

  const [shopifyDomain, setShopifyDomain] = useState('');

  useEffect(() => {
    fetch('/api/app-info').then(r => r.ok ? r.json() : null).then(d => {
      if (d?.dev_url) setAppUrl(d.dev_url);
      if (d?.shopify_domain) setShopifyDomain(d.shopify_domain);
    }).catch(() => {});
  }, []);

  // ── Multi-store helpers ────────────────────────────────────────
  const updateActiveStore = (s: 'test' | 'main') => {
    setActiveStore(s);
    localStorage.setItem('activeStore', s);
  };

  const shopifyFetch = useCallback((url: string, opts: RequestInit = {}): Promise<Response> => {
    const h: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(opts.headers as Record<string, string> || {}),
      'X-Store-Key': activeStore,
    };
    if (activeStore === 'main') h['X-Confirm-Main'] = 'CONFIRM MAIN STORE ACTION';
    return fetch(url, { ...opts, headers: h });
  }, [activeStore]);

  const guardMain = useCallback((label: string, action: () => void) => {
    if (activeStore !== 'main') { action(); return; }
    setConfirmModal({ open: true, label, action });
  }, [activeStore]);

  const handleCopyUrl = useCallback(() => {
    if (!appUrl) return;
    navigator.clipboard.writeText(appUrl).then(() => {
      setUrlCopied(true);
      setTimeout(() => setUrlCopied(false), 2000);
    });
  }, [appUrl]);

  const { data: scrapersData, isLoading: isLoadingStats, isError: isScrapersError, refetch: fetchStats } = useQuery({
    queryKey: ['scrapers'],
    queryFn: async () => {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 12000);
      const res = await fetch(`${API_BASE}/api/scrapers`, { signal: controller.signal });
      clearTimeout(timeoutId);
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    retry: false,
    refetchOnWindowFocus: false,
  });

  const { data: progressData } = useQuery({
    queryKey: ['progress'],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/progress`);
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    refetchInterval: autoRefresh ? 2000 : false,
    retry: false,
  });

  const { data: qualityData } = useQuery({
    queryKey: ['quality'],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/quality`);
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    refetchInterval: Object.values(progressData ?? {}).some((p: any) => p?.is_running) ? 5000 : 30000,
    retry: false,
  });

  const { data: qaData, refetch: refetchQa } = useQuery({
    queryKey: ['qa'],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/qa`);
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    refetchInterval: view === 'qa' ? 15000 : false,
    retry: false,
  });

  const prevProgressRef = useRef<Record<string, any>>({});
  useEffect(() => {
    if (!progressData || !shopifyOps) return;
    Object.entries(shopifyOps).forEach(([scraperId, op]) => {
      if (!op) return;
      const prev = prevProgressRef.current[scraperId];
      const curr = progressData[scraperId];
      if (!curr) return;
      const wasRunning = prev?.is_running !== false;
      const nowDone = curr.is_running === false && curr.progress === 100;
      if (wasRunning && nowDone) {
        const r = curr.shopify_result;
        if (r) {
          if (op === 'upload') {
            toast('success', 'Upload Complete', `${r.created} created, ${r.skipped} existed, ${r.failed} failed`);
          } else if (op === 'update') {
            toast('success', 'Update Complete', `${r.updated} updated, ${r.skipped} not matched, ${r.failed} failed`);
          } else if (op === 'delete-oos') {
            toast('warning', 'Delete OOS Complete', `${r.deleted} removed, ${r.skipped} safety-skipped, ${r.failed} failed`);
          }
        }
        setShopifyOps(prev => ({ ...prev, [scraperId]: null }));
      }
    });

    if (globalShopifyOp) {
      const prev = prevProgressRef.current['__global__'];
      const curr = progressData['__global__'];
      if (curr) {
        const wasRunning = prev?.is_running !== false;
        const nowDone = curr.is_running === false && curr.progress === 100;
        if (wasRunning && nowDone) {
          const r = curr.shopify_result;
          if (r) {
            const parts = [
              r.created > 0 ? `${r.created} created` : '',
              r.updated > 0 ? `${r.updated} updated` : '',
              r.deleted > 0 ? `${r.deleted} deleted` : '',
              r.failed  > 0 ? `${r.failed} failed`   : '',
            ].filter(Boolean).join(', ');
            toast('success', 'Global Op Complete', parts || 'All scrapers processed.');
          }
          setGlobalShopifyOp(null);
        }
      }
    }

    prevProgressRef.current = progressData;
  }, [progressData, shopifyOps, globalShopifyOp]);

  const rawScrapers = scrapersData?.scrapers || (Array.isArray(scrapersData) ? scrapersData : []);
  const backendOnline = scrapersData ? true : (isScrapersError ? false : null);

  let dashboardStats = scrapersData?.summary || { total_products: 0, total_scrapes: 0, last_updated: 'Never' };

  const websites: WebsiteStats[] = rawScrapers.length
    ? rawScrapers.map((s: any, i: number) => {
      const key = s.id || s.name;
      const prog = progressData?.[key] || progressData?.[s.name];
      return {
        id: String(i + 1),
        scraper_id: s.id,
        name: s.display_name || s.name,
        lastUpdated: s.last_updated || 'Never',
        totalProducts: Number(s.total_products) || 0,
        currency: s.currency || (s.id === 'michael_kors' ? 'INR' : ['marcjacobs','tory','coach','gymshark','aloyoga'].includes(s.id) ? 'USD' : 'GBP'),
        category: s.category || (s.id === 'cruise_fashion' ? 'Outlet/Brands' : 'Standard Catalog'),
        status: prog?.status || s.status || 'idle',
        progress: prog?.progress || 0,
        is_running: prog?.is_running ?? (s.status === 'running'),
        stuck: prog?.stuck ?? false,
        products_count: prog?.products_count ?? s.products_count,
        shopify_op: prog?.shopify_op,
        shopify_counts: prog?.shopify_counts,
        shopify_result: prog?.shopify_result,
        quality: prog?.quality ?? qualityData?.quality?.[s.id] ?? undefined,
      };
    })
    : FALLBACK_SCRAPERS;

  if (scrapersData && !scrapersData.summary) {
    dashboardStats = {
      total_products: websites.reduce((a, c) => a + c.totalProducts, 0),
      total_scrapes: 0,
      last_updated: new Date().toISOString(),
    };
  }

  const handleUpdateAll = () => {
    fetchStats();
    queryClient.invalidateQueries({ queryKey: ['progress'] });
    toast('info', 'Refreshing', 'Fetching latest stats from the server…');
  };

  const totalProducts = dashboardStats.total_products;
  const totalScrapes = dashboardStats.total_scrapes;
  const activeScrapers = websites.length;
  const lastScrapeTime = dashboardStats.last_updated === 'Never' || !dashboardStats.last_updated
    ? 'Never'
    : new Date(dashboardStats.last_updated).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  const handleDownloadSiteCSV = async (scraperId: string) => {
    try {
      const response = await fetch(`${API_BASE}/api/download/${scraperId}`);
      if (!response.ok) {
        const err = await response.json();
        toast('error', 'Download Failed', err.error || 'No CSV available. Run the scraper first.');
        return;
      }
      const blob = new Blob([await response.arrayBuffer()], { type: 'text/csv;charset=utf-8;' });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      const cd = response.headers.get('content-disposition');
      let filename = `${scraperId}_products_shopify.csv`;
      if (cd) {
        const m = cd.match(/filename[^;=\n]*=\s*(?:UTF-8''|["']?)([^"'\n;]+)/i);
        if (m?.[1]) filename = decodeURIComponent(m[1].trim().replace(/['"]/g, ''));
      }
      if (!filename.toLowerCase().endsWith('.csv')) filename = filename.replace(/\.[^.]+$/, '') + '.csv';
      link.setAttribute('download', filename);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
      toast('success', 'CSV Downloaded', `${filename} saved.`);
    } catch (e: any) {
      toast('error', 'Download Error', e.message);
    }
  };

  const transformToShopify = useCallback((products: Product[]) => {
    const rows: any[] = [];
    products.forEach(product => {
      if (!product) return;
      const variants = product.variants || [];
      const title = product.Title || '';
      const handle = product.Handle || generateHandle(title);
      const body = product['Body (HTML)'] || '';
      const vendor = product.Vendor || '';
      const type = product.Type || '';
      const tags = product.Tags || '';
      const seenVariants = new Set<string>();
      const unique_variants: Variant[] = [];
      variants.forEach(v => {
        const sku = v['Variant SKU'] || `SKU-${Math.random().toString(36).substr(2, 9)}`;
        const size = v.size || 'One Size';
        const price = v['Variant Price'] || '0.00';
        const key = `${sku}-${size}`;
        if (!seenVariants.has(key)) { seenVariants.add(key); unique_variants.push({ ...v, 'Variant SKU': sku, size, 'Variant Price': price }); }
      });
      if (!unique_variants.length) unique_variants.push({ 'Variant SKU': `PROD-${Math.random().toString(36).substr(2, 5)}`, size: 'One Size', 'Variant Price': '0.00', images: [] } as any);
      const allImages: string[] = [];
      const seenImages = new Set<string>();
      unique_variants.forEach(v => v.images?.forEach(img => { if (img && !seenImages.has(img)) { seenImages.add(img); allImages.push(img); } }));
      ((product as any).images || []).forEach((img: any) => { if (img && !seenImages.has(img)) { seenImages.add(img); allImages.push(img); } });
      const maxRows = Math.max(unique_variants.length, allImages.length);
      for (let i = 0; i < maxRows; i++) {
        const isFirst = i === 0;
        const variant = unique_variants[i] || null;
        const imageSrc = allImages[i] || '';
        rows.push({ Handle: handle, Title: isFirst ? title : '', 'Body (HTML)': isFirst ? body : '', Vendor: isFirst ? vendor : '', Type: isFirst ? type : '', Tags: isFirst ? tags : '', Published: isFirst ? 'TRUE' : '', 'Option1 Name': isFirst ? 'Size' : '', 'Option1 Value': variant?.size || '', 'Variant SKU': variant?.['Variant SKU'] || '', 'Variant Price': variant?.['Variant Price'] || '', 'Variant Compare At Price': variant?.['Variant Compare At Price'] || '', 'Variant Inventory Qty': variant ? 10 : '', 'Variant Inventory Policy': variant ? 'deny' : '', 'Variant Fulfillment Service': variant ? 'manual' : '', 'Variant Requires Shipping': variant ? 'TRUE' : '', 'Variant Taxable': variant ? 'TRUE' : '', 'Image Src': imageSrc, 'Image Position': imageSrc ? i + 1 : '' });
      }
    });
    return rows;
  }, []);

  const handleProcess = () => {
    setIsProcessing(true); setError(null);
    try {
      let parsed = JSON.parse(inputData);
      if (parsed?.products) parsed = parsed.products;
      const arr = Array.isArray(parsed) ? parsed : [parsed];
      const rows = transformToShopify(arr);
      if (!rows.length) throw new Error('No valid products could be extracted. Check JSON structure.');
      setTransformedRows(rows); setProducts(arr);
      toast('success', 'Data Processed', `Generated ${rows.length} rows for Shopify.`);
    } catch (e: any) {
      setError(e.message || 'Invalid JSON format');
      setTransformedRows([]);
      toast('error', 'Processing Error', e.message);
    } finally { setIsProcessing(false); }
  };

  const downloadCSV = () => {
    if (!transformedRows.length) return;
    const csv = Papa.unparse(transformedRows, { quotes: true, header: true, skipEmptyLines: true });
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.setAttribute('download', 'shopify_products.csv');
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
    toast('success', 'CSV Exported', 'shopify_products.csv downloaded.');
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => { setInputData(ev.target?.result as string); toast('info', 'File Loaded', file.name); };
    reader.readAsText(file);
  };

  const loadSample = () => {
    const sample = [{ Title: 'Valentino Leather Shoe', 'Body (HTML)': '<p>Luxury leather shoes crafted in Italy.</p>', Vendor: 'Valentino', Type: 'Shoes', Tags: 'luxury, leather, footwear', variants: [{ 'Variant SKU': 'VAL-SHOE-BLK-42', size: '42', 'Variant Price': '499.00', 'Variant Compare At Price': '599.00', images: ['https://picsum.photos/seed/shoe1/800/800'] }] }];
    setInputData(JSON.stringify(sample, null, 2)); toast('info', 'Sample Loaded', 'A demo product was loaded into the editor.');
  };

  const handleRunScraper = async (scraperId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/scrape`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_email: 'admin@mirage.com', scraper_ids: [scraperId] }) });
      if (res.ok) {
        queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 10, status: 'Initializing…', products_count: 0 } }));
        toast('success', 'Scraper Started', `"${scraperId}" is now running.`);
      } else {
        const err = await res.json(); toast('error', 'Start Failed', err.error);
      }
    } catch {
      toast('error', 'Connection Error', 'Backend is offline.');
    }
  };

  const handleCancelScrape = async (scraperId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/scrape/cancel/${scraperId}`, { method: 'POST' });
      if (res.ok) {
        toast('warning', 'Stopping Scraper', 'Cancellation signal sent.');
        queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { ...(old?.[scraperId] || {}), status: 'Stopping...', progress: 99 } }));
      } else { const err = await res.json(); toast('error', 'Cancel Failed', err.error || `Server returned ${res.status}`); }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleRestartScrape = async (scraperId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/scrape/restart`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_email: 'admin@mirage.com', scraper_ids: [scraperId] }) });
      if (res.ok) {
        setShopifyOps(prev => ({ ...prev, [scraperId]: null }));
        queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Restarting…', products_count: 0 } }));
        toast('info', 'Scraper Restarted', 'A fresh run has been initiated.');
      } else { const err = await res.json(); toast('error', 'Restart Failed', err.error); }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleShopifyCancel = async (scraperId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/shopify/cancel/${scraperId}`, { method: 'POST' });
      if (res.ok) {
        toast('warning', 'Stopping Shopify Op', 'Cancellation signal sent — finishing current batch.');
        queryClient.setQueryData(['progress'], (old: any) => ({
          ...old,
          [scraperId]: { ...(old?.[scraperId] || {}), status: 'Cancelling…', progress: old?.[scraperId]?.progress ?? 50 },
        }));
      } else {
        const err = await res.json();
        toast('error', 'Cancel Failed', err.error || 'No active Shopify op found.');
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleShopifyUpload = (scraperId: string) => {
    guardMain(`Upload "${scraperId}" to Shopify`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/upload/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('info', activeStore === 'main' ? 'MAIN Upload Started' : 'Shopify Upload Started', 'New products are being pushed to your store.');
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'upload' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Shopify Upload: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Upload Failed', err.error || 'Could not start Shopify upload.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyUpdate = (scraperId: string) => {
    guardMain(`Update "${scraperId}" in Shopify`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/update/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('info', activeStore === 'main' ? 'MAIN Update Started' : 'Shopify Update Started', 'Updating existing products in your store.');
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'update' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Shopify Update: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Update Failed', err.error || 'Could not start Shopify update.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyCheckOos = async (scraperId: string) => {
    setShopifyOps(prev => ({ ...prev, [scraperId]: 'check-oos' }));
    try {
      toast('info', 'Checking OOS…', 'Comparing Shopify store against current CSV.');
      const res = await shopifyFetch(`${API_BASE}/api/shopify/check-oos/${scraperId}`);
      if (res.ok) {
        const data = await res.json();
        const oos = data.oos || [];
        if (oos.length === 0) {
          toast('success', 'No OOS Products', `All ${data.total_shopify} Shopify products are present in the current CSV.`);
        } else {
          toast('warning', `${oos.length} OOS Products Found`,
            `${oos.length} products not in current CSV. Use "Delete OOS" to remove them.\n${oos.slice(0,3).map((p: any) => p.title).join(', ')}${oos.length > 3 ? `… +${oos.length - 3} more` : ''}`
          );
        }
      } else { const err = await res.json(); toast('error', 'OOS Check Failed', err.error || 'Could not check OOS status.'); }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
    finally { setShopifyOps(prev => ({ ...prev, [scraperId]: null })); }
  };

  const handleShopifyDeleteOos = (scraperId: string) => {
    guardMain(`Delete OOS products for "${scraperId}"`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/delete-oos/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('warning', 'Deleting OOS Products', 'Removing discontinued products from Shopify.');
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'delete-oos' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Shopify Delete OOS: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Delete OOS Failed', err.error || 'Could not start OOS deletion.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyUpdateImages = (scraperId: string) => {
    guardMain(`Fix images for "${scraperId}"`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/update-images/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('info', 'Fix Images Started', 'Re-linking colour variant images for existing products.');
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'reimage' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Shopify Fix Images: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Fix Images Failed', err.error || 'Could not start image fix.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyCheckImages = async (scraperId: string) => {
    toast('info', 'Image QC Running', `Auditing Shopify variant image links for ${scraperId}…`);
    try {
      const res = await shopifyFetch(`${API_BASE}/api/shopify/check-images/${scraperId}`);
      if (res.ok) {
        const data = await res.json();
        const { total, ok, issues_count, no_images_count, missing_variant_img_count, pass_rate } = data;
        if (issues_count === 0) {
          toast('success', 'Image QC Passed ✓', `All ${total} products have correct images & variant links (${pass_rate}% pass rate).`);
        } else {
          toast('warning', `Image QC: ${issues_count} issue(s)`, `${total} total — ${no_images_count} no images, ${missing_variant_img_count} missing variant links. Use Fix Images to repair.`);
        }
      } else {
        const err = await res.json();
        toast('error', 'Image QC Failed', err.error || 'Could not run image audit.');
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleShopifyDedup = (scraperId: string) => {
    guardMain(`Remove duplicate products for "${scraperId}" from Shopify`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/dedup/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('info', 'Dedup Started', `Scanning "${scraperId}" for duplicate products…`);
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'dedup' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Dedup: Scanning for duplicates…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Dedup Failed', err.error || 'Could not start dedup.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyNuke = (scraperId: string) => {
    guardMain(`CLEAR ALL "${scraperId}" products from Shopify`, () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/delete-all/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('warning', 'Store Nuke Started', `Deleting ALL products for "${scraperId}" from Shopify.`);
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'nuke' }));
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, [scraperId]: { is_running: true, progress: 5, status: 'Shopify Nuke Store: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Nuke Failed', err.error || 'Could not start store nuke.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyUploadAll = () => {
    guardMain('Upload ALL scrapers to Shopify', () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/upload-all`, { method: 'POST' });
          if (res.ok) {
            toast('info', activeStore === 'main' ? 'MAIN Global Upload Started' : 'Global Upload Started', 'Uploading new products for all scrapers.');
            setGlobalShopifyOp('upload-all');
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, __global__: { is_running: true, progress: 3, status: 'Upload All: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Global Upload Failed', err.error || 'Could not start global upload.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyUpdateAll = () => {
    guardMain('Update ALL scrapers in Shopify', () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/update-all`, { method: 'POST' });
          if (res.ok) {
            toast('info', activeStore === 'main' ? 'MAIN Global Update Started' : 'Global Update Started', 'Updating existing products for all scrapers.');
            setGlobalShopifyOp('update-all');
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, __global__: { is_running: true, progress: 3, status: 'Update All: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Global Update Failed', err.error || 'Could not start global update.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifyCheckOosAll = async () => {
    setGlobalShopifyOp('check-oos-all');
    try {
      toast('info', 'Checking OOS (All)…', 'Comparing Shopify store against all CSVs.');
      const res = await shopifyFetch(`${API_BASE}/api/shopify/check-oos-all`);
      if (res.ok) {
        const data = await res.json();
        const totalOos = data.total_oos ?? 0;
        toast(totalOos === 0 ? 'success' : 'warning',
          totalOos === 0 ? 'No OOS Products' : `${totalOos} OOS Products Found`,
          totalOos === 0 ? 'All Shopify products are in current CSVs.' : `${totalOos} products across all scrapers not in current CSVs.`
        );
      } else { const err = await res.json(); toast('error', 'OOS Check Failed', err.error || 'Could not check OOS status.'); }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
    finally { setGlobalShopifyOp(null); }
  };

  const handleShopifyDeleteOosAll = () => {
    guardMain('Delete OOS from ALL scrapers in Shopify', () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/delete-oos-all`, { method: 'POST' });
          if (res.ok) {
            toast('warning', 'Global Delete OOS Started', 'Removing discontinued products from all scrapers.');
            setGlobalShopifyOp('delete-oos-all');
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, __global__: { is_running: true, progress: 3, status: 'Delete OOS All: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Global Delete OOS Failed', err.error || 'Could not start global delete.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleShopifySyncAll = () => {
    guardMain('Full Sync ALL scrapers in Shopify', () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/sync-all`, { method: 'POST' });
          if (res.ok) {
            toast('info', activeStore === 'main' ? 'MAIN Full Sync Started' : 'Full Sync Started', 'Phase 1: Upload → Phase 2: Update across all scrapers.');
            setGlobalShopifyOp('sync-all');
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, __global__: { is_running: true, progress: 2, status: 'Full Sync: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Full Sync Failed', err.error || 'Could not start sync.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleCompare = async (scraperId: string, scraperName: string) => {
    setComparePanel({ scraperId, scraperName, loading: true, data: null, error: null });
    try {
      const res = await fetch(`${API_BASE}/api/shopify/compare/${scraperId}`);
      const json = await res.json();
      if (!res.ok) {
        setComparePanel(prev => prev ? { ...prev, loading: false, error: json.error ?? `HTTP ${res.status}` } : null);
      } else {
        setComparePanel(prev => prev ? { ...prev, loading: false, data: json } : null);
      }
    } catch (e: any) {
      setComparePanel(prev => prev ? { ...prev, loading: false, error: e.message } : null);
    }
  };

  const handleShopifyNukeAll = () => {
    guardMain('DELETE ALL products from ALL scrapers', () => {
      (async () => {
        try {
          const res = await shopifyFetch(`${API_BASE}/api/shopify/delete-all-all`, { method: 'POST' });
          if (res.ok) {
            toast('warning', 'Global Nuke Started', 'Purging ALL scraper products from Shopify store...');
            setGlobalShopifyOp('nuke-all');
            queryClient.setQueryData(['progress'], (old: any) => ({ ...old, __global__: { is_running: true, progress: 3, status: 'Nuke All: Starting…', products_count: 0 } }));
          } else { const err = await res.json(); toast('error', 'Global Nuke Failed', err.error || 'Could not start global nuke.'); }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleFullPipeline = async () => {
    if (!window.confirm(
      'Full Pipeline will run 5 phases across all scrapers:\n\n' +
      '1. Run all 10 scrapers (may take 30–120 min)\n' +
      '2. Validate all CSVs (tags, pricing, descriptions)\n' +
      '3. Upload new products to Shopify\n' +
      '4. Check out-of-stock products\n' +
      '5. Delete OOS products from store\n\n' +
      'This is a long-running operation. You can cancel at any time.\n\nProceed?'
    )) return;
    try {
      const res = await fetch(`${API_BASE}/api/shopify/full-pipeline`, { method: 'POST' });
      if (res.ok) {
        toast('info', 'Full Pipeline Started', 'Phase 1/5: Launching all scrapers…');
        setGlobalShopifyOp('full-pipeline');
        queryClient.setQueryData(['progress'], (old: any) => ({
          ...old,
          __global__: { is_running: true, progress: 2, status: 'Full Pipeline: Starting…', products_count: 0 },
        }));
      } else {
        const err = await res.json();
        toast('error', 'Pipeline Failed to Start', err.error || 'Could not start full pipeline.');
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleApproveForMain = async (scraperId: string) => {
    toast('info', 'Checking quality gate…', `Validating "${scraperId}" for MAIN store approval.`);
    try {
      const res = await fetch(`${API_BASE}/api/approve/${scraperId}`, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        setApprovedScrapers(prev => new Set([...prev, scraperId]));
        toast('success', 'Approved for MAIN', `${scraperId} passed quality gate (${data.pass_rate}%) and is approved for MAIN store promotion.`);
      } else {
        toast('error', 'Approval Denied', data.error || 'Quality gate failed — fix all issues first.');
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handlePromote = (scraperId: string) => {
    guardMain(`Promote "${scraperId}" to MAIN STORE`, () => {
      (async () => {
        try {
          toast('info', 'Promoting to MAIN…', `Quality gate running then uploading ${scraperId} to MAIN STORE.`);
          const res = await shopifyFetch(`${API_BASE}/api/shopify/promote/${scraperId}`, { method: 'POST' });
          if (res.ok) {
            toast('success', '→ MAIN Upload Started', `${scraperId} products are being uploaded to MAIN STORE.`);
            setShopifyOps(prev => ({ ...prev, [scraperId]: 'upload' }));
            queryClient.setQueryData(['progress'], (old: any) => ({
              ...old, [scraperId]: { is_running: true, progress: 5, status: 'MAIN Upload: Starting…', products_count: 0 },
            }));
          } else {
            const err = await res.json();
            if (err.quality) {
              toast('error', 'Quality Gate Blocked', err.error || 'Quality checks failed — fix issues before promoting to MAIN.');
            } else {
              toast('error', 'Promote Failed', err.error || 'Could not promote to MAIN.');
            }
          }
        } catch (e: any) { toast('error', 'Connection Error', e.message); }
      })();
    });
  };

  const handleValidate = async (scraperId: string) => {
    toast('info', 'Validating CSV…', `Running quality checks for "${scraperId}"`);
    try {
      const res = await fetch(`${API_BASE}/api/validate/${scraperId}`);
      const data = await res.json();
      if (!res.ok) {
        toast('error', 'Validation Error', data.error || 'Could not validate CSV.');
        return;
      }
      setValidationModal(data);
      if (data.errors === 0 && data.warnings === 0) {
        toast('success', 'All Clear', `${data.total} products — 0 errors, 0 warnings.`);
      } else if (data.errors === 0) {
        toast('warning', 'Warnings Found', `${data.total} products — ${data.warnings} warnings, 0 errors.`);
      } else {
        toast('error', 'Errors Found', `${data.errors} products have critical issues. Review before uploading.`);
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleValidateAll = async () => {
    toast('info', 'Validating All CSVs…', 'Running quality gate across all 10 scrapers…');
    try {
      const res = await fetch(`${API_BASE}/api/validate-all`);
      const data = await res.json();
      if (!res.ok) {
        toast('error', 'Validation Error', data.error || 'Could not validate CSVs.');
        return;
      }
      const combined: any = {
        scraper_id: 'All Scrapers',
        csv_path: `${Object.keys(data.scrapers ?? {}).length} scrapers`,
        total:     data.total_products ?? 0,
        ok:        0, warnings: 0, errors: 0,
        pass_rate: 0,
        ready_to_upload: data.all_clear,
        products: [] as any[],
      };
      for (const r of Object.values(data.scrapers ?? {}) as any[]) {
        combined.ok       += r.ok       ?? 0;
        combined.warnings += r.warnings ?? 0;
        combined.errors   += r.errors   ?? 0;
        combined.products  = combined.products.concat(r.products ?? []);
      }
      combined.pass_rate = combined.total > 0
        ? Math.round((combined.ok + combined.warnings) / combined.total * 100 * 10) / 10
        : 0;
      setValidationModal(combined);
      if (combined.errors === 0) {
        toast('success', 'All Scrapers Clear', `${combined.total} products across all scrapers — 0 critical errors.`);
      } else {
        toast('error', 'Issues Found', `${combined.errors} critical errors across all scrapers. Review before uploading.`);
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleQCUpload = async (scraperId: string) => {
    toast('info', 'QC & Upload', `Running quality checks for "${scraperId}"…`);
    try {
      const res = await fetch(`${API_BASE}/api/qc-upload/${scraperId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload: false }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast('error', 'QC Error', data.error || 'Could not validate CSV.');
        return;
      }
      setValidationModal({ ...data, _pendingUpload: true });
      if (data.errors === 0 && data.warnings === 0) {
        toast('success', 'QC Passed', `${data.total} products — all clear. Click "Upload" in the report.`);
      } else if (data.errors === 0) {
        toast('warning', 'QC Warnings', `${data.total} products — ${data.warnings} warnings. Review then upload.`);
      } else {
        toast('error', 'QC Errors', `${data.errors} critical issues found. Fix before uploading.`);
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleQCUploadAll = async () => {
    toast('info', 'QC & Upload All', 'Running quality checks across all scrapers…');
    try {
      const res = await fetch(`${API_BASE}/api/qc-upload-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload: false }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast('error', 'QC Error', data.error || 'Could not validate CSVs.');
        return;
      }
      const combined: any = {
        scraper_id: 'All Scrapers',
        csv_path: `${Object.keys(data.scrapers ?? {}).length} scrapers`,
        total: data.total_products ?? 0,
        ok: 0, warnings: 0, errors: 0,
        pass_rate: 0,
        ready_to_upload: data.all_clear,
        products: [] as any[],
        per_category_summary: {} as any,
        _pendingUploadAll: true,
      };
      for (const r of Object.values(data.scrapers ?? {}) as any[]) {
        combined.ok       += r.ok       ?? 0;
        combined.warnings += r.warnings ?? 0;
        combined.errors   += r.errors   ?? 0;
        combined.products  = combined.products.concat(r.products ?? []);
        for (const [cat, cs] of Object.entries(r.per_category_summary ?? {}) as any[]) {
          if (!combined.per_category_summary[cat]) {
            combined.per_category_summary[cat] = { ok: 0, warnings: 0, errors: 0 };
          }
          combined.per_category_summary[cat].ok       += cs.ok       ?? 0;
          combined.per_category_summary[cat].warnings += cs.warnings ?? 0;
          combined.per_category_summary[cat].errors   += cs.errors   ?? 0;
        }
      }
      combined.pass_rate = combined.total > 0
        ? Math.round((combined.ok + combined.warnings) / combined.total * 100 * 10) / 10
        : 0;
      setValidationModal(combined);
      if (combined.errors === 0) {
        toast('success', 'All Scrapers QC Clear', `${combined.total} products — 0 critical errors. Upload now from the report.`);
      } else {
        toast('error', 'QC Issues Found', `${combined.errors} critical errors across all scrapers.`);
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleUploadFromModal = async (scraperId: string) => {
    if (!scraperId || scraperId === 'All Scrapers') return;
    toast('info', 'Starting Upload', `Uploading "${scraperId}" products to Shopify…`);
    try {
      const res = await fetch(`${API_BASE}/api/qc-upload/${scraperId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload: true }),
      });
      const data = await res.json();
      if (!res.ok || !data.upload_started) {
        toast('error', 'Upload Error', data.upload_message || data.error || 'Could not start upload.');
      } else {
        toast('success', 'Upload Started', data.upload_message || `Uploading ${scraperId}…`);
        setShopifyOps(prev => ({ ...prev, [scraperId]: 'upload' }));
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleUploadAllFromModal = async () => {
    toast('info', 'QC & Upload All', 'Uploading all QC-passing scrapers…');
    try {
      const res = await fetch(`${API_BASE}/api/qc-upload-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload: true }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast('error', 'Upload Error', data.error || 'Could not start uploads.');
        return;
      }
      if (data.upload_count > 0) {
        toast('success', 'Uploads Started', `${data.upload_count} scraper(s) uploading to Shopify.`);
        setGlobalShopifyOp('qc-upload-all');
      } else {
        toast('warning', 'No Uploads Started', 'No scrapers passed QC without errors.');
      }
    } catch (e: any) { toast('error', 'Connection Error', e.message); }
  };

  const handleClearStats = async () => {
    try {
      await fetch(`${API_BASE}/api/stats/clear`, { method: 'POST' });
      fetchStats();
      toast('warning', 'Stats Cleared', 'All progress stats have been reset.');
    } catch { toast('error', 'Failed', 'Could not clear stats.'); }
  };

  const handleScrape = async () => {
    if (!scrapeUrl) return;
    setIsScraping(true); setError(null);
    toast('info', 'Scraping URL', `Extracting from ${scrapeUrl}…`);
    try {
      let response = await fetch(`${API_BASE}/api/scrape_url`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: scrapeUrl }) });
      if (response.status === 404) response = await fetch('/api/scrape', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: scrapeUrl }) });
      if (!response.ok) { const err = await response.json(); throw new Error(err.error || 'Failed to scrape URL'); }
      const { products } = await response.json();
      setInputData(JSON.stringify(products, null, 2));
      toast('success', 'Extraction Complete', `Retrieved product data from ${scrapeUrl}`);
    } catch (e: any) {
      setError(e.message || 'Scraping failed');
      toast('error', 'Scraping Failed', e.message);
    } finally { setIsScraping(false); }
  };

  const handleDeleteProducts = async (scraperId: string) => {
    if (!window.confirm(`⚠️ ATTENTION: This will permanently DELETE all local data and Shopify registry entries for "${scraperId}".\n\nThis action CANNOT be undone.\n\nProceed?`)) return;
    try {
      const res = await fetch(`${API_BASE}/api/scrapers/${scraperId}/delete-products`, { method: 'POST' });
      if (res.ok) {
        toast('success', 'Data Deleted', `All products for "${scraperId}" have been removed.`);
        fetchStats(); // Refresh the list
      } else {
        const err = await res.json();
        toast('error', 'Delete Failed', err.error || 'Could not delete products.');
      }
    } catch (e: any) {
      toast('error', 'Connection Error', e.message);
    }
  };

  const handleDownloadTags = async () => {
    toast('info', 'Collecting Tags…', 'Reading all scraper CSVs for unique tags.');
    try {
      const res = await fetch(`${API_BASE}/api/tags`);
      if (!res.ok) throw new Error('Could not load tags from backend.');
      const data = await res.json();
      const tags: string[] = data.tags ?? [];
      if (!tags.length) {
        toast('warning', 'No Tags Found', 'Run some scrapers first to populate CSV data.');
        return;
      }
      const csv = Papa.unparse(tags.map(t => ({ Tag: t })));
      const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.setAttribute('href', url); link.setAttribute('download', 'unique-tags.csv');
      link.style.visibility = 'hidden'; document.body.appendChild(link); link.click(); document.body.removeChild(link);
      URL.revokeObjectURL(url);
      toast('success', 'Tags Downloaded', `${tags.length} unique tags saved to unique-tags.csv.`);
    } catch (e: any) {
      toast('error', 'Tags Export Failed', e.message);
    }
  };

  // ── Render ──────────────────────────────────
  return (
    <div className="min-h-screen selection:bg-primary/30">
      <ToastContainer />

      <MainStoreConfirmModal
        open={confirmModal.open}
        opLabel={confirmModal.label}
        onConfirm={() => {
          confirmModal.action?.();
          setConfirmModal({ open: false, label: '', action: null });
        }}
        onCancel={() => setConfirmModal({ open: false, label: '', action: null })}
      />

      {/* Header */}
      <header className="glass-panel border-x-0 border-t-0 border-b border-white/10 sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-8 py-4 sm:py-5 flex flex-col sm:flex-row justify-between items-center gap-4">
          {/* Logo */}
          <div className="flex items-center gap-4 group cursor-pointer" onClick={() => setView('dashboard')}>
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-primary to-secondary flex items-center justify-center shadow-lg shadow-primary/20 group-hover:scale-105 transition-transform">
              <Zap className="text-white w-6 h-6 fill-white" />
            </div>
            <div>
              <h1 className="text-lg font-black tracking-tight text-white uppercase flex items-center gap-2">
                MIRAGE <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-primary/20 text-primary border border-primary/20">PRO</span>
              </h1>
              <p className="text-[9px] text-slate-500 font-bold uppercase tracking-[0.2em]">Next-Gen Scraper Engine</p>
            </div>
          </div>

          {/* Nav Actions */}
          <div className="flex flex-wrap justify-center sm:justify-end items-center gap-2">
            {backendOnline === true && (
              <button
                onClick={() => setAutoRefresh(!autoRefresh)}
                className={cn(
                  "flex items-center gap-1.5 px-3 py-1.5 border text-[10px] font-black uppercase tracking-widest transition-colors cursor-pointer",
                  autoRefresh
                    ? "border-[#ff4d00]/40 text-[#ff4d00] hover:border-[#ff4d00]"
                    : "border-[#333] text-[#555] hover:border-[#555]"
                )}
              >
                {autoRefresh ? (
                  <><span className="w-1.5 h-1.5 bg-[#ff4d00] animate-pulse inline-block" /> LIVE</>
                ) : (
                  <><span className="w-1.5 h-1.5 bg-[#555] inline-block" /> PAUSED</>
                )}
              </button>
            )}
            {backendOnline === false && (
              <button onClick={() => fetchStats()} className="flex items-center gap-1.5 px-3 py-1.5 border border-red-600 text-red-500 text-[10px] font-black uppercase tracking-widest hover:bg-red-600 hover:text-black transition-colors">
                <span className="w-1.5 h-1.5 bg-red-500 inline-block" /> OFFLINE — RETRY
              </button>
            )}
            <button
              onClick={handleClearStats}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black text-[#555] hover:text-red-500 uppercase tracking-widest border border-transparent hover:border-red-900 transition-colors"
            >
              <Trash2 className="w-3 h-3" /> Clear
            </button>
            <button
              onClick={handleUpdateAll}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black text-[#555] hover:text-white uppercase tracking-widest border border-[#222] hover:border-[#444] transition-colors"
            >
              <RefreshCw className="w-3 h-3" /> Refresh
            </button>
            <button
              onClick={() => setView(view === 'quality' ? 'dashboard' : 'quality')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border",
                view === 'quality'
                  ? 'bg-primary border-primary text-white'
                  : 'border-[#333] text-[#555] hover:border-[#555] hover:text-white'
              )}
            >
              <ShieldCheck className="w-3 h-3" />
              {view === 'quality' ? 'Dashboard' : 'Quality'}
            </button>
            <button
              onClick={() => setView(view === 'qa' ? 'dashboard' : 'qa')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border relative",
                view === 'qa'
                  ? 'bg-amber-500 border-amber-500 text-black'
                  : 'border-[#333] text-[#555] hover:border-amber-500/50 hover:text-amber-400'
              )}
            >
              <ClipboardList className="w-3 h-3" />
              {view === 'qa' ? 'Dashboard' : 'QA Review'}
              {(qaData?.pending ?? 0) > 0 && view !== 'qa' && (
                <span className="absolute -top-1.5 -right-1.5 min-w-[16px] h-4 flex items-center justify-center rounded-full bg-amber-500 text-black text-[8px] font-black px-1">
                  {qaData.pending > 99 ? '99+' : qaData.pending}
                </span>
              )}
            </button>
            <button
              onClick={() => setView(view === 'logs' ? 'dashboard' : 'logs')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border",
                view === 'logs'
                  ? 'bg-[#ff4d00] border-[#ff4d00] text-black'
                  : 'border-[#333] text-[#555] hover:border-[#555] hover:text-white'
              )}
            >
              <History className="w-3 h-3" />
              {view === 'logs' ? 'Dashboard' : 'Logs'}
            </button>
            <button
              onClick={() => setView(view === 'auto_sync' ? 'dashboard' : 'auto_sync')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border relative",
                view === 'auto_sync'
                  ? 'bg-emerald-500 border-emerald-500 text-black'
                  : 'border-[#333] text-[#555] hover:border-emerald-500/40 hover:text-emerald-400'
              )}
            >
              <RefreshCcw className="w-3 h-3" />
              Auto Sync
            </button>
            <button
              onClick={() => setView(view === 'monitoring' ? 'dashboard' : 'monitoring')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border relative",
                view === 'monitoring'
                  ? 'bg-violet-500 border-violet-500 text-white'
                  : 'border-[#333] text-[#555] hover:border-violet-500/40 hover:text-violet-400'
              )}
            >
              <Activity className="w-3 h-3" />
              Monitor
            </button>
            <button
              onClick={() => setView(view === 'dashboard' ? 'scraper' : 'dashboard')}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-colors border",
                view === 'scraper'
                  ? 'bg-white border-white text-black'
                  : 'border-[#333] text-[#555] hover:border-[#555] hover:text-white'
              )}
            >
              <LayoutGrid className="w-3 h-3" />
              {view === 'scraper' ? 'Dashboard' : 'Tools'}
            </button>

            {/* ── Store Switcher ── */}
            <div className="flex items-center gap-1 ml-1 pl-3 border-l border-white/10">
              <button
                onClick={() => updateActiveStore('test')}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest border rounded transition-all',
                  activeStore === 'test'
                    ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-400'
                    : 'border-[#222] text-[#444] hover:border-emerald-500/30 hover:text-emerald-600'
                )}
                title="Switch to TEST store"
              >
                <Store className="w-3 h-3" />
                TEST
              </button>
              <button
                onClick={() => updateActiveStore('main')}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-black uppercase tracking-widest border rounded transition-all',
                  activeStore === 'main'
                    ? 'bg-red-500/15 border-red-500/50 text-red-400'
                    : 'border-[#222] text-[#444] hover:border-red-500/30 hover:text-red-600'
                )}
                title="Switch to MAIN store (live)"
              >
                {activeStore === 'main' && (
                  <span className="relative flex h-2 w-2 mr-0.5">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-red-500"></span>
                  </span>
                )}
                MAIN
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-8 py-10">
        <AnimatePresence mode="wait">
          {view === 'monitoring' ? (
            <motion.div
              key="monitoring"
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -16 }}
              transition={{ duration: 0.25 }}
            >
              <MonitoringDashboard onBack={() => setView('dashboard')} />
            </motion.div>
          ) : view === 'auto_sync' ? (
            <AutoSyncCenter
              progressData={progressData ?? null}
              onToast={toast}
            />
          ) : view === 'logs' ? (
            <ActivityLogsPage onBack={() => setView('dashboard')} />
          ) : view === 'qa' ? (
            <QAReviewPage onBack={() => setView('dashboard')} initialData={qaData} shopifyDomain={shopifyDomain} />
          ) : view === 'quality' ? (
            <QualityGatePanel
              qualityData={qualityData?.quality ?? {}}
              scrapers={websites}
              onClose={() => setView('dashboard')}
            />
          ) : view === 'dashboard' ? (
            <motion.div
              key="dashboard"
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -16 }}
              transition={{ duration: 0.25 }}
              className="space-y-12"
            >
              {/* Hero */}
              <div className="relative py-12 px-10 rounded-[2.5rem] overflow-hidden">
                <div className="absolute inset-0 bg-gradient-to-r from-primary/10 to-secondary/10" />
                <div className="absolute -top-24 -right-24 w-96 h-96 bg-primary/20 rounded-full blur-[100px]" />
                <div className="absolute -bottom-24 -left-24 w-96 h-96 bg-secondary/10 rounded-full blur-[100px]" />
                
                <div className="relative">
                  <motion.div
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ delay: 0.05 }}
                    className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-primary/10 border border-primary/20 text-[10px] font-black text-primary uppercase tracking-[0.2em] mb-6"
                  >
                    <span className="relative flex h-2 w-2">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-primary opacity-75"></span>
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-primary"></span>
                    </span>
                    System Online
                  </motion.div>
                  <motion.h2
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.1 }}
                    className="text-5xl md:text-7xl font-black tracking-tight text-white leading-[0.9]"
                  >
                    WELCOME<br />
                    <span className="text-gradient">RUDRA.</span>
                  </motion.h2>
                  <motion.p
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: 0.15 }}
                    className="text-slate-400 mt-8 text-base max-w-xl font-medium"
                  >
                    Powerful multi-target scraping engine with real-time Shopify synchronization and automated inventory management.
                  </motion.p>
                </div>
              </div>

              {/* Stat Cards */}
              <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                {[
                  { label: 'Total Products', value: totalProducts.toLocaleString(), icon: Database,    accent: 'border-primary',   delay: 0.1  },
                  { label: 'Active Scrapers', value: activeScrapers,               icon: Activity,    accent: 'border-secondary', delay: 0.15 },
                  { label: 'Total Scrapes',   value: totalScrapes,                 icon: TrendingUp,  accent: 'border-primary',   delay: 0.2  },
                  { label: 'Last Update',     value: lastScrapeTime,               icon: Clock,       accent: 'border-secondary', delay: 0.25 },
                ].map(({ label, value, icon: Icon, accent, delay }) => (
                  <StatCard key={label} label={label} value={value} icon={Icon} accent={accent} delay={delay} />
                ))}
              </section>

              {/* Global Shopify Command Center */}
              <GlobalShopifyPanel
                globalOp={globalShopifyOp}
                globalProgress={progressData?.['__global__'] ?? null}
                allScrapersProgress={progressData ?? undefined}
                onUploadAll={handleShopifyUploadAll}
                onUpdateAll={handleShopifyUpdateAll}
                onCheckOosAll={handleShopifyCheckOosAll}
                onDeleteOosAll={handleShopifyDeleteOosAll}
                onSyncAll={handleShopifySyncAll}
                onNukeAll={handleShopifyNukeAll}
                onFullPipeline={handleFullPipeline}
                onValidateAll={handleValidateAll}
                onQCUploadAll={handleQCUploadAll}
                onViewLogs={() => setView('logs')}
              />

              {/* Active Websites */}
              <section className="space-y-8">
                <div className="flex justify-between items-center border-b border-white/10 pb-6">
                  <div>
                    <h3 className="text-2xl font-black text-white uppercase tracking-tight">Active Scrapers</h3>
                    <p className="text-slate-500 text-[11px] mt-1 uppercase tracking-widest font-bold">{websites.length} configured endpoints</p>
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => setIsAddModalOpen(true)}
                      className="flex items-center gap-2 px-5 py-2.5 rounded-xl border border-white/10 text-slate-400 hover:border-white/30 hover:text-white text-[10px] font-black uppercase tracking-widest transition-all bg-white/5 active:scale-95"
                    >
                      <Plus className="w-4 h-4" /> Register New
                    </button>
                    <button
                      onClick={handleDownloadTags}
                      className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-primary hover:bg-primary-hover text-white text-[10px] font-black uppercase tracking-widest transition-all shadow-lg shadow-primary/20 active:scale-95"
                    >
                      <Tag className="w-4 h-4" /> Export Tags
                    </button>
                  </div>
                </div>

                {isLoadingStats && !websites.length ? (
                  <div className="text-center py-20 text-[#333]">
                    <Cpu className="w-8 h-8 animate-spin mx-auto mb-4" />
                    <p className="text-[10px] font-black uppercase tracking-widest">CONNECTING TO SCRAPERS…</p>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {websites.map(site => (
                      <ScraperCard
                        key={site.id}
                        site={site}
                        onRun={() => handleRunScraper(site.scraper_id || site.id)}
                        onRestart={() => handleRestartScrape(site.scraper_id || site.id)}
                        onCancel={() => handleCancelScrape(site.scraper_id || site.id)}
                        onShopifyCancel={() => handleShopifyCancel(site.scraper_id || site.id)}
                        onDownload={() => handleDownloadSiteCSV(site.scraper_id || site.id)}
                        onShopifyUpload={() => handleShopifyUpload(site.scraper_id || site.id)}
                        onShopifyUpdate={() => handleShopifyUpdate(site.scraper_id || site.id)}
                        onShopifyUpdateImages={() => handleShopifyUpdateImages(site.scraper_id || site.id)}
                        onShopifyCheckImages={() => handleShopifyCheckImages(site.scraper_id || site.id)}
                        onShopifyCheckOos={() => handleShopifyCheckOos(site.scraper_id || site.id)}
                        onShopifyDeleteOos={() => handleShopifyDeleteOos(site.scraper_id || site.id)}
                        onShopifyNuke={() => handleShopifyNuke(site.scraper_id || site.id)}
                        onShopifyDedup={() => handleShopifyDedup(site.scraper_id || site.id)}
                        onDeleteProducts={() => handleDeleteProducts(site.scraper_id || site.id)}
                        onValidate={() => handleValidate(site.scraper_id || site.id)}
                        onQCUpload={() => handleQCUpload(site.scraper_id || site.id)}
                        onApprove={() => handleApproveForMain(site.scraper_id || site.id)}
                        onPromote={() => handlePromote(site.scraper_id || site.id)}
                        onCompare={() => handleCompare(site.scraper_id || site.id, site.name)}
                        isApproved={approvedScrapers.has(site.scraper_id || site.id)}
                        activeShopifyOp={shopifyOps[site.scraper_id || site.id] ?? null}
                        activeStore={activeStore}
                      />
                    ))}
                  </div>
                )}
              </section>
            </motion.div>
          ) : (
            <motion.div
              key="scraper"
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }}
              transition={{ duration: 0.2 }}
              className="space-y-8"
            >
              {/* Header row */}
              <div className="flex items-center gap-6 border-b border-white/10 pb-10">
                <button
                  onClick={() => setView('dashboard')}
                  className="w-12 h-12 flex items-center justify-center rounded-2xl border border-white/10 text-slate-400 hover:border-white/30 hover:text-white hover:bg-white/5 transition-all active:scale-90 shadow-lg"
                >
                  <ArrowLeft className="w-5 h-5" />
                </button>
                <div>
                  <h2 className="text-3xl font-black text-white uppercase tracking-tight">Scraper Tools</h2>
                  <p className="text-slate-500 text-[11px] mt-1 uppercase tracking-widest font-bold">Extract, transform, and export product data</p>
                </div>
              </div>

              {/* URL Scraper */}
              <div className="glass-panel rounded-3xl p-10 space-y-8 shadow-2xl relative overflow-hidden group">
                <div className="absolute top-0 right-0 w-64 h-64 bg-primary/5 rounded-full blur-3xl -mr-32 -mt-32 group-hover:bg-primary/10 transition-colors" />
                <div className="flex items-center justify-between relative z-10">
                  <div>
                    <h3 className="text-sm font-black text-white uppercase tracking-[0.2em] flex items-center gap-3">
                      <div className="w-2.5 h-2.5 rounded-full bg-primary shadow-lg shadow-primary/50" />
                      Target URL Extractor
                    </h3>
                    <p className="text-[11px] text-slate-500 mt-2 uppercase tracking-widest font-medium">Enter a product or category URL from a supported site to extract structured data.</p>
                  </div>
                  <button onClick={loadSample} className="px-5 py-2 rounded-xl text-[10px] font-black text-primary border border-primary/20 hover:bg-primary/10 uppercase tracking-widest transition-all active:scale-95">
                    Load Sample Data
                  </button>
                </div>
                <div className="flex gap-4 relative z-10">
                  <input
                    type="text"
                    value={scrapeUrl}
                    onChange={e => setScrapeUrl(e.target.value)}
                    placeholder="https://www.cruisefashion.com/outlet/..."
                    onKeyDown={e => e.key === 'Enter' && handleScrape()}
                    className="flex-1 px-6 py-4 text-sm bg-white/5 border border-white/10 rounded-2xl focus:border-primary/50 focus:ring-1 focus:ring-primary/50 outline-none text-white placeholder:text-slate-600 transition-all font-mono"
                  />
                  <button
                    onClick={handleScrape}
                    disabled={!scrapeUrl || isScraping}
                    className={cn(
                      'px-10 py-4 rounded-2xl text-sm font-black flex items-center gap-3 uppercase tracking-widest transition-all shadow-lg active:scale-95',
                      !scrapeUrl || isScraping
                        ? 'bg-white/5 text-slate-600 cursor-not-allowed border border-white/5'
                        : 'bg-primary hover:bg-primary-hover text-white shadow-primary/20'
                    )}
                  >
                    {isScraping ? <><RefreshCw className="w-5 h-5 animate-spin" /> Extracting…</> : <><Download className="w-5 h-5" /> Extract</>}
                  </button>
                </div>
              </div>

              {/* Two columns */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Left — JSON Editor */}
                <div className="space-y-6">
                  <div className="flex items-center justify-between">
                    <h3 className="text-[11px] font-black text-white uppercase tracking-[0.2em] flex items-center gap-3">
                      <FileJson className="w-4 h-4 text-primary" /> Product Data (JSON)
                    </h3>
                    <label className="cursor-pointer flex items-center gap-2 px-4 py-2 rounded-xl text-[10px] font-black text-slate-400 hover:text-white border border-white/10 hover:bg-white/5 uppercase tracking-widest transition-all active:scale-95">
                      <Upload className="w-4 h-4" /> Import JSON
                      <input type="file" accept=".json" onChange={handleFileUpload} className="hidden" />
                    </label>
                  </div>
                  <div className="relative h-[500px] glass-panel rounded-3xl overflow-hidden border-white/10">
                    <textarea
                      value={inputData}
                      onChange={e => setInputData(e.target.value)}
                      placeholder='Scraped data will appear here…'
                      className="w-full h-full p-8 font-mono text-xs bg-transparent outline-none resize-none text-slate-400 placeholder:text-slate-800 transition-colors"
                    />
                    <button
                      onClick={handleProcess}
                      disabled={!inputData || isProcessing}
                      className={cn(
                        'absolute bottom-6 right-6 px-6 py-3 rounded-2xl font-black text-[11px] flex items-center gap-3 uppercase tracking-widest transition-all shadow-xl active:scale-95',
                        !inputData || isProcessing
                          ? 'bg-white/5 text-slate-600 cursor-not-allowed'
                          : 'bg-primary hover:bg-primary-hover text-white shadow-primary/20'
                      )}
                    >
                      <RefreshCw className={cn('w-4 h-4', isProcessing && 'animate-spin')} />
                      Generate CSV
                    </button>
                  </div>
                  {error && (
                    <motion.div
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      className="p-5 rounded-2xl border border-rose-500/20 bg-rose-500/5 flex items-start gap-4 text-rose-400"
                    >
                      <AlertCircle className="w-5 h-5 shrink-0 mt-0.5" />
                      <div>
                        <p className="font-black text-[11px] uppercase tracking-widest">Processing Error</p>
                        <p className="text-[11px] font-medium opacity-80 mt-1">{error}</p>
                      </div>
                    </motion.div>
                  )}
                </div>

                {/* Right — Preview */}
                <div className="space-y-6">
                  <div className="flex items-center justify-between">
                    <h3 className="text-[11px] font-black text-white uppercase tracking-[0.2em] flex items-center gap-3">
                      <CheckCircle2 className="w-4 h-4 text-secondary" /> Output Preview
                    </h3>
                    <button
                      onClick={downloadCSV}
                      disabled={!transformedRows.length}
                      className={cn(
                        'flex items-center gap-2 px-5 py-2 rounded-xl text-[10px] font-black uppercase tracking-widest transition-all shadow-xl active:scale-95 border',
                        !transformedRows.length
                          ? 'text-slate-600 border-white/5 cursor-not-allowed bg-white/5'
                          : 'text-white bg-secondary border-secondary shadow-secondary/20 hover:brightness-110'
                      )}
                    >
                      <Download className="w-4 h-4" /> Export CSV
                    </button>
                  </div>
                  <div className="glass-panel rounded-3xl overflow-hidden shadow-2xl" style={{ height: '500px' }}>
                    {transformedRows.length > 0 ? (
                      <div className="overflow-auto h-full scrollbar-thin scrollbar-thumb-white/10">
                        <table className="w-full text-left text-[11px] font-mono">
                          <thead className="bg-white/5 sticky top-0 z-10 backdrop-blur-xl">
                            <tr>
                              {Object.keys(transformedRows[0]).map(h => (
                                <th key={h} className="px-4 py-4 border-b border-white/5 font-black text-slate-500 whitespace-nowrap uppercase text-[9px] tracking-[0.1em]">{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {transformedRows.slice(0, 50).map((row, idx) => (
                              <tr key={idx} className="border-b border-white/[0.02] hover:bg-white/[0.02] transition-colors">
                                {Object.values(row).map((val: any, vIdx) => (
                                  <td key={vIdx} className="px-4 py-3.5 truncate max-w-[140px] text-slate-400 font-medium" title={String(val)}>{String(val)}</td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {transformedRows.length > 50 && (
                          <div className="p-6 text-center bg-white/[0.01]">
                            <p className="text-slate-500 text-[10px] font-black uppercase tracking-[0.2em]">Showing 50 of {transformedRows.length} rows — export to see all.</p>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="h-full flex flex-col items-center justify-center text-slate-700 p-12 text-center">
                        <div className="w-20 h-20 rounded-[2rem] bg-white/5 flex items-center justify-center mb-8">
                          <FileSpreadsheet className="w-10 h-10 opacity-20" />
                        </div>
                        <p className="font-black text-base text-white uppercase tracking-widest">No Data Yet</p>
                        <p className="text-[11px] mt-3 max-w-xs text-slate-500 uppercase tracking-widest font-bold leading-relaxed">Scrape a URL or paste JSON on the left, then click Generate CSV.</p>
                      </div>
                    )}
                  </div>

                  {/* Capabilities */}
                  <div className="glass-panel rounded-[2rem] p-8 border-primary/10 relative overflow-hidden group">
                    <div className="absolute inset-0 bg-gradient-to-br from-primary/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
                    <p className="text-[10px] font-black text-slate-400 uppercase tracking-[0.3em] mb-6 relative z-10">Scraper Engine Capabilities</p>
                    <div className="grid grid-cols-2 gap-x-8 gap-y-4 relative z-10">
                      {['API-First Extraction', 'Headless Crawler', 'Shopify Schema Map', 'Variant Logic', 'Deduplication', 'Data Validation'].map((rule, i) => (
                        <div key={i} className="flex items-center gap-3 text-[10px] text-slate-300 font-bold py-1 uppercase tracking-wider group/item">
                          <div className="w-1.5 h-1.5 rounded-full bg-primary shadow-sm shadow-primary/50 group-hover/item:scale-150 transition-transform" />
                          {rule}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
        <AddWebsiteModal
          isOpen={isAddModalOpen}
          onClose={() => setIsAddModalOpen(false)}
          onSuccess={fetchStats}
        />

        <AnimatePresence>
          {validationModal && (
            <ValidationModal
              data={validationModal}
              onClose={() => setValidationModal(null)}
              onUpload={handleUploadFromModal}
              onUploadAll={handleUploadAllFromModal}
            />
          )}
        </AnimatePresence>

        <AnimatePresence>
          {comparePanel && (
            <StoreComparisonPanel
              scraperId={comparePanel.scraperId}
              scraperName={comparePanel.scraperName}
              loading={comparePanel.loading}
              data={comparePanel.data}
              error={comparePanel.error}
              onClose={() => setComparePanel(null)}
            />
          )}
        </AnimatePresence>
      </main>

      {/* Footer */}
      <footer className="max-w-7xl mx-auto px-8 py-12 border-t border-white/5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <p className="text-[10px] text-slate-500 font-black uppercase tracking-[0.2em]">
            Production-grade eCommerce scraper · Verified Shopify Output
          </p>
        </div>
        <div className="flex items-center gap-6">
          <p className="text-[10px] text-slate-600 font-black uppercase tracking-[0.3em]">MIRAGE v2.4.0</p>
          <div className="h-4 w-px bg-white/10" />
          <p className="text-[10px] text-primary font-black uppercase tracking-[0.3em] bg-primary/10 px-3 py-1 rounded-full border border-primary/20">Rudra</p>
        </div>
      </footer>
    </div>
  );
}
