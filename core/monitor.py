"""
Real-Time Product Monitoring Engine
====================================
Compares the latest scraped CSV for each active scraper against the previous
snapshot stored in the DB.  Detects OOS, back-in-stock, price changes, variant
changes, new products, and deleted products; emits structured change events;
and applies the corresponding Shopify action automatically.

Called from:
  • The hourly APScheduler job in startup_auto_sync_scheduler() (app.py)
  • POST /api/monitoring/trigger  (manual trigger from dashboard)
"""

import csv
import logging
import os
import re
import requests
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .monitor_db import (
    save_snapshot,
    get_latest_snapshot,
    record_change,
    mark_change_synced,
    start_monitor_run,
    finish_monitor_run,
    create_notification,
    get_pending_notifications,
    mark_notifications_sent,
)

logger = logging.getLogger(__name__)

# ─── Global state ─────────────────────────────────────────────────────────────
_monitor_running = False
_monitor_lock = threading.Lock()
_monitor_current_scraper: Optional[str] = None
_monitor_last_run: Optional[str] = None   # ISO timestamp of last completed cycle
_monitor_stop_event = threading.Event()

# ─── SMTP / notification settings ────────────────────────────────────────────
def _smtp_creds():
    return os.getenv("SMTP_FROM_EMAIL", "").strip(), os.getenv("SMTP_APP_PASSWORD", "").strip()

def _notify_email():
    return os.getenv("NOTIFY_EMAIL", "studioeditopia@gmail.com").strip()

# ─── Change-type human labels ─────────────────────────────────────────────────
CHANGE_LABELS = {
    "oos":            ("Out of Stock", "warning"),
    "back_in_stock":  ("Back in Stock", "info"),
    "new_product":    ("New Product Added", "info"),
    "deleted":        ("Product Removed", "warning"),
    "price_change":   ("Price Changed", "info"),
    "variant_change": ("Variant Change", "info"),
}


# ─── CSV helpers ─────────────────────────────────────────────────────────────

def _load_csv_fingerprint(csv_path: str) -> tuple[dict, int]:
    """
    Read a Shopify-format CSV and return:
      (fingerprint_dict, product_count)

    fingerprint_dict  →  handle →  {
        title, price, compare_price, variant_count,
        sku_set, source_url, has_inventory
    }
    """
    if not os.path.exists(csv_path):
        return {}, 0

    handle_data: dict[str, dict] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                handle = (row.get("Handle") or "").strip()
                if not handle:
                    continue
                price = (row.get("Variant Price") or "0").strip()
                sku = (row.get("Variant SKU") or "").strip()
                compare = (row.get("Variant Compare At Price") or "").strip()
                source_url = (row.get("Custom Product URL") or row.get("Source URL") or "").strip()
                qty_raw = (row.get("Variant Inventory Qty") or "").strip()
                has_inv = qty_raw not in ("", "0") if qty_raw else True

                if handle not in handle_data:
                    handle_data[handle] = {
                        "title":          (row.get("Title") or handle).strip(),
                        "price":          price,
                        "compare_price":  compare,
                        "variant_count":  0,
                        "sku_set":        set(),
                        "source_url":     source_url,
                        "has_inventory":  has_inv,
                    }
                d = handle_data[handle]
                if sku:
                    d["sku_set"].add(sku)
                if price and price not in ("0", "0.0", "0.00"):
                    d["variant_count"] += 1
                if has_inv:
                    d["has_inventory"] = True

    except Exception as e:
        logger.warning(f"[Monitor] CSV read error ({csv_path}): {e}")
        return {}, 0

    # Convert sets to lists for JSON serialisation
    for h in handle_data:
        handle_data[h]["sku_set"] = list(handle_data[h]["sku_set"])

    return handle_data, len(handle_data)


