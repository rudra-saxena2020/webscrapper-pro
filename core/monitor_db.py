"""
Monitoring Database Layer
=========================
All DB functions for the real-time product monitoring & inventory sync system.
"""

import json
import logging
from datetime import datetime, timezone
from psycopg2.extras import DictCursor

from .db import get_connection, _return_connection

logger = logging.getLogger(__name__)


# ─── Schema initialisation ────────────────────────────────────────────────────

def init_monitoring_tables() -> None:
    """Create all monitoring tables and indexes if they don't already exist."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            logger.warning("[Monitor] No DB connection — monitoring tables skipped (local mode)")
            return

        # Product snapshots — compact fingerprint of each scraper's product list
        cur.execute("""
            CREATE TABLE IF NOT EXISTS product_snapshots (
                id          SERIAL PRIMARY KEY,
                scraper_id  TEXT NOT NULL,
                snapshot_data JSONB NOT NULL,
                product_count INTEGER DEFAULT 0,
                taken_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_snapshots_scraper
            ON product_snapshots(scraper_id, taken_at DESC);
        """)

        # Inventory change events
        cur.execute("""
            CREATE TABLE IF NOT EXISTS inventory_changes (
                id                 SERIAL PRIMARY KEY,
                scraper_id         TEXT NOT NULL,
                change_type        TEXT NOT NULL,
                product_title      TEXT,
                product_handle     TEXT,
                shopify_product_id TEXT,
                source_url         TEXT,
                shopify_url        TEXT,
                sku                TEXT,
                previous_value     JSONB,
                new_value          JSONB,
                sync_status        TEXT NOT NULL DEFAULT 'pending',
                sync_error         TEXT,
                detected_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                synced_at          TIMESTAMPTZ
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_inv_changes_scraper
            ON inventory_changes(scraper_id, detected_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_inv_changes_type
            ON inventory_changes(change_type, detected_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_inv_changes_sync
            ON inventory_changes(sync_status, detected_at DESC);
        """)

        # Monitor run log — one row per hourly cycle
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scraper_monitor_runs (
                id              SERIAL PRIMARY KEY,
                scraper_id      TEXT NOT NULL,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at    TIMESTAMPTZ,
                status          TEXT NOT NULL DEFAULT 'running',
                products_checked INTEGER DEFAULT 0,
                changes_found   INTEGER DEFAULT 0,
                error_message   TEXT
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_monitor_runs_scraper
            ON scraper_monitor_runs(scraper_id, started_at DESC);
        """)

        # In-app + email notifications
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_notifications (
                id            SERIAL PRIMARY KEY,
                event_type    TEXT NOT NULL,
                scraper_id    TEXT,
                product_title TEXT,
                message       TEXT,
                severity      TEXT NOT NULL DEFAULT 'info',
                channel       TEXT NOT NULL DEFAULT 'dashboard',
                status        TEXT NOT NULL DEFAULT 'pending',
                change_id     INTEGER,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at       TIMESTAMPTZ,
                seen_at       TIMESTAMPTZ
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_notif_status
            ON monitoring_notifications(status, created_at DESC);
        """)

        conn.commit()
        cur.close()
        logger.info("[Monitor] Monitoring tables ready.")
    except Exception as e:
        logger.error(f"[Monitor] Table init error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            _return_connection(conn)


# ─── Snapshots ────────────────────────────────────────────────────────────────

def save_snapshot(scraper_id: str, snapshot_data: dict, product_count: int) -> int | None:
    """Store a product fingerprint snapshot for a scraper. Returns new row ID."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return None
        cur.execute(
            """
            INSERT INTO product_snapshots (scraper_id, snapshot_data, product_count)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (scraper_id, json.dumps(snapshot_data), product_count),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return row_id
    except Exception as e:
        logger.error(f"[Monitor] save_snapshot error ({scraper_id}): {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def get_latest_snapshot(scraper_id: str) -> dict | None:
    """Return the most recent snapshot dict for a scraper, or None."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return None
        cur.execute(
            """
            SELECT id, snapshot_data, product_count, taken_at
            FROM product_snapshots
            WHERE scraper_id = %s
            ORDER BY taken_at DESC
            LIMIT 1
            """,
            (scraper_id,),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {
                "id": row["id"],
                "snapshot_data": row["snapshot_data"] if isinstance(row["snapshot_data"], dict) else json.loads(row["snapshot_data"]),
                "product_count": row["product_count"],
                "taken_at": row["taken_at"].isoformat() if row["taken_at"] else None,
            }
        return None
    except Exception as e:
        logger.error(f"[Monitor] get_latest_snapshot error ({scraper_id}): {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def list_snapshots(scraper_id: str, limit: int = 20) -> list:
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return []
        cur.execute(
            """
            SELECT id, product_count, taken_at
            FROM product_snapshots
            WHERE scraper_id = %s
            ORDER BY taken_at DESC
            LIMIT %s
            """,
            (scraper_id, limit),
        )
        rows = cur.fetchall()
        cur.close()
        return [{"id": r["id"], "product_count": r["product_count"],
                 "taken_at": r["taken_at"].isoformat() if r["taken_at"] else None} for r in rows]
    except Exception as e:
        logger.error(f"[Monitor] list_snapshots error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


# ─── Change events ────────────────────────────────────────────────────────────

def record_change(
    scraper_id: str,
    change_type: str,
    product_title: str = None,
    product_handle: str = None,
    shopify_product_id: str = None,
    source_url: str = None,
    shopify_url: str = None,
    sku: str = None,
    previous_value: dict = None,
    new_value: dict = None,
    sync_status: str = "pending",
) -> int | None:
    """Insert one inventory change event. Returns the new row ID."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return None
        cur.execute(
            """
            INSERT INTO inventory_changes
              (scraper_id, change_type, product_title, product_handle,
               shopify_product_id, source_url, shopify_url, sku,
               previous_value, new_value, sync_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                scraper_id, change_type, product_title, product_handle,
                shopify_product_id, source_url, shopify_url, sku,
                json.dumps(previous_value) if previous_value else None,
                json.dumps(new_value) if new_value else None,
                sync_status,
            ),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return row_id
    except Exception as e:
        logger.error(f"[Monitor] record_change error: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def mark_change_synced(change_id: int, error: str = None) -> None:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        status = "failed" if error else "synced"
        cur.execute(
            """
            UPDATE inventory_changes
            SET sync_status = %s, sync_error = %s, synced_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (status, error, change_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"[Monitor] mark_change_synced error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def get_changes(
    scraper_id: str = None,
    change_type: str = None,
    sync_status: str = None,
    search: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return []
        where, params = [], []
        if scraper_id and scraper_id != "all":
            where.append("scraper_id = %s"); params.append(scraper_id)
        if change_type and change_type != "all":
            where.append("change_type = %s"); params.append(change_type)
        if sync_status and sync_status != "all":
            where.append("sync_status = %s"); params.append(sync_status)
        if search:
            where.append("(product_title ILIKE %s OR product_handle ILIKE %s OR sku ILIKE %s OR shopify_product_id ILIKE %s)")
            params += [f"%{search}%"] * 4
        if date_from:
            where.append("detected_at >= %s"); params.append(date_from)
        if date_to:
            where.append("detected_at <= %s"); params.append(date_to + "T23:59:59")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(
            f"""
            SELECT id, scraper_id, change_type, product_title, product_handle,
                   shopify_product_id, source_url, shopify_url, sku,
                   previous_value, new_value, sync_status, sync_error,
                   detected_at, synced_at
            FROM inventory_changes
            {where_sql}
            ORDER BY detected_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
        cur.close()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "scraper_id": r["scraper_id"],
                "change_type": r["change_type"],
                "product_title": r["product_title"],
                "product_handle": r["product_handle"],
                "shopify_product_id": r["shopify_product_id"],
                "source_url": r["source_url"],
                "shopify_url": r["shopify_url"],
                "sku": r["sku"],
                "previous_value": r["previous_value"],
                "new_value": r["new_value"],
                "sync_status": r["sync_status"],
                "sync_error": r["sync_error"],
                "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
                "synced_at": r["synced_at"].isoformat() if r["synced_at"] else None,
            })
        return result
    except Exception as e:
        logger.error(f"[Monitor] get_changes error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def get_changes_count(
    scraper_id: str = None,
    change_type: str = None,
    sync_status: str = None,
    search: str = None,
    date_from: str = None,
    date_to: str = None,
) -> int:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return 0
        where, params = [], []
        if scraper_id and scraper_id != "all":
            where.append("scraper_id = %s"); params.append(scraper_id)
        if change_type and change_type != "all":
            where.append("change_type = %s"); params.append(change_type)
        if sync_status and sync_status != "all":
            where.append("sync_status = %s"); params.append(sync_status)
        if search:
            where.append("(product_title ILIKE %s OR product_handle ILIKE %s OR sku ILIKE %s OR shopify_product_id ILIKE %s)")
            params += [f"%{search}%"] * 4
        if date_from:
            where.append("detected_at >= %s"); params.append(date_from)
        if date_to:
            where.append("detected_at <= %s"); params.append(date_to + "T23:59:59")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(f"SELECT COUNT(*) FROM inventory_changes {where_sql}", params)
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception as e:
        logger.error(f"[Monitor] get_changes_count error: {e}")
        return 0
    finally:
        if conn:
            _return_connection(conn)


def get_monitoring_metrics() -> dict:
    """Aggregate dashboard numbers for today + all time."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return _empty_metrics()
        # All-time totals
        cur.execute("""
            SELECT
                COUNT(*)                                                  AS total_changes,
                COUNT(*) FILTER (WHERE change_type = 'oos')               AS total_oos,
                COUNT(*) FILTER (WHERE change_type = 'back_in_stock')     AS total_back,
                COUNT(*) FILTER (WHERE change_type = 'new_product')       AS total_new,
                COUNT(*) FILTER (WHERE change_type = 'deleted')           AS total_deleted,
                COUNT(*) FILTER (WHERE sync_status = 'failed')            AS total_sync_fail,
                COUNT(*) FILTER (WHERE detected_at >= CURRENT_DATE)       AS today_changes,
                COUNT(*) FILTER (WHERE change_type='oos'
                                   AND detected_at >= CURRENT_DATE)       AS today_oos,
                COUNT(*) FILTER (WHERE change_type='back_in_stock'
                                   AND detected_at >= CURRENT_DATE)       AS today_back,
                COUNT(*) FILTER (WHERE change_type='new_product'
                                   AND detected_at >= CURRENT_DATE)       AS today_new,
                COUNT(*) FILTER (WHERE change_type='deleted'
                                   AND detected_at >= CURRENT_DATE)       AS today_deleted,
                COUNT(*) FILTER (WHERE sync_status='failed'
                                   AND detected_at >= CURRENT_DATE)       AS today_sync_fail
            FROM inventory_changes
        """)
        row = cur.fetchone()

        # Latest successful monitor run
        cur.execute("""
            SELECT MAX(completed_at) AS last_run
            FROM scraper_monitor_runs
            WHERE status = 'completed'
        """)
        run_row = cur.fetchone()

        # Products monitored (latest snapshot per scraper, summed)
        cur.execute("""
            SELECT COALESCE(SUM(product_count), 0) AS total_monitored
            FROM (
                SELECT DISTINCT ON (scraper_id) product_count
                FROM product_snapshots
                ORDER BY scraper_id, taken_at DESC
            ) sub
        """)
        prod_row = cur.fetchone()

        cur.close()
        d = dict(row) if row else {}
        return {
            "total_changes":       int(d.get("total_changes", 0) or 0),
            "total_oos":           int(d.get("total_oos", 0) or 0),
            "total_back":          int(d.get("total_back", 0) or 0),
            "total_new":           int(d.get("total_new", 0) or 0),
            "total_deleted":       int(d.get("total_deleted", 0) or 0),
            "total_sync_fail":     int(d.get("total_sync_fail", 0) or 0),
            "today_changes":       int(d.get("today_changes", 0) or 0),
            "today_oos":           int(d.get("today_oos", 0) or 0),
            "today_back":          int(d.get("today_back", 0) or 0),
            "today_new":           int(d.get("today_new", 0) or 0),
            "today_deleted":       int(d.get("today_deleted", 0) or 0),
            "today_sync_fail":     int(d.get("today_sync_fail", 0) or 0),
            "last_monitor_run":    run_row["last_run"].isoformat() if (run_row and run_row["last_run"]) else None,
            "total_monitored":     int((prod_row["total_monitored"] if prod_row else 0) or 0),
        }
    except Exception as e:
        logger.error(f"[Monitor] get_monitoring_metrics error: {e}")
        return _empty_metrics()
    finally:
        if conn:
            _return_connection(conn)


def _empty_metrics() -> dict:
    keys = ["total_changes","total_oos","total_back","total_new","total_deleted","total_sync_fail",
            "today_changes","today_oos","today_back","today_new","today_deleted","today_sync_fail","total_monitored"]
    return {k: 0 for k in keys} | {"last_monitor_run": None}


# ─── Monitor run log ──────────────────────────────────────────────────────────

def start_monitor_run(scraper_id: str) -> int | None:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return None
        cur.execute(
            "INSERT INTO scraper_monitor_runs (scraper_id) VALUES (%s) RETURNING id",
            (scraper_id,),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return row_id
    except Exception as e:
        logger.error(f"[Monitor] start_monitor_run error: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def finish_monitor_run(
    run_id: int,
    status: str,
    products_checked: int = 0,
    changes_found: int = 0,
    error_message: str = None,
) -> None:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            """
            UPDATE scraper_monitor_runs
            SET status=%s, products_checked=%s, changes_found=%s,
                error_message=%s, completed_at=CURRENT_TIMESTAMP
            WHERE id=%s
            """,
            (status, products_checked, changes_found, error_message, run_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"[Monitor] finish_monitor_run error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def get_scraper_monitor_status(scraper_ids: list) -> dict:
    """Return the latest monitor run status for each scraper."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return {}
        cur.execute(
            """
            SELECT DISTINCT ON (scraper_id)
                scraper_id, status, products_checked, changes_found,
                started_at, completed_at, error_message
            FROM scraper_monitor_runs
            WHERE scraper_id = ANY(%s)
            ORDER BY scraper_id, started_at DESC
            """,
            (scraper_ids,),
        )
        rows = cur.fetchall()
        cur.close()
        result = {}
        for r in rows:
            result[r["scraper_id"]] = {
                "status":           r["status"],
                "products_checked": r["products_checked"],
                "changes_found":    r["changes_found"],
                "started_at":       r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at":     r["completed_at"].isoformat() if r["completed_at"] else None,
                "error_message":    r["error_message"],
            }
        return result
    except Exception as e:
        logger.error(f"[Monitor] get_scraper_monitor_status error: {e}")
        return {}
    finally:
        if conn:
            _return_connection(conn)


# ─── Notifications ────────────────────────────────────────────────────────────

def create_notification(
    event_type: str,
    scraper_id: str = None,
    product_title: str = None,
    message: str = None,
    severity: str = "info",
    channel: str = "dashboard",
    change_id: int = None,
) -> int | None:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return None
        cur.execute(
            """
            INSERT INTO monitoring_notifications
              (event_type, scraper_id, product_title, message, severity, channel, change_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (event_type, scraper_id, product_title, message, severity, channel, change_id),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return row_id
    except Exception as e:
        logger.error(f"[Monitor] create_notification error: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def get_pending_notifications(limit: int = 50) -> list:
    """Return only unsent notifications (status='pending') — never re-returns after mark_notifications_sent."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return []
        cur.execute(
            """
            SELECT id, event_type, scraper_id, product_title, message,
                   severity, channel, status, change_id, created_at
            FROM monitoring_notifications
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id":            r["id"],
                "event_type":    r["event_type"],
                "scraper_id":    r["scraper_id"],
                "product_title": r["product_title"],
                "message":       r["message"],
                "severity":      r["severity"],
                "channel":       r["channel"],
                "status":        r["status"],
                "change_id":     r["change_id"],
                "created_at":    r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Monitor] get_pending_notifications error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def get_unseen_notifications(limit: int = 20) -> list:
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return []
        cur.execute(
            """
            SELECT id, event_type, scraper_id, product_title, message,
                   severity, created_at
            FROM monitoring_notifications
            WHERE status != 'seen' AND channel = 'dashboard'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id":            r["id"],
                "event_type":    r["event_type"],
                "scraper_id":    r["scraper_id"],
                "product_title": r["product_title"],
                "message":       r["message"],
                "severity":      r["severity"],
                "created_at":    r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Monitor] get_unseen_notifications error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def mark_notifications_seen(notification_ids: list = None) -> int:
    """Mark notifications as seen. If ids is None, marks all dashboard ones seen."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return 0
        if notification_ids:
            cur.execute(
                "UPDATE monitoring_notifications SET status='seen', seen_at=CURRENT_TIMESTAMP WHERE id = ANY(%s)",
                (notification_ids,),
            )
        else:
            cur.execute(
                "UPDATE monitoring_notifications SET status='seen', seen_at=CURRENT_TIMESTAMP WHERE channel='dashboard' AND status != 'seen'"
            )
        updated = cur.rowcount
        conn.commit()
        cur.close()
        return updated
    except Exception as e:
        logger.error(f"[Monitor] mark_notifications_seen error: {e}")
        return 0
    finally:
        if conn:
            _return_connection(conn)


def mark_notifications_sent(notification_ids: list) -> None:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            "UPDATE monitoring_notifications SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id = ANY(%s)",
            (notification_ids,),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"[Monitor] mark_notifications_sent error: {e}")
    finally:
        if conn:
            _return_connection(conn)
