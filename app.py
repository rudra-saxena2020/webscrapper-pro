import json
from flask import Flask, request, jsonify, send_file, redirect, send_from_directory
from flask_cors import CORS
import os
import threading
import time
from datetime import datetime
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Import our project modules
from core.db import (
    init_db, upsert_all_product_data, update_scrape_record,
    get_scrape_history, start_scrape_record,
    get_latest_scrape_record, get_scraper_stats,
    get_connection, heartbeat_scrape_record, _return_connection,
    init_shopify_tables, get_shopify_logs, get_shopify_logs_count,
    get_shopify_log_stats, log_shopify_action, delete_all_scraper_products,
    approve_product, rework_product, get_qa_products,
    get_active_main_scrapers, start_auto_sync_run, finish_auto_sync_run,
    get_last_auto_sync_run, upsert_oos_pending, get_oos_pending, clear_oos_pending,
    cleanup_stale_sync_runs, save_kv, load_kv,
)
from core.monitor_db import (
    init_monitoring_tables, get_changes, get_changes_count,
    get_monitoring_metrics, get_scraper_monitor_status,
    list_snapshots, mark_notifications_seen, get_unseen_notifications,
)
from core.monitor import (
    run_monitoring_cycle, stop_monitoring_cycle, get_monitor_state,
    take_snapshot_after_sync,
)
from scrapers_run import get_available_scrapers

# Load environment variables
load_dotenv()

# Shopify credentials startup check (optional — log warning, don't crash)
if not (os.getenv("SHOPIFY_STORE_URL", "").strip() and os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()):
    import logging as _log
    _log.getLogger(__name__).warning(
        "⚠️  Shopify secrets not configured (SHOPIFY_STORE_URL / SHOPIFY_ACCESS_TOKEN). "
        "Shopify publish buttons will return 503 until these are set as environment secrets."
    )

import sys
import io

# Fix Windows encoding issues for emojis/unicode
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # Fallback for older python types
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

app = Flask(__name__)
# GLOBAL PERMISSIVE CORS - This fixes the 'Failed to fetch' error in the dashboard
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Silent noisy loggers
for lib in ["httpx", "httpcore", "urllib3", "supabase"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

# Database setup will be handled in the __main__ block

# Track active runs and progress
active_runs = {}
scraper_progress = {}
active_stop_events = {}

# Stop events for Shopify ops (upload / update / delete-oos / nuke)
# Keyed by scraper_id or '__global__' for global ops
shopify_stop_events = {}

# In-memory approval registry for MAIN store promotion
# Scraper IDs added here by POST /api/approve/<id> (requires 100% QC pass)
# Cleared by DELETE /api/approve/<id> or after a successful promote
approved_for_main: set = set()

# Process Lock for Scrapers
active_job_locks = {}

# ── Auto-sync state ───────────────────────────────────────────────────────────
_auto_sync_running = False
_auto_sync_stop_event = threading.Event()
_auto_sync_scheduler = None          # APScheduler BackgroundScheduler instance
_auto_sync_current_scraper: str = None  # scraper currently being processed
_last_triggered_by: str = 'Scheduler (internal)'  # populated by trigger endpoint
_db_tables_ready = threading.Event()  # set after init_shopify_tables() completes

# ── DO worker heartbeat ────────────────────────────────────────────────────────
_do_worker_heartbeat: dict = {
    'last_seen': None,    # ISO-8601 timestamp of last ping
    'version':   None,    # version string sent by the worker
}

# ── Shared sync token (optional) ──────────────────────────────────────────────
_SYNC_TOKEN: str = os.getenv('SYNC_TOKEN', '').strip()

# ── Email notifications ────────────────────────────────────────────────────────
_NOTIFY_EMAIL: str   = os.getenv('NOTIFY_EMAIL', 'studioeditopia@gmail.com').strip()
_SMTP_FROM:    str   = os.getenv('SMTP_FROM_EMAIL', '').strip()
_SMTP_PASS:    str   = os.getenv('SMTP_APP_PASSWORD', '').strip()

def _send_sync_email(status: str, run_type: str, totals: dict, scrapers: dict, error: str = '') -> None:
    """Send a sync completion/failure email. No-op if SMTP creds not configured."""
    if not _SMTP_FROM or not _SMTP_PASS:
        return
    try:
        ist_time = datetime.utcnow().strftime('%d %b %Y %H:%M') + ' UTC'
        emoji = '✅' if status == 'completed' else ('⚠️' if status == 'cancelled' else '❌')
        subject = f"{emoji} Mirage Auto Sync — {status.upper()} ({run_type} · {ist_time})"

        # Build per-scraper rows
        scraper_rows = ''
        for sid, r in (scrapers or {}).items():
            err_cell = f'<td style="color:#f87171">{r.get("error","")}</td>' if r.get('error') else '<td style="color:#34d399">OK</td>'
            upd = r.get('update', {}).get('updated', 0)
            upl = r.get('upload', {}).get('created', 0)
            oos = r.get('oos', {}).get('deleted', 0)
            scraper_rows += (
                f'<tr><td style="padding:4px 12px;font-weight:600">{sid}</td>'
                f'{err_cell}'
                f'<td style="padding:4px 12px;text-align:center">{upd}</td>'
                f'<td style="padding:4px 12px;text-align:center">{upl}</td>'
                f'<td style="padding:4px 12px;text-align:center">{oos}</td></tr>'
            )

        status_color = '#34d399' if status == 'completed' else ('#fbbf24' if status == 'cancelled' else '#f87171')
        html = f"""
<html><body style="font-family:sans-serif;background:#0f0f12;color:#e2e8f0;margin:0;padding:24px">
  <div style="max-width:640px;margin:auto;background:#1a1a24;border-radius:16px;overflow:hidden;border:1px solid #ffffff18">
    <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:24px">
      <h1 style="margin:0;font-size:20px;color:#fff">{emoji} Mirage Auto Sync</h1>
      <p style="margin:6px 0 0;color:#c4b5fd;font-size:13px">{run_type.title()} run · {ist_time}</p>
    </div>
    <div style="padding:24px">
      <div style="display:inline-block;padding:6px 16px;border-radius:20px;background:{status_color}22;border:1px solid {status_color}55;color:{status_color};font-weight:700;font-size:13px;letter-spacing:1px;text-transform:uppercase">{status}</div>
      {'<p style="color:#f87171;margin-top:12px;font-size:13px">⚠ ' + error + '</p>' if error else ''}
      <table style="width:100%;border-collapse:collapse;margin:20px 0;font-size:13px">
        <tr>
          <td style="padding:12px;background:#ffffff08;border-radius:8px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#34d399">{totals.get('updated',0)}</div>
            <div style="color:#94a3b8;font-size:11px;margin-top:4px">UPDATED</div>
          </td>
          <td style="width:12px"></td>
          <td style="padding:12px;background:#ffffff08;border-radius:8px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#818cf8">{totals.get('uploaded',0)}</div>
            <div style="color:#94a3b8;font-size:11px;margin-top:4px">ADDED</div>
          </td>
          <td style="width:12px"></td>
          <td style="padding:12px;background:#ffffff08;border-radius:8px;text-align:center">
            <div style="font-size:24px;font-weight:800;color:#f87171">{totals.get('oos_deleted',0)}</div>
            <div style="color:#94a3b8;font-size:11px;margin-top:4px">REMOVED</div>
          </td>
        </tr>
      </table>
      {'<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="color:#64748b;text-align:left"><th style="padding:4px 12px">Scraper</th><th>Status</th><th style="text-align:center">Updated</th><th style="text-align:center">Added</th><th style="text-align:center">Removed</th></tr></thead><tbody>' + scraper_rows + '</tbody></table>' if scraper_rows else ''}
    </div>
    <div style="padding:16px 24px;border-top:1px solid #ffffff10;color:#475569;font-size:11px">Mirage Scraper Engine — auto-sync notification</div>
  </div>
</body></html>"""

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'Mirage Sync <{_SMTP_FROM}>'
        msg['To']      = _NOTIFY_EMAIL
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as srv:
            srv.login(_SMTP_FROM, _SMTP_PASS)
            srv.sendmail(_SMTP_FROM, [_NOTIFY_EMAIL], msg.as_string())
        logger.info(f'[Email] Sync notification sent → {_NOTIFY_EMAIL}')
    except Exception as e:
        logger.warning(f'[Email] Failed to send notification: {e}')

def perform_scraping(user_email, scraper_ids=None):
    """Background worker that calls the scrapers with active job guard"""
    global scraper_progress

    # Per-sid DB scrape record IDs so every exit path can update the DB status.
    scrape_ids: dict = {}

    try:
        if not scraper_ids: return

        # 2. STATUS INITIALIZATION — create a DB record for each scraper
        for sid in scraper_ids:
            active_job_locks[sid] = True
            _rec_id = start_scrape_record(sid)
            scrape_ids[sid] = _rec_id
            scraper_progress[sid] = {
                'progress': 5,
                'status': 'Initializing Engine...',
                'is_running': True,
                'products_count': 0,
                'stuck': False,
                'last_heartbeat': time.time(),
                '_scrape_id': _rec_id,
            }

        # 3. SHARED STOP EVENT
        batch_stop_event = threading.Event()
        for sid in scraper_ids:
            active_stop_events[sid] = batch_stop_event

        # 4. ENGINE IMPORTS (Only if needed)
        from scrapers.cruise_fashion.cruise_fashion import complete_workflow_cruise_fashion
        from scrapers.coach.coach import complete_workflow_coach
        from scrapers.michael_kors.michael_kors import complete_workflow_michael_kors
        from scrapers.karl.karl import complete_workflow_karl
        from scrapers.marcjacobs.marcjacobs import complete_workflow_marcjacobs
        from scrapers.tory.tory import complete_workflow_tory
        from scrapers.mytheresa.mytheresa import complete_workflow_mytheresa
        from scrapers.thedesignerboxuk.thedesignerboxuk import complete_workflow_thedesignerboxuk
        from scrapers.uk_polene.uk_polene import complete_workflow_uk_polene
        from scrapers.hoka.hoka import complete_workflow_hoka
        from scrapers.drmartens.drmartens import complete_workflow_drmartens
        from scrapers.ugg.ugg import complete_workflow_ugg
        from scrapers.organicbasics.organicbasics import complete_workflow_organicbasics
        from scrapers.skims.skims import complete_workflow_skims
        from scrapers.thereformation.thereformation import complete_workflow_thereformation
        from scrapers.underarmour.underarmour import complete_workflow_underarmour
        from scrapers.stanley.stanley import complete_workflow_stanley1913
        from scrapers.gemopticians.gemopticians import complete_workflow_gemopticians
        from scrapers.katspade_outlet.kateoutlet import complete_workflow_kate_outlet
        from scrapers.jwpei.jwpei import complete_workflow_jwpei

        registry = {
            "cruise_fashion":   complete_workflow_cruise_fashion,
            "coach":            complete_workflow_coach,
            "michael_kors":     complete_workflow_michael_kors,
            "karl":             complete_workflow_karl,
            "marcjacobs":       complete_workflow_marcjacobs,
            "tory":             complete_workflow_tory,
            "mytheresa":        complete_workflow_mytheresa,
            "thedesignerboxuk": complete_workflow_thedesignerboxuk,
            "uk_polene":        complete_workflow_uk_polene,
            "hoka":             complete_workflow_hoka,
            "drmartens":        complete_workflow_drmartens,
            "ugg":              complete_workflow_ugg,
            "organicbasics":    complete_workflow_organicbasics,
            "skims":            complete_workflow_skims,
            "thereformation":   complete_workflow_thereformation,
            "underarmour":      complete_workflow_underarmour,
            "stanley1913":      complete_workflow_stanley1913,
            "gemopticians":     complete_workflow_gemopticians,
            "katespadeoutlet":  complete_workflow_kate_outlet,
            "jwpei":            complete_workflow_jwpei,
        }

        for sid in scraper_ids:
            if sid not in registry:
                logger.warning(f"Unknown scraper: {sid}")
                continue

            scrape_id = scrape_ids.get(sid)

            # 5. UI PROGRESS COUPLING — default-arg binding prevents loop-variable capture
            def progress_cb(*args, _sid=sid, **kwargs):
                if not args: return
                p, s, c = 0, "", 0
                if isinstance(args[0], str) and len(args) > 1:
                    p = args[1] if len(args) > 1 else 0
                    s = args[2] if len(args) > 2 else ""
                    c = args[3] if len(args) > 3 else None
                else:
                    p = args[0] if len(args) > 0 else 0
                    s = args[1] if len(args) > 1 else ""
                    c = args[2] if len(args) > 2 else None

                current = scraper_progress.get(_sid, {})
                scraper_progress[_sid] = {
                    'progress': p if p is not None else 0,
                    'status': str(s) if s is not None else "",
                    'is_running': True,
                    'products_count': c if c is not None else current.get('products_count', 0),
                    'stuck': False,
                    'last_heartbeat': time.time(),
                    '_scrape_id': current.get('_scrape_id'),
                }

            # --- HEARTBEAT SYSTEM ---
            # Updates last_heartbeat on every tick (thread-alive signal).
            # progress_cb also updates last_heartbeat on real work callbacks.
            # Together they ensure the watchdog only fires when both the thread
            # AND real progress callbacks stop completely.
            def heartbeat_loop(stop_evt, _sid=sid):
                while not stop_evt.is_set() and active_job_locks.get(_sid):
                    logger.info(f"[HEARTBEAT] {_sid} still running... (Progress: {scraper_progress.get(_sid, {}).get('progress', 0)}%)")
                    if _sid in scraper_progress:
                        scraper_progress[_sid]['last_heartbeat'] = time.time()
                    time.sleep(15)

            heart_stop = threading.Event()
            threading.Thread(target=heartbeat_loop, args=(heart_stop,), daemon=True).start()

            try:
                # 6. LAUNCH
                logger.info(f"[LAUNCH] Starting {sid}...")
                registry[sid](progress_callback=progress_cb, stop_event=batch_stop_event)

                # Check whether we returned normally or via stop-event
                final_count = scraper_progress.get(sid, {}).get('products_count', 0)
                if batch_stop_event.is_set():
                    # Cancelled by user — do NOT record as completed
                    logger.info(f"[CANCEL] {sid} stopped via stop-event")
                    scraper_progress[sid]['status'] = "Cancelled ⏹"
                    scraper_progress[sid]['is_running'] = False
                    scraper_progress[sid]['stuck'] = False
                    if scrape_id:
                        update_scrape_record(scrape_id, status='failed',
                                             products_count=final_count,
                                             error_message='Job cancelled by user')
                else:
                    # Normal completion — run quality gate then store result
                    scraper_progress[sid]['is_running'] = False
                    scraper_progress[sid]['stuck'] = False
                    qg_full = None
                    try:
                        from core.quality_gate import run_quality_gate as _run_qg
                        scraper_progress[sid]['status'] = 'Running Quality Gate…'
                        _qg_csv = _shopify_csv_path(sid)
                        if _qg_csv:
                            qg_full = _run_qg(sid, _qg_csv)
                            # Store full result in memory (includes products for frontend)
                            scraper_progress[sid]['quality'] = qg_full
                            # Use data_pass_rate (pure product quality) for the status message;
                            # fall back to pass_rate if data_pass_rate is not present (older code path)
                            _pass = qg_full.get('data_pass_rate', qg_full.get('pass_rate', 0))
                            _config_blocked = qg_full.get('config_blocked', False)
                            if _config_blocked:
                                scraper_progress[sid]['status'] = (
                                    f"Completed ✅ — QG {_pass}% (data) | "
                                    "⚠ Shopify creds not set — upload blocked"
                                )
                            else:
                                scraper_progress[sid]['status'] = f"Completed ✅ — QG {_pass}% pass"
                        else:
                            scraper_progress[sid]['status'] = "Completed ✅"
                    except Exception as _qge:
                        logger.warning(f"[QG] Quality gate failed for {sid}: {_qge}")
                        scraper_progress[sid]['status'] = "Completed ✅"
                    if scrape_id:
                        import json as _json
                        # DB: condensed summary (no full products list to keep JSONB size small)
                        _db_qg = None
                        if qg_full:
                            _db_qg = {k: v for k, v in qg_full.items() if k != 'products'}
                            _db_qg['failed_products'] = [
                                {'title': p['title'], 'sku': p['sku'], 'issues': p.get('issues', [])}
                                for p in qg_full.get('products', [])
                                if p.get('severity') == 'error'
                            ]
                        update_scrape_record(scrape_id, status='completed',
                                             products_count=final_count,
                                             quality_report=_json.dumps(_db_qg) if _db_qg else None)

            except Exception as e:
                logger.exception(f"[CRASH] Scraper {sid} failed: {e}")
                scraper_progress[sid] = {
                    'progress': 100,
                    'status': f'Error: {e}',
                    'is_running': False,
                    'stuck': False,
                    '_scrape_id': scrape_id,
                }
                if scrape_id:
                    update_scrape_record(scrape_id, status='failed', error_message=str(e))
            finally:
                heart_stop.set()
                active_job_locks[sid] = False

    except Exception as e:
        logger.error(f"Global worker error: {e}")
    finally:
        # Final safety unlock — covers any path not handled above
        if scraper_ids:
            for sid in scraper_ids:
                active_job_locks[sid] = False
                prog = scraper_progress.get(sid, {})
                if prog.get('is_running'):
                    scraper_progress[sid]['is_running'] = False
                    scraper_progress[sid]['stuck'] = False
                    _rec_id = scrape_ids.get(sid) or prog.get('_scrape_id')
                    if _rec_id:
                        try:
                            update_scrape_record(_rec_id, status='failed',
                                                 error_message='Job terminated unexpectedly')
                        except Exception:
                            pass
                if sid in active_stop_events: del active_stop_events[sid]
                if sid in active_runs: del active_runs[sid]


def _watchdog_stuck_jobs():
    """
    Daemon thread: every 60 s scans scraper_progress for jobs that are still
    marked is_running but have had no heartbeat update for more than 10 minutes.
    Those jobs are automatically transitioned to Stuck, their lock is released,
    and the DB scrape record is marked failed.
    """
    STUCK_TIMEOUT = 600   # 10 minutes without a heartbeat → stuck
    CHECK_INTERVAL = 60   # poll interval in seconds
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            now = time.time()
            for sid, prog in list(scraper_progress.items()):
                if not prog.get('is_running'):
                    continue
                last_hb = prog.get('last_heartbeat')
                if last_hb is None:
                    continue
                elapsed = now - last_hb
                if elapsed > STUCK_TIMEOUT:
                    mins = elapsed / 60
                    logger.warning(f"[WATCHDOG] {sid} stuck — no heartbeat for {mins:.1f} min, marking failed")
                    scraper_progress[sid]['status'] = f'Stuck — no progress for {mins:.0f} min ⚠️'
                    scraper_progress[sid]['is_running'] = False
                    scraper_progress[sid]['stuck'] = True

                    # Signal any running worker to stop before releasing the
                    # lock — prevents an old worker from later overwriting the
                    # stuck status with "Completed ✅" or a DB completed record.
                    if sid in active_stop_events:
                        try:
                            active_stop_events[sid].set()
                        except Exception:
                            pass
                    active_job_locks[sid] = False

                    rec_id = prog.get('_scrape_id')
                    if rec_id:
                        try:
                            update_scrape_record(
                                rec_id, status='failed',
                                error_message=f'Job stuck — no heartbeat for {mins:.0f} min'
                            )
                        except Exception as we:
                            logger.warning(f"[WATCHDOG] DB update failed for {sid}: {we}")
        except Exception as e:
            logger.warning(f"[WATCHDOG] check error: {e}")

@app.route('/api/app-info', methods=['GET'])
def app_info():
    import os
    dev_domain = os.environ.get('REPLIT_DEV_DOMAIN', '')
    dev_url = f'https://{dev_domain}' if dev_domain else ''
    raw_shopify = os.environ.get('SHOPIFY_STORE_URL', '').strip().rstrip('/')
    shopify_domain = raw_shopify.split('://', 1)[-1].split('/')[0]
    return jsonify({'dev_url': dev_url, 'dev_domain': dev_domain, 'shopify_domain': shopify_domain})

@app.route('/api/scrape', methods=['POST'])
def start_scraping():
    logger.info("Received /api/scrape request")
    """
    PHASE 1: GLOBAL JOB LOCK (STRICT)
    Rejected new requests if job is already in progress.
    """
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON body'}), 400
        scraper_ids = data.get('scraper_ids', [])
        user_email = data.get('user_email', 'admin@mirage.com')
        if not isinstance(scraper_ids, list) or not scraper_ids:
            return jsonify({'error': 'scraper_ids must be a non-empty list'}), 400
        
        # Unify lock checks
        running = [sid for sid in scraper_ids if active_job_locks.get(sid)]
        if running:
            logger.warning(f"[AUDIT] Denied concurrent run request for active scrapers: {running}")
            return jsonify({'error': 'Scraper busy - Task in progress', 'scrapers': running}), 429
        
        # 1. State: STARTING
        for sid in scraper_ids:
            active_job_locks[sid] = True
            active_runs[sid] = {'started_at': datetime.now().isoformat()}
            
        threading.Thread(target=perform_scraping, args=(user_email, scraper_ids), daemon=True).start()
        return jsonify({'message': 'Process initiated', 'scrapers': scraper_ids}), 200
    except Exception as e:
        logger.exception(f"❌ [API ERR] Start Scrape Failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress', methods=['GET'])
def get_progress():
    return jsonify(scraper_progress)

@app.route('/api/scrapers', methods=['GET', 'OPTIONS'])
def get_scrapers():
    logger.info("Received /api/scrapers request")
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200

    import concurrent.futures

    CURRENCY_MAP = {
        'coach':            'USD',
        'cruise_fashion':   'GBP',
        'michael_kors':     'INR',
        'karl':             'GBP',
        'marcjacobs':       'USD',
        'tory':             'USD',
        'gymshark':         'USD',
        'aloyoga':          'USD',
        'mytheresa':        'EUR',
        'thedesignerboxuk': 'GBP',
        'uk_polene':        'GBP',
        'hoka':             'USD',
        'drmartens':        'USD',
        'ugg':              'USD',
        'organicbasics':    'GBP',
        'skims':            'USD',
        'thereformation':   'USD',
        'underarmour':      'USD',
        'stanley1913':      'USD',
        'gemopticians':     'INR',
        'katespadeoutlet':  'USD',
        'jwpei':            'USD',
    }

    def _fetch_scraper_data(sid, name):
        try:
            stats = get_scraper_stats(sid)
            latest = get_latest_scrape_record(sid)
            latest_csv = get_latest_scrape_record(sid, must_have_csv=True)
            local_csv = os.path.join("scraped_files", f"{sid}_latest.csv")
            csv_available = os.path.exists(local_csv)
            return {
                'id': sid,
                'name': name,
                'display_name': name,
                'currency': CURRENCY_MAP.get(sid, 'GBP'),
                'total_products': int(stats.get('total_products', 0)),
                'last_updated': stats.get('updated_at', 'Never'),
                'last_csv_url': latest_csv.get('csv_url') if latest_csv else None,
                'csv_available': csv_available,
                'status': latest.get('status', 'idle') if latest else 'idle'
            }
        except Exception as e:
            logger.warning(f"DB fetch failed for {sid}: {e}")
            local_csv = os.path.join("scraped_files", f"{sid}_latest.csv")
            return {
                'id': sid,
                'name': name,
                'display_name': name,
                'currency': CURRENCY_MAP.get(sid, 'GBP'),
                'total_products': 0,
                'last_updated': 'DB Unavailable',
                'last_csv_url': None,
                'csv_available': os.path.exists(local_csv),
                'status': 'idle'
            }

    try:
        available = get_available_scrapers()
        scrapers_list = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_fetch_scraper_data, sid, name): sid
                for sid, (name, _) in available.items()
            }
            # Increased timeout to 20s to handle cold Supabase connections
            try:
                for future in concurrent.futures.as_completed(futures, timeout=20):
                    try:
                        scrapers_list.append(future.result(timeout=20))
                    except Exception as e:
                        sid = futures[future]
                        logger.error(f"❌ [DB TIMEOUT] Future result failed for {sid}: {e}")
                        scrapers_list.append({
                            'id': sid, 'name': sid, 'display_name': sid,
                            'total_products': 0, 'last_updated': 'Timeout',
                            'last_csv_url': None, 'status': 'idle'
                        })
            except concurrent.futures.TimeoutError:
                logger.error("❌ [CRITICAL] Stats fetch timed out (20s) - some scrapers might be missing")

        # Build summary stats
        total_products = sum(s.get('total_products', 0) for s in scrapers_list)
        last_updated_vals = [s['last_updated'] for s in scrapers_list if s.get('last_updated') and s['last_updated'] not in ('Never', 'Error', 'Timeout', 'DB Unavailable')]
        last_updated = max(last_updated_vals) if last_updated_vals else 'Never'

        try:
            conn, cur = get_connection()
            cur.execute("SELECT COUNT(*) as cnt FROM scrapes WHERE status = 'completed'")
            row = cur.fetchone()
            total_scrapes = int(row['cnt']) if row else 0
            _return_connection(conn)
        except Exception:
            total_scrapes = 0

        summary = {
            'total_products': total_products,
            'total_scrapes': total_scrapes,
            'last_updated': last_updated,
        }

        return jsonify({'scrapers': scrapers_list, 'summary': summary}), 200
    except Exception as e:
        logger.error(f"Stats fetch error: {e}")
        available = get_available_scrapers()
        fallback = [
            {'id': sid, 'name': name, 'display_name': name, 'currency': CURRENCY_MAP.get(sid, 'GBP'),
             'total_products': 0, 'last_updated': 'Error', 'status': 'idle', 'csv_available': False}
            for sid, (name, _) in available.items()
        ]
        return jsonify({'scrapers': fallback, 'summary': {'total_products': 0, 'total_scrapes': 0, 'last_updated': 'Never'}}), 200

