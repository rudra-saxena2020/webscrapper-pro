import os
import re
import json
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client
load_dotenv()

import logging
import time

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

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Initialize Supabase Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_KEY else None
except Exception as e:
    print(f"⚠️ Warning: Supabase client failed to initialize: {e}")
    supabase = None

# Direct Postgres connection string (port 5432)
DATABASE_URL = os.getenv("DATABASE_URL")

# -------------------------------------------------------
# Connection pool — reuse connections instead of creating
# a new TCP socket to Supabase on every DB call.
# -------------------------------------------------------
import threading as _threading
from psycopg2 import pool as _pg_pool

_pool: "_pg_pool.ThreadedConnectionPool | None" = None
_pool_lock = _threading.Lock()

def _get_pool() -> "_pg_pool.ThreadedConnectionPool":
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _pg_pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=20,
                    dsn=DATABASE_URL,
                    connect_timeout=20,
                    # TCP keepalives — prevents SSL drops on idle connections
                    # in the deployed VM environment
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5,
                )
    return _pool

def get_connection(cursor_factory=DictCursor, retries=5):
    """Borrow a connection from the pool with physical connectivity retries."""
    if not DATABASE_URL or "[PROJECT_REF]" in DATABASE_URL:
        # Silently fail here as we will handle local mode in higher-level functions
        return None, None

    last_err = None
    for attempt in range(retries):
        conn = None
        try:
            conn = _get_pool().getconn()
            # Verify connection is still alive (Supabase can drop idle ones)
            with conn.cursor() as probe_cur:
                probe_cur.execute("SELECT 1")
            conn.rollback() # Clear probe transaction before setting session
            
            conn.autocommit = False
            cur = conn.cursor(cursor_factory=cursor_factory) if cursor_factory else conn.cursor()
            cur.execute("SET statement_timeout = '5min'") # 5 minutes for large JSONB
            return conn, cur
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            last_err = e
            if conn is not None:
                try:
                    _get_pool().putconn(conn, close=True)
                except Exception:
                    pass
            # If connection is dead, try to recover it from the pool or replace it
            logger.warning(f"⚠️ DB Connection Probe failed (attempt {attempt+1}): {e}")
            time.sleep(1)
            continue
    raise last_err

