"""
DigitalOcean Worker — External Sync Scheduler
==============================================
Fires POST /api/auto-sync/trigger on the Replit app at:
  • 04:30 UTC  →  10:00 AM IST
  • 16:30 UTC  →  10:00 PM IST

Keep-alive strategy
-------------------
Replit sleeps after ~5 minutes of inactivity.  This worker sends a
heartbeat every 4 minutes, which is enough to keep the dyno awake
continuously.  Before each scheduled sync trigger it also runs a
dedicated "wake-and-wait" phase that polls /api/health until Replit
responds (up to 90 s), so the trigger never fires at a sleeping server.

Required env vars (set in DigitalOcean App Platform):
  REPLIT_APP_URL  — e.g. https://myapp.replit.app
  SYNC_TOKEN      — shared secret (same value set in Replit secrets)

Optional:
  WORKER_VERSION  — version label shown in the dashboard (default "1.0.0")
  POLL_INTERVAL   — seconds between status polls while a sync is running (default 60)
  SYNC_TIMEOUT_H  — hours before poll loop gives up waiting (default 3)
  WAKE_TIMEOUT    — seconds to wait for Replit to wake up (default 90)
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger("do_worker")

REPLIT_APP_URL  = os.environ["REPLIT_APP_URL"].rstrip("/")
SYNC_TOKEN      = os.environ["SYNC_TOKEN"]
WORKER_VERSION  = os.getenv("WORKER_VERSION", "1.0.0")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
SYNC_TIMEOUT_H  = float(os.getenv("SYNC_TIMEOUT_H", "3"))
WAKE_TIMEOUT    = int(os.getenv("WAKE_TIMEOUT", "90"))

AUTH_HEADERS = {
    "X-Sync-Token": SYNC_TOKEN,
    "Content-Type": "application/json",
}

HEALTH_URL    = f"{REPLIT_APP_URL}/api/health"
TRIGGER_URL   = f"{REPLIT_APP_URL}/api/auto-sync/trigger"
STATUS_URL    = f"{REPLIT_APP_URL}/api/auto-sync/status"
HEARTBEAT_URL = f"{REPLIT_APP_URL}/api/auto-sync/heartbeat"

# Trigger retry window — if Replit is still not responding after the
# wake phase, keep retrying for up to 10 more minutes.
MAX_RETRY_SECS  = 600
RETRY_INTERVAL  = 60


def _wake_replit() -> bool:
    """
    Poll /api/health until Replit responds with 200, giving it up to
    WAKE_TIMEOUT seconds to boot from sleep.

    Returns True if Replit is up, False if the timeout is exceeded.
    This runs BEFORE every sync trigger so we never fire at a sleeping server.
    """
    deadline = time.monotonic() + WAKE_TIMEOUT
    attempt  = 0
    logger.info(f"[Wake] Pinging {HEALTH_URL} — waiting up to {WAKE_TIMEOUT}s for Replit to wake…")

    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = requests.get(HEALTH_URL, timeout=10)
            if resp.status_code == 200:
                logger.info(f"[Wake] ✅ Replit is awake (attempt {attempt})")
                return True
            logger.info(f"[Wake] Got {resp.status_code} on attempt {attempt}, retrying…")
        except requests.exceptions.RequestException as exc:
            logger.info(f"[Wake] Attempt {attempt} — {exc}, retrying…")
        time.sleep(5)

    logger.error(f"[Wake] ❌ Replit did not wake within {WAKE_TIMEOUT}s — aborting trigger.")
    return False


def _post_trigger(run_type: str) -> bool:
    """
    POST to the trigger endpoint with retry for transient failures.
    Returns True when the sync was started successfully, False if all retries failed.
    """
    deadline = time.monotonic() + MAX_RETRY_SECS
    attempt  = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            logger.info(f"[Trigger] Attempt {attempt} — POST {TRIGGER_URL} (run_type={run_type})")
            resp = requests.post(
                TRIGGER_URL,
                headers=AUTH_HEADERS,
                json={"run_type": run_type},
                timeout=30,
            )
            if resp.status_code == 202:
                logger.info(f"[Trigger] ✅ Accepted — {resp.json()}")
                return True
            if resp.status_code == 409:
                logger.info("[Trigger] ℹ️  Sync already running — skipping this window.")
                return True
            if resp.status_code == 401:
                logger.error("[Trigger] ❌ 401 Unauthorized — check SYNC_TOKEN. Aborting.")
                return False
            logger.warning(
                f"[Trigger] ⚠️  Unexpected {resp.status_code}: {resp.text[:200]}, "
                f"retry in {RETRY_INTERVAL}s"
            )
        except requests.exceptions.Timeout:
            logger.warning(f"[Trigger] ⏳ Request timed out, retry in {RETRY_INTERVAL}s")
        except requests.exceptions.ConnectionError as exc:
            logger.warning(f"[Trigger] ⏳ Connection error ({exc}), retry in {RETRY_INTERVAL}s")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(RETRY_INTERVAL, remaining))

    logger.error(f"[Trigger] ❌ All retries exhausted after {MAX_RETRY_SECS}s. Giving up.")
    return False


def _poll_until_complete() -> None:
    """
    Poll /api/auto-sync/status every POLL_INTERVAL seconds until the sync
    finishes (state becomes 'idle') or SYNC_TIMEOUT_H hours pass.
    """
    deadline = time.monotonic() + SYNC_TIMEOUT_H * 3600
    logger.info(f"[Poll] Polling status (timeout={SYNC_TIMEOUT_H}h, interval={POLL_INTERVAL}s)")

    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp = requests.get(STATUS_URL, headers=AUTH_HEADERS, timeout=20)
            if resp.status_code != 200:
                logger.warning(f"[Poll] Status endpoint returned {resp.status_code}, will retry")
                continue
            data       = resp.json()
            is_running = data.get("is_running", False)
            current    = data.get("current_scraper") or "—"
            logger.info(f"[Poll] is_running={is_running}  current_scraper={current}")
            if not is_running:
                last_run = data.get("last_run") or {}
                logger.info(f"[Poll] ✅ Sync finished — status: {last_run.get('status', 'unknown')}")
                return
        except requests.exceptions.RequestException as exc:
            logger.warning(f"[Poll] Request error: {exc}")

    logger.error(f"[Poll] ⏰ Polling timeout after {SYNC_TIMEOUT_H}h")


def _run_sync(run_type: str) -> None:
    label = "10:00 AM" if run_type == "morning" else "10:00 PM"
    logger.info(f"═══ Scheduled sync: {run_type} ({label} IST) ═══")

    # Step 1 — wake Replit if it's sleeping
    awake = _wake_replit()
    if not awake:
        logger.error("[Sync] Aborting — Replit is unreachable after wake attempts.")
        return

    # Step 2 — fire the sync trigger
    started = _post_trigger(run_type)
    if started:
        _poll_until_complete()

    logger.info(f"═══ Sync window complete: {run_type} ═══")


def _send_heartbeat() -> None:
    """
    Ping Replit every 4 minutes to prevent it from going to sleep.
    A lightweight GET to /api/health is used so even if the auth token
    is wrong the server stays awake; the POST heartbeat is also sent
    for the dashboard badge.
    """
    # First: wake ping (no auth needed — just keeps the dyno alive)
    try:
        requests.get(HEALTH_URL, timeout=10)
    except Exception:
        pass

    # Second: authenticated heartbeat for the dashboard badge
    try:
        resp = requests.post(
            HEARTBEAT_URL,
            headers=AUTH_HEADERS,
            json={"version": WORKER_VERSION},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info(f"[Heartbeat] ✅ OK (version={WORKER_VERSION})")
        else:
            logger.warning(f"[Heartbeat] ⚠️  {resp.status_code}: {resp.text[:100]}")
    except requests.exceptions.RequestException as exc:
        logger.warning(f"[Heartbeat] ⚠️  Failed: {exc}")


def main() -> None:
    logger.info(f"DigitalOcean Sync Worker v{WORKER_VERSION} starting…")
    logger.info(f"  Target  : {REPLIT_APP_URL}")
    logger.info(f"  Schedule: 04:30 UTC (10 AM IST) and 16:30 UTC (10 PM IST)")
    logger.info(f"  Heartbeat every 4 minutes (keeps Replit awake)")
    logger.info(f"  Wake timeout: {WAKE_TIMEOUT}s before each trigger")

    _send_heartbeat()

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        lambda: _run_sync("morning"),
        trigger="cron",
        hour=4, minute=30,
        id="sync_morning",
        name="Morning sync (10:00 AM IST)",
        max_instances=1,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        lambda: _run_sync("evening"),
        trigger="cron",
        hour=16, minute=30,
        id="sync_evening",
        name="Evening sync (10:00 PM IST)",
        max_instances=1,
        misfire_grace_time=300,
    )

    # 4-minute heartbeat — Replit sleeps after ~5 min inactivity
    scheduler.add_job(
        _send_heartbeat,
        trigger="interval",
        minutes=4,
        id="heartbeat",
        name="Keep-alive heartbeat (every 4 min)",
        max_instances=1,
    )

    logger.info("Scheduler started — waiting for next fire time.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker shutting down.")


if __name__ == "__main__":
    main()