@app.route('/api/history', methods=['GET'])
@app.route('/api/scrape-history', methods=['GET'])
def get_history():
    return jsonify(get_scrape_history(limit=50)), 200

@app.route('/api/stats/clear', methods=['POST'])
def clear_stats():
    """Reset in-memory progress state for all scrapers (does not delete DB records)."""
    global scraper_progress, active_runs, active_job_locks
    scraper_progress = {}
    active_runs = {}
    active_job_locks = {}
    logger.info("Stats cleared via API")
    return jsonify({'message': 'Stats cleared'}), 200

@app.route('/api/scrapers/<scraper_id>/delete-products', methods=['POST'])
def delete_scraper_products(scraper_id):
    """Deletes all products and Shopify registry entries for a specific scraper."""
    logger.info(f"Received request to delete all products for scraper: {scraper_id}")
    success = delete_all_scraper_products(scraper_id)
    if success:
        return jsonify({'message': f'All products for {scraper_id} deleted successfully'}), 200
    else:
        return jsonify({'error': 'Failed to delete products'}), 500

@app.route('/api/scrapers/add', methods=['POST'])
def add_scraper():
    """Register a new scraper website in the registry JSON."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400
    name = data.get('name', '').strip()
    url = data.get('url', '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url are required'}), 400

    import re
    scraper_id = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

    registry_path = os.path.join(os.path.dirname(__file__), "scrapers_registry.json")
    try:
        existing = []
        if os.path.exists(registry_path):
            with open(registry_path, 'r') as f:
                existing = json.load(f)

        if any(s.get('id') == scraper_id for s in existing):
            return jsonify({'error': f'Scraper "{scraper_id}" already exists'}), 409

        existing.append({'id': scraper_id, 'name': name, 'base_url': url, 'type': 'cruise_fashion'})
        with open(registry_path, 'w') as f:
            json.dump(existing, f, indent=2)

        logger.info(f"Registered new scraper: {scraper_id} → {url}")
        return jsonify({'message': f'Scraper "{name}" registered', 'id': scraper_id}), 201
    except Exception as e:
        logger.exception(f"Failed to register scraper: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<scraper_id>', methods=['GET'])
def download_latest_csv(scraper_id):
    from core.shopify_transformer import fix_csv_inventory_rows
    local_path = os.path.join("scraped_files", f"{scraper_id}_latest.csv")
    if os.path.exists(local_path):
        # Always repair inventory rows before serving so the downloaded CSV
        # is guaranteed to pass Shopify's strict importer validation.
        fix_csv_inventory_rows(local_path)
        download_name = f"{scraper_id}_products_shopify.csv"
        response = send_file(
            local_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="text/csv",
        )
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return response
    # Fall back to exports/ then Supabase URL
    export_path = os.path.join("exports", f"{scraper_id}_shopify.csv")
    if os.path.exists(export_path):
        fix_csv_inventory_rows(export_path)
        download_name = f"{scraper_id}_products_shopify.csv"
        response = send_file(export_path, as_attachment=True, download_name=download_name, mimetype="text/csv")
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return response
    latest = get_latest_scrape_record(scraper_id, must_have_csv=True)
    if latest and latest.get('csv_url'):
        return redirect(latest['csv_url'])
    return jsonify({'error': 'No CSV available'}), 404

@app.route('/api/tags', methods=['GET'])
def export_unique_tags():
    """Return all unique tags across all available scraper CSVs."""
    import csv as csv_mod
    all_tags: set = set()
    for sid in SHOPIFY_ALL_SCRAPERS:
        for csv_path in [
            os.path.join("scraped_files", f"{sid}_latest.csv"),
            os.path.join("exports", f"{sid}_shopify.csv"),
        ]:
            if os.path.exists(csv_path):
                try:
                    with open(csv_path, encoding='utf-8-sig', errors='replace') as f:
                        reader = csv_mod.DictReader(f)
                        for row in reader:
                            tags_val = row.get('Tags', '') or ''
                            for tag in tags_val.split(','):
                                t = tag.strip()
                                if t:
                                    all_tags.add(t)
                except Exception:
                    pass
                break
    sorted_tags = sorted(all_tags, key=str.lower)
    return jsonify({'tags': sorted_tags, 'total': len(sorted_tags)})


@app.route('/api/scrape/cancel/<scraper_id>', methods=['POST'])
def cancel_scraping(scraper_id):
    if scraper_id in active_stop_events:
        active_stop_events[scraper_id].set()
        return jsonify({'message': 'Cancellation signal sent'}), 200
    return jsonify({'error': 'Not running'}), 404

# ── Shopify Admin API routes ──────────────────────────────────────────────────

def _shopify_csv_path(scraper_id: str) -> str:
    """
    Return the best available local CSV path for a scraper.
    Priority: scraped_files/{id}_latest.csv first (always freshest data from the scraper),
              then exports/{id}_shopify.csv as a fallback if latest is absent.
    """
    for path in [
        os.path.join("scraped_files", f"{scraper_id}_latest.csv"),
        os.path.join("exports", f"{scraper_id}_shopify.csv"),
    ]:
        if os.path.exists(path):
            return path
    return None

def _shopify_progress_emit(scraper_id: str, pct: int, status: str, count: int,
                          result: dict = None, shopify_op: str = None, counts: dict = None,
                          quota_sleeping: bool = False, quota_resume_at: str = None):
    data = {
        'progress': pct,
        'status': status,
        'is_running': pct < 100,
        'products_count': count,
        'quota_sleeping': quota_sleeping,
    }
    if quota_resume_at is not None:
        data['quota_resume_at'] = quota_resume_at
    if result is not None:
        data['shopify_result'] = result
    if shopify_op is not None:
        data['shopify_op'] = shopify_op
    if counts is not None:
        data['shopify_counts'] = counts
        # Flat field contract required by task spec — keyed by op type for clarity
        data['shopify_uploaded_count'] = counts.get('created', 0)
        data['shopify_updated_count']  = counts.get('updated', 0)
        data['shopify_deleted_count']  = counts.get('deleted', 0)
        data['shopify_skipped_count']  = counts.get('skipped', 0)
        data['shopify_failed_count']   = counts.get('failed', 0)
        if 'estimated_variants' in counts:
            data['estimated_variants'] = counts['estimated_variants']
    scraper_progress[scraper_id] = data

def _shopify_stop_register(scraper_id: str) -> threading.Event:
    """Create and register a fresh stop event for a Shopify op; cancel any previous one."""
    ev = threading.Event()
    old = shopify_stop_events.get(scraper_id)
    if old:
        old.set()  # cancel any lingering op
    shopify_stop_events[scraper_id] = ev
    return ev

def _shopify_stop_clear(scraper_id: str):
    shopify_stop_events.pop(scraper_id, None)


# ── Multi-store helpers ────────────────────────────────────────────────────────

def _get_request_store_key() -> str:
    """Read the X-Store-Key header from the current Flask request (default: 'test')."""
    key = request.headers.get('X-Store-Key', 'test').strip().lower()
    return key if key in ('test', 'main') else 'test'


def _has_main_confirmation() -> bool:
    """Check that the caller sent the required double-confirmation header for MAIN STORE writes."""
    return request.headers.get('X-Confirm-Main', '').strip() == 'CONFIRM MAIN STORE ACTION'


def _check_main_write(store_key: str):
    """
    For MAIN store write operations: enforce confirmation header.
    Returns (True, None) if allowed, (False, error_response) if blocked.
    """
    if store_key != 'main':
        return True, None
    if not _has_main_confirmation():
        return False, (jsonify({'error': 'MAIN STORE write requires X-Confirm-Main: CONFIRM MAIN STORE ACTION header.'}), 403)
    return True, None


def _get_store_info() -> dict:
    """Return which stores are configured (without exposing credentials)."""
    test_configured = bool(
        (os.getenv("TEST_SHOPIFY_STORE_URL") or os.getenv("SHOPIFY_STORE_URL", "")).strip()
        and (os.getenv("TEST_SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_ACCESS_TOKEN", "")).strip()
    )
    main_configured = bool(
        os.getenv("MAIN_SHOPIFY_STORE_URL", "").strip()
        and os.getenv("MAIN_SHOPIFY_ACCESS_TOKEN", "").strip()
    )

    def _domain(url_key, fallback_key=None):
        raw = os.getenv(url_key, "") or (os.getenv(fallback_key, "") if fallback_key else "")
        raw = raw.strip().rstrip("/")
        if not raw:
            return None
        raw = raw.split("://", 1)[-1]
        return raw.split("/")[0]

    return {
        'test': {
            'configured': test_configured,
            'domain': _domain("TEST_SHOPIFY_STORE_URL", "SHOPIFY_STORE_URL"),
        },
        'main': {
            'configured': main_configured,
            'domain': _domain("MAIN_SHOPIFY_STORE_URL"),
        },
    }


@app.route('/api/store', methods=['GET'])
def get_store_info():
    """Return configured store status (no credentials exposed)."""
    return jsonify(_get_store_info()), 200


@app.route('/api/shopify/cancel/<scraper_id>', methods=['POST'])
def shopify_cancel(scraper_id):
    """Signal an active Shopify op (upload/update/delete-oos/nuke) to stop cleanly."""
    ev = shopify_stop_events.get(scraper_id)
    if ev and not ev.is_set():
        ev.set()
        _shopify_progress_emit(scraper_id, scraper_progress.get(scraper_id, {}).get('progress', 50),
                               'Cancelling… finishing current batch', 0)
        return jsonify({'message': 'Shopify op cancellation signal sent'}), 200
    return jsonify({'error': 'No active Shopify op for this scraper'}), 404


@app.route('/api/shopify/upload/<scraper_id>', methods=['POST'])
def shopify_upload(scraper_id):
    """Upload new products from local CSV to Shopify (skips existing SKUs/handles)."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err

    # MAIN STORE: block upload unless quality gate is 100 %
    if store_key == 'main':
        try:
            from core.quality_gate import run_quality_gate
            _qg_csv = _shopify_csv_path(scraper_id)
            qg = run_quality_gate(scraper_id, _qg_csv)
            pass_rate = qg.get('pass_rate', 0)
            if pass_rate < 100:
                return jsonify({
                    'error': (
                        f'MAIN STORE blocked: quality gate {pass_rate:.1f}% < 100%. '
                        'Fix all issues first, then promote to MAIN.'
                    ),
                    'quality': qg,
                }), 403
        except Exception as _qge:
            logger.warning(f"[QG] Could not check quality gate for {scraper_id}: {_qge}")

    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}. Run the scraper first.'}), 404
    try:
        from core.shopify_publisher import upload_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Shopify Upload: Fetching existing products…', 0,
                              shopify_op='upload',
                              counts={'created': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            upload_done = threading.Event()

            def _quota_heartbeat():
                """Fires every 20 s; emits quota-sleep state to frontend when sleeping."""
                while True:
                    if upload_done.wait(timeout=20.0):
                        break  # Upload finished — stop heartbeat
                    if stop_ev.is_set():
                        break  # A newer op cancelled this upload — stop heartbeat
                    # Re-import each tick to read current module-level values
                    import core.shopify_publisher as _pub
                    _ra = _pub._daily_limit_resume_at.get(store_key)
                    _rts = _pub._daily_limit_resume_ts_unix.get(store_key)
                    if _ra and _rts:
                        remaining = max(0.0, _rts - time.time())
                        mins = int(remaining // 60)
                        secs_r = int(remaining % 60)
                        cur = scraper_progress.get(scraper_id, {})
                        c = cur.get('shopify_uploaded_count', 0)
                        _shopify_progress_emit(
                            scraper_id,
                            cur.get('progress', 50),
                            f'Shopify Upload: ⏸ Quota sleep — resumes at {_ra} (in {mins}m {secs_r}s) | {c} created',
                            c,
                            shopify_op='upload',
                            counts=cur.get('shopify_counts') or {},
                            quota_sleeping=True,
                            quota_resume_at=_ra,
                        )

            threading.Thread(target=_quota_heartbeat, daemon=True).start()

            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'Shopify Upload: {status}', count,
                                          shopify_op='upload', counts=counts)
                result = upload_products(scraper_id, csv_path, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                status_msg = (
                    f"Upload {'stopped' if stopped else 'complete'} — "
                    f"{result['created']} created, {result['skipped']} skipped, {result['failed']} failed"
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['created'], result,
                                      shopify_op='upload',
                                      counts={'created': result['created'], 'skipped': result['skipped'],
                                              'failed': result['failed'], 'total': result['created'] + result['skipped'] + result['failed'],
                                              'processed': result['created'] + result['skipped'] + result['failed']})
            except Exception as e:
                logger.exception(f"[Upload] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Upload failed: {e}', 0, shopify_op='upload',
                                      counts={'created': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                upload_done.set()
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Upload started', 'csv': csv_path}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify upload error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/update/<scraper_id>', methods=['POST'])
def shopify_update(scraper_id):
    """Update title/body/price/images for existing Shopify products matched by SKU."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import update_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Shopify Update: Fetching existing products…', 0,
                              shopify_op='update',
                              counts={'updated': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            update_done = threading.Event()

            def _quota_heartbeat():
                """Fires every 20 s; emits quota-sleep state to frontend when sleeping."""
                while True:
                    if update_done.wait(timeout=20.0):
                        break  # Update finished — stop heartbeat
                    if stop_ev.is_set():
                        break  # A newer op cancelled this update — stop heartbeat
                    import core.shopify_publisher as _pub
                    _ra = _pub._daily_limit_resume_at.get(store_key)
                    _rts = _pub._daily_limit_resume_ts_unix.get(store_key)
                    if _ra and _rts:
                        remaining = max(0.0, _rts - time.time())
                        mins = int(remaining // 60)
                        secs_r = int(remaining % 60)
                        cur = scraper_progress.get(scraper_id, {})
                        c = cur.get('shopify_updated_count', 0)
                        _shopify_progress_emit(
                            scraper_id,
                            cur.get('progress', 10),
                            f'Shopify Update: ⏸ Quota sleep — resumes at {_ra} (in {mins}m {secs_r}s) | {c} updated',
                            c,
                            shopify_op='update',
                            counts=cur.get('shopify_counts') or {},
                            quota_sleeping=True,
                            quota_resume_at=_ra,
                        )

            threading.Thread(target=_quota_heartbeat, daemon=True).start()

            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'Shopify Update: {status}', count,
                                          shopify_op='update', counts=counts)
                result = update_products(scraper_id, csv_path, progress_callback=cb, stop_event=stop_ev, skip_images=True, add_tag_if_missing=True)
                stopped = stop_ev.is_set()
                ilf = result.get('image_link_failures', 0)
                status_msg = (
                    f"Update {'stopped' if stopped else 'complete'} — "
                    f"{result['updated']} updated, {result['skipped']} skipped, {result['failed']} failed"
                    + (f", {ilf} image-link failure(s)" if ilf else "")
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['updated'], result,
                                      shopify_op='update',
                                      counts={'updated': result['updated'], 'skipped': result['skipped'],
                                              'failed': result['failed'], 'total': result['updated'] + result['skipped'] + result['failed'],
                                              'processed': result['updated'] + result['skipped'] + result['failed']})
            except Exception as e:
                logger.exception(f"[Update] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Update failed: {e}', 0, shopify_op='update',
                                      counts={'updated': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                update_done.set()
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Update started', 'csv': csv_path}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify update error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/fix-tags/<scraper_id>', methods=['POST'])
def shopify_fix_tags(scraper_id):
    """
    Scan the MAIN Shopify store for products tagged rudrascrapper-{scraper_id}
    and fix on-the-fly:
      - 'women' tag → 'womens', 'men' tag → 'mens'
      - Missing product_type inferred from tags (marcjacobs taxonomy)
    Stops after 100 consecutive pages with 0 matching products.
    Progress tracked in scraper_progress under key '{scraper_id}__fixtags'.
    Requires X-Store-Key: main + X-Confirm-Main: CONFIRM MAIN STORE ACTION.
    """
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err

    import re as _re

    def _infer_type(tags_list, title):
        t = [x.strip().lower() for x in tags_list]
        tl = title.lower()
        # Check specific store taxonomy tags first (higher priority than generic catch-alls)
        bag_tags = (
            'womens-handbags', 'mens-bags', 'womens-shoulderbags', 'womens-totebags',
            'womens-crossbodybag', 'womens-minibag', 'mens-shoulderbags', 'mens-totebags',
        )
        if any(x in t for x in bag_tags):
            return 'Wallet' if 'wallet' in tl else 'Bag'
        if any(x in t for x in ('womens-jewellery', 'mens-jewellery', 'jewellery', 'jewelry')):
            return 'Jewelry'
        if any(x in t for x in ('womens-smallaccessories', 'mens-smallaccessories')):
            return 'Wallet' if 'wallet' in tl else 'Accessories'
        # Footwear/apparel checked after specific taxonomy tags
        if any(x in t for x in ('womens-footwear', 'mens-footwear')):
            return 'Shoes'
        if any(x in t for x in ('womens-apparel', 'mens-apparel')):
            return 'Apparel'
        # Generic fallbacks (lowest priority — may appear on non-apparel products)
        if 'footwear' in t:
            return 'Shoes'
        if 'apparel' in t:
            return 'Apparel'
        return 'Accessories'

    def _fix_gender_tags(tags_str):
        parts = [p.strip() for p in tags_str.split(',') if p.strip()]
        fixed = []
        for p in parts:
            pl = p.lower()
            if pl == 'women':
                fixed.append('womens')
            elif pl == 'men':
                fixed.append('mens')
            else:
                fixed.append(p)
        return ', '.join(fixed)

    def _next_url(link_header):
        if not link_header:
            return None
        m = _re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        return m.group(1) if m else None

    progress_key = f'{scraper_id}__fixtags'
    _shopify_progress_emit(progress_key, 1, 'Fix-tags: starting scan…', 0, shopify_op='fix_tags',
                           counts={'updated': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

    def _run():
        from core.shopify_publisher import _set_store_key, _shopify_request
        _set_store_key('main')
        tag_filter = f'rudrascrapper-{scraper_id}'
        updated = skipped = failed = pages = 0
        zero_streak = 0
        MAX_ZERO = 100
        RATE_GAP = 0.55
        url = None
        params = {"limit": 250, "fields": "id,title,product_type,tags",
                  "tag": tag_filter}
        while True:
            try:
                import time as _time
                data = _shopify_request('GET', url or '/products.json',
                                        params=params if not url else None)
                # _shopify_request strips headers; re-fetch link via requests for pagination
                from core.shopify_publisher import _get_credentials
                import requests as _req
                store_url, token = _get_credentials()
                _base = f"https://{store_url}/admin/api/2025-01"
                _headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
                _url = url if url else f"{_base}/products.json"
                _r = _req.get(_url, headers=_headers,
                              params=params if not url else None, timeout=30)
                link_header = _r.headers.get('Link', '')
                data = _r.json()
            except Exception as e:
                logger.error(f"[fix-tags:{scraper_id}] Fetch error: {e}")
                break

            _time.sleep(RATE_GAP)
            batch = data.get('products', [])
            pages += 1
            if not batch:
                logger.info(f"[fix-tags:{scraper_id}] Page {pages}: empty — store exhausted")
                break

            matching = [p for p in batch
                        if tag_filter in (p.get('tags') or '').lower()]

            if matching:
                zero_streak = 0
                for p in matching:
                    pid = p['id']
                    title = p.get('title', '')
                    tags_str = p.get('tags', '')
                    ptype = (p.get('product_type') or '').strip()
                    new_tags = _fix_gender_tags(tags_str)
                    tag_list = [t.strip() for t in new_tags.split(',') if t.strip()]
                    new_type = ptype if ptype else _infer_type(tag_list, title)
                    if new_tags == tags_str and new_type == ptype:
                        skipped += 1
                        continue
                    try:
                        _shopify_request('PUT', f'/products/{pid}.json',
                                         payload={"product": {"id": pid,
                                                              "tags": new_tags,
                                                              "product_type": new_type}})
                        updated += 1
                        logger.info(f"[fix-tags:{scraper_id}] FIXED [{updated}] {title[:45]!r}"
                                    f"  gender_fix={new_tags!=tags_str}  type:{ptype!r}→{new_type!r}")
                    except Exception as e:
                        failed += 1
                        logger.error(f"[fix-tags:{scraper_id}] FAIL  {title[:45]!r}: {e}")
                    pct = min(99, 1 + int(updated / max(1, updated + skipped + failed) * 90))
                    _shopify_progress_emit(progress_key, pct,
                                           f'Fix-tags: {updated} fixed, {skipped} ok, {pages} pages…',
                                           updated, shopify_op='fix_tags',
                                           counts={'updated': updated, 'skipped': skipped,
                                                   'failed': failed,
                                                   'total': updated + skipped + failed,
                                                   'processed': updated + skipped + failed})
            else:
                zero_streak += 1
                if zero_streak >= MAX_ZERO:
                    logger.info(f"[fix-tags:{scraper_id}] {zero_streak} zero pages — scan complete")
                    break

            nxt = _next_url(link_header)
            if not nxt:
                logger.info(f"[fix-tags:{scraper_id}] Page {pages}: no next page — scan complete")
                break
            url = nxt

        msg = (f"Fix-tags complete — {updated} fixed, {skipped} already-correct, "
               f"{failed} failed, {pages} pages scanned")
        logger.info(f"[fix-tags:{scraper_id}] === DONE: {msg} ===")
        _shopify_progress_emit(progress_key, 100, msg, updated, shopify_op='fix_tags',
                               counts={'updated': updated, 'skipped': skipped,
                                       'failed': failed,
                                       'total': updated + skipped + failed,
                                       'processed': updated + skipped + failed})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'message': f'Fix-tags scan started for {scraper_id} on MAIN store',
                    'progress_key': progress_key}), 202


@app.route('/api/shopify/update-images/<scraper_id>', methods=['POST'])
def shopify_update_images(scraper_id):
    """Re-link colour variant images for all existing products without re-uploading."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import reimage_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Shopify Fix Images: Loading products…', 0,
                              shopify_op='reimage',
                              counts={'reimaged': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'Shopify Fix Images: {status}', count,
                                          shopify_op='reimage', counts=counts)
                result = reimage_products(scraper_id, csv_path, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                ilf = result.get('image_link_failures', 0)
                status_msg = (
                    f"Fix Images {'stopped' if stopped else 'complete'} — "
                    f"{result['reimaged']} fixed, {result['skipped']} skipped, {result['failed']} failed"
                    + (f", {ilf} image-link failure(s)" if ilf else "")
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['reimaged'], result,
                                      shopify_op='reimage',
                                      counts={'reimaged': result['reimaged'], 'skipped': result['skipped'],
                                              'failed': result['failed'],
                                              'total': result['reimaged'] + result['skipped'] + result['failed'],
                                              'processed': result['reimaged'] + result['skipped'] + result['failed']})
            except Exception as e:
                logger.exception(f"[Reimage] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Fix Images failed: {e}', 0, shopify_op='reimage',
                                      counts={'reimaged': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Fix Images started', 'csv': csv_path}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify fix-images error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/fix-products/<scraper_id>', methods=['POST'])
def shopify_fix_products(scraper_id):
    """
    Comprehensive product fixer for MAIN store — uses CSV as source of truth.
    Scans newest-first (so recently-uploaded batches are fixed immediately).
    Fixes per product:
      - product_type: normalized from CSV handle→type map (Handbag→Bag etc.)
      - Gender tags: 'women'→'womens', 'men'→'mens'
      - Broad category tag: adds 'bags'/'accessories'/'apparel'/'footwear' if missing
    Requires X-Store-Key: main + X-Confirm-Main: CONFIRM MAIN STORE ACTION.
    """
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err

    import re as _re
    import pandas as _pd

    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404

    # Build handle→type map from CSV (lowercase handles for matching)
    try:
        _df = _pd.read_csv(csv_path, low_memory=False)
        _first = _df[~_df.duplicated(subset='Handle', keep='first')].copy()
        _handle_type_map = {
            str(h).strip().lower(): str(t).strip()
            for h, t in zip(_first['Handle'], _first['Type'])
            if str(t).strip() and str(t).strip().lower() not in ('nan', '')
        }
    except Exception as e:
        return jsonify({'error': f'CSV read error: {e}'}), 500

    _BROAD_CATEGORIES = {'bags', 'accessories', 'apparel', 'footwear', 'watches', 'jewelry'}
    _TYPE_TO_BROAD = {
        'Bag': 'bags', 'Shoes': 'footwear', 'Apparel': 'apparel',
        'Jewelry': 'accessories', 'Accessories': 'accessories', 'Wallet': 'accessories',
    }

    def _fix_gender_tags(tags_str):
        parts = [p.strip() for p in tags_str.split(',') if p.strip()]
        return ', '.join(
            'womens' if p.lower() == 'women' else 'mens' if p.lower() == 'men' else p
            for p in parts
        )

    def _ensure_broad(tags_str, correct_type):
        parts = [p.strip() for p in tags_str.split(',') if p.strip()]
        lower_parts = {p.lower() for p in parts}
        if lower_parts & _BROAD_CATEGORIES:
            return tags_str  # already has one
        broad = _TYPE_TO_BROAD.get(correct_type, 'accessories')
        parts.append(broad)
        return ', '.join(parts)

    def _next_url(link_header):
        m = _re.search(r'<([^>]+)>;\s*rel="next"', link_header or '')
        return m.group(1) if m else None

    progress_key = f'{scraper_id}__fixproducts'
    _shopify_progress_emit(progress_key, 1, 'Fix-products: starting scan (newest first)…', 0,
                           shopify_op='fix_products',
                           counts={'updated': 0, 'skipped': 0, 'failed': 0,
                                   'total': 0, 'processed': 0})

    def _run():
        from core.shopify_publisher import _set_store_key, _get_credentials
        import requests as _req, time as _time
        _set_store_key('main')

        store_url, token = _get_credentials()
        _base = f"https://{store_url}/admin/api/2025-01"
        _hdrs = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        tag_filter = f'rudrascrapper-{scraper_id}'
        RATE_GAP = 0.55
        MAX_ZERO = 100
        updated = skipped = failed = pages = zero_streak = 0
        url = None
        params = {
            "limit": 250,
            "fields": "id,handle,product_type,tags",
            "tag": tag_filter,
            "order": "created_at desc",   # newest first → find just-uploaded batch quickly
        }

        while True:
            _time.sleep(RATE_GAP)
            try:
                _r = _req.get(url if url else f"{_base}/products.json",
                              headers=_hdrs,
                              params=params if not url else None,
                              timeout=30)
                if _r.status_code == 429:
                    _time.sleep(float(_r.headers.get('Retry-After', 4)))
                    continue
                _r.raise_for_status()
                data = _r.json()
                link_header = _r.headers.get('Link', '')
            except Exception as e:
                logger.error(f"[fix-products:{scraper_id}] Fetch error: {e}")
                break

            batch = data.get('products', [])
            pages += 1
            if not batch:
                logger.info(f"[fix-products:{scraper_id}] Page {pages}: empty — store exhausted")
                break

            matching = [p for p in batch if tag_filter in (p.get('tags') or '').lower()]

            if matching:
                zero_streak = 0
                for p in matching:
                    pid          = p['id']
                    handle       = (p.get('handle') or '').strip().lower()
                    cur_type     = (p.get('product_type') or '').strip()
                    cur_tags     = (p.get('tags') or '').strip()

                    # Determine correct type from CSV map
                    csv_type = _handle_type_map.get(handle, '')
                    new_type = csv_type if csv_type else cur_type
                    new_tags = _fix_gender_tags(cur_tags)
                    new_tags = _ensure_broad(new_tags, new_type)

                    if new_type == cur_type and new_tags == cur_tags:
                        skipped += 1
                        continue

                    try:
                        _time.sleep(RATE_GAP)
                        r2 = _req.put(f"{_base}/products/{pid}.json",
                                      headers=_hdrs,
                                      json={"product": {"id": pid,
                                                        "product_type": new_type,
                                                        "tags": new_tags}},
                                      timeout=30)
                        if r2.status_code == 429:
                            _time.sleep(float(r2.headers.get('Retry-After', 4)))
                            r2 = _req.put(f"{_base}/products/{pid}.json",
                                          headers=_hdrs,
                                          json={"product": {"id": pid,
                                                            "product_type": new_type,
                                                            "tags": new_tags}},
                                          timeout=30)
                        r2.raise_for_status()
                        updated += 1
                        type_changed = cur_type != new_type
                        tag_changed  = cur_tags != new_tags
                        logger.info(
                            f"[fix-products:{scraper_id}] FIXED [{updated}] {handle[:45]}"
                            f"  type={cur_type!r}→{new_type!r}  tags_fixed={tag_changed}"
                        )
                    except Exception as e:
                        failed += 1
                        logger.error(f"[fix-products:{scraper_id}] FAIL {handle}: {e}")

                    pct = min(99, 1 + int(updated / max(1, updated + skipped + failed) * 90))
                    _shopify_progress_emit(
                        progress_key, pct,
                        f'Fix-products: {updated} fixed, {skipped} ok, {pages} pages…',
                        updated, shopify_op='fix_products',
                        counts={'updated': updated, 'skipped': skipped, 'failed': failed,
                                'total': updated + skipped + failed,
                                'processed': updated + skipped + failed}
                    )
            else:
                zero_streak += 1
                if zero_streak >= MAX_ZERO:
                    logger.info(f"[fix-products:{scraper_id}] {zero_streak} zero pages — scan complete")
                    break

            nxt = _next_url(link_header)
            if not nxt:
                logger.info(f"[fix-products:{scraper_id}] Page {pages}: no next page — complete")
                break
            url = nxt

        msg = (f"Fix-products complete — {updated} fixed, {skipped} already-correct, "
               f"{failed} failed, {pages} pages scanned")
        logger.info(f"[fix-products:{scraper_id}] === DONE: {msg} ===")
        _shopify_progress_emit(
            progress_key, 100, msg, updated, shopify_op='fix_products',
            counts={'updated': updated, 'skipped': skipped, 'failed': failed,
                    'total': updated + skipped + failed,
                    'processed': updated + skipped + failed}
        )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({
        'message': f'Fix-products scan started for {scraper_id} (newest-first, CSV-guided)',
        'progress_key': progress_key,
        'csv_products_mapped': len(_handle_type_map),
    }), 202


@app.route('/api/fix-csv/<scraper_id>', methods=['POST'])
def fix_csv_from_db(scraper_id):
    """
    Re-generate the Shopify CSV for a scraper from its stored DB products,
    without re-scraping the website.  Useful after fixing transformer logic
    (e.g. variant image fallback) to produce a clean CSV immediately.
    """
    try:
        import json as _json
        from core.db import get_product_by_website, upload_csv_to_supabase
        from core.shopify_transformer import transform_to_shopify, export_shopify_csv, fix_csv_inventory_rows

        row = get_product_by_website(scraper_id)
        if not row:
            return jsonify({'error': f'No product data in DB for {scraper_id}.'}), 404

        products_data = row.get("products", {})
        if isinstance(products_data, str):
            products_data = _json.loads(products_data)
        if isinstance(products_data, dict):
            products = products_data.get("products", [])
        else:
            products = products_data or []

        if not products:
            return jsonify({'error': 'Product list in DB is empty.'}), 404

        rows = transform_to_shopify(products)

        os.makedirs("scraped_files", exist_ok=True)
        os.makedirs("exports", exist_ok=True)
        csv_latest = os.path.join("scraped_files", f"{scraper_id}_latest.csv")
        csv_export = os.path.join("exports", f"{scraper_id}_shopify.csv")

        export_shopify_csv(rows, csv_latest)
        fix_csv_inventory_rows(csv_latest)
        export_shopify_csv(rows, csv_export)
        fix_csv_inventory_rows(csv_export)

        csv_url = None
        try:
            csv_url = upload_csv_to_supabase(csv_latest, scraper_id)
            record_id = start_scrape_record(scraper_id)
            update_scrape_record(record_id, status="completed", products_count=len(products), csv_url=csv_url)
        except Exception as _e:
            logger.warning(f"[fix-csv] Supabase upload skipped: {_e}")

        return jsonify({
            'message': f'CSV regenerated for {scraper_id}',
            'products': len(products),
            'csv_rows': len(rows),
            'csv_path': csv_latest,
            'csv_url': csv_url,
        }), 200
    except Exception as e:
        logger.exception(f"fix-csv error for {scraper_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/fix-product-images/<scraper_id>/<int:shopify_product_id>', methods=['POST'])
def shopify_fix_product_images(scraper_id, shopify_product_id):
    """Re-link colour variant images for a single Shopify product."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import fix_single_product_images, _set_store_key
        _set_store_key(store_key)
        result = fix_single_product_images(scraper_id, shopify_product_id, csv_path)
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify fix-product-images error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/check-images/<scraper_id>', methods=['GET'])
def shopify_check_images(scraper_id):
    """Audit Shopify products for missing images and unlinked colour variant images."""
    try:
        from core.shopify_publisher import check_variant_images, _set_store_key
        _set_store_key(_get_request_store_key())
        result = check_variant_images(scraper_id)
        return jsonify(result), 200
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify check-images error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/check-oos/<scraper_id>', methods=['GET'])
def shopify_check_oos(scraper_id):
    """Return list of Shopify products not present in current CSV (potential OOS)."""
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import check_oos_products, _set_store_key
        _set_store_key(_get_request_store_key())
        result = check_oos_products(scraper_id, csv_path)
        return jsonify(result), 200
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify check-oos error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/tag-repair/<scraper_id>', methods=['POST'])
def shopify_tag_repair(scraper_id):
    """
    Handle-based tag repair: for each product in the CSV, look it up on Shopify by handle
    and add the RudraScrapper-{scraper_id} tag if missing.
    Runs in a background thread; progress is available via /api/shopify/progress/<scraper_id>.
    Requires X-Confirm-Main header for Main store.
    """
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import tag_repair_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 0, 'Tag repair: starting…', 0, shopify_op='tag-repair',
                               counts={'tagged': 0, 'already_tagged': 0, 'not_found': 0, 'failed': 0, 'total': 0})

        def _run():
            _set_store_key(store_key)
            def cb(pct, msg, count):
                _shopify_progress_emit(scraper_id, pct, msg, 0, shopify_op='tag-repair',
                                       counts={'tagged': count, 'already_tagged': 0,
                                               'not_found': 0, 'failed': 0, 'total': 0})
            result = tag_repair_products(scraper_id, csv_path, progress_callback=cb)
            _shopify_progress_emit(scraper_id, 100,
                                   f"Tag repair done — {result['tagged']} newly tagged, "
                                   f"{result['already_tagged']} already OK, "
                                   f"{result['not_found']} not on Shopify",
                                   0, shopify_op='tag-repair', counts=result)
            log_shopify_action(scraper_id, 'tag_repair', status='success',
                               result={'store': store_key, **result})

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({'message': 'Tag repair started', 'csv': csv_path, 'store': store_key}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify tag-repair error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/delete-oos/<scraper_id>', methods=['POST'])
def shopify_delete_oos(scraper_id):
    """Delete Shopify products that are no longer in the current CSV."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
    try:
        from core.shopify_publisher import delete_oos_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Shopify Delete OOS: Checking Shopify vs CSV…', 0,
                              shopify_op='delete-oos',
                              counts={'deleted': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'Shopify Delete OOS: {status}', count,
                                          shopify_op='delete-oos', counts=counts)
                result = delete_oos_products(scraper_id, csv_path, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                status_msg = (
                    f"Delete OOS {'stopped' if stopped else 'complete'} — "
                    f"{result['deleted']} deleted, {result['skipped']} skipped, {result['failed']} failed"
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['deleted'], result,
                                      shopify_op='delete-oos',
                                      counts={'deleted': result['deleted'], 'skipped': result['skipped'],
                                              'failed': result['failed'], 'total': result.get('oos_count', result['deleted'] + result['skipped'] + result['failed']),
                                              'processed': result['deleted'] + result['skipped'] + result['failed']})
            except Exception as e:
                logger.exception(f"[Delete OOS] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Delete OOS failed: {e}', 0, shopify_op='delete-oos',
                                      counts={'deleted': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Delete OOS started'}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify delete-oos error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/variant-quota', methods=['GET'])
def shopify_variant_quota():
    """Return current per-store daily variant quota state (read-only, no auth required)."""
    import core.shopify_publisher as _pub
    from datetime import timezone, timedelta
    next_midnight = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    # Build per-store quota status
    quota_by_store = {}
    for sk in ('test', 'main'):
        ra = _pub._daily_limit_resume_at.get(sk)
        rts = _pub._daily_limit_resume_ts_unix.get(sk)
        quota_by_store[sk] = {
            'sleeping': bool(ra),
            'resume_at': ra,
            'remaining_seconds': max(0, rts - time.time()) if rts else None,
        }
    return jsonify({
        'sleeping': any(v['sleeping'] for v in quota_by_store.values()),
        'resume_at': quota_by_store.get('test', {}).get('resume_at'),
        'remaining_seconds': quota_by_store.get('test', {}).get('remaining_seconds'),
        'session_variants_created': _pub._session_variants_created,
        'reset_utc': '00:00 UTC',
        'next_reset_utc': next_midnight.strftime('%H:%M UTC on %Y-%m-%d'),
        'by_store': quota_by_store,
    })


@app.route('/api/shopify/compare/<scraper_id>', methods=['GET'])
def shopify_compare(scraper_id):
    """Compare products tagged for this scraper between TEST and MAIN stores."""
    from concurrent.futures import ThreadPoolExecutor
    from core.shopify_publisher import get_scraper_products_summary, _set_store_key

    test_products: dict = {}
    main_products: dict = {}
    test_error: str | None = None
    main_error: str | None = None

    def fetch_test():
        _set_store_key('test')
        return get_scraper_products_summary(scraper_id)

    def fetch_main():
        _set_store_key('main')
        return get_scraper_products_summary(scraper_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_test = ex.submit(fetch_test)
            f_main = ex.submit(fetch_main)
            try:
                test_products = f_test.result()
            except EnvironmentError as e:
                test_error = str(e)
            except Exception as e:
                logger.exception(f"[Compare] TEST fetch error: {e}")
                test_error = str(e)
            try:
                main_products = f_main.result()
            except EnvironmentError as e:
                main_error = str(e)
            except Exception as e:
                logger.exception(f"[Compare] MAIN fetch error: {e}")
                main_error = str(e)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    def _norm_tags(tags_str: str) -> frozenset:
        """Case-insensitive, order-insensitive tag set for comparison."""
        return frozenset(t.strip().lower() for t in (tags_str or "").split(",") if t.strip())

    # Build MAIN's reverse SKU→handle index for SKU-aware join fallback.
    # When a TEST product's handle doesn't exist in MAIN, we check if any of
    # its SKUs belong to a MAIN product — handles can drift but SKUs are stable.
    main_sku_to_handle: dict[str, str] = {}
    for mh, mp in main_products.items():
        for sku in mp.get("skus", []):
            if sku and sku not in main_sku_to_handle:
                main_sku_to_handle[sku] = mh

    products_list = []
    matched = only_test = only_main = differs = identical = 0
    matched_main_handles: set[str] = set()

    # First pass: all TEST products (handle-match first, SKU-match fallback)
    for test_handle in sorted(test_products.keys()):
        t = test_products[test_handle]
        m_handle = test_handle
        m = main_products.get(test_handle)

        if m is None:
            # SKU-aware fallback: find MAIN product that shares a SKU
            for sku in t.get("skus", []):
                candidate = main_sku_to_handle.get(sku)
                if candidate and candidate not in matched_main_handles:
                    m_handle = candidate
                    m = main_products[candidate]
                    break

        if m is not None:
            matched_main_handles.add(m_handle)
            matched += 1
            diff = (
                t['image_count']   != m['image_count'] or
                t['variant_count'] != m['variant_count'] or
                t['price']         != m['price'] or
                _norm_tags(t.get('tags', '')) != _norm_tags(m.get('tags', ''))
            )
            status = 'differs' if diff else 'identical'
            if diff:
                differs += 1
            else:
                identical += 1
        else:
            only_test += 1
            status = 'only_test'

        products_list.append({
            'handle': test_handle,
            'title': t['title'],
            'status': status,
            'test': t,
            'main': m,
        })

    # Second pass: MAIN products with no TEST counterpart
    for main_handle in sorted(main_products.keys()):
        if main_handle not in matched_main_handles:
            only_main += 1
            m = main_products[main_handle]
            products_list.append({
                'handle': main_handle,
                'title': m['title'],
                'status': 'only_main',
                'test': None,
                'main': m,
            })

    # Sort: differs → only_test → only_main → identical
    _order = {'differs': 0, 'only_test': 1, 'only_main': 2, 'identical': 3}
    products_list.sort(key=lambda x: (_order.get(x['status'], 9), x['title'].lower()))

    return jsonify({
        'scraper_id': scraper_id,
        'test_total': len(test_products),
        'main_total': len(main_products),
        'matched': matched,
        'identical': identical,
        'differs': differs,
        'only_test': only_test,
        'only_main': only_main,
        'test_error': test_error,
        'main_error': main_error,
        'products': products_list,
    }), 200


@app.route('/api/shopify/dedup/<scraper_id>', methods=['POST'])
def shopify_dedup(scraper_id):
    """Remove duplicate Shopify products for a scraper (keeps most-variant copy per base handle)."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import deduplicate_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Dedup: Scanning for duplicates…', 0,
                              shopify_op='dedup',
                              counts={'deleted': 0, 'kept': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, status, count,
                                          shopify_op='dedup', counts=counts)
                result = deduplicate_products(scraper_id, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                status_msg = (
                    f"Dedup {'stopped' if stopped else 'complete'} — "
                    f"{result['deleted']} removed, {result['kept']} kept, {result['failed']} failed"
                    + (f" ({result['groups_with_dupes']} dupe groups)" if result.get('groups_with_dupes') else "")
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['deleted'], result,
                                      shopify_op='dedup',
                                      counts={'deleted': result['deleted'], 'kept': result.get('kept', 0),
                                              'failed': result['failed'],
                                              'total': result['deleted'] + result.get('kept', 0) + result['failed'],
                                              'processed': result['deleted'] + result.get('kept', 0) + result['failed']})
            except Exception as e:
                logger.exception(f"[Dedup] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Dedup failed: {e}', 0, shopify_op='dedup',
                                      counts={'deleted': 0, 'kept': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Dedup started'}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify dedup error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/verification-report', methods=['POST'])
def shopify_verification_report():
    """
    Generate and write an upload verification report comparing CSV vs live Shopify
    for one or more scrapers.  Body JSON: {"scraper_ids": [...], "store_key": "test"}.
    Returns the report text and the file path it was written to.
    """
    try:
        from core.verification_report import write_verification_report, build_verification_report
        from core.shopify_publisher import _set_store_key
        body = request.get_json(silent=True) or {}
        store_key = body.get("store_key") or _get_request_store_key()
        scraper_ids = body.get("scraper_ids") or [s["id"] for s in get_available_scrapers()]
        report_path = body.get("path") or None

        _set_store_key(store_key)
        out_path = write_verification_report(scraper_ids, store_key=store_key, path=report_path)

        with open(out_path, "r", encoding="utf-8") as fh:
            report_text = fh.read()

        return jsonify({
            "ok":          True,
            "report_path": out_path,
            "report_text": report_text,
            "scraper_ids": scraper_ids,
            "store_key":   store_key,
        }), 200
    except Exception as e:
        logger.exception(f"Verification report error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/delete-all/<scraper_id>', methods=['POST'])
def shopify_delete_all(scraper_id):
    """Delete ALL products from Shopify that belong to this scraper (no CSV comparison)."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import delete_all_shopify_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'Shopify Delete All: Fetching products…', 0,
                              shopify_op='nuke',
                              counts={'deleted': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key(store_key)
            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'Shopify Delete All: {status}', count,
                                          shopify_op='nuke', counts=counts)
                result = delete_all_shopify_products(scraper_id, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                status_msg = (
                    f"Delete All {'stopped' if stopped else 'complete'} — "
                    f"{result['deleted']} deleted, {result.get('skipped', 0)} skipped, {result['failed']} failed"
                )
                total = result.get('total', result['deleted'] + result.get('skipped', 0) + result['failed'])
                _shopify_progress_emit(scraper_id, 100, status_msg, result['deleted'], result,
                                      shopify_op='nuke',
                                      counts={'deleted': result['deleted'], 'skipped': result.get('skipped', 0),
                                              'failed': result['failed'], 'total': total, 'processed': total})
            except Exception as e:
                logger.exception(f"[Delete All] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'Delete All failed: {e}', 0, shopify_op='nuke',
                                      counts={'deleted': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Delete All started'}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify delete-all error: {e}")
        return jsonify({'error': str(e)}), 500


# ── Global Shopify operations ─────────────────────────────────────────────────

SHOPIFY_ALL_SCRAPERS = [
    'coach', 'cruise_fashion', 'michael_kors', 'karl',
    'marcjacobs', 'tory', 'gymshark', 'aloyoga', 'mytheresa', 'thedesignerboxuk', 'uk_polene', 'hoka',
    'drmartens', 'ugg', 'skims', 'thereformation', 'underarmour', 'organicbasics',
    'stanley1913', 'gemopticians', 'katespadeoutlet',
]

def _run_global_op(op_name: str, per_scraper_fn, aggregate_key: str, store_key: str = 'test'):
    """
    Generic runner for global Shopify ops (upload-all / update-all / delete-oos-all).
    Runs per_scraper_fn(scraper_id) sequentially for all scrapers.
    Emits progress under '__global__' key and per-scraper key simultaneously.
    store_key is propagated to the Shopify publisher so the correct store is used.
    """
    from core.shopify_publisher import _set_store_key as _ssk_global
    _ssk_global(store_key)

    global_id = '__global__'
    total_scrapers = len(SHOPIFY_ALL_SCRAPERS)
    _shopify_progress_emit(global_id, 3, f'{op_name}: Starting across all scrapers…', 0)

    agg = {'created': 0, 'updated': 0, 'deleted': 0, 'skipped': 0, 'failed': 0}
    shopify_op_type = ('upload' if aggregate_key == 'created'
                       else 'update' if aggregate_key == 'updated'
                       else 'delete-oos')
    empty_counts = {'created': 0, 'updated': 0, 'deleted': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0}

    for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
        base_pct = int(5 + (idx / total_scrapers) * 88)
        _shopify_progress_emit(global_id, base_pct,
                               f'{op_name}: [{idx+1}/{total_scrapers}] {sid}…',
                               agg.get(aggregate_key, 0),
                               shopify_op=shopify_op_type, counts=dict(agg))
        _shopify_progress_emit(sid, 5, f'{op_name}: Starting…', 0,
                               shopify_op=shopify_op_type, counts=dict(empty_counts))

        csv_path = _shopify_csv_path(sid)
        if not csv_path:
            _shopify_progress_emit(sid, 100, 'No CSV found — skipped', 0,
                                   shopify_op=shopify_op_type, counts=dict(empty_counts))
            try:
                from core.shopify_publisher import _cur_store_key as _csk
                _sk = _csk()
            except Exception:
                _sk = 'test'
            log_shopify_action(sid, op_name.lower().replace(' ', '_'),
                               status='skipped', notes='No CSV file available', store=_sk)
            continue

        def _cb(pct, status, count, counts=None, _sid=sid, _base=base_pct, _op=shopify_op_type):
            _shopify_progress_emit(_sid, pct, f'{op_name}: {status}', count,
                                   shopify_op=_op, counts=counts)
            frac_pct = int(_base + (pct / 100) * (88 / total_scrapers))
            _shopify_progress_emit(global_id, min(frac_pct, 95),
                                   f'{op_name}: [{idx+1}/{total_scrapers}] {_sid} — {status}',
                                   agg.get(aggregate_key, 0),
                                   shopify_op=shopify_op_type, counts=dict(agg))

        try:
            result = per_scraper_fn(sid, csv_path, _cb)
            for k in agg:
                agg[k] += result.get(k, 0)
            done_val = result.get(aggregate_key, 0)
            final_counts = {k: result.get(k, 0) for k in empty_counts}
            final_counts['total'] = sum(result.get(k, 0) for k in ('created', 'updated', 'deleted', 'skipped', 'failed'))
            final_counts['processed'] = final_counts['total']
            _shopify_progress_emit(sid, 100,
                f'{op_name} complete — {done_val} {aggregate_key}', done_val, result,
                shopify_op=shopify_op_type, counts=final_counts)
        except Exception as e:
            logger.exception(f"[Global {op_name}] {sid} failed: {e}")
            err_counts = dict(empty_counts)
            err_counts['failed'] = 1
            _shopify_progress_emit(sid, 100, f'{op_name} failed: {e}', 0,
                                   shopify_op=shopify_op_type, counts=err_counts)
            try:
                from core.shopify_publisher import _cur_store_key as _csk2
                _sk2 = _csk2()
            except Exception:
                _sk2 = 'test'
            log_shopify_action(sid, op_name.lower().replace(' ', '_'),
                               status='failed', error=str(e), store=_sk2)

    summary_msg = (
        f'{op_name} done — '
        + ', '.join(f'{v} {k}' for k, v in agg.items() if v > 0)
        or f'{op_name} done'
    )
    _shopify_progress_emit(global_id, 100, summary_msg, agg.get(aggregate_key, 0), agg,
                           shopify_op=shopify_op_type, counts=dict(agg))
    try:
        from core.shopify_publisher import _cur_store_key as _csk3
        _sk3 = _csk3()
    except Exception:
        _sk3 = 'test'
    log_shopify_action('__global__', op_name.lower().replace(' ', '_'),
                       status='success', result=agg,
                       notes=f'All {total_scrapers} scrapers processed', store=_sk3)


@app.route('/api/shopify/delete-all-all', methods=['POST'])
def shopify_delete_all_all():
    """Delete ALL products for ALL scrapers from Shopify store."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import delete_all_shopify_products, _set_store_key
        def _run():
            _set_store_key(store_key)
            global_id = '__global__'
            total_scrapers = len(SHOPIFY_ALL_SCRAPERS)
            agg = {'deleted': 0, 'failed': 0}
            _shopify_progress_emit(global_id, 3, 'NUKE ALL: Starting...', 0)

            for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                base_pct = int(5 + (idx / total_scrapers) * 88)
                _shopify_progress_emit(sid, 5, 'NUKE: Starting...', 0)

                def _cb(pct, status, count, counts=None, _sid=sid, _base=base_pct):
                    _shopify_progress_emit(_sid, pct, f'NUKE: {status}', count)
                    frac_pct = int(_base + (pct / 100) * (88 / total_scrapers))
                    _shopify_progress_emit(global_id, min(frac_pct, 95), f'NUKE ALL: [{idx+1}/{total_scrapers}] {_sid} — {status}', agg['deleted'])

                try:
                    result = delete_all_shopify_products(sid, progress_callback=_cb)
                    agg['deleted'] += result.get('deleted', 0)
                    agg['failed'] += result.get('failed', 0)
                    _shopify_progress_emit(sid, 100, f"NUKE complete — {result.get('deleted', 0)} deleted", result.get('deleted', 0))
                except Exception as e:
                    logger.exception(f"[Global NUKE] {sid} failed: {e}")
                    _shopify_progress_emit(sid, 100, f'NUKE failed: {e}', 0)

            _shopify_progress_emit(global_id, 100, f"NUKE ALL done — {agg['deleted']} products deleted", agg['deleted'], agg)
            log_shopify_action('__global__', 'nuke_all', status='success', result=agg,
                               notes=f'All {len(SHOPIFY_ALL_SCRAPERS)} scrapers', store=store_key)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Global nuke started'}), 202
    except Exception as e:
        logger.exception(f"Global nuke error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/approve/<scraper_id>', methods=['POST'])
def approve_for_main(scraper_id):
    """
    Approve a scraper's products for MAIN store promotion.
    Requires the scraper to have a 100% quality gate pass rate.
    """
    try:
        from core.quality_gate import run_quality_gate
        csv_path = _shopify_csv_path(scraper_id)
        if not csv_path:
            return jsonify({'error': f'No CSV for {scraper_id} — run the scraper first.'}), 404
        qg = run_quality_gate(scraper_id, csv_path)
        pass_rate = qg.get('pass_rate', 0)
        if pass_rate < 100:
            return jsonify({
                'error': (
                    f'Approval denied: {pass_rate:.1f}% quality gate pass rate. '
                    'All products must score 100% before being approved for MAIN STORE.'
                ),
                'pass_rate': pass_rate,
                'errors': qg.get('errors', 0),
            }), 403
        approved_for_main.add(scraper_id)
        logger.info(f"[Approve] {scraper_id} approved for MAIN store promotion (QG {pass_rate}%)")
        return jsonify({
            'approved': True,
            'scraper_id': scraper_id,
            'pass_rate': pass_rate,
            'message': f'{scraper_id} approved for MAIN store promotion.',
        }), 200
    except Exception as e:
        logger.exception(f"[Approve] Error approving {scraper_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/approve/<scraper_id>', methods=['DELETE'])
def revoke_approval(scraper_id):
    """Revoke MAIN store promotion approval for a scraper."""
    approved_for_main.discard(scraper_id)
    return jsonify({'approved': False, 'scraper_id': scraper_id, 'message': f'{scraper_id} approval revoked.'}), 200


@app.route('/api/approve', methods=['GET'])
def list_approvals():
    """Return the current set of scrapers approved for MAIN store promotion."""
    return jsonify({'approved': list(approved_for_main)}), 200


@app.route('/api/shopify/promote/<scraper_id>', methods=['POST'])
def shopify_promote(scraper_id):
    """
    Promote scraper products to MAIN STORE.
    Requires prior approval via POST /api/approve/<id> AND X-Confirm-Main header.
    Runs a fresh quality gate check before upload.
    """
    if not _has_main_confirmation():
        return jsonify({'error': 'MAIN STORE promote requires X-Confirm-Main: CONFIRM MAIN STORE ACTION header.'}), 403

    if scraper_id not in approved_for_main:
        return jsonify({
            'error': (
                f'"{scraper_id}" has not been approved for MAIN store promotion. '
                'Run quality gate validation and click "Approve for MAIN" first.'
            ),
            'needs_approval': True,
        }), 403

    csv_path = _shopify_csv_path(scraper_id)
    if not csv_path:
        return jsonify({'error': f'No CSV found for {scraper_id}. Run the scraper first.'}), 404

    try:
        from core.quality_gate import run_quality_gate
        qg = run_quality_gate(scraper_id, csv_path)
        pass_rate = qg.get('pass_rate', 0)
        if pass_rate < 100:
            approved_for_main.discard(scraper_id)
            return jsonify({
                'error': (
                    f'Quality gate blocked: {pass_rate:.1f}% pass rate. '
                    'All products must pass quality checks before promoting to MAIN STORE.'
                ),
                'quality': qg,
            }), 403
    except Exception as e:
        logger.warning(f"[Promote] Quality gate check failed for {scraper_id}: {e}")

    try:
        from core.shopify_publisher import upload_products, _set_store_key
        stop_ev = _shopify_stop_register(scraper_id)
        _shopify_progress_emit(scraper_id, 5, 'MAIN STORE Upload: Starting…', 0,
                              shopify_op='upload',
                              counts={'created': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

        def _run():
            _set_store_key('main')
            try:
                def cb(pct, status, count, counts=None):
                    _shopify_progress_emit(scraper_id, pct, f'MAIN Upload: {status}', count,
                                          shopify_op='upload', counts=counts)
                result = upload_products(scraper_id, csv_path, progress_callback=cb, stop_event=stop_ev)
                stopped = stop_ev.is_set()
                status_msg = (
                    f"MAIN Upload {'stopped' if stopped else 'complete'} — "
                    f"{result['created']} created, {result['skipped']} skipped, {result['failed']} failed"
                )
                _shopify_progress_emit(scraper_id, 100, status_msg, result['created'], result,
                                      shopify_op='upload',
                                      counts={'created': result['created'], 'skipped': result['skipped'],
                                              'failed': result['failed'],
                                              'total': result['created'] + result['skipped'] + result['failed'],
                                              'processed': result['created'] + result['skipped'] + result['failed']})
                # Clear approval after successful promote
                approved_for_main.discard(scraper_id)
            except Exception as e:
                logger.exception(f"[Promote] thread error: {e}")
                _shopify_progress_emit(scraper_id, 100, f'MAIN Upload failed: {e}', 0, shopify_op='upload',
                                      counts={'created': 0, 'skipped': 0, 'failed': 1, 'total': 0, 'processed': 0})
            finally:
                _shopify_stop_clear(scraper_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'MAIN STORE upload started', 'csv': csv_path}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Shopify promote error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/upload-all', methods=['POST'])
def shopify_upload_all():
    """Upload new products from CSV for ALL scrapers."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import upload_products
        def _fn(sid, csv_path, cb):
            return upload_products(sid, csv_path, progress_callback=cb)
        threading.Thread(
            target=_run_global_op,
            args=('Upload All', _fn, 'created', store_key),
            daemon=True
        ).start()
        return jsonify({'message': 'Global upload started for all scrapers', 'store': store_key}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/update-all', methods=['POST'])
def shopify_update_all():
    """Update existing products for ALL scrapers."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import update_products
        def _fn(sid, csv_path, cb):
            return update_products(sid, csv_path, progress_callback=cb)
        threading.Thread(
            target=_run_global_op,
            args=('Update All', _fn, 'updated', store_key),
            daemon=True
        ).start()
        return jsonify({'message': 'Global update started for all scrapers', 'store': store_key}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/check-oos-all', methods=['GET'])
def shopify_check_oos_all():
    """Check OOS products for ALL scrapers — returns aggregated summary."""
    store_key = _get_request_store_key()
    try:
        from core.shopify_publisher import check_oos_products, _set_store_key as _ssk_oos
        _ssk_oos(store_key)
        results = {}
        total_oos = 0
        for sid in SHOPIFY_ALL_SCRAPERS:
            csv_path = _shopify_csv_path(sid)
            if not csv_path:
                results[sid] = {'error': 'No CSV found'}
                continue
            try:
                r = check_oos_products(sid, csv_path)
                results[sid] = r
                total_oos += len(r.get('oos', []))
                log_shopify_action(sid, 'check_oos', status='success',
                                   result={'skipped': len(r.get('oos', []))},
                                   notes=f"{r.get('total_shopify',0)} in Shopify, {r.get('total_csv',0)} in CSV",
                                   store=store_key)
            except Exception as e:
                results[sid] = {'error': str(e)}
        return jsonify({'scrapers': results, 'total_oos': total_oos, 'store': store_key}), 200
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/delete-oos-all', methods=['POST'])
def shopify_delete_oos_all():
    """Delete OOS products for ALL scrapers."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import delete_oos_products
        def _fn(sid, csv_path, cb):
            return delete_oos_products(sid, csv_path, progress_callback=cb)
        threading.Thread(
            target=_run_global_op,
            args=('Delete OOS All', _fn, 'deleted', store_key),
            daemon=True
        ).start()
        return jsonify({'message': 'Global delete-OOS started for all scrapers', 'store': store_key}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/sync-all', methods=['POST'])
def shopify_sync_all():
    """Full sync (upload-new then update-existing) for ALL scrapers."""
    store_key = _get_request_store_key()
    ok, err = _check_main_write(store_key)
    if not ok:
        return err
    try:
        from core.shopify_publisher import upload_products, update_products, _set_store_key as _ssk_sync
        global_id = '__global__'

        def _run():
            _ssk_sync(store_key)
            total = len(SHOPIFY_ALL_SCRAPERS)
            agg = {'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0}
            _shopify_progress_emit(global_id, 2, 'Full Sync: Phase 1/2 — Uploading new products…', 0,
                                   shopify_op='upload', counts=dict(agg))

            for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                base_pct = int(3 + (idx / total) * 44)
                csv_path = _shopify_csv_path(sid)
                _shopify_progress_emit(sid, 5, 'Sync Upload: Starting…', 0,
                                       shopify_op='upload', counts={'created':0,'updated':0,'skipped':0,'failed':0})
                if not csv_path:
                    _shopify_progress_emit(sid, 100, 'No CSV — skipped', 0)
                    continue
                try:
                    def _cb(pct, status, count, counts=None, _sid=sid, _base=base_pct):
                        _shopify_progress_emit(_sid, pct, f'Sync Upload: {status}', count,
                                               shopify_op='upload', counts=counts)
                        _shopify_progress_emit(global_id, min(int(_base + pct * 44/100/total), 47),
                                               f'Sync Upload [{idx+1}/{total}] {_sid}: {status}', agg['created'],
                                               shopify_op='upload', counts=dict(agg))
                    r = upload_products(sid, csv_path, progress_callback=_cb)
                    agg['created'] += r.get('created', 0)
                    agg['skipped'] += r.get('skipped', 0)
                    agg['failed']  += r.get('failed', 0)
                    _shopify_progress_emit(sid, 50, f"Upload done — {r['created']} new", r['created'],
                                           shopify_op='upload', counts=r)
                except Exception as e:
                    logger.exception(f"[SyncAll Upload] {sid}: {e}")
                    _shopify_progress_emit(sid, 50, f'Upload failed: {e}', 0)

            _shopify_progress_emit(global_id, 50, 'Full Sync: Phase 2/2 — Updating existing products…', agg['created'],
                                   shopify_op='update', counts=dict(agg))

            for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                base_pct = int(51 + (idx / total) * 44)
                csv_path = _shopify_csv_path(sid)
                _shopify_progress_emit(sid, 55, 'Sync Update: Starting…', 0,
                                       shopify_op='update', counts={'created':0,'updated':0,'skipped':0,'failed':0})
                if not csv_path:
                    _shopify_progress_emit(sid, 100, 'No CSV — skipped', 0)
                    continue
                try:
                    def _cb2(pct, status, count, counts=None, _sid=sid, _base=base_pct):
                        _shopify_progress_emit(_sid, int(50 + pct * 0.5), f'Sync Update: {status}', count,
                                               shopify_op='update', counts=counts)
                        _shopify_progress_emit(global_id, min(int(_base + pct * 44/100/total), 97),
                                               f'Sync Update [{idx+1}/{total}] {_sid}: {status}', agg['updated'],
                                               shopify_op='update', counts=dict(agg))
                    r = update_products(sid, csv_path, progress_callback=_cb2)
                    agg['updated'] += r.get('updated', 0)
                    _shopify_progress_emit(sid, 100,
                        f"Sync done — {r['updated']} updated", r['updated'],
                        shopify_op='update',
                        counts={'created': 0, 'updated': r.get('updated',0), 'skipped': r.get('skipped',0), 'failed': r.get('failed',0)})
                except Exception as e:
                    logger.exception(f"[SyncAll Update] {sid}: {e}")
                    _shopify_progress_emit(sid, 100, f'Update failed: {e}', 0)

            summary = (f"Full Sync done — {agg['created']} uploaded, "
                       f"{agg['updated']} updated, {agg['failed']} failed")
            _shopify_progress_emit(global_id, 100, summary, agg['created'] + agg['updated'], agg,
                                   shopify_op='update', counts=dict(agg))
            try:
                from core.shopify_publisher import _cur_store_key as _csk_sync
                _sk_sync = _csk_sync()
            except Exception:
                _sk_sync = 'test'
            log_shopify_action('__global__', 'sync_all', status='success', result=agg,
                               notes=f'Full sync across {total} scrapers', store=_sk_sync)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Full sync started for all scrapers', 'store': store_key}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Quality Gate Validation ───────────────────────────────────────────────────

@app.route('/api/validate/<scraper_id>', methods=['GET'])
def validate_scraper(scraper_id):
    """Run quality-gate validation on a scraper's latest CSV."""
    try:
        from core.quality_gate import validate_csv
        csv_path = _shopify_csv_path(scraper_id)
        if not csv_path:
            return jsonify({'error': f'No CSV found for {scraper_id}. Run the scraper first.'}), 404
        result = validate_csv(scraper_id, csv_path)
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"[Validate] {scraper_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/recheck/<scraper_id>', methods=['POST'])
def qa_recheck_scraper(scraper_id):
    """
    Re-run quality gate on a scraper's latest CSV and persist the result
    into the most-recent completed scrape record so the QA Review page
    reflects the updated gate results immediately.
    """
    try:
        from core.quality_gate import validate_csv
        from core.db import update_scraper_quality_report
        csv_path = _shopify_csv_path(scraper_id)
        if not csv_path:
            return jsonify({'error': f'No CSV found for {scraper_id}.'}), 404
        result = validate_csv(scraper_id, csv_path)
        saved = update_scraper_quality_report(scraper_id, result)
        logger.info(f"[QA Recheck] {scraper_id}: errors={result.get('errors',0)}, saved={saved}")
        return jsonify({**result, 'saved': saved}), 200
    except Exception as e:
        logger.exception(f"[QA Recheck] {scraper_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/recheck-all', methods=['POST'])
def qa_recheck_all():
    """Re-run quality gate for every scraper that has a CSV and persist results."""
    try:
        from core.quality_gate import validate_csv
        from core.db import update_scraper_quality_report
        summary = {}
        for sid in SHOPIFY_ALL_SCRAPERS:
            csv_path = _shopify_csv_path(sid)
            if not csv_path:
                summary[sid] = {'error': 'No CSV', 'errors': 0, 'saved': False}
                continue
            try:
                result = validate_csv(sid, csv_path)
                saved  = update_scraper_quality_report(sid, result)
                summary[sid] = {'errors': result.get('errors', 0), 'pass_rate': result.get('pass_rate'), 'saved': saved}
            except Exception as e:
                summary[sid] = {'error': str(e), 'errors': -1, 'saved': False}
        total_errors = sum(v.get('errors', 0) for v in summary.values() if isinstance(v.get('errors'), int) and v['errors'] >= 0)
        return jsonify({'scrapers': summary, 'total_errors': total_errors}), 200
    except Exception as e:
        logger.exception(f"[QA Recheck All]: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/validate-all', methods=['GET'])
def validate_all():
    """Run quality-gate validation on ALL scrapers' latest CSVs."""
    try:
        from core.quality_gate import validate_csv
        results = {}
        total_errors = 0
        total_products = 0
        for sid in SHOPIFY_ALL_SCRAPERS:
            csv_path = _shopify_csv_path(sid)
            if not csv_path:
                results[sid] = {'error': 'No CSV', 'total': 0, 'errors': 0, 'warnings': 0, 'ok': 0, 'pass_rate': 0, 'ready_to_upload': False}
                continue
            try:
                r = validate_csv(sid, csv_path)
                results[sid] = r
                total_errors   += r.get('errors', 0)
                total_products += r.get('total', 0)
            except Exception as e:
                results[sid] = {'error': str(e), 'total': 0, 'errors': 0, 'warnings': 0, 'ok': 0, 'pass_rate': 0, 'ready_to_upload': False}
        return jsonify({
            'scrapers':       results,
            'total_products': total_products,
            'total_errors':   total_errors,
            'all_clear':      total_errors == 0,
        }), 200
    except Exception as e:
        logger.exception(f"[ValidateAll]: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/quality', methods=['GET'])
def get_quality():
    """
    Return per-scraper quality summaries.
    Live data comes from scraper_progress[sid]['quality'] (populated after each run).
    Falls back to the latest completed scrapes row quality_report column for
    scrapers that haven't run since the last restart.
    """
    try:
        live: dict = {}
        for sid, prog in list(scraper_progress.items()):
            q = prog.get('quality')
            if q:
                live[sid] = q

        # Fill in historical results from DB for scrapers with no live data
        try:
            from core.db import get_connection, _return_connection
            conn, cur = get_connection()
            if conn:
                cur.execute("""
                    SELECT DISTINCT ON (scraper_id) scraper_id, quality_report
                    FROM scrapes
                    WHERE status = 'completed' AND quality_report IS NOT NULL
                    ORDER BY scraper_id, completed_at DESC NULLS LAST
                """)
                rows = cur.fetchall()
                cur.close()
                _return_connection(conn)
                import json as _json
                for row in rows:
                    sid, qr = row[0], row[1]
                    if sid not in live and qr:
                        try:
                            live[sid] = _json.loads(qr) if isinstance(qr, str) else qr
                        except Exception:
                            pass
        except Exception as _dbe:
            logger.debug(f"[Quality] DB historical lookup skipped: {_dbe}")

        return jsonify({'quality': live}), 200
    except Exception as e:
        logger.exception(f"[Quality] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa', methods=['GET'])
def get_qa_overview():
    """Aggregate QA counts + all products across every scraper."""
    try:
        result = get_qa_products(scraper_id=None)
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"[QA] overview error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/<scraper_id>', methods=['GET'])
def get_qa_for_scraper(scraper_id):
    """Per-scraper QA product list with status + event history."""
    try:
        result = get_qa_products(scraper_id=scraper_id)
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"[QA] scraper {scraper_id} error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/<scraper_id>/<shopify_product_id>/approve', methods=['POST'])
def qa_approve(scraper_id, shopify_product_id):
    """Mark a product APPROVED after manual QA review."""
    try:
        body = request.get_json(silent=True) or {}
        reason = body.get('reason', '')
        ok = approve_product(shopify_product_id, scraper_id, reason=reason or None)
        if ok:
            logger.info(f"[QA] APPROVED {shopify_product_id} ({scraper_id})")
            return jsonify({'ok': True, 'qa_status': 'APPROVED'}), 200
        return jsonify({'ok': False, 'error': 'Product not found in registry for this scraper'}), 404
    except Exception as e:
        logger.exception(f"[QA] approve error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/<scraper_id>/<shopify_product_id>/rework', methods=['POST'])
def qa_rework(scraper_id, shopify_product_id):
    """Flag a product REWORK_REQUIRED after manual QA review."""
    try:
        body = request.get_json(silent=True) or {}
        reason = body.get('reason', '')
        ok = rework_product(shopify_product_id, scraper_id, reason=reason or None)
        if ok:
            logger.info(f"[QA] REWORK_REQUIRED {shopify_product_id} ({scraper_id}): {reason}")
            return jsonify({'ok': True, 'qa_status': 'REWORK_REQUIRED'}), 200
        return jsonify({'ok': False, 'error': 'Product not found in registry for this scraper'}), 404
    except Exception as e:
        logger.exception(f"[QA] rework error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qc-upload/<scraper_id>', methods=['POST'])
def qc_and_upload(scraper_id):
    """
    QC & Upload pipeline for a single scraper.
    POST body: {"upload": true}
      1. Validate the scraper's latest CSV (enhanced 5-category checks).
      2. If upload=true AND no hard errors → start upload in background.
    Returns the full validation result plus upload_started flag.
    """
    try:
        from core.quality_gate import validate_csv
        from core.shopify_publisher import upload_products
        body = request.get_json(silent=True) or {}
        do_upload = bool(body.get('upload', False))

        csv_path = _shopify_csv_path(scraper_id)
        if not csv_path:
            return jsonify({'error': f'No CSV found for {scraper_id}. Run the scraper first.'}), 404

        result = validate_csv(scraper_id, csv_path)

        upload_started = False
        if do_upload:
            if result.get('errors', 0) > 0:
                result['upload_blocked'] = True
                result['upload_message'] = (
                    f"Upload blocked — {result['errors']} product(s) have critical errors. "
                    "Fix them first or the upload may produce bad data."
                )
            else:
                stop_ev = _shopify_stop_register(scraper_id)
                _shopify_progress_emit(scraper_id, 5,
                    f"QC passed — starting upload of {result['total']} products…", 0,
                    shopify_op='upload',
                    counts={'created': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

                _total = result['total']
                def _run():
                    try:
                        def cb(pct, status, count, counts=None):
                            _shopify_progress_emit(scraper_id, pct,
                                f'Shopify Upload: {status}', count,
                                shopify_op='upload', counts=counts)
                        r = upload_products(scraper_id, csv_path,
                                            progress_callback=cb,
                                            stop_event=stop_ev)
                        stopped = stop_ev.is_set()
                        msg = (f"Upload {'stopped' if stopped else 'complete'} — "
                               f"{r['created']} created, {r['skipped']} skipped, "
                               f"{r['failed']} failed")
                        _shopify_progress_emit(scraper_id, 100, msg, r['created'], r,
                            shopify_op='upload',
                            counts={'created': r['created'], 'skipped': r['skipped'],
                                    'failed': r['failed'],
                                    'total': r['created'] + r['skipped'] + r['failed'],
                                    'processed': r['created'] + r['skipped'] + r['failed']})
                    except Exception as ex:
                        logger.exception(f"[QCUpload] thread error: {ex}")
                        _shopify_progress_emit(scraper_id, 100,
                            f'Upload failed: {ex}', 0, shopify_op='upload',
                            counts={'created': 0, 'skipped': 0, 'failed': 1,
                                    'total': 0, 'processed': 0})
                    finally:
                        _shopify_stop_clear(scraper_id)

                threading.Thread(target=_run, daemon=True).start()
                upload_started = True
                result['upload_message'] = (
                    f"QC passed ({result['total']} products, "
                    f"{result.get('warnings', 0)} warnings) — upload started."
                )

        result['upload_started'] = upload_started
        return jsonify(result), 200
    except Exception as e:
        logger.exception(f"[QCUpload] {scraper_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/qc-upload-all', methods=['POST'])
def qc_and_upload_all():
    """
    QC & Upload all scrapers.
    Validates every scraper's CSV; those with 0 errors are uploaded in background threads.
    Returns per-scraper validation + upload_started status.
    """
    try:
        from core.quality_gate import validate_csv
        from core.shopify_publisher import upload_products

        body = request.get_json(silent=True) or {}
        do_upload = bool(body.get('upload', False))

        global_id = '__global__'
        stop_ev   = _shopify_stop_register(global_id)
        total_scrapers = len(SHOPIFY_ALL_SCRAPERS)

        results        = {}
        total_products = 0
        total_errors   = 0
        upload_count   = 0

        _shopify_progress_emit(global_id, 2, 'QC: Validating all CSVs…', 0)

        for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
            if stop_ev.is_set():
                break
            csv_path = _shopify_csv_path(sid)
            pct = int(5 + (idx / total_scrapers) * 45)
            if not csv_path:
                results[sid] = {'error': 'No CSV', 'total': 0, 'errors': 0,
                                'warnings': 0, 'ok': 0, 'pass_rate': 0,
                                'ready_to_upload': False, 'upload_started': False}
                _shopify_progress_emit(global_id, pct, f'QC: {sid} — no CSV found', 0)
                continue
            try:
                r = validate_csv(sid, csv_path)
                r['upload_started'] = False
                results[sid]    = r
                total_products += r.get('total', 0)
                total_errors   += r.get('errors', 0)
                _shopify_progress_emit(
                    global_id, pct,
                    f"QC: {sid} — {r.get('ok', 0)} OK, "
                    f"{r.get('warnings', 0)} warn, {r.get('errors', 0)} errors", 0,
                )
            except Exception as e:
                results[sid] = {'error': str(e), 'total': 0, 'errors': 1,
                                'warnings': 0, 'ok': 0, 'pass_rate': 0,
                                'ready_to_upload': False, 'upload_started': False}
                logger.exception(f"[QCAll] {sid}: {e}")

        if do_upload and not stop_ev.is_set():
            _shopify_progress_emit(global_id, 55, 'QC complete — launching uploads for passing scrapers…', 0)
            for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                if stop_ev.is_set():
                    break
                r = results.get(sid, {})
                if r.get('errors', 1) > 0:
                    continue
                csv_path = _shopify_csv_path(sid)
                if not csv_path:
                    continue
                sid_stop = _shopify_stop_register(sid)
                _shopify_progress_emit(sid, 5,
                    f"QC passed — starting upload…", 0,
                    shopify_op='upload',
                    counts={'created': 0, 'skipped': 0, 'failed': 0, 'total': 0, 'processed': 0})

                def _make_run(_sid, _csv, _stop):
                    def _run():
                        try:
                            def cb(pct, status, count, counts=None):
                                _shopify_progress_emit(_sid, pct,
                                    f'Shopify Upload: {status}', count,
                                    shopify_op='upload', counts=counts)
                            r = upload_products(_sid, _csv,
                                                progress_callback=cb,
                                                stop_event=_stop)
                            stopped = _stop.is_set()
                            msg = (f"Upload {'stopped' if stopped else 'complete'} — "
                                   f"{r['created']} created, {r['skipped']} skipped, "
                                   f"{r['failed']} failed")
                            _shopify_progress_emit(_sid, 100, msg, r['created'], r,
                                shopify_op='upload',
                                counts={'created': r['created'], 'skipped': r['skipped'],
                                        'failed': r['failed'],
                                        'total': r['created'] + r['skipped'] + r['failed'],
                                        'processed': r['created'] + r['skipped'] + r['failed']})
                        except Exception as ex:
                            logger.exception(f"[QCUploadAll] {_sid} thread: {ex}")
                            _shopify_progress_emit(_sid, 100, f'Upload failed: {ex}', 0,
                                shopify_op='upload',
                                counts={'created': 0, 'skipped': 0, 'failed': 1,
                                        'total': 0, 'processed': 0})
                        finally:
                            _shopify_stop_clear(_sid)
                    return _run

                threading.Thread(target=_make_run(sid, csv_path, sid_stop), daemon=True).start()
                results[sid]['upload_started'] = True
                upload_count += 1
            _shopify_progress_emit(
                global_id, 100,
                f'QC done — {upload_count}/{total_scrapers} uploads launched', upload_count,
            )

        return jsonify({
            'scrapers':       results,
            'total_products': total_products,
            'total_errors':   total_errors,
            'all_clear':      total_errors == 0,
            'upload_started': upload_count > 0,
            'upload_count':   upload_count,
        }), 200
    except Exception as e:
        logger.exception(f"[QCUploadAll]: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/full-pipeline', methods=['POST'])
def shopify_full_pipeline():
    """
    Full Pipeline:
      Phase 1 — Run all scrapers (wait for completion)
      Phase 2 — Validate all CSVs
      Phase 3 — Upload new products to Shopify
      Phase 4 — Check OOS
      Phase 5 — Delete OOS products
    """
    try:
        from core.shopify_publisher import upload_products, check_oos_products, delete_oos_products
        from core.quality_gate import validate_csv

        global_id = '__global__'
        total = len(SHOPIFY_ALL_SCRAPERS)
        stop_ev = _shopify_stop_register(global_id)

        def _run():
            try:
                agg = {'created': 0, 'skipped': 0, 'failed': 0, 'deleted': 0,
                       'validation_errors': 0, 'validation_warnings': 0}

                # ── Phase 1: Launch all scrapers ──────────────────────────────
                _shopify_progress_emit(global_id, 2, 'Full Pipeline: Phase 1/5 — Launching all scrapers…', 0)
                already_running = [s for s in SHOPIFY_ALL_SCRAPERS if active_job_locks.get(s)]
                scrapers_to_run = SHOPIFY_ALL_SCRAPERS  # run all regardless

                threading.Thread(
                    target=perform_scraping,
                    args=('pipeline@mirage.com', scrapers_to_run),
                    daemon=True,
                ).start()

                # Wait for all scrapers to finish (poll every 15 s, max 2 h)
                MAX_WAIT = 7200
                waited = 0
                while waited < MAX_WAIT:
                    if stop_ev.is_set():
                        _shopify_progress_emit(global_id, 100, 'Full Pipeline cancelled during scraping phase', 0)
                        return
                    still = [s for s in SHOPIFY_ALL_SCRAPERS if active_job_locks.get(s)]
                    if not still:
                        break
                    pct = int(2 + (1 - len(still) / total) * 33)
                    _shopify_progress_emit(global_id, pct,
                        f'Phase 1/5 — {len(still)}/{total} scrapers still running…', 0)
                    time.sleep(15)
                    waited += 15

                if stop_ev.is_set():
                    _shopify_progress_emit(global_id, 100, 'Full Pipeline cancelled', 0)
                    return

                # ── Phase 2: Validate ─────────────────────────────────────────
                _shopify_progress_emit(global_id, 37, 'Full Pipeline: Phase 2/5 — Validating all CSVs…', 0)
                for sid in SHOPIFY_ALL_SCRAPERS:
                    if stop_ev.is_set():
                        break
                    csv_path = _shopify_csv_path(sid)
                    if not csv_path:
                        continue
                    try:
                        r = validate_csv(sid, csv_path)
                        agg['validation_errors']   += r.get('errors', 0)
                        agg['validation_warnings'] += r.get('warnings', 0)
                        _shopify_progress_emit(sid, 40,
                            f"Validated — {r.get('ok', 0)} OK, "
                            f"{r.get('warnings', 0)} warn, {r.get('errors', 0)} err", 0)
                    except Exception as e:
                        logger.exception(f"[Pipeline Validate] {sid}: {e}")

                if stop_ev.is_set():
                    _shopify_progress_emit(global_id, 100, 'Full Pipeline cancelled after validation', 0)
                    return

                _shopify_progress_emit(global_id, 42,
                    f'Validation done — {agg["validation_errors"]} errors, '
                    f'{agg["validation_warnings"]} warnings across all scrapers', 0)
                time.sleep(1)

                # ── Phase 3: Upload ───────────────────────────────────────────
                _shopify_progress_emit(global_id, 45, 'Full Pipeline: Phase 3/5 — Uploading new products…', 0,
                                       shopify_op='upload', counts=dict(agg))
                for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                    if stop_ev.is_set():
                        break
                    base_pct = int(45 + (idx / total) * 27)
                    csv_path = _shopify_csv_path(sid)
                    if not csv_path:
                        continue
                    try:
                        def _cb3(pct, status, count, counts=None, _sid=sid, _base=base_pct):
                            _shopify_progress_emit(_sid, pct, f'Pipeline Upload: {status}', count,
                                                   shopify_op='upload', counts=counts)
                            _shopify_progress_emit(global_id,
                                min(int(_base + pct * 27 / 100 / total), 71),
                                f'Phase 3/5 Upload [{idx+1}/{total}] {_sid}: {status}', agg['created'],
                                shopify_op='upload', counts=dict(agg))
                        r = upload_products(sid, csv_path, progress_callback=_cb3, stop_event=stop_ev)
                        agg['created'] += r.get('created', 0)
                        agg['skipped'] += r.get('skipped', 0)
                        agg['failed']  += r.get('failed', 0)
                        _shopify_progress_emit(sid, 72,
                            f"Upload done — {r['created']} new", r['created'],
                            shopify_op='upload', counts=r)
                    except Exception as e:
                        logger.exception(f"[Pipeline Upload] {sid}: {e}")
                        _shopify_progress_emit(sid, 72, f'Upload failed: {e}', 0)

                if stop_ev.is_set():
                    _shopify_progress_emit(global_id, 100,
                        f'Pipeline cancelled — {agg["created"]} uploaded so far', agg['created'])
                    return

                # ── Phase 4: Check OOS ────────────────────────────────────────
                _shopify_progress_emit(global_id, 73,
                    'Full Pipeline: Phase 4/5 — Checking OOS products…', agg['created'])
                oos_total = 0
                for sid in SHOPIFY_ALL_SCRAPERS:
                    csv_path = _shopify_csv_path(sid)
                    if not csv_path:
                        continue
                    try:
                        r = check_oos_products(sid, csv_path)
                        oos_total += len(r.get('oos', []))
                    except Exception as e:
                        logger.exception(f"[Pipeline OOS Check] {sid}: {e}")

                _shopify_progress_emit(global_id, 80,
                    f'OOS check done — {oos_total} discontinued products found', agg['created'])
                time.sleep(1)

                # ── Phase 5: Delete OOS ───────────────────────────────────────
                _shopify_progress_emit(global_id, 82,
                    'Full Pipeline: Phase 5/5 — Deleting OOS products…', agg['created'],
                    shopify_op='delete-oos', counts=dict(agg))
                for idx, sid in enumerate(SHOPIFY_ALL_SCRAPERS):
                    if stop_ev.is_set():
                        break
                    base_pct = int(82 + (idx / total) * 14)
                    csv_path = _shopify_csv_path(sid)
                    if not csv_path:
                        continue
                    try:
                        def _cb5(pct, status, count, counts=None, _sid=sid, _base=base_pct):
                            _shopify_progress_emit(_sid, pct, f'Pipeline Delete OOS: {status}', count,
                                                   shopify_op='delete-oos', counts=counts)
                            _shopify_progress_emit(global_id,
                                min(int(_base + pct * 14 / 100 / total), 96),
                                f'Phase 5/5 Delete OOS [{idx+1}/{total}] {_sid}: {status}', agg['deleted'],
                                shopify_op='delete-oos', counts=dict(agg))
                        r = delete_oos_products(sid, csv_path, progress_callback=_cb5, stop_event=stop_ev)
                        agg['deleted'] += r.get('deleted', 0)
                        _shopify_progress_emit(sid, 100,
                            f"OOS delete done — {r.get('deleted', 0)} removed", r.get('deleted', 0), r,
                            shopify_op='delete-oos', counts=r)
                    except Exception as e:
                        logger.exception(f"[Pipeline Delete OOS] {sid}: {e}")
                        _shopify_progress_emit(sid, 100, f'Delete OOS failed: {e}', 0)

                summary = (
                    f"Full Pipeline done — "
                    f"{agg['created']} uploaded, "
                    f"{agg['deleted']} OOS deleted, "
                    f"{agg['validation_errors']} validation errors"
                )
                _shopify_progress_emit(global_id, 100, summary, agg['created'], agg)
                try:
                    from core.shopify_publisher import _cur_store_key as _csk_pipe
                    _sk_pipe = _csk_pipe()
                except Exception:
                    _sk_pipe = 'test'
                log_shopify_action('__global__', 'full_pipeline', status='success', result=agg,
                                   notes=f'5-phase pipeline across {total} scrapers', store=_sk_pipe)
            except Exception as e:
                logger.exception(f"[Full Pipeline] thread error: {e}")
                _shopify_progress_emit(global_id, 100, f'Full Pipeline failed: {e}', 0)
            finally:
                _shopify_stop_clear(global_id)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'message': 'Full pipeline started'}), 202
    except EnvironmentError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        logger.exception(f"Full pipeline error: {e}")
        return jsonify({'error': str(e)}), 500


# ── Shopify Activity Logs API ─────────────────────────────────────────────────

@app.route('/api/shopify/logs', methods=['GET'])
def shopify_logs():
    """Paginated, filterable Shopify activity log endpoint."""
    scraper_id  = request.args.get('scraper_id')
    action_type = request.args.get('action_type')
    status      = request.args.get('status')
    store       = request.args.get('store')
    search      = request.args.get('search')
    date_from   = request.args.get('date_from')
    date_to     = request.args.get('date_to')
    limit       = min(int(request.args.get('limit', 50)), 200)
    offset      = int(request.args.get('offset', 0))

    try:
        logs  = get_shopify_logs(scraper_id, action_type, status, search, date_from, date_to, limit, offset, store=store)
        total = get_shopify_logs_count(scraper_id, action_type, status, search, date_from, date_to, store=store)
        return jsonify({'logs': logs, 'total': total, 'limit': limit, 'offset': offset}), 200
    except Exception as e:
        logger.exception(f"Logs API error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/logs/stats', methods=['GET'])
def shopify_logs_stats():
    """Aggregate stats for the Shopify dashboard cards."""
    try:
        stats = get_shopify_log_stats()
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopify/logs/export', methods=['GET'])
def shopify_logs_export():
    """Download all matching activity logs as a CSV file."""
    import csv as _csv
    import io as _io

    scraper_id  = request.args.get('scraper_id')
    action_type = request.args.get('action_type')
    status      = request.args.get('status')
    search      = request.args.get('search')
    date_from   = request.args.get('date_from')
    date_to     = request.args.get('date_to')

    try:
        logs = get_shopify_logs(scraper_id, action_type, status, search, date_from, date_to, limit=5000, offset=0)
        buf = _io.StringIO()
        if logs:
            writer = _csv.DictWriter(buf, fieldnames=logs[0].keys())
            writer.writeheader()
            writer.writerows(logs)
        buf.seek(0)
        from flask import Response
        return Response(
            buf.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=shopify_activity_logs.csv'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scrape/restart', methods=['POST'])
def restart_scraping():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400
    sid = data.get('scraper_ids', [None])[0]
    if not sid: return jsonify({'error': 'No scraper_id provided'}), 400
    
    # 1. Kill previous scraper thread
    if sid in active_stop_events: 
        active_stop_events[sid].set()
        time.sleep(1.0) # Let it cleanup

    # 1b. Also cancel any active Shopify op for this scraper
    shopify_ev = shopify_stop_events.get(sid)
    if shopify_ev and not shopify_ev.is_set():
        shopify_ev.set()
        logger.info(f"[Restart] Cancelled active Shopify op for {sid}")
    
    # Force unlock to ensure fresh start
    active_job_locks[sid] = False
    
    # 2. CLEAR DISCOVERY CACHES
    cache_base = f"scraped_files/{sid}"
    for suffix in ["_cache.json", "_active.json", "_checkpoint.json", "_codes_checkpoint.json", "_products_cache.json"]:
        c_path = os.path.join(os.getcwd(), f"{cache_base}{suffix}")
        if os.path.exists(c_path):
            try: os.remove(c_path)
            except: pass
            
    # 3. Hard Reset Progress UI
    scraper_progress[sid] = {'progress': 5, 'status': 'Hard Reset & Restarting...', 'is_running': True, 'products_count': 0, 'stuck': False, 'last_heartbeat': time.time()}
    
    # 4. START FRESH
    return start_scraping()

# ── Auto-sync engine ──────────────────────────────────────────────────────────

def _auto_oos_pass(scraper_id: str, csv_path: str, store_key: str,
                   stop_ev: threading.Event) -> dict:
    """
    Double-pass OOS removal for one scraper.

    Logic (true consecutive-miss guard):
      - Fetch current OOS list from Shopify (products tagged this scraper, absent from CSV).
      - Fetch pending entries from oos_pending_removal DB table.
      - Products in pending BUT now back in CSV → clear from pending (came back in stock).
      - Products in pending AND still OOS → second consecutive miss → delete + clear pending.
      - Products newly OOS (not in pending) → first miss → add to pending.

    Returns dict with counts: {cleared, flagged, deleted, skipped, failed}.
    """
    from core.shopify_publisher import check_oos_products, _set_store_key
    from core.shopify_publisher import _shopify_request, _try_verify_ownership, _try_remove, _has_scrapper_tag
    _set_store_key(store_key)

    cleared = flagged = deleted = skipped = failed = 0

    try:
        oos_result  = check_oos_products(scraper_id, csv_path)
        oos_list    = oos_result.get('oos', [])
    except Exception as e:
        logger.error(f"[AutoSync OOS] check_oos_products failed for {scraper_id}: {e}")
        return {'cleared': 0, 'flagged': 0, 'deleted': 0, 'skipped': 0, 'failed': 1}

    # Current OOS IDs from Shopify (strings)
    current_oos_ids = {str(p['id']) for p in oos_list}

    # Pending entries from DB
    pending_rows = get_oos_pending(scraper_id, store=store_key)
    pending_ids  = {r['id'] for r in pending_rows}  # already strings from DB helper

    # 1. Products that were pending but are NO LONGER OOS → clear (came back in stock)
    reinstated = pending_ids - current_oos_ids
    for pid in reinstated:
        if stop_ev.is_set():
            break
        try:
            clear_oos_pending(pid, store=store_key)
            logger.info(f"[AutoSync OOS] Reinstated (cleared pending): {pid} for {scraper_id}")
            cleared += 1
        except Exception as e:
            logger.warning(f"[AutoSync OOS] clear error {pid}: {e}")

    if stop_ev.is_set():
        return {'cleared': cleared, 'flagged': flagged, 'deleted': deleted, 'skipped': skipped, 'failed': failed}

    # 2. Products that are STILL OOS and were already pending → second consecutive miss → delete
    to_delete = [p for p in oos_list if str(p['id']) in pending_ids]
    for p in to_delete:
        if stop_ev.is_set():
            break
        pid = str(p['id'])
        try:
            current = _shopify_request("GET", f"/products/{pid}.json", params={"fields": "id,tags,title"})
            cp = current.get("product", {})
            if not _has_scrapper_tag(cp, scraper_id):
                logger.warning(f"[AutoSync OOS] SAFETY ABORT (tag): {pid}")
                skipped += 1
                continue
            if not _try_verify_ownership(pid, scraper_id):
                logger.warning(f"[AutoSync OOS] SAFETY ABORT (DB): {pid}")
                skipped += 1
                continue
            _shopify_request("DELETE", f"/products/{pid}.json")
            _try_remove(pid)
            clear_oos_pending(pid, store=store_key)
            log_shopify_action(scraper_id, 'auto_sync', status='success',
                               notes=f"phase:oos_delete | Deleted OOS (pass 2): {p.get('title', pid)[:80]}",
                               store=store_key)
            deleted += 1
        except Exception as e:
            logger.warning(f"[AutoSync OOS] delete error {pid}: {e}")
            failed += 1

    if stop_ev.is_set():
        return {'cleared': cleared, 'flagged': flagged, 'deleted': deleted, 'skipped': skipped, 'failed': failed}

    # 3. Products newly OOS (first miss) → flag into pending
    to_flag = [p for p in oos_list if str(p['id']) not in pending_ids]
    for p in to_flag:
        if stop_ev.is_set():
            break
        try:
            upsert_oos_pending(
                scraper_id, str(p['id']),
                title=p.get('title'), handle=p.get('handle'),
                store=store_key,
            )
            log_shopify_action(scraper_id, 'auto_sync', status='success',
                               notes=f"phase:oos_flag | Flagged OOS (pass 1): {p.get('title', p['id'])[:80]}",
                               store=store_key)
            flagged += 1
        except Exception as e:
            logger.warning(f"[AutoSync OOS] flag error {p['id']}: {e}")

    return {'cleared': cleared, 'flagged': flagged, 'deleted': deleted, 'skipped': skipped, 'failed': failed}


def _run_auto_sync(run_type: str = 'scheduled') -> None:
    """
    Main auto-sync orchestrator.  Runs as a daemon thread.

    For each active MAIN-store scraper (discovered from DB):
      1. Scrape  → wait for completion (retry once on failure)
      2. QC validate CSV
      3. Update existing Shopify products
      4. Upload new Shopify products
      5. Double-pass OOS removal

    All actions logged to auto_sync_runs and shopify_activity_logs.
    Cancel signal propagates to the currently-running scraper thread.
    """
    global _auto_sync_running, _auto_sync_stop_event, _auto_sync_current_scraper

    if _auto_sync_running:
        logger.warning("[AutoSync] Already running — skipping duplicate trigger")
        return

    _auto_sync_running = True
    _auto_sync_stop_event = threading.Event()
    _auto_sync_current_scraper = None
    stop_ev = _auto_sync_stop_event

    logger.info(f"[AutoSync] ▶ Starting {run_type} sync")

    # Discover active MAIN-store scrapers
    active = get_active_main_scrapers()
    if not active:
        logger.warning("[AutoSync] No active MAIN scrapers in DB — falling back to SHOPIFY_ALL_SCRAPERS")
        active = list(SHOPIFY_ALL_SCRAPERS)

    run_id = start_auto_sync_run(run_type, active, store='main')
    store_key = 'main'

    report = {
        'run_type': run_type,
        'scrapers': {},
        'totals': {
            'updated': 0, 'uploaded': 0,
            'oos_flagged': 0, 'oos_deleted': 0,
            'qc_failed': 0, 'scrape_failed': 0,
        },
    }

    def _run_scrape_and_wait(sid: str) -> bool:
        """
        Launch scraper, wait for it to finish, return True on success.

        Race-condition fix: set active_job_locks[sid] = True *before* starting
        the thread so the polling loop cannot exit prematurely before
        perform_scraping has had a chance to initialize the lock itself.

        Success is determined from the DB scrape record (status='completed'),
        not from scraper_progress status strings (which can be stale/ambiguous).
        """
        scrape_ev = threading.Event()
        active_stop_events[sid] = scrape_ev  # cancel propagation hook

        # Pre-arm the lock to prevent the poll loop from exiting before
        # the spawned thread initializes it (perform_scraping also sets it).
        active_job_locks[sid] = True
        scrape_record_id_before = None
        try:
            rec = get_latest_scrape_record(sid)
            scrape_record_id_before = rec['id'] if rec else None
        except Exception:
            pass

        t = threading.Thread(
            target=perform_scraping,
            args=('auto_sync@mirage.com', [sid]),
            daemon=True,
        )
        t.start()
        # Brief warmup: give the thread time to enter its initialization block
        time.sleep(3)

        MAX_WAIT = 7200
        waited = 0
        while waited < MAX_WAIT:
            if stop_ev.is_set():
                scrape_ev.set()  # propagate cancel to the scraper thread
                return False
            if not active_job_locks.get(sid):
                break
            time.sleep(15)
            waited += 15

        # Deterministic success: check DB scrape record status
        try:
            rec = get_latest_scrape_record(sid)
            if rec and rec.get('id') != scrape_record_id_before:
                return rec.get('status') == 'completed'
            # Fallback: record not yet written — use progress status
            prog = scraper_progress.get(sid, {})
            status = prog.get('status', '')
            return bool(status) and 'Error' not in status and 'Cancelled' not in status
        except Exception:
            prog = scraper_progress.get(sid, {})
            status = prog.get('status', '')
            return bool(status) and 'Error' not in status and 'Cancelled' not in status

    try:
        from core.shopify_publisher import upload_products, update_products, _set_store_key
        from core.quality_gate import validate_csv

        for sid in active:
            if stop_ev.is_set():
                logger.info(f"[AutoSync] Cancelled before {sid}")
                break

            _auto_sync_current_scraper = sid
            scraper_report = {
                'scrape': 'pending', 'qc': None,
                'update': {}, 'upload': {}, 'oos': {},
                'error': None, 'retried': False,
            }
            report['scrapers'][sid] = scraper_report

            # ── Step 1: Scrape (with one automatic retry) ─────────────────────
            logger.info(f"[AutoSync] ── Scraping {sid}…")
            scrape_ok = False
            try:
                scrape_ok = _run_scrape_and_wait(sid)
                if not scrape_ok and not stop_ev.is_set():
                    # Retry once
                    logger.warning(f"[AutoSync] {sid} scrape failed — retrying once…")
                    scraper_report['retried'] = True
                    time.sleep(5)
                    scrape_ok = _run_scrape_and_wait(sid)
            except Exception as e:
                logger.exception(f"[AutoSync] Scrape error {sid}: {e}")
                scraper_report['error'] = str(e)

            if stop_ev.is_set():
                scraper_report['scrape'] = 'cancelled'
                scraper_report['error'] = 'Sync cancelled during scrape'
                report['totals']['scrape_failed'] += 1
                break

            if not scrape_ok:
                prog = scraper_progress.get(sid, {})
                scraper_report['scrape'] = 'failed'
                scraper_report['error'] = prog.get('status', 'Scrape failed after retry')
                report['totals']['scrape_failed'] += 1
                logger.warning(f"[AutoSync] {sid} scrape failed after retry — skipping Shopify ops")
                continue

            scraper_report['scrape'] = 'ok'

            # ── Step 2: QC validate ───────────────────────────────────────────
            csv_path = _shopify_csv_path(sid)
            if not csv_path:
                scraper_report['qc'] = 'no_csv'
                logger.warning(f"[AutoSync] No CSV for {sid} after scrape — skipping")
                continue

            qc_result = {}
            try:
                qc_result = validate_csv(sid, csv_path)
                scraper_report['qc'] = {
                    'pass_rate': qc_result.get('pass_rate', 0),
                    'errors':    qc_result.get('errors', 0),
                    'warnings':  qc_result.get('warnings', 0),
                }
                if qc_result.get('errors', 0) > 0:
                    report['totals']['qc_failed'] += 1
                    logger.warning(f"[AutoSync] {sid} QC has {qc_result['errors']} errors — Shopify ops will be skipped")
                    scraper_report['error'] = f"QC blocked: {qc_result['errors']} errors"
                    continue
            except Exception as e:
                logger.warning(f"[AutoSync] QC error {sid}: {e}")
                scraper_report['qc'] = 'error'

            if stop_ev.is_set():
                break

            _set_store_key(store_key)

            # ── Step 3: Update existing products ──────────────────────────────
            logger.info(f"[AutoSync] ── Updating {sid}…")
            try:
                upd_result = update_products(sid, csv_path, stop_event=stop_ev)
                scraper_report['update'] = {
                    'updated': upd_result.get('updated', 0),
                    'skipped': upd_result.get('skipped', 0),
                    'failed':  upd_result.get('failed', 0),
                }
                report['totals']['updated'] += upd_result.get('updated', 0)
                log_shopify_action(sid, 'auto_sync', status='success',
                                   result=scraper_report['update'],
                                   notes='phase:update', store=store_key)
            except Exception as e:
                logger.exception(f"[AutoSync] Update error {sid}: {e}")
                scraper_report['update'] = {'error': str(e)}

            if stop_ev.is_set():
                break

            # ── Step 4: Upload new products ───────────────────────────────────
            logger.info(f"[AutoSync] ── Uploading new products for {sid}…")
            try:
                upl_result = upload_products(sid, csv_path, stop_event=stop_ev)
                scraper_report['upload'] = {
                    'created': upl_result.get('created', 0),
                    'skipped': upl_result.get('skipped', 0),
                    'failed':  upl_result.get('failed', 0),
                }
                report['totals']['uploaded'] += upl_result.get('created', 0)
                log_shopify_action(sid, 'auto_sync', status='success',
                                   result=scraper_report['upload'],
                                   notes='phase:upload', store=store_key)
            except Exception as e:
                logger.exception(f"[AutoSync] Upload error {sid}: {e}")
                scraper_report['upload'] = {'error': str(e)}

            if stop_ev.is_set():
                break

            # ── Step 5: Double-pass OOS removal ───────────────────────────────
            logger.info(f"[AutoSync] ── OOS pass for {sid}…")
            try:
                oos_r = _auto_oos_pass(sid, csv_path, store_key, stop_ev)
                scraper_report['oos'] = oos_r
                report['totals']['oos_flagged']  += oos_r.get('flagged', 0)
                report['totals']['oos_deleted']  += oos_r.get('deleted', 0)
            except Exception as e:
                logger.exception(f"[AutoSync] OOS pass error {sid}: {e}")
                scraper_report['oos'] = {'error': str(e)}

            # ── Step 6: Refresh monitoring baseline so the next hourly scan
            # detects only *new* changes, not the ones we just synced.
            try:
                take_snapshot_after_sync(sid)
                logger.info(f"[AutoSync] {sid}: monitoring baseline refreshed")
            except Exception as e:
                logger.warning(f"[AutoSync] {sid}: failed to refresh monitoring baseline: {e}")

        final_status = 'cancelled' if stop_ev.is_set() else 'completed'
        finish_auto_sync_run(run_id, final_status, report)
        log_shopify_action('__auto_sync__', 'auto_sync', status=final_status,
                           result=report.get('totals'), store=store_key,
                           notes=f"{run_type} sync — {len(active)} scrapers")
        logger.info(f"[AutoSync] ✅ {run_type} sync {final_status}. Totals: {report['totals']}")
        threading.Thread(
            target=_send_sync_email,
            args=(final_status, run_type, report.get('totals', {}), report.get('scrapers', {})),
            daemon=True,
        ).start()

    except Exception as e:
        logger.exception(f"[AutoSync] Fatal orchestrator error: {e}")
        try:
            finish_auto_sync_run(run_id, 'failed', {**report, 'fatal_error': str(e)})
        except Exception:
            pass
        threading.Thread(
            target=_send_sync_email,
            args=('failed', run_type, report.get('totals', {}), report.get('scrapers', {}), str(e)),
            daemon=True,
        ).start()
    finally:
        _auto_sync_running = False
        _auto_sync_current_scraper = None


def startup_auto_sync_scheduler() -> None:
    """Start APScheduler with 10:00 AM and 10:00 PM IST cron jobs.

    Also cleans up stale 'running' DB rows from previous restarts and
    restores the DO worker heartbeat from persistent storage.
    """
    global _auto_sync_scheduler, _do_worker_heartbeat

    # ── 1. Wait for DB tables to be created before attempting cleanup ──────────
    _db_tables_ready.wait(timeout=30)

    # ── 2. Clean up orphaned 'running' rows from previous process ─────────────
    fixed = cleanup_stale_sync_runs(stale_after_minutes=5)
    if fixed:
        logger.warning(f"[AutoSync] Startup cleanup: marked {fixed} orphaned run(s) as failed")

    # ── 2. Restore DO worker heartbeat from DB ─────────────────────────────────
    try:
        saved_hb = load_kv('do_worker_heartbeat')
        if saved_hb:
            import json as _json
            _do_worker_heartbeat = _json.loads(saved_hb)
            logger.info(f"[AutoSync] Restored DO worker heartbeat: last_seen={_do_worker_heartbeat.get('last_seen')}")
    except Exception as e:
        logger.warning(f"[AutoSync] Could not restore DO heartbeat: {e}")

    # ── 3. Start APScheduler ──────────────────────────────────────────────────
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import pytz

        ist = pytz.timezone('Asia/Kolkata')
        scheduler = BackgroundScheduler(timezone=ist)
        scheduler.add_job(
            lambda: threading.Thread(target=_run_auto_sync, args=('morning',), daemon=True).start(),
            CronTrigger(hour=10, minute=0, timezone=ist),
            id='auto_sync_morning',
            name='Auto Sync — 10 AM IST',
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: threading.Thread(target=_run_auto_sync, args=('evening',), daemon=True).start(),
            CronTrigger(hour=22, minute=0, timezone=ist),
            id='auto_sync_evening',
            name='Auto Sync — 10 PM IST',
            replace_existing=True,
        )
        # ── Hourly monitoring job ─────────────────────────────────────────
        scheduler.add_job(
            lambda: threading.Thread(
                target=run_monitoring_cycle, daemon=True
            ).start(),
            'interval',
            hours=1,
            id='monitor_hourly',
            name='Inventory Monitor — hourly',
            replace_existing=True,
        )

        scheduler.start()
        _auto_sync_scheduler = scheduler
        logger.info("🕙 Auto-sync scheduler started — fires at 10:00 AM and 10:00 PM IST daily + hourly monitoring")
    except Exception as e:
        logger.error(f"[AutoSync] Scheduler startup failed (non-fatal): {e}")


# ── Auto-sync API routes ──────────────────────────────────────────────────────

@app.route('/api/auto-sync/status', methods=['GET'])
def auto_sync_status():
    """
    Return auto-sync status: next scheduled run times, last run summary, running flag.

    Response includes:
      is_running      — bool
      next_run        — ISO string of the soonest upcoming scheduled run (or null)
      next_runs       — list of all upcoming scheduled jobs with name + next_run
      last_run        — full last auto_sync_runs DB row (or null)
      last_report     — report_json from last run (convenience alias)
      active_scrapers — scraper_ids with MAIN store products in DB
      current_scraper — scraper currently being processed (when is_running=true)
      scheduler_alive — bool
    """
    global _auto_sync_running, _auto_sync_scheduler, _auto_sync_current_scraper

    next_runs = []
    next_run  = None
    if _auto_sync_scheduler:
        try:
            for job in _auto_sync_scheduler.get_jobs():
                nf = job.next_run_time
                nf_iso = nf.isoformat() if nf else None
                next_runs.append({
                    'id':       job.id,
                    'name':     job.name,
                    'next_run': nf_iso,
                })
            # Soonest fire time across all jobs
            fire_times = [j.next_run_time for j in _auto_sync_scheduler.get_jobs()
                          if j.next_run_time]
            if fire_times:
                next_run = min(fire_times).isoformat()
        except Exception:
            pass

    last_run = None
    try:
        last_run = get_last_auto_sync_run(store='main')
    except Exception:
        pass

    active_scrapers = []
    try:
        active_scrapers = get_active_main_scrapers()
    except Exception:
        pass

    return jsonify({
        'is_running':              _auto_sync_running,
        'current_scraper':         _auto_sync_current_scraper,
        'next_run':                next_run,
        'next_runs':               next_runs,
        'last_run':                last_run,
        'last_report':             last_run.get('report_json') if last_run else None,
        'active_scrapers':         active_scrapers,
        'scheduler_alive':         _auto_sync_scheduler is not None and (
            _auto_sync_scheduler.running if hasattr(_auto_sync_scheduler, 'running') else False
        ),
        'last_triggered_by':       _last_triggered_by,
        'do_worker_last_heartbeat': _do_worker_heartbeat.get('last_seen'),
        'do_worker_version':        _do_worker_heartbeat.get('version'),
    }), 200


@app.route('/api/auto-sync/history', methods=['GET'])
def auto_sync_history():
    """Return the last 20 auto-sync run records."""
    from core.db import get_auto_sync_history
    limit = min(int(request.args.get('limit', 20)), 50)
    runs = get_auto_sync_history(store='main', limit=limit)
    return jsonify({'runs': runs, 'total': len(runs)})


@app.route('/api/health', methods=['GET'])
def health_check():
    """Lightweight public health endpoint — used by DO worker to wake Replit."""
    return jsonify({'ok': True, 'ts': datetime.utcnow().isoformat() + 'Z'}), 200


@app.route('/api/auto-sync/trigger', methods=['POST'])
def auto_sync_trigger():
    """Manually fire the auto-sync pipeline immediately (bypasses schedule).

    If SYNC_TOKEN env var is set, the request must supply a matching
    X-Sync-Token header.  When the var is unset all callers are accepted
    (backwards-compatible for local / manual use).
    """
    global _auto_sync_running, _last_triggered_by

    # ── Main store confirmation guard ─────────────────────────────────────────
    # Manual triggers always target the MAIN store pipeline, so the
    # X-Confirm-Main header is mandatory for every non-worker request.
    # DO-worker requests supply X-Sync-Token — they are server-side and
    # intentional, so they are exempted from the typed-confirmation requirement.
    is_do_worker = bool(request.headers.get('X-Sync-Token', '').strip())
    if not is_do_worker:
        if not _has_main_confirmation():
            logger.warning('[AutoSync] Trigger rejected — missing X-Confirm-Main header')
            return jsonify({'error': 'MAIN STORE write requires X-Confirm-Main: CONFIRM MAIN STORE ACTION header.'}), 403

    # ── Token auth (optional) ─────────────────────────────────────────────────
    # Only reject when a token IS provided but it's wrong.
    # Requests with no token (e.g. the browser dashboard "Run Now" button) are
    # always accepted — they're just labelled "manual" instead of "DigitalOcean worker".
    if _SYNC_TOKEN:
        provided = request.headers.get('X-Sync-Token', '').strip()
        if provided and provided != _SYNC_TOKEN:
            logger.warning('[AutoSync] Trigger rejected — invalid X-Sync-Token')
            return jsonify({'error': 'Unauthorized — invalid X-Sync-Token'}), 401

    if _auto_sync_running:
        return jsonify({'error': 'Auto-sync is already running'}), 409

    data = request.get_json(silent=True) or {}
    run_type = data.get('run_type', 'manual')

    # Detect whether this was fired by the DO worker (token present in header)
    _last_triggered_by = (
        'DigitalOcean worker'
        if request.headers.get('X-Sync-Token')
        else 'manual'
    )

    threading.Thread(target=_run_auto_sync, args=(run_type,), daemon=True).start()
    return jsonify({
        'message': f'Auto-sync ({run_type}) started',
        'run_type': run_type,
        'triggered_by': _last_triggered_by,
    }), 202


@app.route('/api/auto-sync/heartbeat', methods=['POST'])
def auto_sync_heartbeat():
    """Accept a heartbeat ping from the DigitalOcean worker.

    Body (JSON, all optional):
      version  — worker version string (e.g. "1.0.0")

    Validates the X-Sync-Token header when SYNC_TOKEN env var is set.
    """
    global _do_worker_heartbeat

    if _SYNC_TOKEN:
        provided = request.headers.get('X-Sync-Token', '').strip()
        if provided != _SYNC_TOKEN:
            logger.warning('[DO Heartbeat] Rejected — invalid or missing X-Sync-Token')
            return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    _do_worker_heartbeat = {
        'last_seen': datetime.utcnow().isoformat() + 'Z',
        'version':   data.get('version', _do_worker_heartbeat.get('version')),
    }
    # Persist to DB so heartbeat survives Flask restarts
    try:
        save_kv('do_worker_heartbeat', json.dumps(_do_worker_heartbeat))
    except Exception:
        pass
    logger.info(f"[DO Heartbeat] Received — version={_do_worker_heartbeat['version']}")
    return jsonify({'ok': True, 'recorded': _do_worker_heartbeat['last_seen']}), 200


@app.route('/api/auto-sync/cancel', methods=['POST'])
def auto_sync_cancel():
    """
    Cancel a running auto-sync.
    Propagates the stop signal to both the orchestrator and the currently-running
    scraper thread (via active_stop_events) so scraping stops immediately.
    """
    global _auto_sync_running, _auto_sync_stop_event, _auto_sync_current_scraper

    if not _auto_sync_running:
        return jsonify({'error': 'No auto-sync is currently running'}), 404

    # Signal orchestrator to stop after current unit
    _auto_sync_stop_event.set()

    # Propagate to the currently-running scraper thread (if any)
    sid = _auto_sync_current_scraper
    if sid:
        ev = active_stop_events.get(sid)
        if ev and not ev.is_set():
            ev.set()
            logger.info(f"[AutoSync Cancel] Propagated stop to scraper thread: {sid}")

    return jsonify({
        'message': 'Cancellation signal sent — sync will stop after current batch',
        'current_scraper': sid,
    }), 200


# ── Monitoring API routes ─────────────────────────────────────────────────────

@app.route('/api/monitoring/status', methods=['GET'])
def monitoring_status():
    """Return monitoring runtime state."""
    from core.monitor import get_monitor_state
    state = get_monitor_state()
    return jsonify({'state': state}), 200


@app.route('/api/monitoring/metrics', methods=['GET'])
def monitoring_metrics():
    """Return aggregate monitoring metrics."""
    from core.monitor_db import get_monitoring_metrics
    try:
        m = get_monitoring_metrics()
        return jsonify(m), 200
    except Exception as e:
        logger.exception(f"[Monitor] metrics error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/monitoring/changes', methods=['GET'])
def monitoring_changes():
    """Paginated inventory change log with optional filters."""
    from core.monitor_db import get_changes, get_changes_count
    scraper_id  = request.args.get('scraper_id')
    change_type = request.args.get('change_type')
    sync_status = request.args.get('sync_status')
    search      = request.args.get('search')
    date_from   = request.args.get('date_from')
    date_to     = request.args.get('date_to')
    limit       = min(int(request.args.get('limit', 50)), 200)
    offset      = int(request.args.get('offset', 0))
    try:
        changes = get_changes(
            scraper_id=scraper_id, change_type=change_type,
            sync_status=sync_status, search=search,
            date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
        total = get_changes_count(
            scraper_id=scraper_id, change_type=change_type,
            sync_status=sync_status, search=search,
            date_from=date_from, date_to=date_to,
        )
        # Collect distinct scraper ids for filter dropdown
        from core.monitor_db import get_connection as _gc, _return_connection as _rc
        scraper_ids_list = []
        try:
            conn2, cur2 = get_connection()
            cur2.execute("SELECT DISTINCT scraper_id FROM inventory_changes ORDER BY scraper_id")
            scraper_ids_list = [r[0] for r in cur2.fetchall()]
            cur2.close()
            _return_connection(conn2)
        except Exception:
            pass
        return jsonify({'changes': changes, 'total': total, 'scraper_ids': scraper_ids_list}), 200
    except Exception as e:
        logger.exception(f"[Monitor] changes endpoint error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/monitoring/scraper-status', methods=['GET'])
def monitoring_scraper_status():
    """Return per-scraper monitor run status + snapshot counts."""
    from core.monitor_db import get_scraper_monitor_status, list_snapshots
    try:
        conn2, cur2 = get_connection()
        cur2.execute("SELECT DISTINCT scraper_id FROM scraper_monitor_runs")
        all_sids = [r[0] for r in cur2.fetchall()]
        cur2.close()
        _return_connection(conn2)
    except Exception:
        all_sids = list(SHOPIFY_ALL_SCRAPERS)

    statuses = get_scraper_monitor_status(all_sids) if all_sids else {}

    # snapshot counts
    snap_counts = {}
    for sid in all_sids:
        snaps = list_snapshots(sid, limit=1)
        snap_counts[sid] = len(snaps)

    return jsonify({'scrapers': statuses, 'snapshot_counts': snap_counts}), 200


@app.route('/api/monitoring/snapshots/<scraper_id>', methods=['GET'])
def monitoring_snapshots(scraper_id: str):
    """List recent snapshots for a scraper."""
    from core.monitor_db import list_snapshots
    limit = min(int(request.args.get('limit', 20)), 100)
    snaps = list_snapshots(scraper_id, limit=limit)
    return jsonify({'snapshots': snaps, 'scraper_id': scraper_id}), 200


@app.route('/api/monitoring/scraper-counts', methods=['GET'])
def monitoring_scraper_counts():
    """Return per-scraper main-store vs source product counts."""
    from core.monitor import get_scraper_main_vs_source_counts
    counts = get_scraper_main_vs_source_counts()
    return jsonify({'scrapers': counts}), 200


@app.route('/api/monitoring/trigger', methods=['POST'])
def monitoring_trigger():
    """Manually trigger a monitoring cycle (runs in background thread)."""
    from core.monitor import run_monitoring_cycle, get_monitor_state
    state = get_monitor_state()
    if state['is_running']:
        return jsonify({'error': 'Monitoring cycle already running'}), 409

    data = request.get_json(silent=True) or {}
    scraper_ids = data.get('scraper_ids') or None  # None → all scrapers

    threading.Thread(
        target=run_monitoring_cycle,
        kwargs={'scraper_ids': scraper_ids},
        daemon=True,
    ).start()

    return jsonify({
        'message': 'Monitoring cycle started',
        'scraper_ids': scraper_ids or 'all',
    }), 202


@app.route('/api/monitoring/notifications', methods=['GET'])
def monitoring_notifications_list():
    """Return unseen dashboard notifications."""
    from core.monitor_db import get_unseen_notifications
    limit = min(int(request.args.get('limit', 50)), 200)
    notifs = get_unseen_notifications(limit=limit)
    return jsonify({'notifications': notifs, 'count': len(notifs)}), 200


@app.route('/api/monitoring/notifications/mark-seen', methods=['POST'])
def monitoring_notifications_mark_seen():
    """Mark dashboard notifications as seen."""
    from core.monitor_db import mark_notifications_seen
    data = request.get_json(silent=True) or {}
    ids = data.get('ids')  # list or None (None = mark all)
    updated = mark_notifications_seen(ids)
    return jsonify({'updated': updated}), 200


def startup_cruise_colcode_cache():
    """
    On startup, pre-build the Cruise Fashion 8-digit colour code cache in the background.
    This avoids a 12-minute inline block when the user first runs the Cruise scraper.
    Saves to scraped_files/cruise_colcode_cache.json once complete.
    """
    import re as _re
    import json as _json
    import threading as _th
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from curl_cffi import requests as _cr

    COLCODE_CACHE = "scraped_files/cruise_colcode_cache.json"
    PARTIAL_CACHE = COLCODE_CACHE + ".partial"

    if os.path.exists(COLCODE_CACHE):
        return  # Already built — nothing to do

    BRAND_SLUGS = [
        "off-white", "polo-ralph-lauren", "represent", "balenciaga", "dsquared2", "moschino",
        "tom-ford", "fred-perry", "palm-angels", "alexander-mcqueen", "versace", "amiri", "dolce",
        "casablanca", "billionaire-boys-club", "ralph-lauren", "givenchy", "diesel", "true-religion",
        "ami-paris", "kenzo", "balmain", "gucci", "heron-preston", "axel-arigato", "valentino",
        "jacquemus", "neil-barrett", "purple-brand", "rhude", "giuseppe-zanotti", "versace-jeans",
        "marcelo-burlon", "ted-baker", "vivienne-westwood", "steve-madden", "marc-jacobs", "burberry",
        "tory-burch", "self-portrait", "jean-paul-gaultier", "jimmy-choo", "stella-mccartney",
        "love-moschino", "chloe", "longchamp", "ugg", "golden-goose", "stone-island", "cp-company",
        "prada", "saint-laurent", "fendi", "moncler", "belstaff", "barbour", "boss", "armani",
        "calvin-klein", "tommy-hilfiger", "lacoste", "canada-goose", "marni", "loewe",
        "bottega-veneta", "louboutin", "michael-kors", "coach", "pinko", "ganni", "acne-studios"
    ]

    try:
        logger.info("🗺️ [STARTUP] Building Cruise Fashion colour code cache in background...")
        r = _cr.get("https://www.cruisefashion.com/sitemap/products-CRUS-0.xml.gz",
                    impersonate="chrome131", timeout=30)
        if not r or not r.ok:
            logger.warning("⚠️ [STARTUP] Could not fetch Cruise Fashion sitemap.")
            return
        all_urls = _re.findall(r'<loc>(https://www\.cruisefashion\.com/[^<]+)</loc>', r.text)
        target_urls = [u for u in all_urls if any(b in u.lower() for b in BRAND_SLUGS)]
        logger.info(f"🎯 [STARTUP] {len(target_urls)} target-brand URLs to resolve.")

        all_codes = set()
        lock = _th.Lock()
        done = [0]

        def _fetch(url):
            try:
                resp = _cr.get(url, impersonate="chrome131", timeout=10,
                               headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"})
                if resp and resp.ok:
                    return list(set(_re.findall(r'#colcode=(\d{7,9})', resp.text)))
                return []
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=30) as exc:
            futures = {exc.submit(_fetch, u): u for u in target_urls}
            for future in as_completed(futures):
                codes = future.result()
                with lock:
                    done[0] += 1
                    if codes:
                        all_codes.update(codes)
                    n = done[0]
                    if n % 3000 == 0:
                        logger.info(f"   [STARTUP CACHE] {n}/{len(target_urls)} pages | {len(all_codes)} colour codes")
                        os.makedirs("scraped_files", exist_ok=True)
                        with open(PARTIAL_CACHE, "w") as pf:
                            _json.dump(list(all_codes), pf)

        os.makedirs("scraped_files", exist_ok=True)
        with open(COLCODE_CACHE, "w") as cf:
            _json.dump(list(all_codes), cf)
        if os.path.exists(PARTIAL_CACHE):
            os.remove(PARTIAL_CACHE)
        logger.info(f"✅ [STARTUP] Cruise colour code cache built: {len(all_codes)} codes → {COLCODE_CACHE}")
    except Exception as e:
        logger.error(f"⚠️ [STARTUP] Cruise cache build error: {e}")


def startup_db_cleanup():
    # Deterministically reset in-memory run state — these dicts are already
    # empty on fresh process start, but explicit clearing is required for
    # correctness after a hot-reload or edge-case startup sequence.
    global active_job_locks, active_runs, active_stop_events
    active_job_locks.clear()
    active_runs.clear()
    active_stop_events.clear()

    try:
        init_db()
        conn, cur = get_connection()
        # Clean up any jobs that were 'running' when the server crashed/stopped.
        cur.execute("""
            UPDATE scrapes
            SET status = 'failed',
                completed_at = NOW(),
                error_message = 'Server restarted — job aborted'
            WHERE status = 'running'
        """)
        conn.commit()
        _return_connection(conn)
        logger.info("💾 Database ready and cleaned.")
    except Exception as e:
        logger.error(f"⚠️ DB startup notice: {e}")
    # Create Shopify audit tables (non-fatal if DB unavailable)
    try:
        init_shopify_tables()
    except Exception as e:
        logger.warning(f"⚠️ Shopify table init notice: {e}")
    # Create monitoring tables (non-fatal)
    try:
        init_monitoring_tables()
    except Exception as e:
        logger.warning(f"⚠️ Monitoring table init notice: {e}")
    finally:
        _db_tables_ready.set()  # Signal that all tables are now created

    # ── Seed product counts from local CSVs if production DB is empty ──────────
    # This ensures the dashboard shows correct counts after a fresh deployment.
    try:
        _seed_product_counts_from_csv()
    except Exception as e:
        logger.warning(f"⚠️ CSV seed notice: {e}")


def _seed_product_counts_from_csv():
    """
    On startup, if the products table has no row for a scraper but a local CSV
    exists, insert a lightweight metadata row (count + timestamp) so the
    dashboard shows the correct product count without needing a full re-scrape.
    """
    import csv as _csv
    import os as _os
    from core.db import get_connection as _get_conn, _return_connection as _ret_conn, upsert_all_product_data as _upsert

    scraper_csvs = {
        "coach":            "scraped_files/coach_latest.csv",
        "cruise_fashion":   "scraped_files/cruise_fashion_latest.csv",
        "michael_kors":     "scraped_files/michael_kors_latest.csv",
        "karl":             "scraped_files/karl_latest.csv",
        "marcjacobs":       "scraped_files/marcjacobs_latest.csv",
        "tory":             "scraped_files/tory_latest.csv",
        "mytheresa":        "scraped_files/mytheresa_latest.csv",
        "thedesignerboxuk": "scraped_files/thedesignerboxuk_latest.csv",
        "uk_polene":        "scraped_files/uk_polene_latest.csv",
        "hoka":             "scraped_files/hoka_latest.csv",
    }

    conn, cur = _get_conn()
    try:
        for scraper_id, csv_path in scraper_csvs.items():
            if not _os.path.exists(csv_path):
                continue

            # Check if this scraper already has a products row
            cur.execute("SELECT total_products FROM products WHERE website_url = %s", (scraper_id,))
            row = cur.fetchone()
            existing_count = int(row["total_products"]) if row and row["total_products"] else 0

            if existing_count > 0:
                logger.info(f"📊 [{scraper_id}] DB already has {existing_count} products — skipping seed.")
                continue

            # Count unique handles (products, not variant rows) in the CSV
            try:
                handles = set()
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row_data in reader:
                        h = row_data.get("Handle", "").strip()
                        if h:
                            handles.add(h)
                count = len(handles)
                if count == 0:
                    continue

                # Insert lightweight metadata row (no full products JSON)
                cur.execute("""
                    INSERT INTO products (website_url, type, total_products, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (website_url) DO UPDATE
                        SET total_products = EXCLUDED.total_products,
                            updated_at     = EXCLUDED.updated_at
                """, (scraper_id, {
                    "coach": "USD", "marcjacobs": "USD", "tory": "USD", "mytheresa": "EUR",
                    "hoka": "USD", "drmartens": "USD", "ugg": "USD",
                    "cruise_fashion": "GBP", "karl": "GBP",
                    "michael_kors": "INR",
                }.get(scraper_id, "USD"), str(count)))

                # Also insert a completed scrape record if none exists
                cur.execute("SELECT id FROM scrapes WHERE scraper_id = %s AND status = 'completed' LIMIT 1", (scraper_id,))
                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO scrapes (scraper_id, status, products_count, started_at, completed_at)
                        VALUES (%s, 'completed', %s, NOW(), NOW())
                    """, (scraper_id, count))

                conn.commit()
                logger.info(f"🌱 [{scraper_id}] Seeded {count} products from {csv_path}")

            except Exception as e:
                conn.rollback()
                logger.warning(f"⚠️ Seed failed for {scraper_id}: {e}")
    finally:
        _ret_conn(conn)

# ... (API routes are the same) ...

def _kill_port(port: int):
    """Kill any process listening on `port` (Platform aware)."""
    import platform
    if platform.system() == "Windows":
        try:
            import subprocess
            # shell=False: run netstat directly, filter in Python (no injection surface)
            out = subprocess.check_output(["netstat", "-ano"], shell=False).decode()
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.call(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return

    import signal as _signal
    hex_port = format(port, '04X')
    inode = None
    for proto in ('tcp', 'tcp6'):
        try:
            with open(f'/proc/net/{proto}') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    local_port = parts[1].split(':')[1].upper()
                    state = parts[3]
                    if local_port == hex_port and state == '0A':  # 0A = LISTEN
                        inode = parts[9]
                        break
        except Exception:
            pass
        if inode:
            break
    if not inode:
        return
    my_pid = str(os.getpid())
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or pid == my_pid:
                continue
            try:
                for fd in os.listdir(f'/proc/{pid}/fd'):
                    try:
                        link = os.readlink(f'/proc/{pid}/fd/{fd}')
                        if f'socket:[{inode}]' in link:
                            os.kill(int(pid), _signal.SIGKILL)
                            time.sleep(1)
                            return
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

# ── Processed images static route ─────────────────────────────────────────────
_PROCESSED_DIR = os.path.join(os.path.dirname(__file__), 'processed_images')
os.makedirs(_PROCESSED_DIR, exist_ok=True)

@app.route('/processed/<filename>')
def serve_processed_image(filename):
    """Serve BiRefNet-processed hero images so Shopify can fetch them via CSV."""
    return send_from_directory(_PROCESSED_DIR, filename)

@app.route('/api/image-processing/status', methods=['GET'])
def image_processing_status():
    """Return image-processing pipeline stats: queue depth, counts, cache hit rate."""
    try:
        from core.image_processor import get_image_processor
        proc = get_image_processor()
        return jsonify(proc.status())
    except Exception as e:
        logger.warning(f"[ImageProc] Status endpoint error: {e}")
        return jsonify({
            "enabled": False,
            "workers": 0,
            "queue_depth": 0,
            "processed": 0,
            "errors": 0,
            "cache_hits": 0,
            "cache_size": 0,
            "model": None,
            "error": str(e),
        })

# ── SPA catch-all (production: Flask serves the built React app) ─────────────
_DIST_DIR = os.path.join(os.path.dirname(__file__), 'dist')

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(os.path.join(_DIST_DIR, 'assets'), filename)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_spa(path):
    # Let /api/* routes fall through (they're registered above this catch-all)
    index = os.path.join(_DIST_DIR, 'index.html')
    if os.path.exists(index):
        return send_from_directory(_DIST_DIR, 'index.html')
    return jsonify({'error': 'Frontend not built. Run: npm run build'}), 404


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 8000))
    # Retry-aware startup: kill stale occupant, wait, retry up to 8 times
    import socket as _socket
    for _attempt in range(8):
        _kill_port(port)
        time.sleep(1.5)
        try:
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            _s.bind(('0.0.0.0', port))
            _s.close()
            break  # port is free
        except OSError:
            if _attempt < 7:
                logger.warning(f"Port {port} still busy (attempt {_attempt+1}/8), retrying...")
            else:
                raise
    print(f"Starting Flask backend on 0.0.0.0:{port}...")
    threading.Thread(target=startup_db_cleanup, daemon=True).start()
    threading.Thread(target=startup_cruise_colcode_cache, daemon=True).start()
    threading.Thread(target=_watchdog_stuck_jobs, daemon=True).start()
    threading.Thread(target=startup_auto_sync_scheduler, daemon=True).start()
    # Initialise the ImageProcessor singleton (lazy model load — startup is instant)
    try:
        from core.image_processor import get_image_processor as _get_ip
        _get_ip()
        logger.info("[Startup] ImageProcessor singleton initialised.")
    except Exception as _ip_e:
        logger.warning(f"[Startup] ImageProcessor init skipped: {_ip_e}")
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=port, threaded=True)