def _csv_path_for(scraper_id: str) -> Optional[str]:
    """Return the path of the latest Shopify CSV for a scraper, or None."""
    candidates = [
        f"scraped_files/{scraper_id}_latest.csv",
        f"scraped_files/{scraper_id}.csv",
        f"scraped_files/{scraper_id}_shopify.csv",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


# ─── Shopify URL helper ───────────────────────────────────────────────────────

def _shopify_product_url(handle: str) -> Optional[str]:
    store_url = os.getenv("MAIN_SHOPIFY_STORE_URL", "").strip().rstrip("/")
    if not store_url:
        return None
    domain = store_url.split("://", 1)[-1]
    return f"https://{domain}/products/{handle}"


# ─── Diffing engine ───────────────────────────────────────────────────────────

def _diff_snapshots(old: dict, new: dict) -> list[dict]:
    """
    Compare two handle-keyed fingerprint dicts and return a list of change
    event dicts suitable for passing to record_change().
    """
    changes = []
    old_handles = set(old.keys())
    new_handles = set(new.keys())

    # Products deleted from source
    for handle in old_handles - new_handles:
        prev = old[handle]
        changes.append({
            "change_type":    "deleted",
            "product_title":  prev["title"],
            "product_handle": handle,
            "sku":            (prev["sku_set"] or [""])[0] if prev["sku_set"] else None,
            "source_url":     prev.get("source_url"),
            "previous_value": {"variant_count": prev["variant_count"], "price": prev["price"]},
            "new_value":      None,
        })

    # New products
    for handle in new_handles - old_handles:
        curr = new[handle]
        changes.append({
            "change_type":    "new_product",
            "product_title":  curr["title"],
            "product_handle": handle,
            "sku":            (curr["sku_set"] or [""])[0] if curr["sku_set"] else None,
            "source_url":     curr.get("source_url"),
            "previous_value": None,
            "new_value":      {"variant_count": curr["variant_count"], "price": curr["price"]},
        })

    # Existing products — check for changes
    for handle in old_handles & new_handles:
        prev = old[handle]
        curr = new[handle]

        # OOS: had variants before, now zero or inventory gone
        prev_active = prev["variant_count"] > 0 and prev.get("has_inventory", True)
        curr_active = curr["variant_count"] > 0 and curr.get("has_inventory", True)

        if prev_active and not curr_active:
            changes.append({
                "change_type":    "oos",
                "product_title":  curr["title"],
                "product_handle": handle,
                "sku":            None,
                "source_url":     curr.get("source_url"),
                "previous_value": {"variant_count": prev["variant_count"], "price": prev["price"]},
                "new_value":      {"variant_count": curr["variant_count"], "price": curr["price"]},
            })
            continue

        if not prev_active and curr_active:
            changes.append({
                "change_type":    "back_in_stock",
                "product_title":  curr["title"],
                "product_handle": handle,
                "sku":            None,
                "source_url":     curr.get("source_url"),
                "previous_value": {"variant_count": prev["variant_count"], "price": prev["price"]},
                "new_value":      {"variant_count": curr["variant_count"], "price": curr["price"]},
            })
            continue

        # Price change
        try:
            old_price = float(prev["price"] or 0)
            new_price = float(curr["price"] or 0)
            if old_price and new_price and abs(old_price - new_price) > 0.01:
                changes.append({
                    "change_type":    "price_change",
                    "product_title":  curr["title"],
                    "product_handle": handle,
                    "sku":            None,
                    "source_url":     curr.get("source_url"),
                    "previous_value": {"price": prev["price"]},
                    "new_value":      {"price": curr["price"]},
                })
        except (ValueError, TypeError):
            pass

        # Variant count change (excluding OOS already detected above)
        if prev["variant_count"] != curr["variant_count"] and prev_active and curr_active:
            changes.append({
                "change_type":    "variant_change",
                "product_title":  curr["title"],
                "product_handle": handle,
                "sku":            None,
                "source_url":     curr.get("source_url"),
                "previous_value": {"variant_count": prev["variant_count"]},
                "new_value":      {"variant_count": curr["variant_count"]},
            })

    return changes


# ─── Shopify sync actions ────────────────────────────────────────────────────

def _apply_shopify_change(change: dict, scraper_id: str, change_id: int) -> None:
    """Apply a Shopify update for one detected change, with retries."""
    change_type = change["change_type"]
    handle = change.get("product_handle", "")

    try:
        from .shopify_publisher import _set_store_key, _shopify_request, get_scraper_products

        _set_store_key("main")

        if change_type == "oos":
            # Find the product in Shopify by handle and set to draft
            products = get_scraper_products(scraper_id)
            matched = [p for p in products if (p.get("handle") or "").split("-part-")[0] == handle.split("-part-")[0]]
            for p in matched:
                for attempt in range(3):
                    try:
                        _shopify_request("PUT", f"/products/{p['id']}.json",
                                         payload={"product": {"id": p["id"], "status": "draft"}})
                        logger.info(f"[Monitor] OOS → draft: {p['id']} ({handle})")
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
            mark_change_synced(change_id)

        elif change_type == "back_in_stock":
            # Re-publish draft products matching this handle
            products = get_scraper_products(scraper_id)
            matched = [p for p in products if (p.get("handle") or "").split("-part-")[0] == handle.split("-part-")[0]]
            for p in matched:
                for attempt in range(3):
                    try:
                        _shopify_request("PUT", f"/products/{p['id']}.json",
                                         payload={"product": {"id": p["id"], "status": "active"}})
                        logger.info(f"[Monitor] Back in stock → active: {p['id']} ({handle})")
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
            mark_change_synced(change_id)

        elif change_type == "deleted":
            # Archive (draft) the Shopify product
            products = get_scraper_products(scraper_id)
            matched = [p for p in products if (p.get("handle") or "").split("-part-")[0] == handle.split("-part-")[0]]
            for p in matched:
                for attempt in range(3):
                    try:
                        existing_tags = p.get("tags", "")
                        if isinstance(existing_tags, list):
                            existing_tags = ", ".join(existing_tags)
                        new_tags = existing_tags + ", archived-from-source" if existing_tags else "archived-from-source"
                        _shopify_request("PUT", f"/products/{p['id']}.json",
                                         payload={"product": {"id": p["id"], "status": "draft", "tags": new_tags}})
                        logger.info(f"[Monitor] Deleted → archived draft: {p['id']} ({handle})")
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
            mark_change_synced(change_id)

        elif change_type == "price_change":
            # Price changes are handled by the regular update cycle; mark as synced
            mark_change_synced(change_id)

        elif change_type == "variant_change":
            mark_change_synced(change_id)

        elif change_type == "new_product":
            # New products are handled by the upload pipeline in the regular sync
            mark_change_synced(change_id)

    except Exception as e:
        logger.warning(f"[Monitor] Shopify sync failed for change {change_id} ({change_type}): {e}")
        mark_change_synced(change_id, error=str(e))


# ─── Email notifications ──────────────────────────────────────────────────────

def _flush_email_notifications(notifications: list) -> None:
    """Send batched email alert for pending notifications."""
    from_email, app_password = _smtp_creds()
    if not from_email or not app_password:
        return

    email_notifs = [n for n in notifications if n["channel"] == "email"]
    if not email_notifs:
        return

    try:
        lines = []
        for n in email_notifs:
            label, _ = CHANGE_LABELS.get(n["event_type"], (n["event_type"], "info"))
            lines.append(f"<li><b>{label}</b> — {n.get('product_title','?')} ({n.get('scraper_id','?')})<br><small>{n.get('message','')}</small></li>")

        html = f"""
<html><body style="font-family:sans-serif;background:#0f0f12;color:#e2e8f0;padding:24px">
  <div style="max-width:600px;margin:auto;background:#1a1a24;border-radius:12px;padding:24px;border:1px solid #ffffff18">
    <h2 style="color:#818cf8;margin:0 0 16px">🔔 Mirage Monitoring Alerts</h2>
    <ul style="padding-left:20px;line-height:1.8;font-size:13px">
      {''.join(lines)}
    </ul>
    <hr style="border-color:#ffffff15;margin-top:24px"/>
    <p style="font-size:11px;color:#475569">Mirage Scraper Engine — real-time inventory monitoring</p>
  </div>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 Mirage — {len(email_notifs)} inventory alert(s)"
        msg["From"] = f"Mirage Monitor <{from_email}>"
        msg["To"] = _notify_email()
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as srv:
            srv.login(from_email, app_password)
            srv.sendmail(from_email, [_notify_email()], msg.as_string())

        ids = [n["id"] for n in email_notifs]
        mark_notifications_sent(ids)
        logger.info(f"[Monitor] Email alerts sent ({len(email_notifs)} events)")
    except Exception as e:
        logger.warning(f"[Monitor] Email flush failed: {e}")


# ─── Per-scraper monitor cycle ────────────────────────────────────────────────

def _monitor_one_scraper(scraper_id: str) -> dict:
    """
    Run one monitoring cycle for a single scraper.
    Returns summary dict: {products_checked, changes_found, errors}
    """
    run_id = start_monitor_run(scraper_id)
    products_checked = changes_found = 0
    errors = 0

    try:
        csv_path = _csv_path_for(scraper_id)
        if not csv_path:
            logger.info(f"[Monitor] {scraper_id}: no CSV found, skipping")
            finish_monitor_run(run_id, "skipped", error_message="No CSV file found") if run_id else None
            return {"products_checked": 0, "changes_found": 0, "errors": 0}

        # Build current fingerprint
        current_fp, product_count = _load_csv_fingerprint(csv_path)
        products_checked = product_count

        # Get previous snapshot
        prev_snap = get_latest_snapshot(scraper_id)

        if prev_snap is None:
            # No previous snapshot — save current as baseline, no changes to report
            save_snapshot(scraper_id, current_fp, product_count)
            logger.info(f"[Monitor] {scraper_id}: baseline snapshot saved ({product_count} products)")
            finish_monitor_run(run_id, "completed", products_checked=product_count, changes_found=0) if run_id else None
            return {"products_checked": product_count, "changes_found": 0, "errors": 0}

        prev_fp = prev_snap["snapshot_data"]

        # Diff
        raw_changes = _diff_snapshots(prev_fp, current_fp)

        # Persist each change event + queue notifications
        NOTIFY_TYPES = {"oos", "back_in_stock", "new_product", "deleted"}
        EMAIL_TYPES  = {"oos", "back_in_stock", "new_product", "deleted"}
        SEVERITY = {
            "oos":            "warning",
            "back_in_stock":  "info",
            "new_product":    "info",
            "deleted":        "warning",
            "price_change":   "info",
            "variant_change": "info",
        }

        for ch in raw_changes:
            shopify_url = _shopify_product_url(ch.get("product_handle", ""))
            change_id = record_change(
                scraper_id      = scraper_id,
                change_type     = ch["change_type"],
                product_title   = ch.get("product_title"),
                product_handle  = ch.get("product_handle"),
                source_url      = ch.get("source_url"),
                shopify_url     = shopify_url,
                sku             = ch.get("sku"),
                previous_value  = ch.get("previous_value"),
                new_value       = ch.get("new_value"),
                sync_status     = "pending",
            )
            changes_found += 1

            label, _ = CHANGE_LABELS.get(ch["change_type"], (ch["change_type"], "info"))
            msg = f"{label}: {ch.get('product_title','?')} [{scraper_id}]"

            if ch["change_type"] in NOTIFY_TYPES:
                # Dashboard notification
                create_notification(
                    event_type    = ch["change_type"],
                    scraper_id    = scraper_id,
                    product_title = ch.get("product_title"),
                    message       = msg,
                    severity      = SEVERITY.get(ch["change_type"], "info"),
                    channel       = "dashboard",
                    change_id     = change_id,
                )
                # Email notification
                if ch["change_type"] in EMAIL_TYPES:
                    create_notification(
                        event_type    = ch["change_type"],
                        scraper_id    = scraper_id,
                        product_title = ch.get("product_title"),
                        message       = msg,
                        severity      = SEVERITY.get(ch["change_type"], "info"),
                        channel       = "email",
                        change_id     = change_id,
                    )

            # Apply Shopify action in background for significant changes
            if ch["change_type"] in {"oos", "back_in_stock", "deleted"} and change_id:
                ch_copy = dict(ch)
                threading.Thread(
                    target=_apply_shopify_change,
                    args=(ch_copy, scraper_id, change_id),
                    daemon=True,
                ).start()

        # Save new snapshot (becomes baseline for next run)
        save_snapshot(scraper_id, current_fp, product_count)

        finish_monitor_run(run_id, "completed",
                           products_checked=product_count,
                           changes_found=changes_found) if run_id else None

        logger.info(
            f"[Monitor] {scraper_id}: {product_count} products checked, "
            f"{changes_found} changes detected"
        )

    except Exception as e:
        errors += 1
        logger.exception(f"[Monitor] {scraper_id} cycle error: {e}")
        finish_monitor_run(run_id, "failed", error_message=str(e)) if run_id else None

    return {"products_checked": products_checked, "changes_found": changes_found, "errors": errors}


# ─── Full monitoring cycle ────────────────────────────────────────────────────

def run_monitoring_cycle(scraper_ids: list = None) -> dict:
    """
    Run a complete monitoring cycle across all (or specified) active scrapers.
    Thread-safe: will refuse to start if already running.
    Returns summary dict.
    """
    global _monitor_running, _monitor_current_scraper, _monitor_last_run

    with _monitor_lock:
        if _monitor_running:
            logger.warning("[Monitor] Already running — skipping duplicate trigger")
            return {"skipped": True, "reason": "already_running"}
        _monitor_running = True
        _monitor_stop_event.clear()

    total_products = total_changes = total_errors = 0

    try:
        # Discover active scrapers
        if not scraper_ids:
            try:
                import json as _json, os as _os
                reg_path = "scrapers_registry.json"
                if _os.path.exists(reg_path):
                    with open(reg_path) as f:
                        reg = _json.load(f)
                    scraper_ids = [r["id"] for r in reg]
                else:
                    from scrapers_run import get_available_scrapers
                    scraper_ids = list(get_available_scrapers().keys())
            except Exception as e:
                logger.warning(f"[Monitor] Could not load scraper list: {e}")
                scraper_ids = []

        if not scraper_ids:
            logger.warning("[Monitor] No scrapers to monitor")
            return {"total_products": 0, "total_changes": 0, "total_errors": 0}

        logger.info(f"[Monitor] ▶ Starting monitoring cycle for {len(scraper_ids)} scrapers")

        for sid in scraper_ids:
            if _monitor_stop_event.is_set():
                logger.info("[Monitor] Stop event received — aborting cycle")
                break
            _monitor_current_scraper = sid
            result = _monitor_one_scraper(sid)
            total_products += result.get("products_checked", 0)
            total_changes  += result.get("changes_found", 0)
            total_errors   += result.get("errors", 0)

        _monitor_last_run = __import__("datetime").datetime.utcnow().isoformat() + "Z"

        # Flush email notifications (batched)
        try:
            pending = get_pending_notifications(limit=100)
            if pending:
                _flush_email_notifications(pending)
        except Exception as e:
            logger.warning(f"[Monitor] Email flush error: {e}")

        logger.info(
            f"[Monitor] ✅ Cycle complete — "
            f"{total_products} products, {total_changes} changes, {total_errors} errors"
        )
        return {
            "total_products": total_products,
            "total_changes":  total_changes,
            "total_errors":   total_errors,
            "scrapers":       len(scraper_ids),
        }

    except Exception as e:
        logger.exception(f"[Monitor] Cycle fatal error: {e}")
        return {"total_products": 0, "total_changes": 0, "total_errors": 1, "error": str(e)}
    finally:
        _monitor_running = False
        _monitor_current_scraper = None


def stop_monitoring_cycle() -> None:
    _monitor_stop_event.set()


def take_snapshot_after_sync(scraper_id: str) -> None:
    """
    Convenience function called after a successful auto-sync or manual scrape
    so the monitoring baseline is always current after Shopify is updated.
    """
    try:
        csv_path = _csv_path_for(scraper_id)
        if not csv_path:
            return
        fp, count = _load_csv_fingerprint(csv_path)
        if fp:
            save_snapshot(scraper_id, fp, count)
            logger.info(f"[Monitor] Post-sync snapshot saved for {scraper_id} ({count} products)")
    except Exception as e:
        logger.warning(f"[Monitor] Post-sync snapshot error ({scraper_id}): {e}")


# ─── Status accessors (for API) ───────────────────────────────────────────────

def get_monitor_state() -> dict:
    return {
        "is_running":       _monitor_running,
        "current_scraper":  _monitor_current_scraper,
        "last_run":         _monitor_last_run,
    }


# ─── Main-store vs source counts ──────────────────────────────────────────────

_counts_cache = {"data": None, "timestamp": 0}

def get_scraper_main_vs_source_counts(scraper_ids: list = None) -> dict:
    """
    Return, for each scraper, the number of products currently in the MAIN
    Shopify store (tagged RudraScrapper-{sid}) and the number in the latest
    source CSV.  Results are cached for 60 seconds so dashboard polling does
    not hammer Shopify.
    """
    global _counts_cache
    now = time.time()
    if _counts_cache["data"] and now - _counts_cache["timestamp"] < 60:
        return _counts_cache["data"]

    try:
        from .shopify_publisher import _set_store_key, _get_credentials, _scrapper_tag
        _set_store_key("main")
    except Exception as e:
        logger.warning(f"[Monitor] Cannot switch to main store for counts: {e}")
        return {}

    # Discover active scrapers
    if not scraper_ids:
        try:
            import json as _json, os as _os
            reg_path = "scrapers_registry.json"
            if _os.path.exists(reg_path):
                with open(reg_path) as f:
                    reg = _json.load(f)
                scraper_ids = [r["id"] for r in reg]
            else:
                from scrapers_run import get_available_scrapers
                scraper_ids = list(get_available_scrapers().keys())
        except Exception as e:
            logger.warning(f"[Monitor] Could not load scraper list for counts: {e}")
            scraper_ids = []

    if not scraper_ids:
        return {}

    from .monitor_db import get_scraper_monitor_status
    statuses = get_scraper_monitor_status(scraper_ids)

    result = {}
    for sid in scraper_ids:
        tag = _scrapper_tag(sid)
        status_info = statuses.get(sid, {})

        # Main Shopify count via GraphQL productsCount (reliable tag filtering)
        main_count = None
        try:
            store_url, token = _get_credentials()
            gql_url = f"https://{store_url}/admin/api/2025-01/graphql.json"
            headers = {
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            }
            query = f'{{ productsCount(query: "tag:{tag}") {{ count }} }}'
            resp = requests.post(gql_url, headers=headers, json={"query": query}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            main_count = int(data["data"]["productsCount"]["count"])
        except Exception as e:
            logger.warning(f"[Monitor] Main store count failed for {sid}: {e}")

        # Source CSV count
        source_count = None
        try:
            csv_path = _csv_path_for(sid)
            if csv_path:
                _, count = _load_csv_fingerprint(csv_path)
                source_count = count
        except Exception as e:
            logger.warning(f"[Monitor] Source CSV count failed for {sid}: {e}")

        # Only include scrapers that are actually active on the MAIN store
        # (have products tagged there). This keeps the dashboard list focused.
        if main_count and main_count > 0:
            result[sid] = {
                "main_count":       main_count,
                "source_count":     source_count,
                "status":           status_info.get("status") or "unknown",
                "products_checked": status_info.get("products_checked"),
                "changes_found":    status_info.get("changes_found"),
                "last_check":       status_info.get("completed_at"),
                "error_message":    status_info.get("error_message"),
            }

    _counts_cache = {"data": result, "timestamp": now}
    return result
