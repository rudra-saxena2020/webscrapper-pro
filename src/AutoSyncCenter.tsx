import React, { useState, useEffect, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Clock,
  Play,
  XCircle,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  Calendar,
  Zap,
  Activity,
  Database,
  RotateCcw,
  Cpu,
  Shield,
  Info,
  Wifi,
  WifiOff,
  User,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ─── Types ───────────────────────────────────────────────────────────────────
interface NextRun {
  id: string;
  name: string;
  next_run: string;
}

interface AutoSyncRun {
  id: number;
  run_type: string;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  active_scrapers: string[];
  report_json: any;
  store: string;
}

interface AutoSyncStatus {
  is_running: boolean;
  current_scraper: string | null;
  next_run: string | null;
  next_runs: NextRun[];
  last_run: AutoSyncRun | null;
  last_report: any | null;
  active_scrapers: string[];
  scheduler_alive: boolean;
  last_triggered_by: string | null;
  do_worker_last_heartbeat: string | null;
  do_worker_version: string | null;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function formatIST(isoStr: string | null): string {
  if (!isoStr) return '—';
  try {
    return new Date(isoStr).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: true,
    });
  } catch {
    return isoStr;
  }
}

function durationStr(started: string | null, completed: string | null): string {
  if (!started || !completed) return '—';
  const diff = Math.round((new Date(completed).getTime() - new Date(started).getTime()) / 1000);
  if (diff < 60) return `${diff}s`;
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function runTypeLabel(rt: string): string {
  if (rt === 'morning' || rt === 'auto_sync_morning') return 'Morning';
  if (rt === 'evening' || rt === 'auto_sync_evening') return 'Evening';
  if (rt === 'manual') return 'Manual';
  if (rt === 'test') return 'Test';
  return rt;
}

function isSlotMatch(run: AutoSyncRun, slot: 'morning' | 'evening'): boolean {
  return run.run_type === slot || run.run_type === `auto_sync_${slot}`;
}

function statusColor(status: string) {
  if (status === 'completed') return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
  if (status === 'failed') return 'text-red-400 bg-red-500/10 border-red-500/30';
  if (status === 'running') return 'text-primary bg-primary/10 border-primary/30';
  if (status === 'cancelled') return 'text-slate-400 bg-white/5 border-white/15';
  return 'text-slate-400 bg-white/5 border-white/10';
}

function statusIcon(status: string) {
  if (status === 'completed') return <CheckCircle2 className="w-3 h-3 text-emerald-400" />;
  if (status === 'failed') return <AlertCircle className="w-3 h-3 text-red-400" />;
  if (status === 'running') return <span className="w-2 h-2 rounded-full bg-primary animate-pulse inline-block" />;
  if (status === 'cancelled') return <XCircle className="w-3 h-3 text-slate-500" />;
  return <Info className="w-3 h-3 text-slate-500" />;
}

// ─── Live Countdown Hook ──────────────────────────────────────────────────────
function useCountdown(targetISO: string | null): string {
  const [display, setDisplay] = useState('—');
  useEffect(() => {
    if (!targetISO) { setDisplay('—'); return; }
    const compute = () => {
      const diff = Math.max(0, Math.floor((new Date(targetISO).getTime() - Date.now()) / 1000));
      if (diff === 0) { setDisplay('Now'); return; }
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      const s = diff % 60;
      setDisplay(h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`);
    };
    compute();
    const iv = setInterval(compute, 1000);
    return () => clearInterval(iv);
  }, [targetISO]);
  return display;
}

// ─── Confirm Modal ────────────────────────────────────────────────────────────
const CONFIRM_PHRASE = 'CONFIRM MAIN STORE ACTION';

function TriggerConfirmModal({
  open, onConfirm, onCancel,
}: { open: boolean; onConfirm: () => void; onCancel: () => void }) {
  const [text, setText] = useState('');
  const match = text.trim() === CONFIRM_PHRASE;

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
              <p className="text-[9px] font-black uppercase tracking-[0.2em] text-red-400 mb-0.5">Auto Sync — Manual Trigger</p>
              <h4 className="text-sm font-black text-white uppercase tracking-widest">Run Full Sync Now?</h4>
            </div>
            <span className="ml-auto px-2 py-1 rounded-md bg-red-600/20 border border-red-500/30 text-[9px] font-black text-red-400 uppercase tracking-widest">MAIN</span>
          </div>
          <div className="mb-5 p-4 rounded-xl bg-red-500/5 border border-red-500/15 text-xs text-slate-300 leading-relaxed">
            This will trigger a <span className="text-white font-bold">full sync across all active MAIN store scrapers</span> immediately — scrape → QC → update → upload → OOS removal.
            <p className="text-slate-500 mt-2 text-[11px]">This runs outside the scheduled 10 AM / 10 PM IST windows.</p>
          </div>
          <div className="mb-6 space-y-2">
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Type to confirm:</p>
            <p className="text-[10px] font-mono text-slate-300 px-3 py-2 bg-white/5 rounded-lg border border-white/10 select-all">{CONFIRM_PHRASE}</p>
            <input
              autoFocus
              value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && match) onConfirm(); }}
              placeholder="Type the phrase above…"
              className={cn(
                'w-full px-4 py-3 rounded-xl bg-white/5 border text-sm text-white placeholder-slate-600 outline-none transition-all font-mono',
                match ? 'border-red-500/60 focus:border-red-500' : 'border-white/10 focus:border-white/25',
              )}
            />
          </div>
          <div className="flex gap-3">
            <button onClick={onCancel}
              className="flex-1 py-3 text-[10px] font-black uppercase tracking-widest border border-white/10 text-slate-400 hover:text-white hover:border-white/25 rounded-xl transition-all">
              Cancel
            </button>
            <button
              onClick={() => { if (match) onConfirm(); }}
              disabled={!match}
              className={cn(
                'flex-1 py-3 text-[10px] font-black uppercase tracking-widest rounded-xl transition-all flex items-center justify-center gap-2',
                match
                  ? 'bg-red-600 hover:bg-red-500 text-white active:scale-95'
                  : 'bg-white/5 text-slate-600 cursor-not-allowed',
              )}
            >
              <Play className="w-3.5 h-3.5" /> Run Now
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

// ─── Schedule Card ────────────────────────────────────────────────────────────
// FIX: slotLastRun is independently resolved per-slot from history, not from
// the global last_run which may be a manual/test run of a different type.
function ScheduleCard({
  slot, label, nextRun, slotLastRun, isActive,
}: {
  slot: 'morning' | 'evening';
  label: string;
  nextRun: NextRun | null;
  slotLastRun: AutoSyncRun | null;
  isActive: boolean;
}) {
  const countdown = useCountdown(nextRun?.next_run ?? null);

  return (
    <div className={cn(
      'glass-card rounded-2xl p-5 border transition-all relative overflow-hidden',
      isActive ? 'border-primary/30 bg-primary/5' : 'border-white/10'
    )}>
      <div className={cn(
        'absolute top-0 left-0 h-[2px] w-full',
        slot === 'morning' ? 'bg-gradient-to-r from-amber-400 to-orange-400' : 'bg-gradient-to-r from-indigo-400 to-purple-400'
      )} />

      <div className="flex items-start justify-between mb-3">
        <div>
          <p className={cn(
            'text-[9px] font-black uppercase tracking-[0.2em] mb-1',
            slot === 'morning' ? 'text-amber-400' : 'text-indigo-400'
          )}>{slot === 'morning' ? '☀️ Morning' : '🌙 Evening'}</p>
          <p className="text-lg font-black text-white">{label} IST</p>
        </div>
        {slotLastRun ? (
          <span className={cn(
            'flex items-center gap-1 px-2 py-1 rounded-lg border text-[9px] font-black uppercase tracking-widest',
            statusColor(slotLastRun.status)
          )}>
            {statusIcon(slotLastRun.status)}
            {slotLastRun.status}
          </span>
        ) : (
          <span className="px-2 py-1 rounded-lg border border-white/10 text-[9px] font-black uppercase tracking-widest text-slate-600 bg-white/5">
            Pending
          </span>
        )}
      </div>

      {nextRun && (
        <div className="flex items-center gap-2 mt-3">
          <Clock className="w-3 h-3 text-slate-500 shrink-0" />
          <span className="text-[10px] text-slate-400">
            Next: <span className="font-mono font-bold text-white">{countdown}</span>
          </span>
        </div>
      )}

      {slotLastRun && (
        <p className="text-[10px] text-slate-500 mt-1.5 font-mono">
          Last: {formatIST(slotLastRun.started_at)}
          {slotLastRun.completed_at && (
            <span className="text-slate-600"> · {durationStr(slotLastRun.started_at, slotLastRun.completed_at)}</span>
          )}
        </p>
      )}
    </div>
  );
}

// ─── Run History Row ──────────────────────────────────────────────────────────
function HistoryRow({ run }: { run: AutoSyncRun }) {
  const [expanded, setExpanded] = useState(false);
  const totals = run.report_json?.totals ?? {};
  const scraperCount = (run.active_scrapers ?? []).length ||
    Object.keys(run.report_json?.scrapers ?? {}).length;

  return (
    <>
      <tr
        className="border-b border-white/5 hover:bg-white/[0.02] transition-colors cursor-pointer"
        onClick={() => setExpanded(e => !e)}
      >
        <td className="px-4 py-3 font-mono text-[10px] text-slate-400 whitespace-nowrap">{formatIST(run.started_at)}</td>
        <td className="px-4 py-3">
          <span className={cn(
            'px-2 py-0.5 rounded text-[9px] font-black uppercase tracking-widest border',
            run.run_type.includes('morning') ? 'bg-amber-500/10 border-amber-500/25 text-amber-400' :
            run.run_type.includes('evening') ? 'bg-indigo-500/10 border-indigo-500/25 text-indigo-400' :
            'bg-white/5 border-white/10 text-slate-400'
          )}>
            {runTypeLabel(run.run_type)}
          </span>
        </td>
        <td className="px-4 py-3">
          <span className={cn(
            'flex items-center gap-1.5 w-fit px-2 py-0.5 rounded border text-[9px] font-black uppercase tracking-widest',
            statusColor(run.status)
          )}>
            {statusIcon(run.status)} {run.status}
          </span>
        </td>
        <td className="px-4 py-3 text-[11px] text-slate-300 tabular-nums font-mono text-center">{scraperCount}</td>
        <td className="px-4 py-3 text-[11px] text-emerald-400 tabular-nums font-mono text-center">{totals.updated ?? 0}</td>
        <td className="px-4 py-3 text-[11px] text-primary tabular-nums font-mono text-center">{totals.uploaded ?? 0}</td>
        <td className="px-4 py-3 text-[11px] text-red-400 tabular-nums font-mono text-center">{totals.oos_deleted ?? 0}</td>
        <td className="px-4 py-3 text-[11px] text-slate-400 font-mono whitespace-nowrap">{durationStr(run.started_at, run.completed_at)}</td>
        <td className="px-4 py-3 text-slate-600">
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        </td>
      </tr>
      <AnimatePresence>
        {expanded && run.report_json?.scrapers && (
          <tr>
            <td colSpan={9} className="p-0">
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="px-6 py-4 bg-white/[0.015] border-b border-white/5">
                  <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 mb-3">Per-Scraper Breakdown</p>
                  <div className="grid grid-cols-2 gap-2">
                    {Object.entries(run.report_json.scrapers as Record<string, any>).map(([sid, r]) => (
                      <div key={sid} className="flex items-start gap-3 p-3 rounded-xl bg-white/5 border border-white/10">
                        <div className="shrink-0 mt-0.5">
                          {r.error ? <AlertCircle className="w-3.5 h-3.5 text-red-400" /> : <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
                        </div>
                        <div className="min-w-0">
                          <p className="text-[10px] font-black text-white uppercase tracking-widest">{sid}</p>
                          {r.error && <p className="text-[9px] text-red-400 mt-0.5 truncate">{r.error}</p>}
                          <div className="flex gap-3 mt-1 flex-wrap">
                            {r.update?.updated != null && (
                              <span className="text-[9px] text-emerald-400 font-mono">↑ {r.update.updated} updated</span>
                            )}
                            {r.upload?.created != null && (
                              <span className="text-[9px] text-primary font-mono">+ {r.upload.created} added</span>
                            )}
                            {r.oos?.deleted != null && (
                              <span className="text-[9px] text-red-400 font-mono">× {r.oos.deleted} removed</span>
                            )}
                            {r.qc?.passed === false && (
                              <span className="text-[9px] text-amber-400 font-mono">⚠ QC failed</span>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </motion.div>
            </td>
          </tr>
        )}
      </AnimatePresence>
    </>
  );
}

// ─── Live Progress Panel ──────────────────────────────────────────────────────
// FIX: each scraper row now shows a mini progress bar in addition to stage chips.
// Completed scrapers (those before currentScraper in the ordered list) show 100%.
function LiveProgressPanel({
  currentScraper,
  activeScrapers,
  progressData,
}: {
  currentScraper: string | null;
  activeScrapers: string[];
  progressData: Record<string, any> | null;
}) {
  const stages = ['Scraping', 'QC', 'Update', 'Upload', 'OOS'];
  const currentIdx = currentScraper ? activeScrapers.indexOf(currentScraper) : -1;

  const getStageForStatus = (status: string): number => {
    const s = (status || '').toLowerCase();
    if (s.includes('oos') || s.includes('delete')) return 4;
    if (s.includes('upload')) return 3;
    if (s.includes('update')) return 2;
    if (s.includes('qc') || s.includes('quality')) return 1;
    return 0;
  };

  return (
    <div className="space-y-2">
      {activeScrapers.slice(0, 12).map((sid, scraperListIdx) => {
        const prog = progressData?.[sid];
        const isActive = sid === currentScraper;
        const isDone = currentIdx >= 0 && scraperListIdx < currentIdx;
        const status = prog?.status ?? '';
        const stageIdx = isActive ? getStageForStatus(status) : isDone ? stages.length : -1;
        const progressPct = isActive
          ? (prog?.progress ?? 0)
          : isDone ? 100 : 0;

        return (
          <div key={sid} className={cn(
            'p-3 rounded-xl border transition-all',
            isActive ? 'border-primary/30 bg-primary/5' : isDone ? 'border-emerald-500/15 bg-emerald-500/5' : 'border-white/5 bg-white/[0.02]'
          )}>
            <div className="flex items-center gap-4 mb-2">
              {/* Scraper name + status */}
              <div className="w-32 shrink-0">
                <div className="flex items-center gap-1.5">
                  {isActive && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse shrink-0" />}
                  {isDone && <CheckCircle2 className="w-3 h-3 text-emerald-400 shrink-0" />}
                  <p className={cn('text-[10px] font-black uppercase tracking-widest truncate',
                    isActive ? 'text-white' : isDone ? 'text-emerald-400' : 'text-slate-600'
                  )}>{sid.replace(/_/g, ' ')}</p>
                </div>
                {isActive && status && (
                  <p className="text-[8px] text-slate-500 mt-0.5 truncate font-mono pl-3">{status}</p>
                )}
              </div>

              {/* Stage chips */}
              <div className="flex items-center gap-1 flex-1">
                {stages.map((stage, i) => (
                  <React.Fragment key={stage}>
                    <div className={cn(
                      'px-1.5 py-0.5 rounded text-[8px] font-black uppercase tracking-widest transition-all whitespace-nowrap',
                      isActive && i === stageIdx ? 'bg-primary text-white animate-pulse' :
                      (isActive && i < stageIdx) || isDone ? 'bg-emerald-500/15 text-emerald-500' :
                      'bg-white/5 text-slate-700'
                    )}>
                      {stage}
                    </div>
                    {i < stages.length - 1 && (
                      <ChevronRight className={cn(
                        'w-2 h-2 shrink-0',
                        (isActive && i < stageIdx) || isDone ? 'text-emerald-500/40' : 'text-white/5'
                      )} />
                    )}
                  </React.Fragment>
                ))}
              </div>

              {/* Progress % */}
              <span className={cn(
                'text-[10px] font-mono shrink-0 tabular-nums',
                isActive ? 'text-primary' : isDone ? 'text-emerald-400' : 'text-slate-700'
              )}>
                {progressPct}%
              </span>
            </div>

            {/* Mini progress bar */}
            <div className="h-1 bg-white/5 rounded-full overflow-hidden">
              <motion.div
                className={cn(
                  'h-full rounded-full bg-gradient-to-r',
                  isActive ? 'from-primary to-secondary' : isDone ? 'from-emerald-500 to-emerald-400' : 'from-white/10 to-white/10'
                )}
                animate={{ width: `${progressPct}%` }}
                transition={{ duration: 0.5, ease: 'easeOut' }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── DO Worker Status Row ─────────────────────────────────────────────────────
function DOWorkerStatusRow({
  lastHeartbeat,
  workerVersion,
  lastTriggeredBy,
}: {
  lastHeartbeat: string | null;
  workerVersion: string | null;
  lastTriggeredBy: string | null;
}) {
  const [, forceRender] = useState(0);

  useEffect(() => {
    const iv = setInterval(() => forceRender(n => n + 1), 30_000);
    return () => clearInterval(iv);
  }, []);

  const workerBadge = (() => {
    if (!lastHeartbeat) {
      return {
        label: 'Unreachable',
        cls: 'text-slate-500 bg-white/5 border-white/10',
        icon: <WifiOff className="w-3 h-3 text-slate-600" />,
      };
    }
    const ageMin = (Date.now() - new Date(lastHeartbeat).getTime()) / 60_000;
    if (ageMin < 40) {
      return {
        label: 'Online',
        cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
        icon: <Wifi className="w-3 h-3 text-emerald-400" />,
      };
    }
    if (ageMin < 90) {
      return {
        label: 'Delayed',
        cls: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
        icon: <Wifi className="w-3 h-3 text-amber-400" />,
      };
    }
    return {
      label: 'Unreachable',
      cls: 'text-red-400 bg-red-500/10 border-red-500/30',
      icon: <WifiOff className="w-3 h-3 text-red-400" />,
    };
  })();

  const triggeredByIsWorker = lastTriggeredBy === 'DigitalOcean worker';

  return (
    <div className="grid grid-cols-2 gap-4">
      {/* DO Worker badge */}
      <div className="glass-card rounded-2xl p-5 border border-white/10 flex items-center gap-4">
        <div className="w-9 h-9 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center shrink-0">
          <Wifi className="w-4 h-4 text-slate-400" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 mb-1">DO Worker Status</p>
          <div className="flex items-center gap-2 flex-wrap">
            <span className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[9px] font-black uppercase tracking-widest',
              workerBadge.cls,
            )}>
              {workerBadge.icon}
              {workerBadge.label}
            </span>
            {workerVersion && (
              <span className="text-[9px] text-slate-600 font-mono">v{workerVersion}</span>
            )}
          </div>
          {lastHeartbeat && (
            <p className="text-[9px] text-slate-600 mt-1.5 font-mono">
              Last ping: {formatIST(lastHeartbeat)}
            </p>
          )}
          {!lastHeartbeat && (
            <p className="text-[9px] text-slate-700 mt-1.5 font-mono">No heartbeat received yet</p>
          )}
        </div>
      </div>

      {/* Last triggered by */}
      <div className="glass-card rounded-2xl p-5 border border-white/10 flex items-center gap-4">
        <div className="w-9 h-9 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center shrink-0">
          <User className="w-4 h-4 text-slate-400" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 mb-1">Last Triggered By</p>
          <div className="flex items-center gap-2">
            <span className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[9px] font-black uppercase tracking-widest',
              triggeredByIsWorker
                ? 'text-sky-400 bg-sky-500/10 border-sky-500/30'
                : 'text-slate-300 bg-white/5 border-white/10',
            )}>
              {triggeredByIsWorker
                ? <Wifi className="w-3 h-3 text-sky-400" />
                : <User className="w-3 h-3 text-slate-400" />}
              {lastTriggeredBy ?? 'Scheduler (internal)'}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────
export default function AutoSyncCenter({
  progressData,
  onToast,
}: {
  progressData: Record<string, any> | null;
  onToast: (type: 'success' | 'error' | 'info' | 'warning', title: string, msg?: string) => void;
}) {
  const [showConfirm, setShowConfirm] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isTriggering, setIsTriggering] = useState(false);

  const { data: status, refetch: refetchStatus } = useQuery<AutoSyncStatus>({
    queryKey: ['auto-sync-status'],
    queryFn: async () => {
      const res = await fetch('/api/auto-sync/status');
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    refetchInterval: 3000,
    retry: false,
  });

  // FIX: fetch last 10 runs (spec says "last 10 runs")
  const { data: historyData, refetch: refetchHistory } = useQuery<{ runs: AutoSyncRun[] }>({
    queryKey: ['auto-sync-history'],
    queryFn: async () => {
      const res = await fetch('/api/auto-sync/history?limit=10');
      if (!res.ok) throw new Error('Offline');
      return res.json();
    },
    refetchInterval: status?.is_running ? 10000 : 30000,
    retry: false,
  });

  const countdown = useCountdown(status?.next_run ?? null);
  const isRunning = status?.is_running ?? false;
  const runs = historyData?.runs ?? [];

  // FIX: independently derive the most recent morning/evening run from history
  // so each schedule card always shows its own last execution status/timestamp
  const morningLastRun = runs.find(r => isSlotMatch(r, 'morning')) ?? null;
  const eveningLastRun = runs.find(r => isSlotMatch(r, 'evening')) ?? null;

  const morningSlot = status?.next_runs?.find(r => r.id === 'auto_sync_morning') ?? null;
  const eveningSlot = status?.next_runs?.find(r => r.id === 'auto_sync_evening') ?? null;
  const activeScrapers = status?.active_scrapers ?? [];

  const totals = status?.last_report?.totals ?? {};

  // FIX: compute Total / Active / Skipped counts from last report's per-scraper results
  const scraperBreakdown = (() => {
    const scrapers = status?.last_report?.scrapers as Record<string, any> | undefined;
    if (!scrapers) return { total: activeScrapers.length, active: 0, skipped: 0 };
    const entries = Object.values(scrapers);
    const total = entries.length || activeScrapers.length;
    const active = entries.filter(r => !r.error && r.scrape !== 'cancelled').length;
    const skipped = total - active;
    return { total, active, skipped };
  })();

  const handleTrigger = useCallback(async () => {
    setShowConfirm(false);
    setIsTriggering(true);
    try {
      const res = await fetch('/api/auto-sync/trigger', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Confirm-Main': CONFIRM_PHRASE,
        },
        body: JSON.stringify({ run_type: 'manual' }),
      });
      const data = await res.json();
      if (!res.ok) {
        onToast('error', 'Trigger Failed', data.error);
      } else {
        onToast('success', 'Auto Sync Started', 'Full sync pipeline running across all MAIN store scrapers.');
        refetchStatus();
        refetchHistory();
      }
    } catch {
      onToast('error', 'Connection Error', 'Flask backend may be offline.');
    } finally {
      setIsTriggering(false);
    }
  }, [onToast, refetchStatus, refetchHistory]);

  const handleCancel = useCallback(async () => {
    setIsCancelling(true);
    try {
      const res = await fetch('/api/auto-sync/cancel', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        onToast('error', 'Cancel Failed', data.error);
      } else {
        onToast('info', 'Cancelling', 'Stop signal sent — sync will halt after current scraper finishes.');
        refetchStatus();
      }
    } catch {
      onToast('error', 'Connection Error', 'Flask backend may be offline.');
    } finally {
      setIsCancelling(false);
    }
  }, [onToast, refetchStatus]);

  return (
    <motion.div
      key="auto-sync"
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -16 }}
      transition={{ duration: 0.25 }}
      className="space-y-8"
    >
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3 mb-2">
            {isRunning && (
              <span className="relative flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500" />
              </span>
            )}
            <h2 className="text-3xl font-black text-white uppercase tracking-tight">Auto Sync Center</h2>
          </div>
          <p className="text-[11px] text-slate-500 font-bold uppercase tracking-[0.2em]">
            {isRunning
              ? `Running — ${status?.current_scraper ?? 'initializing'}…`
              : status?.scheduler_alive
                ? `Scheduler active · Next run in ${countdown}`
                : 'Scheduler offline'}
          </p>
        </div>

        <div className="flex items-center gap-2">
          {isRunning ? (
            <button
              onClick={handleCancel}
              disabled={isCancelling}
              className="flex items-center gap-2 px-5 py-2.5 text-[10px] font-black uppercase tracking-widest border border-red-500/40 text-red-400 hover:bg-red-500/10 rounded-xl transition-all disabled:opacity-50"
            >
              {isCancelling ? <RefreshCw className="w-3 h-3 animate-spin" /> : <XCircle className="w-3.5 h-3.5" />}
              Cancel Sync
            </button>
          ) : (
            <button
              onClick={() => setShowConfirm(true)}
              disabled={isTriggering}
              className="flex items-center gap-2 px-5 py-2.5 text-[10px] font-black uppercase tracking-widest bg-primary hover:bg-primary/90 text-white rounded-xl transition-all active:scale-95 disabled:opacity-50 shadow-lg shadow-primary/20"
            >
              {isTriggering ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              Run Now
            </button>
          )}
        </div>
      </div>

      {/* Schedule + Countdown Row */}
      <div className="grid grid-cols-3 gap-4">
        {/* FIX: pass per-slot last run independently */}
        <ScheduleCard
          slot="morning"
          label="10:00 AM"
          nextRun={morningSlot}
          slotLastRun={morningLastRun}
          isActive={isRunning && isSlotMatch(runs[0] ?? { run_type: '' } as AutoSyncRun, 'morning')}
        />
        <ScheduleCard
          slot="evening"
          label="10:00 PM"
          nextRun={eveningSlot}
          slotLastRun={eveningLastRun}
          isActive={isRunning && isSlotMatch(runs[0] ?? { run_type: '' } as AutoSyncRun, 'evening')}
        />
        {/* Next Run Countdown */}
        <div className="glass-card rounded-2xl p-5 border border-white/10 flex flex-col items-center justify-center text-center">
          <div className="w-10 h-10 rounded-xl bg-primary/10 border border-primary/20 flex items-center justify-center mb-3">
            <Clock className="w-5 h-5 text-primary" />
          </div>
          <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 mb-1">Next Scheduled Run</p>
          <p className="text-2xl font-black text-white font-mono tabular-nums">{countdown}</p>
          {status?.next_run && (
            <p className="text-[10px] text-slate-500 mt-1 font-mono">{formatIST(status.next_run)}</p>
          )}
        </div>
      </div>

      {/* DO Worker + Last Triggered Row */}
      <DOWorkerStatusRow
        lastHeartbeat={status?.do_worker_last_heartbeat ?? null}
        workerVersion={status?.do_worker_version ?? null}
        lastTriggeredBy={status?.last_triggered_by ?? null}
      />

      {/* Last Run Totals */}
      {status?.last_report && (
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: 'Products Updated', value: totals.updated ?? 0, color: 'text-emerald-400', icon: RotateCcw },
            { label: 'Products Added', value: totals.uploaded ?? 0, color: 'text-primary', icon: Activity },
            { label: 'OOS Flagged', value: totals.oos_flagged ?? 0, color: 'text-amber-400', icon: Shield },
            { label: 'Products Removed', value: totals.oos_deleted ?? 0, color: 'text-red-400', icon: Database },
          ].map(({ label, value, color, icon: Icon }) => (
            <div key={label} className="glass-card rounded-2xl p-4 border border-white/10">
              <div className="flex items-center justify-between mb-2">
                <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500 leading-tight">{label}</p>
                <Icon className={cn('w-3.5 h-3.5 shrink-0', color)} />
              </div>
              <p className={cn('text-2xl font-black tabular-nums', color)}>{value.toLocaleString()}</p>
              <p className="text-[9px] text-slate-600 mt-1 font-mono">last run</p>
            </div>
          ))}
        </div>
      )}

      {/* Active Scrapers Widget */}
      {/* FIX: show Total / Active / Skipped counts */}
      <div className="glass-panel rounded-2xl p-6 border border-white/10">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
              <Cpu className="w-4 h-4 text-primary" />
            </div>
            <div>
              <p className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500">Active Scrapers — MAIN Store</p>
              <p className="text-sm font-black text-white">Scraper Detection Results</p>
            </div>
          </div>
          {/* Total / Active / Skipped counts */}
          <div className="flex items-center gap-4 text-[10px] font-mono">
            <div className="text-center">
              <p className="text-slate-500 uppercase tracking-widest text-[8px] font-black">Total</p>
              <p className="text-white font-black text-base">{scraperBreakdown.total}</p>
            </div>
            <div className="h-6 w-px bg-white/10" />
            <div className="text-center">
              <p className="text-emerald-400 uppercase tracking-widest text-[8px] font-black">Active</p>
              <p className="text-emerald-400 font-black text-base">{scraperBreakdown.active}</p>
            </div>
            <div className="h-6 w-px bg-white/10" />
            <div className="text-center">
              <p className="text-slate-500 uppercase tracking-widest text-[8px] font-black">Skipped</p>
              <p className="text-slate-400 font-black text-base">{scraperBreakdown.skipped}</p>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          {activeScrapers.length === 0 ? (
            <p className="text-[11px] text-slate-600 font-mono">No scrapers registered on MAIN store yet.</p>
          ) : (
            activeScrapers.map(sid => {
              const isCurrently = sid === status?.current_scraper;
              return (
                <span key={sid} className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[10px] font-black uppercase tracking-widest transition-all',
                  isCurrently
                    ? 'bg-primary/15 border-primary/40 text-primary'
                    : isRunning
                      ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-400'
                      : 'bg-white/5 border-white/10 text-slate-300'
                )}>
                  {isCurrently && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-ping" />}
                  {sid.replace(/_/g, ' ')}
                </span>
              );
            })
          )}
        </div>
      </div>

      {/* Live Progress Panel — only when running */}
      <AnimatePresence>
        {isRunning && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3 }}
            className="overflow-hidden"
          >
            <div className="glass-panel rounded-2xl p-6 border border-primary/20 bg-primary/5">
              <div className="flex items-center gap-3 mb-5">
                <div className="w-8 h-8 rounded-lg bg-primary/15 border border-primary/30 flex items-center justify-center">
                  <Activity className="w-4 h-4 text-primary" />
                </div>
                <div>
                  <p className="text-[9px] font-black uppercase tracking-[0.2em] text-primary">Live Pipeline</p>
                  <p className="text-sm font-black text-white">
                    Processing: <span className="text-primary">{status?.current_scraper?.replace(/_/g, ' ') ?? 'initializing…'}</span>
                  </p>
                </div>
              </div>
              <LiveProgressPanel
                currentScraper={status?.current_scraper ?? null}
                activeScrapers={activeScrapers}
                progressData={progressData}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Run History Table — last 10 runs */}
      <div className="glass-panel rounded-2xl overflow-hidden border border-white/10">
        <div className="px-6 py-4 border-b border-white/5 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Calendar className="w-4 h-4 text-slate-400" />
            <p className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">Run History</p>
            <span className="text-[9px] text-slate-600 font-mono">last 10 runs</span>
          </div>
          <span className="text-[9px] text-slate-600 font-mono">{runs.length} records</span>
        </div>

        {!runs.length ? (
          <div className="py-16 text-center">
            <div className="w-12 h-12 rounded-2xl bg-white/5 flex items-center justify-center mx-auto mb-4">
              <Clock className="w-6 h-6 text-slate-700" />
            </div>
            <p className="text-[11px] font-black text-slate-600 uppercase tracking-widest">No runs yet</p>
            <p className="text-[10px] text-slate-700 mt-1">Auto-sync fires at 10 AM and 10 PM IST daily.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-white/5">
                  {[
                    'Date (IST)', 'Type', 'Status', 'Scrapers Run',
                    'Products Updated', 'Products Added', 'Products Removed', 'Duration', ''
                  ].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-[9px] font-black uppercase tracking-[0.15em] text-slate-600 whitespace-nowrap">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <AnimatePresence>
                  {runs.map(run => (
                    <HistoryRow key={run.id} run={run} />
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        )}
      </div>

      <AnimatePresence>
        {showConfirm && (
          <TriggerConfirmModal
            open={showConfirm}
            onConfirm={handleTrigger}
            onCancel={() => setShowConfirm(false)}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}