def _return_connection(conn):
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def init_db():
    """Initialize database tables or local storage directory."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local mode initialization
            os.makedirs("data", exist_ok=True)
            print("💾 Running in Local Mode (no database). Data saved to 'data/' folder.")
            return

        # Create scrapes table for history
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrapes (
                id SERIAL PRIMARY KEY,
                scraper_id TEXT NOT NULL,
                started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP WITH TIME ZONE,
                status TEXT DEFAULT 'running',
                products_count INTEGER DEFAULT 0,
                csv_url TEXT,
                error_message TEXT
            );
        """)
        cur.execute("ALTER TABLE scrapes ADD COLUMN IF NOT EXISTS quality_report JSONB;")
        
        # Ensure products table exists (it already does, but for safety)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                website_url TEXT PRIMARY KEY,
                type TEXT,
                products JSONB,
                total_products TEXT,
                updated_at TEXT
            );
        """)
        
        # ── Key-value store (heartbeats, settings) — needed early ───────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_kv (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        cur.close()
        print("Database tables initialized.")
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
    finally:
        if conn:
            _return_connection(conn)

# Call init_db on import to ensure tables exist if DATABASE_URL is available
# if DATABASE_URL:
#     try:
#         init_db()
#     except Exception as e:
#         logger.error(f"Safe skip: Database initialization failed: {e}")
# else:
#     logger.warning("⚠️ No DATABASE_URL found. Running in localized/mock mode.")

# -------------------------------------------------------
# SUPABASE STORAGE
# -------------------------------------------------------
def upload_csv_to_supabase(file_path, scraper_id):
    """Uploads a CSV file to Supabase Storage and returns the public URL."""
    if not supabase:
        print("Warning: Supabase client not initialized. Skipping upload.")
        return None
        
    try:
        file_name = os.path.basename(file_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        storage_path = f"{scraper_id}/{timestamp}_{file_name}"
        
        # Standard folder for all downloads
        bucket_name = "scrapes"
        
        # Check if bucket exists, if not, creating one might be hard via SDK without admin perms, 
        # but usually 'scrapes' should pre-exist or be created manually.
        
        with open(file_path, "rb") as f:
            res = supabase.storage.from_(bucket_name).upload(
                path=storage_path,
                file=f,
                file_options={"content-type": "text/csv"}
            )
            
        # Get public URL
        url = supabase.storage.from_(bucket_name).get_public_url(storage_path)
        print(f"CSV uploaded to Supabase Storage: {url}")
        return url
    except Exception as e:
        err_msg = str(e)
        if "Bucket not found" in err_msg:
            try:
                print("🛠️ Bucket 'scrapes' missing. Attempting auto-creation...")
                # We use the raw requests approach or Supabase client to create bucket
                supabase.storage.create_bucket("scrapes", options={"public": True})
                print("✅ Bucket 'scrapes' created successfully. Retrying upload...")
                # Retry once
                with open(file_path, "rb") as f:
                    supabase.storage.from_(bucket_name).upload(
                        path=storage_path,
                        file=f,
                        file_options={"content-type": "text/csv"},
                    )
                url = supabase.storage.from_(bucket_name).get_public_url(storage_path)
                return url
            except Exception as inner_e:
                print(f"⚠️ Auto-creation failed: {inner_e}")
                print(f"👉 TIP: Please manually create a public bucket named 'scrapes' in your Supabase dashboard.")
        else:
            print(f"❌ Error uploading to Supabase Storage: {e}")
        return None

# -------------------------------------------------------
# SCRAPE HISTORY
# -------------------------------------------------------
def start_scrape_record(scraper_id):
    """Creates a new scrape record and returns the ID."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode: Save to data/scrapes.json
            history = []
            if os.path.exists("data/scrapes.json"):
                with open("data/scrapes.json", "r") as f:
                    try: history = json.load(f)
                    except: history = []
            
            new_id = len(history) + 1
            history.append({
                "id": new_id,
                "scraper_id": scraper_id,
                "status": "running",
                "started_at": datetime.now().isoformat(),
                "products_count": 0
            })
            with open("data/scrapes.json", "w") as f:
                json.dump(history, f, indent=2)
            return new_id

        cur.execute(
            "INSERT INTO scrapes (scraper_id, status) VALUES (%s, %s) RETURNING id",
            (scraper_id, "running")
        )
        scrape_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return scrape_id
    except Exception as e:
        print(f"Error starting scrape record: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)

def update_scrape_record(scrape_id, status="completed", products_count=0, csv_url=None, error_message=None, quality_report=None):
    """Updates an existing scrape record. Only sets completed_at for terminal states."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode: Update data/scrapes.json
            if os.path.exists("data/scrapes.json"):
                with open("data/scrapes.json", "r") as f:
                    history = json.load(f)
                
                for item in history:
                    if item["id"] == scrape_id:
                        item["status"] = status
                        item["products_count"] = products_count
                        item["csv_url"] = csv_url
                        item["error_message"] = error_message
                        if status in ("completed", "failed", "cancelled"):
                            item["completed_at"] = datetime.now().isoformat()
                        break
                
                with open("data/scrapes.json", "w") as f:
                    json.dump(history, f, indent=2)
            return

        terminal_states = ("completed", "failed", "cancelled")
        if status in terminal_states:
            cur.execute("""
                UPDATE scrapes 
                SET status = %s,
                    products_count = %s,
                    csv_url = %s,
                    error_message = %s,
                    quality_report = %s,
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (status, products_count, csv_url, error_message,
                  quality_report, scrape_id))
        else:
            # Heartbeat / running update — don't set completed_at
            cur.execute("""
                UPDATE scrapes 
                SET status = %s,
                    products_count = %s
                WHERE id = %s
            """, (status, products_count, scrape_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Error updating scrape record: {e}")
    finally:
        if conn:
            _return_connection(conn)

def update_scraper_quality_report(scraper_id: str, quality_report: dict) -> bool:
    """
    Persist a fresh quality_report onto the most-recent completed scrape
    for *scraper_id*.  Called after a manual re-validate so the QA Review
    page picks up the corrected gate results without waiting for a new scrape.
    Returns True if a row was actually updated.
    """
    import json as _json
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return False
        cur.execute("""
            UPDATE scrapes
            SET    quality_report = %s
            WHERE  id = (
                SELECT id FROM scrapes
                WHERE  scraper_id = %s AND status = 'completed'
                ORDER  BY completed_at DESC NULLS LAST
                LIMIT  1
            )
        """, (_json.dumps(quality_report), scraper_id))
        updated = cur.rowcount
        conn.commit()
        cur.close()
        return bool(updated)
    except Exception as e:
        logger.warning(f"[QA] update_scraper_quality_report error for {scraper_id}: {e}")
        return False
    finally:
        if conn:
            _return_connection(conn)


def heartbeat_scrape_record(scrape_id, products_count=None):
    """Lightweight heartbeat update — only updates the count, does not touch timestamps or status."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode: Update data/scrapes.json
            if os.path.exists("data/scrapes.json"):
                with open("data/scrapes.json", "r") as f:
                    history = json.load(f)
                
                for item in history:
                    if item["id"] == scrape_id:
                        item["products_count"] = products_count
                        break
                
                with open("data/scrapes.json", "w") as f:
                    json.dump(history, f, indent=2)
            return

        cur.execute(
            "UPDATE scrapes SET products_count = %s WHERE id = %s",
            (products_count, scrape_id)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Heartbeat update failed (non-critical): {e}")
    finally:
        if conn:
            _return_connection(conn)

def get_latest_scrape_record(id_or_url, only_completed=False, must_have_csv=False):
    """Retrieves the latest scrape record for a scraper ID or URL."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            # Local Mode: Read data/scrapes.json
            if not os.path.exists("data/scrapes.json"):
                return None
            
            with open("data/scrapes.json", "r") as f:
                history = json.load(f)
            
            # Simple normalization for comparison
            normalized_id = id_or_url.replace("https://www.", "").replace("http://www.", "").replace("https://", "").replace("http://", "").split(".")[0]
            
            matching = [h for h in history if h.get('scraper_id') == id_or_url or h.get('scraper_id') == normalized_id]
            if only_completed:
                matching = [h for h in matching if h.get('status') == 'completed']
            if must_have_csv:
                matching = [h for h in matching if h.get('csv_url')]
                
            if not matching:
                return None
                
            # Sort by started_at or id
            matching.sort(key=lambda x: x.get('started_at', ''), reverse=True)
            return matching[0]

        query = """
            SELECT id, scraper_id, status, products_count, csv_url, started_at, completed_at, error_message
            FROM scrapes
            WHERE (scraper_id = %s OR scraper_id = %s)
        """
        params = [id_or_url, id_or_url.replace("https://www.", "").replace("http://www.", "").replace("https://", "").replace("http://", "").split(".")[0]]
        
        if only_completed:
            query += " AND status = 'completed'"
        if must_have_csv:
            query += " AND csv_url IS NOT NULL"
            
        query += " ORDER BY id DESC LIMIT 1"
        
        cur.execute(query, params)
        row = cur.fetchone()
        cur.close()
        
        if row:
            return {
                'id': row['id'],
                'scraper_id': row['scraper_id'],
                'status': row['status'],
                'products_count': row['products_count'] or 0,
                'csv_url': row['csv_url'],
                'started_at': row['started_at'].isoformat() if row['started_at'] and hasattr(row['started_at'], 'isoformat') else str(row['started_at']),
                'completed_at': row['completed_at'].isoformat() if row['completed_at'] and hasattr(row['completed_at'], 'isoformat') else str(row['completed_at']),
                'error_message': row['error_message']
            }
        return None
    except Exception as e:
        print(f"Error getting latest scrape record: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)

def get_scrape_history(limit=50):
    """Retrieves the history of scrapes from the database."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            # Local Mode: Read data/scrapes.json
            if os.path.exists("data/scrapes.json"):
                with open("data/scrapes.json", "r") as f:
                    history = json.load(f)
                return sorted(history, key=lambda x: x.get('started_at', ''), reverse=True)[:limit]
            return []

        cur.execute("""
            SELECT id, scraper_id, status, products_count, csv_url, started_at, completed_at, error_message
            FROM scrapes
            ORDER BY started_at DESC, id DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        
        scrapes = []
        for row in rows:
            scrapes.append({
                'id': row['id'],
                'scraper_id': row['scraper_id'],
                'status': row['status'],
                'products_count': row['products_count'] or 0,
                'csv_url': row['csv_url'],
                'started_at': row['started_at'].isoformat() if row['started_at'] and hasattr(row['started_at'], 'isoformat') else str(row['started_at']),
                'completed_at': row['completed_at'].isoformat() if row['completed_at'] and hasattr(row['completed_at'], 'isoformat') else str(row['completed_at']),
                'error_message': row['error_message']
            })
        return scrapes
    except Exception as e:
        print(f"Error getting history: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)

# -------------------------------------------------------
# COLORS
# -------------------------------------------------------
def extract_colors_from_products(products_data):
    colors_set = set()

    if isinstance(products_data, dict) and "products" in products_data:
        products = products_data["products"]
    elif isinstance(products_data, list):
        products = products_data
    else:
        print("Invalid products data format")
        return []

    for product in products:
        if "variants" in product:
            for variant in product["variants"]:
                if "color" in variant and variant["color"]:
                    color = variant["color"].strip()
                    if color:
                        colors_set.add(color)

    colors_list = [{"mapped": "", "original": color} for color in sorted(colors_set)]
    return colors_list

def update_colors_in_database(new_colors: list):
    conn = None
    try:
        conn, cur = get_connection()

        # 1. Fetch existing colors
        cur.execute("SELECT products FROM products WHERE website_url = %s", ("colors",))
        row = cur.fetchone()
        existing_colors = []
        if row and row[0]:
            products_data = row[0]
            if isinstance(products_data, dict) and "colors" in products_data:
                existing_colors = products_data["colors"]
            elif isinstance(products_data, list):
                existing_colors = products_data

        existing_original_colors = {c["original"] for c in existing_colors if isinstance(c, dict) and "original" in c}

        colors_to_add = []
        for new_color in new_colors:
            if new_color["original"] not in existing_original_colors:
                colors_to_add.append(new_color)
                existing_original_colors.add(new_color["original"])

        all_colors = existing_colors + colors_to_add
        colors_json = json.dumps({"colors": all_colors})

        # 2. Upsert to ensure the row exists on fresh databases
        cur.execute("""
            INSERT INTO products (website_url, products)
            VALUES (%s, %s)
            ON CONFLICT (website_url)
            DO UPDATE SET products = EXCLUDED.products
        """, ("colors", colors_json))
        conn.commit()
        cur.close()

        print(f"✅ Colors row updated")
        print(f"📊 Total colors: {len(all_colors)} (Added {len(colors_to_add)} new colors)")

    except Exception as e:
        print(f"❌ Error updating colors row: {e}")
    finally:
        if conn:
            _return_connection(conn)

# -------------------------------------------------------
# TAGS
# -------------------------------------------------------
def upsert_tags_row(new_tags: list):
    conn = None
    try:
        conn, cur = get_connection()

        cur.execute("SELECT products FROM products WHERE website_url = %s", ("tags",))
        row = cur.fetchone()
        existing_tags = set()
        if row and row[0]:
            products_data = row[0]
            if isinstance(products_data, dict) and "tags" in products_data:
                existing_tags = set(products_data["tags"])

        combined_tags = sorted(existing_tags.union(set(new_tags)))
        tags_json = json.dumps({"tags": combined_tags})

        cur.execute("""
            INSERT INTO products (website_url, products)
            VALUES (%s, %s)
            ON CONFLICT (website_url)
            DO UPDATE SET products = EXCLUDED.products
        """, ("tags", tags_json))
        conn.commit()
        cur.close()

        print(f"✅ Tags upserted ({len(combined_tags)} total tags).")

    except Exception as e:
        print(f"Error upserting tags: {e}")
    finally:
        if conn:
            _return_connection(conn)

# -------------------------------------------------------
# PRODUCTS
# -------------------------------------------------------
def upsert_product(product_json: dict, website_url: str, currency: str):
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode: Save to data/products/{website_url}.json
            os.makedirs("data/products", exist_ok=True)
            safe_filename = re.sub(r'[^a-z0-9]', '_', website_url.lower())
            file_path = f"data/products/{safe_filename}.json"
            
            with open(file_path, "w") as f:
                json.dump({
                    "website_url": website_url,
                    "type": currency,
                    "products": product_json.get("products"),
                    "total_products": product_json.get("total_products"),
                    "updated_at": product_json.get("updated_at", datetime.now().isoformat())
                }, f, indent=2)
            print(f"✅ (Local Mode) Saved {website_url} to disk.")
            return

        upsert_data = dict(product_json)
        upsert_data["website_url"] = website_url
        upsert_data["type"] = currency

        products_json = json.dumps(upsert_data.get("products"))
        total_products = upsert_data.get("total_products", "0")
        updated_at = upsert_data.get("updated_at", datetime.now().isoformat(sep=' ', timespec='seconds'))

        query = """
        INSERT INTO products (website_url, type, products, total_products, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (website_url)
        DO UPDATE SET
            type = EXCLUDED.type,
            products = EXCLUDED.products,
            total_products = EXCLUDED.total_products,
            updated_at = EXCLUDED.updated_at;
        """

        cur.execute(query, (website_url, currency, products_json, total_products, updated_at))
        conn.commit()
        cur.close()

        print(f"Upsert successful for website_url '{website_url}'")

    except Exception as e:
        print(f"Error during upsert for website_url '{website_url}': {e}")
    finally:
        if conn:
            _return_connection(conn)

def get_product_by_website(website_url):
    """Fetch the full product record by website URL."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        cur.execute("SELECT * FROM products WHERE website_url = %s", (website_url,))
        row = cur.fetchone()
        cur.close()
        if row:
            return dict(row)
        return None
    except Exception as e:
        print(f"Error fetching product record for '{website_url}': {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)

def get_scraper_stats(website_url):
    """
    Returns lightweight stats for a website to avoid loading massive JSON blobs.
    Accepts both full URL or a scraper ID like 'cruise_fashion'.
    """
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            # Local Mode: Check data/products/
            safe_filename = re.sub(r'[^a-z0-9]', '_', website_url.lower())
            file_path = f"data/products/{safe_filename}.json"
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    data = json.load(f)
                return {
                    'total_products': int(data.get('total_products', 0)),
                    'updated_at': data.get('updated_at', "Unknown")
                }
            # Try history fallback
            latest = get_latest_scrape_record(website_url)
            if latest:
                return {
                    'total_products': int(latest.get('products_count', 0)),
                    'updated_at': latest.get('completed_at') or latest.get('started_at', "Just now")
                }
            return {'total_products': 0, 'updated_at': "Never"}

        # 1. Try EXACT MATCH First
        cur.execute("SELECT total_products, updated_at FROM products WHERE website_url = %s", (website_url,))
        row = cur.fetchone()
        
        # 2. Try FUZZY ONLY if website_url looks like a real URL
        if not row and ('.' in website_url or '/' in website_url):
            try:
                cur.execute("""
                    SELECT total_products, updated_at FROM products 
                    WHERE website_url LIKE %s 
                    LIMIT 1
                """, (f"%{website_url}%",))
                row = cur.fetchone()
            except:
                pass
        
        cur.close()
        
        if row:
            updated_at_val = row['updated_at']
            if updated_at_val and hasattr(updated_at_val, 'isoformat'):
                updated_at_val = updated_at_val.isoformat()
            
            return {
                'total_products': int(row['total_products']) if row['total_products'] is not None else 0,
                'updated_at': str(updated_at_val) if updated_at_val else "Just now"
            }
        
        # Fallback to scrape history
        latest = get_latest_scrape_record(website_url)
        if latest:
            return {
                'total_products': int(latest.get('products_count', 0)),
                'updated_at': latest.get('completed_at') or latest.get('started_at', "Just now")
            }
            
        return {'total_products': 0, 'updated_at': "Never"}
    except Exception as e:
        import traceback
        print(f"Error fetching scraper stats for {website_url}:")
        traceback.print_exc()
        return {'total_products': 0, 'updated_at': "Error"}
    finally:
        if conn:
            _return_connection(conn)

# -------------------------------------------------------
# ALL PRODUCT DATA
# -------------------------------------------------------
def upsert_all_product_data(cleaned_list, website_url, currency="USD"):
    # Chunking to prevent massive JSON payload timeouts
    CHUNK_SIZE = 1000
    total = len(cleaned_list)
    
    # We still want the scraper card to reflect the TOTAL count immediately,
    # so we do one metadata upsert then chunk the actual blobs if needed.
    # However, our schema currently stores everything in one 'products' JSONB column.
    # So we'll try a single commit first, but wrap it in a larger timeout.
    
    upsert_product(
        {
            "products": {"products": cleaned_list},
            "total_products": str(total),
            "updated_at": datetime.now().isoformat(sep=' ', timespec='seconds')
        },
        website_url,
        currency
    )
    print(f"✅ Saved {total} products for '{website_url}'")

    # Colors Update (Background)
    try:
        extracted_colors = extract_colors_from_products({"products": cleaned_list})
        if extracted_colors:
            update_colors_in_database(extracted_colors)
    except Exception as e:
        print(f"Non-critical color update error: {e}")

# -------------------------------------------------------
# SHOPIFY AUDIT TABLES
# -------------------------------------------------------

def init_shopify_tables():
    """Create shopify_activity_logs and shopify_products tables if not present."""
    conn = None
    try:
        conn, cur = get_connection()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_activity_logs (
                id SERIAL PRIMARY KEY,
                scraper_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'success',
                products_created INTEGER DEFAULT 0,
                products_updated INTEGER DEFAULT 0,
                products_deleted INTEGER DEFAULT 0,
                products_skipped INTEGER DEFAULT 0,
                products_failed INTEGER DEFAULT 0,
                error_message TEXT,
                notes TEXT,
                triggered_by TEXT DEFAULT 'admin',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shopify_products (
                id SERIAL PRIMARY KEY,
                scraper_id TEXT NOT NULL,
                shopify_product_id TEXT NOT NULL UNIQUE,
                sku TEXT,
                handle TEXT,
                title TEXT,
                status TEXT DEFAULT 'active',
                qa_status TEXT DEFAULT 'QA_PENDING_REVIEW',
                qa_reviewed_at TIMESTAMP WITH TIME ZONE,
                uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                last_synced_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Migrate existing rows that predate the qa_status column
        cur.execute("ALTER TABLE shopify_products ADD COLUMN IF NOT EXISTS qa_status TEXT DEFAULT 'QA_PENDING_REVIEW';")
        cur.execute("ALTER TABLE shopify_products ADD COLUMN IF NOT EXISTS qa_reviewed_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE shopify_products ADD COLUMN IF NOT EXISTS store VARCHAR(10) DEFAULT 'test';")

        # Migrate existing activity logs that predate the store column
        cur.execute("ALTER TABLE shopify_activity_logs ADD COLUMN IF NOT EXISTS store VARCHAR(10) DEFAULT 'test';")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS qa_events (
                id SERIAL PRIMARY KEY,
                shopify_product_id TEXT NOT NULL,
                scraper_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Indexes for fast lookup
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_shopify_logs_scraper
            ON shopify_activity_logs(scraper_id, created_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_shopify_logs_action
            ON shopify_activity_logs(action_type, status);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_shopify_products_scraper
            ON shopify_products(scraper_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_qa_events_product
            ON qa_events(shopify_product_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_qa_events_scraper
            ON qa_events(scraper_id, created_at DESC);
        """)

        # ── Auto-sync tables ──────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_sync_runs (
                id SERIAL PRIMARY KEY,
                run_type TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ,
                status TEXT NOT NULL DEFAULT 'running',
                active_scrapers JSONB,
                report_json JSONB,
                store TEXT NOT NULL DEFAULT 'main'
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS oos_pending_removal (
                scraper_id TEXT NOT NULL,
                shopify_product_id TEXT NOT NULL,
                title TEXT,
                sku TEXT,
                handle TEXT,
                first_seen_missing_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                store TEXT NOT NULL DEFAULT 'main',
                PRIMARY KEY (shopify_product_id, store)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_auto_sync_runs_store
            ON auto_sync_runs(store, started_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_oos_pending_scraper
            ON oos_pending_removal(scraper_id, store);
        """)
        # ── Key-value store (heartbeats, settings) ───────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_kv (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        cur.close()
        logger.info("💾 Shopify audit tables ready.")
    except Exception as e:
        logger.error(f"Shopify table init error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def log_shopify_action(
    scraper_id: str,
    action_type: str,
    status: str = "success",
    result: dict = None,
    error: str = None,
    notes: str = None,
    triggered_by: str = "admin",
    store: str = "test",
):
    """Insert an immutable audit log entry. Failures are non-fatal."""
    conn = None
    try:
        r = result or {}
        conn, cur = get_connection()
        if not conn:
            import uuid
            log_entry = {
                "id": str(uuid.uuid4()),
                "scraper_id": scraper_id,
                "action_type": action_type,
                "status": status,
                "products_created": r.get("created", 0),
                "products_updated": r.get("updated", 0),
                "products_deleted": r.get("deleted", 0),
                "products_skipped": r.get("skipped", 0),
                "products_failed": r.get("failed", 0),
                "error_message": error,
                "notes": notes,
                "triggered_by": triggered_by,
                "store": store,
                "created_at": datetime.now().isoformat()
            }
            logs = []
            if os.path.exists("data/shopify_logs.json"):
                try:
                    with open("data/shopify_logs.json", "r") as f:
                        logs = json.load(f)
                except Exception:
                    pass
            logs.insert(0, log_entry)
            with open("data/shopify_logs.json", "w") as f:
                json.dump(logs, f, indent=2)
            return

        cur.execute("""
            INSERT INTO shopify_activity_logs
              (scraper_id, action_type, status,
               products_created, products_updated, products_deleted,
               products_skipped, products_failed,
               error_message, notes, triggered_by, store)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            scraper_id, action_type, status,
            r.get("created", 0), r.get("updated", 0), r.get("deleted", 0),
            r.get("skipped", 0), r.get("failed", 0),
            error, notes, triggered_by, store,
        ))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[AuditLog] Non-fatal DB log error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def get_shopify_logs(
    scraper_id: str = None,
    action_type: str = None,
    status: str = None,
    search: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 50,
    offset: int = 0,
    store: str = None,
) -> list:
    """Paginated, filtered shopify activity logs."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            logs = []
            if os.path.exists("data/shopify_logs.json"):
                try:
                    with open("data/shopify_logs.json", "r") as f:
                        logs = json.load(f)
                except Exception:
                    pass
            
            if scraper_id and scraper_id != "all":
                logs = [l for l in logs if l.get("scraper_id") == scraper_id]
            if action_type and action_type != "all":
                logs = [l for l in logs if l.get("action_type") == action_type]
            if status and status != "all":
                logs = [l for l in logs if l.get("status") == status]
            
            return logs[offset : offset + limit]

        where, params = [], []

        if scraper_id and scraper_id != "all":
            where.append("scraper_id = %s"); params.append(scraper_id)
        if action_type and action_type != "all":
            where.append("action_type = %s"); params.append(action_type)
        if status and status != "all":
            where.append("status = %s"); params.append(status)
        if store and store != "all":
            where.append("store = %s"); params.append(store)
        if search:
            where.append("(notes ILIKE %s OR scraper_id ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if date_from:
            where.append("created_at >= %s"); params.append(date_from)
        if date_to:
            where.append("created_at <= %s"); params.append(date_to + "T23:59:59")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(f"""
            SELECT id, scraper_id, action_type, status,
                   products_created, products_updated, products_deleted,
                   products_skipped, products_failed,
                   error_message, notes, triggered_by, created_at,
                   COALESCE(store, 'test') AS store
            FROM shopify_activity_logs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])

        rows = cur.fetchall()
        cur.close()
        logs = []
        for r in rows:
            logs.append({
                "id": r["id"],
                "scraper_id": r["scraper_id"],
                "action_type": r["action_type"],
                "status": r["status"],
                "store": r["store"] or "test",
                "products_created": r["products_created"] or 0,
                "products_updated": r["products_updated"] or 0,
                "products_deleted": r["products_deleted"] or 0,
                "products_skipped": r["products_skipped"] or 0,
                "products_failed": r["products_failed"] or 0,
                "error_message": r["error_message"],
                "notes": r["notes"],
                "triggered_by": r["triggered_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        return logs
    except Exception as e:
        logger.error(f"[Logs] get_shopify_logs error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def get_shopify_logs_count(
    scraper_id: str = None,
    action_type: str = None,
    status: str = None,
    search: str = None,
    date_from: str = None,
    date_to: str = None,
    store: str = None,
) -> int:
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            logs = []
            if os.path.exists("data/shopify_logs.json"):
                try:
                    with open("data/shopify_logs.json", "r") as f:
                        logs = json.load(f)
                except Exception:
                    pass
            
            if scraper_id and scraper_id != "all":
                logs = [l for l in logs if l.get("scraper_id") == scraper_id]
            if action_type and action_type != "all":
                logs = [l for l in logs if l.get("action_type") == action_type]
            if status and status != "all":
                logs = [l for l in logs if l.get("status") == status]
            return len(logs)

        where, params = [], []

        if scraper_id and scraper_id != "all":
            where.append("scraper_id = %s"); params.append(scraper_id)
        if action_type and action_type != "all":
            where.append("action_type = %s"); params.append(action_type)
        if status and status != "all":
            where.append("status = %s"); params.append(status)
        if store and store != "all":
            where.append("store = %s"); params.append(store)
        if search:
            where.append("(notes ILIKE %s OR scraper_id ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if date_from:
            where.append("created_at >= %s"); params.append(date_from)
        if date_to:
            where.append("created_at <= %s"); params.append(date_to + "T23:59:59")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(f"SELECT COUNT(*) FROM shopify_activity_logs {where_sql}", params)
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception as e:
        logger.error(f"[Logs] count error: {e}")
        return 0
    finally:
        if conn:
            _return_connection(conn)


def get_shopify_log_stats() -> dict:
    """Aggregate stats for the dashboard — total ops per action type."""
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            logs = []
            if os.path.exists("data/shopify_logs.json"):
                try:
                    with open("data/shopify_logs.json", "r") as f:
                        logs = json.load(f)
                except Exception:
                    pass
            return {
                "total_uploaded": sum(l.get("products_created", 0) for l in logs),
                "total_updated": sum(l.get("products_updated", 0) for l in logs),
                "total_deleted": sum(l.get("products_deleted", 0) for l in logs),
                "total_failed": sum(l.get("products_failed", 0) for l in logs),
                "total_ops": len(logs),
                "successful_ops": sum(1 for l in logs if l.get("status") == "success"),
                "failed_ops": sum(1 for l in logs if l.get("status") == "failed"),
            }

        cur.execute("""
            SELECT
                COALESCE(SUM(products_created), 0) AS total_uploaded,
                COALESCE(SUM(products_updated), 0) AS total_updated,
                COALESCE(SUM(products_deleted), 0) AS total_deleted,
                COALESCE(SUM(products_failed),  0) AS total_failed,
                COUNT(*) AS total_ops,
                COUNT(*) FILTER (WHERE status = 'success') AS successful_ops,
                COUNT(*) FILTER (WHERE status = 'failed')  AS failed_ops
            FROM shopify_activity_logs
        """)
        row = cur.fetchone()
        cur.close()
        if row:
            return dict(row)
        return {}
    except Exception as e:
        logger.error(f"[Logs] stats error: {e}")
        return {}
    finally:
        if conn:
            _return_connection(conn)


def register_shopify_product(
    scraper_id: str, shopify_product_id: str,
    sku: str = None, handle: str = None, title: str = None,
    store: str = 'test',
):
    """Add a newly uploaded Shopify product to the local registry."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode: Update data/shopify_registry.json
            os.makedirs("data", exist_ok=True)
            registry = []
            if os.path.exists("data/shopify_registry.json"):
                with open("data/shopify_registry.json", "r") as f:
                    registry = json.load(f)
            
            # Find and update or append
            found = False
            for item in registry:
                if item.get("shopify_product_id") == str(shopify_product_id):
                    item.update({
                        "scraper_id": scraper_id,
                        "sku": sku,
                        "handle": handle,
                        "title": title,
                        "store": store,
                        "last_synced_at": datetime.now().isoformat()
                    })
                    found = True
                    break
            
            if not found:
                registry.append({
                    "scraper_id": scraper_id,
                    "shopify_product_id": str(shopify_product_id),
                    "sku": sku,
                    "handle": handle,
                    "title": title,
                    "store": store,
                    "status": "active",
                    "last_synced_at": datetime.now().isoformat()
                })
            
            with open("data/shopify_registry.json", "w") as f:
                json.dump(registry, f, indent=2)
            return

        cur.execute("""
            INSERT INTO shopify_products
              (scraper_id, shopify_product_id, sku, handle, title, status, qa_status, store)
            VALUES (%s, %s, %s, %s, %s, 'active', 'QA_PENDING_REVIEW', %s)
            ON CONFLICT (shopify_product_id)
            DO UPDATE SET
              last_synced_at = CURRENT_TIMESTAMP,
              store = EXCLUDED.store
              -- store is updated on conflict so that a product moved between
              -- test→main is correctly re-labelled.
              -- scraper_id, sku, handle, title, qa_status, qa_reviewed_at are
              -- preserved so that a re-upload never overwrites review decisions
              -- or canonical product metadata.
        """, (scraper_id, str(shopify_product_id), sku, handle, title, store))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[Registry] register_shopify_product error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def remove_shopify_product(shopify_product_id: str):
    """Remove a deleted product from the local registry."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode
            if os.path.exists("data/shopify_registry.json"):
                with open("data/shopify_registry.json", "r") as f:
                    registry = json.load(f)
                
                registry = [item for item in registry if item.get("shopify_product_id") != str(shopify_product_id)]
                
                with open("data/shopify_registry.json", "w") as f:
                    json.dump(registry, f, indent=2)
            return

        cur.execute(
            "DELETE FROM shopify_products WHERE shopify_product_id = %s",
            (str(shopify_product_id),)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[Registry] remove_shopify_product error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def verify_product_ownership(shopify_product_id: str, scraper_id: str) -> bool:
    """
    Second verification layer: checks local DB registry confirms product
    belongs to this scraper. Returns True only if confirmed.
    """
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            # Local Mode
            if os.path.exists("data/shopify_registry.json"):
                with open("data/shopify_registry.json", "r") as f:
                    registry = json.load(f)
                
                for item in registry:
                    if item.get("shopify_product_id") == str(shopify_product_id) and item.get("scraper_id") == scraper_id:
                        return True
            return False

        cur.execute(
            "SELECT 1 FROM shopify_products WHERE shopify_product_id = %s AND scraper_id = %s",
            (str(shopify_product_id), scraper_id)
        )
        row = cur.fetchone()
        cur.close()
        return bool(row)
    except Exception:
        return False
    finally:
        if conn:
            _return_connection(conn)


# -------------------------------------------------------
# QA APPROVAL WORKFLOW
# -------------------------------------------------------

def log_qa_event(shopify_product_id: str, scraper_id: str, action: str, reason: str = None):
    """Insert an immutable QA lifecycle event row."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute("""
            INSERT INTO qa_events (shopify_product_id, scraper_id, action, reason)
            VALUES (%s, %s, %s, %s)
        """, (str(shopify_product_id), scraper_id, action, reason))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[QA] log_qa_event error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def approve_product(shopify_product_id: str, scraper_id: str, reason: str = None) -> bool:
    """Transition a product to APPROVED status and log the event."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return False
        cur.execute("""
            UPDATE shopify_products
            SET qa_status = 'APPROVED', qa_reviewed_at = CURRENT_TIMESTAMP
            WHERE shopify_product_id = %s AND scraper_id = %s
        """, (str(shopify_product_id), scraper_id))
        updated = cur.rowcount
        conn.commit()
        cur.close()
        if updated:
            log_qa_event(shopify_product_id, scraper_id, 'APPROVED', reason)
        return bool(updated)
    except Exception as e:
        logger.warning(f"[QA] approve_product error: {e}")
        return False
    finally:
        if conn:
            _return_connection(conn)


def rework_product(shopify_product_id: str, scraper_id: str, reason: str = None) -> bool:
    """Transition a product to REWORK_REQUIRED status and log the event."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return False
        cur.execute("""
            UPDATE shopify_products
            SET qa_status = 'REWORK_REQUIRED', qa_reviewed_at = CURRENT_TIMESTAMP
            WHERE shopify_product_id = %s AND scraper_id = %s
        """, (str(shopify_product_id), scraper_id))
        updated = cur.rowcount
        conn.commit()
        cur.close()
        if updated:
            log_qa_event(shopify_product_id, scraper_id, 'REWORK_REQUIRED', reason)
        return bool(updated)
    except Exception as e:
        logger.warning(f"[QA] rework_product error: {e}")
        return False
    finally:
        if conn:
            _return_connection(conn)


def get_qa_products(scraper_id: str = None, limit: int = None) -> dict:
    """
    Return products with their QA status, plus aggregate counts.
    If scraper_id is given, filters to that scraper only (no row limit).
    When scraper_id is None, applies a limit (default 2000) to avoid OOM.
    Counts are computed at DB level (not from the paginated slice).
    Each product row includes the last 3 QA events and the scraper's
    latest quality gate score / errors / error list for review context.
    """
    if limit is None:
        # Unbounded for per-scraper; bounded for all-scrapers
        limit = 0 if scraper_id else 2000
    conn = None
    try:
        conn, cur = get_connection(cursor_factory=DictCursor)
        if not conn:
            return {"pending": 0, "approved": 0, "rework": 0, "products": []}

        # ── 1. True aggregate counts (full table, no LIMIT) ──────────────────
        count_where = "WHERE scraper_id = %s" if scraper_id else ""
        count_params = [scraper_id] if scraper_id else []
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE qa_status IS NULL OR qa_status = 'QA_PENDING_REVIEW') AS pending,
                COUNT(*) FILTER (WHERE qa_status = 'APPROVED')                               AS approved,
                COUNT(*) FILTER (WHERE qa_status = 'REWORK_REQUIRED')                        AS rework
            FROM shopify_products
            {count_where}
        """, count_params)
        cnt = cur.fetchone()
        pending_count  = int(cnt["pending"]  or 0)
        approved_count = int(cnt["approved"] or 0)
        rework_count   = int(cnt["rework"]   or 0)

        # ── 2. Latest quality report per scraper ─────────────────────────────
        scraper_filter = "AND scraper_id = %s" if scraper_id else ""
        qr_params      = [scraper_id] if scraper_id else []
        cur.execute(f"""
            SELECT DISTINCT ON (scraper_id) scraper_id, quality_report
            FROM   scrapes
            WHERE  status = 'completed' AND quality_report IS NOT NULL {scraper_filter}
            ORDER  BY scraper_id, completed_at DESC NULLS LAST
        """, qr_params)
        quality_by_scraper: dict = {}
        for qr in cur.fetchall():
            raw = qr["quality_report"]
            if not raw:
                continue
            try:
                qdata = json.loads(raw) if isinstance(raw, str) else raw
                quality_by_scraper[qr["scraper_id"]] = qdata
            except Exception:
                pass

        # ── 3. Paginated product rows ─────────────────────────────────────────
        where      = "WHERE sp.scraper_id = %s" if scraper_id else ""
        params     = [scraper_id] if scraper_id else []
        limit_sql  = "" if limit == 0 else "LIMIT %s"
        limit_args = [] if limit == 0 else [limit]
        cur.execute(f"""
            SELECT
                sp.shopify_product_id,
                sp.scraper_id,
                sp.title,
                sp.sku,
                sp.handle,
                sp.qa_status,
                sp.status,
                sp.uploaded_at,
                sp.qa_reviewed_at,
                sp.last_synced_at
            FROM shopify_products sp
            {where}
            ORDER BY sp.uploaded_at DESC
            {limit_sql}
        """, params + limit_args)
        rows = cur.fetchall()

        # ── 4. Last 3 QA events per product ──────────────────────────────────
        events_by_pid: dict = {}
        if rows:
            pids = [r["shopify_product_id"] for r in rows]
            placeholders = ",".join(["%s"] * len(pids))
            cur.execute(f"""
                SELECT shopify_product_id, action, reason, created_at
                FROM   qa_events
                WHERE  shopify_product_id IN ({placeholders})
                ORDER  BY created_at DESC
            """, pids)
            for ev in cur.fetchall():
                pid = ev["shopify_product_id"]
                if pid not in events_by_pid:
                    events_by_pid[pid] = []
                if len(events_by_pid[pid]) < 3:
                    events_by_pid[pid].append({
                        "action":     ev["action"],
                        "reason":     ev["reason"],
                        "created_at": ev["created_at"].isoformat() if ev["created_at"] else None,
                    })

        cur.close()

        # ── 5. Build product dicts with quality context ───────────────────────
        products = []
        for r in rows:
            qs    = r["qa_status"] or "QA_PENDING_REVIEW"
            sid   = r["scraper_id"]
            qdata = quality_by_scraper.get(sid, {})

            # Extract human-readable error list from the quality report
            # quality_report structure: {categories: {cat: {errors: [{title, message}...]}}}
            error_list: list = []
            for cat_data in (qdata.get("categories") or {}).values():
                for err in (cat_data.get("errors") or []):
                    msg = err.get("message") or err.get("title") or str(err)
                    if msg and msg not in error_list:
                        error_list.append(msg)

            products.append({
                "shopify_product_id":  r["shopify_product_id"],
                "scraper_id":          sid,
                "title":               r["title"]  or "",
                "sku":                 r["sku"]    or "",
                "handle":              r["handle"] or "",
                "qa_status":           qs,
                "status":              r["status"] or "active",
                "uploaded_at":         r["uploaded_at"].isoformat()      if r["uploaded_at"]      else None,
                "qa_reviewed_at":      r["qa_reviewed_at"].isoformat()   if r["qa_reviewed_at"]   else None,
                "last_synced_at":      r["last_synced_at"].isoformat()   if r["last_synced_at"]   else None,
                "qa_events":           events_by_pid.get(r["shopify_product_id"], []),
                # Quality gate context (scraper-level, most recent run)
                "quality_score":       qdata.get("pass_rate"),
                "quality_errors":      qdata.get("errors", 0),
                "quality_warnings":    qdata.get("warnings", 0),
                "quality_error_list":  error_list,
                "quality_ready":       qdata.get("ready_to_upload", None),
                # Full scraper-level quality report for inline expansion
                "quality_report_full": {
                    "total":           qdata.get("total", 0),
                    "pass_rate":       qdata.get("pass_rate"),
                    "errors":          qdata.get("errors", 0),
                    "warnings":        qdata.get("warnings", 0),
                    "ok":              qdata.get("ok", 0),
                    "ready_to_upload": qdata.get("ready_to_upload"),
                    "categories":      qdata.get("categories", {}),
                } if qdata else None,
            })

        return {
            "pending":  pending_count,
            "approved": approved_count,
            "rework":   rework_count,
            "products": products,
        }

    except Exception as e:
        logger.error(f"[QA] get_qa_products error: {e}")
        return {"pending": 0, "approved": 0, "rework": 0, "products": [], "error": str(e)}
    finally:
        if conn:
            _return_connection(conn)


def delete_all_scraper_products(scraper_id: str):
    """Deletes all products and Shopify registry entries for a specific scraper."""
    conn = None
    try:
        conn, cur = get_connection()
        
        # Local cleanup (regardless of DB state)
        safe_filename = re.sub(r'[^a-z0-9]', '_', scraper_id.lower())
        product_file = f"data/products/{safe_filename}.json"
        if os.path.exists(product_file):
            try:
                os.remove(product_file)
                logger.info(f"Deleted local product file: {product_file}")
            except Exception as e:
                logger.error(f"Error deleting local product file: {e}")

        # Local Shopify registry cleanup
        if os.path.exists("data/shopify_registry.json"):
            try:
                with open("data/shopify_registry.json", "r") as f:
                    registry = json.load(f)
                registry = [item for item in registry if item.get("scraper_id") != scraper_id]
                with open("data/shopify_registry.json", "w") as f:
                    json.dump(registry, f, indent=2)
            except Exception as e:
                logger.error(f"Error cleaning local Shopify registry: {e}")

        if not conn:
            return True

        # Database cleanup
        cur.execute("DELETE FROM products WHERE scraper_id = %s", (scraper_id,))
        cur.execute("DELETE FROM shopify_products WHERE scraper_id = %s", (scraper_id,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Error in delete_all_scraper_products: {e}")
        return False
    finally:
        if conn:
            _return_connection(conn)


# -------------------------------------------------------
# Auto-sync helpers
# -------------------------------------------------------

def get_active_main_scrapers() -> list:
    """
    Return list of distinct scraper_ids that have active products on the MAIN store.
    Falls back to empty list if DB is unavailable (caller should handle fallback).
    """
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return []
        # Query scrapers tagged for the main store, or all scrapers with pushed
        # products if no main-store rows exist yet (bootstrapping / migration).
        cur.execute("""
            SELECT DISTINCT scraper_id FROM shopify_products
            WHERE store = 'main'
            ORDER BY scraper_id
        """)
        rows = cur.fetchall()
        if not rows:
            # Fall back to all scrapers that have any pushed products
            cur.execute("SELECT DISTINCT scraper_id FROM shopify_products ORDER BY scraper_id")
            rows = cur.fetchall()
        cur.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning(f"[AutoSync] get_active_main_scrapers error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def start_auto_sync_run(run_type: str, active_scrapers: list, store: str = 'main') -> int:
    """Insert a new auto_sync_runs row and return its id (0 on failure)."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return 0
        cur.execute(
            """
            INSERT INTO auto_sync_runs (run_type, started_at, status, active_scrapers, store)
            VALUES (%s, CURRENT_TIMESTAMP, 'running', %s, %s)
            RETURNING id
            """,
            (run_type, json.dumps(active_scrapers), store),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else 0
    except Exception as e:
        logger.warning(f"[AutoSync] start_auto_sync_run error: {e}")
        return 0
    finally:
        if conn:
            _return_connection(conn)


def finish_auto_sync_run(run_id: int, status: str, report: dict) -> None:
    """Update an auto_sync_runs row with the final status and report."""
    if not run_id:
        return
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            """
            UPDATE auto_sync_runs
               SET status = %s, completed_at = CURRENT_TIMESTAMP, report_json = %s
             WHERE id = %s
            """,
            (status, json.dumps(report), run_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[AutoSync] finish_auto_sync_run error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def get_last_auto_sync_run(store: str = 'main') -> dict:
    """Return the most recent auto_sync_runs row as a dict (or None)."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return None
        cur.execute(
            """
            SELECT id, run_type, started_at, completed_at, status, active_scrapers, report_json, store
              FROM auto_sync_runs
             WHERE store = %s
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (store,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {
            'id': row[0],
            'run_type': row[1],
            'started_at': row[2].isoformat() if row[2] else None,
            'completed_at': row[3].isoformat() if row[3] else None,
            'status': row[4],
            'active_scrapers': row[5],
            'report_json': row[6],
            'store': row[7],
        }
    except Exception as e:
        logger.warning(f"[AutoSync] get_last_auto_sync_run error: {e}")
        return None
    finally:
        if conn:
            _return_connection(conn)


def get_auto_sync_history(store: str = 'main', limit: int = 20) -> list:
    """Return the last `limit` auto_sync_runs rows as a list of dicts."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return []
        cur.execute(
            """
            SELECT id, run_type, started_at, completed_at, status, active_scrapers, report_json, store
              FROM auto_sync_runs
             WHERE store = %s
             ORDER BY started_at DESC
             LIMIT %s
            """,
            (store, limit),
        )
        rows = cur.fetchall()
        cur.close()
        result = []
        for row in rows:
            result.append({
                'id': row[0],
                'run_type': row[1],
                'started_at': row[2].isoformat() if row[2] else None,
                'completed_at': row[3].isoformat() if row[3] else None,
                'status': row[4],
                'active_scrapers': row[5],
                'report_json': row[6],
                'store': row[7],
            })
        return result
    except Exception as e:
        logger.warning(f"[AutoSync] get_auto_sync_history error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def cleanup_stale_sync_runs(stale_after_minutes: int = 30) -> int:
    """Mark any 'running' rows older than stale_after_minutes as 'failed'.

    Called on Flask startup so orphaned runs from previous process restarts
    don't block the scheduler or confuse the dashboard.
    Returns the number of rows fixed.
    """
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return 0
        cur.execute(
            """
            UPDATE auto_sync_runs
               SET status       = 'failed',
                   completed_at = CURRENT_TIMESTAMP,
                   report_json  = COALESCE(report_json, '{}'::jsonb)
                                  || '{"fatal_error": "Interrupted by server restart"}'::jsonb
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '%s minutes'
            RETURNING id
            """,
            (stale_after_minutes,),
        )
        rows = cur.fetchall()
        conn.commit()
        cur.close()
        if rows:
            logger.warning(f"[AutoSync] Cleaned up {len(rows)} stale running run(s): {[r[0] for r in rows]}")
        return len(rows)
    except Exception as e:
        logger.warning(f"[AutoSync] cleanup_stale_sync_runs error: {e}")
        return 0
    finally:
        if conn:
            _return_connection(conn)


def save_kv(key: str, value: str) -> None:
    """Persist a key-value pair to the app_kv table."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            """
            INSERT INTO app_kv (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[KV] save error ({key}): {e}")
    finally:
        if conn:
            _return_connection(conn)


def load_kv(key: str, default: str = '') -> str:
    """Load a value from the app_kv table."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return default
        cur.execute("SELECT value FROM app_kv WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else default
    except Exception as e:
        logger.warning(f"[KV] load error ({key}): {e}")
        return default
    finally:
        if conn:
            _return_connection(conn)


def upsert_oos_pending(scraper_id: str, shopify_product_id: str,
                       title: str = None, sku: str = None,
                       handle: str = None, store: str = 'main') -> None:
    """Insert product into oos_pending_removal (ignore if already present)."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            """
            INSERT INTO oos_pending_removal
                (scraper_id, shopify_product_id, title, sku, handle, store)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (shopify_product_id, store) DO NOTHING
            """,
            (scraper_id, str(shopify_product_id), title, sku, handle, store),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[AutoSync] upsert_oos_pending error: {e}")
    finally:
        if conn:
            _return_connection(conn)


def get_oos_pending(scraper_id: str, store: str = 'main') -> list:
    """Return all pending OOS rows for a scraper."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return []
        cur.execute(
            """
            SELECT shopify_product_id, title, sku, handle, first_seen_missing_at
              FROM oos_pending_removal
             WHERE scraper_id = %s AND store = %s
            """,
            (scraper_id, store),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                'id': r[0],
                'title': r[1],
                'sku': r[2],
                'handle': r[3],
                'first_seen_missing_at': r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[AutoSync] get_oos_pending error: {e}")
        return []
    finally:
        if conn:
            _return_connection(conn)


def clear_oos_pending(shopify_product_id: str, store: str = 'main') -> None:
    """Remove a product from oos_pending_removal after it has been deleted."""
    conn = None
    try:
        conn, cur = get_connection()
        if not conn:
            return
        cur.execute(
            "DELETE FROM oos_pending_removal WHERE shopify_product_id = %s AND store = %s",
            (str(shopify_product_id), store),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"[AutoSync] clear_oos_pending error: {e}")
    finally:
        if conn:
            _return_connection(conn)


# -------------------------------------------------------
# Test
# -------------------------------------------------------
if __name__ == "__main__":
    print("Testing direct Postgres connection...")

    upsert_product(
        product_json={"products": {"products": []}, "total_products": "0"},
        website_url="talal",
        currency="pound"
    )

    print("\nFetching product by website_url 'talal':")
    product = get_product_by_website("talal")
    print(product)
