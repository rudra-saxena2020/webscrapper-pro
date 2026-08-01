import os
import time
import json
import re
import random
import requests
from curl_cffi import requests as cur_requests
from bs4 import BeautifulSoup
from seleniumbase import SB
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

import sys
# Add the project root to sys.path to import db and tag_engine modules correctly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from core.tag_engine import (
    clean_title,
    generate_handle,
    apply_standardized_tags,
    detect_gender,
    append_brand_message,
    sanitize_html_description,
    refine_gender_for_coach_export,
    map_to_store_tags,
    get_uk_size_tags,
    us_size_to_uk_tag,
)
from core.db import upsert_all_product_data, start_scrape_record, update_scrape_record, heartbeat_scrape_record, upload_csv_to_supabase
from core.shopify_transformer import transform_to_shopify, export_shopify_csv, normalize_shopify_csv_file

# Monkey patch to prevent uc_reconnect AttributeError
# SeleniumBase 4.45.10 doesn't have uc_reconnect method
# This ensures if any internal code tries to call it, it won't crash
try:
    from seleniumbase import BaseCase
    if not hasattr(BaseCase, 'uc_reconnect'):
        BaseCase.uc_reconnect = lambda self, timeout=5: None
        print("Monkey-patched uc_reconnect to BaseCase")
except ImportError:
    pass

# URL constants - FIXED: Use coach.com consistently (sitemap returns coach.com URLs)
BASE_URL = "https://www.coach.com"
SITEMAP_URL = "https://www.coach.com/sitemap_0-product.xml"
COACH_CAT_URLS = [
    "https://www.coach.com/shop/women/view-all?sz=1000",
    "https://www.coach.com/shop/men/view-all?sz=1000",
    "https://www.coach.com/shop/outlet/women/view-all?sz=1000",
    "https://www.coach.com/shop/outlet/men/view-all?sz=1000",
    "https://www.coach.com/shop/sale/view-all?gender=Women&sz=1000",
    "https://www.coach.com/shop/sale/view-all?gender=Men&sz=1000"
]

KNOWN_NON_PRODUCT_KEYS = {
    "poshmarkConfigs", "enableStoreContext", "nav-drawer-content-link",
    "pdpContentAreaOne", "isEnableContentFour", "pageSizeAllReviewsModal",
    "inventoryMessageTypes", "loveAtFirstSwipe", "indexableFeaturedQueries",
    "hideQuantityDropdown", "lastSeenpidsCookieMaxAge", "enableOOSExperience",
    "imageType1to1AspectRatio", "useModelAspageIdUGCOnPdp", "_price_",
    "CustomizerHideTags"
}

_NON_PRODUCT_PREFIXES = (
    "swatches-", "accordion-", "tab-", "panel-", "section-",
    "content-", "slot-", "block-", "widget-", "carousel-",
)

def _is_valid_product_id(raw_id):
    """
    Strictly validate Coach PDP identifiers and reject config/CMS keys.

    Coach product IDs follow patterns like C4048, CP116, CCG39-QB/NI.
    They are always 4–24 chars, contain at least one digit, use only
    [A-Za-z0-9-/], and are never camelCase.
    """
    if not raw_id:
        return False

    p_id = str(raw_id).strip().split("?")[0].split("#")[0]
    if not p_id:
        return False

    # Explicit blocklist of known config/CMS keys
    if p_id in KNOWN_NON_PRODUCT_KEYS:
        return False

    # Block known UI-component ID prefixes
    if p_id.lower().startswith(_NON_PRODUCT_PREFIXES):
        return False

    # Coach product IDs are always ≥ 4 chars (3-char codes are color/size tokens)
    if len(p_id) < 4 or len(p_id) > 24:
        return False

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-/]*", p_id):
        return False

    # Must contain at least one digit
    if not any(c.isdigit() for c in p_id):
        return False

    # Exclude camelCase config keys (e.g. "poshmarkConfigs")
    if re.search(r"[a-z][A-Z]", p_id):
        return False

    return True

load_dotenv()

proxy_str = os.getenv("PROXY_URL") or os.getenv("PROXY_CHROME")
if proxy_str and not proxy_str.startswith("http"):
    proxy_str = f"http://{proxy_str}"
proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None
print("Using proxies:", proxies)

def scrape_coach_ids(progress_callback=None, **kwargs):
    """Initial product ID discovery via sitemap and category crawl fallback with enhanced caching."""
    product_ids = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive"
    }
    
    scrape_record_id = kwargs.get('scrape_record_id')
    force_refresh = kwargs.get('force_refresh', False)

    # Enhanced caching logic
    CACHE_DIR = "scraped_files/cache"
    CACHE_FILE = os.path.join(CACHE_DIR, "coach_ids_cache.json")
    CACHE_METADATA = os.path.join(CACHE_DIR, "coach_cache_metadata.json")
    
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    # Load metadata if exists
    metadata = {}
    if os.path.exists(CACHE_METADATA):
        try:
            with open(CACHE_METADATA, "r") as f:
                metadata = json.load(f)
        except:
            metadata = {}
            
    # Load cache if not forcing refresh
    if not force_refresh and os.path.exists(CACHE_FILE):
        try:
            file_age = time.time() - os.path.getmtime(CACHE_FILE)
            cache_ttl = 3600 * 6  # 6 hours TTL for more frequent data freshness
            
            # Check if cache is still valid
            if file_age < cache_ttl:
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                    
                # Handle both legacy list and new dict formats
                if isinstance(data, dict):
                    cached_ids = data.get("ids", [])
                    cached_hints = data.get("hints", {})
                else:
                    cached_ids = data
                    cached_hints = {}

                # Purge stale/junk IDs from older cache schema revisions.
                cached_ids = [pid for pid in cached_ids if _is_valid_product_id(pid)]
                if isinstance(cached_hints, dict):
                    cached_hints = {pid: hint for pid, hint in cached_hints.items() if pid in set(cached_ids)}
                    
                if cached_ids and len(cached_ids) > 50:
                    # Calculate cache statistics
                    cache_hit_rate = metadata.get('hit_count', 0) / max(metadata.get('access_count', 1), 1)
                    
                    print(f"📦 Loaded {len(cached_ids)} product IDs from cache "
                          f"(Age: {int(file_age/3600)}h {int((file_age%3600)/60)}m, "
                          f"Hit rate: {cache_hit_rate:.1%})")
                    
                    # Update metadata
                    metadata['access_count'] = metadata.get('access_count', 0) + 1
                    metadata['hit_count'] = metadata.get('hit_count', 0) + 1
                    metadata['last_accessed'] = time.time()
                    
                    try:
                        with open(CACHE_METADATA, "w") as f:
                            json.dump(metadata, f)
                    except:
                        pass
                    
                    if scrape_record_id:
                        heartbeat_scrape_record(scrape_record_id, len(cached_ids))
                    return cached_ids, cached_hints
                else:
                    print(f"⚠️ Cache exists but has insufficient data ({len(cached_ids) if cached_ids else 0} items)")
            else:
                print(f"🔄 Cache expired ({int(file_age/3600)}h old, TTL: {cache_ttl/3600}h)")
                
        except Exception as e:
            print(f"⚠️ Cache read error: {e}")
    
    metadata['access_count'] = metadata.get('access_count', 0) + 1
    metadata['last_miss'] = time.time()
    
    gender_hints = {}

    # 1. Sitemap Discovery
    print(f"📡 Discovering products from sitemap: {SITEMAP_URL}")
    for attempt in ["proxy", "direct"]:
        try:
            cur_proxies = proxies if attempt == "proxy" else None
            r = cur_requests.get(SITEMAP_URL, headers=headers, proxies=cur_proxies, impersonate="chrome120", timeout=15)
            
            if r.status_code == 407 and attempt == "proxy":
                print("⚠️ Proxy exhausted in Coach sitemap — attempting direct...")
                continue
                
            if r.ok:
                # Extract ALL product URLs from sitemap (not just coachoutlet)
                locs = re.findall(r'<loc>([^<]+)</loc>', r.text)
                for url in locs:
                    if "/products/" in url and (".html" in url or url.count('/') >= 4):
                        # Extract product ID from URL
                        url_parts = url.rstrip('/').split('/')
                        last_part = url_parts[-1]
                        p_id = last_part.replace('.html', '').strip()

                        if _is_valid_product_id(p_id):
                            product_ids.add(p_id)
                            # Robust hint extraction from URL (including dashes)
                            url_lower = url.lower()
                            if any(x in url_lower for x in ['/men/', '/mens/', 'men-', 'mens-']): 
                                gender_hints[p_id] = "men"
                            elif any(x in url_lower for x in ['/women/', '/womens/', 'women-', 'womens-']): 
                                gender_hints[p_id] = "women"
                print(f"✅ Found {len(product_ids)} IDs in sitemap ({attempt}). hints: {len([h for h in gender_hints.values() if h != 'unknown'])}")
                break
        except Exception as e:
            print(f"⚠️ Sitemap error ({attempt}): {e}")
            if attempt == "direct": break

    # 2. High-Speed Category Discovery (Bypasses Selenium for stability)
    print(f"🕵️ Starting high-speed category discovery for {len(COACH_CAT_URLS)} categories...")
    
    def fetch_category_fast(cat_url, category_index):
        category_ids = set()
        cat_gender = "women" if "/women/" in cat_url.lower() or "gender=women" in cat_url.lower() else ("men" if "/men/" in cat_url.lower() or "gender=men" in cat_url.lower() else "unknown")
        
        try:
            print(f"🔍 [{category_index+1}/{len(COACH_CAT_URLS)}] Fetching: {cat_url} (Hint: {cat_gender})")
            # Use curl_cffi to matchPDP level bypassing
            r = cur_requests.get(cat_url, headers=headers, proxies=proxies, impersonate="chrome120", timeout=30)
            
            if r.status_code == 200:
                source = r.text
                # Use high-performance regex discovery
                pids = set(re.findall(r'"sku":"([^"\s]+)\s*[^"]*"', source))
                pids.update(re.findall(r'"id":"([^"]+)"', source))
                pids.update(re.findall(r'data-pid="([^"]+)"', source))
                
                for p in pids:
                    # Clean the ID (remove color variations if present in the ID string)
                    base_p = p.split(' ')[0].split('+')[0]
                    if _is_valid_product_id(base_p):
                        category_ids.add(base_p)
                
                print(f"  ✅ Finished category. Found {len(category_ids)} IDs.")
                return category_ids, cat_gender
            else:
                print(f"⚠️ Category fetch failed ({r.status_code}) on {cat_url}")
        except Exception as e:
            print(f"⚠️ Category fetch error on {cat_url}: {e}")
            
        return category_ids, cat_gender

    # Use a small threadpool for speed but safe enough for proxies
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_url = {executor.submit(fetch_category_fast, url, i): i for i, url in enumerate(COACH_CAT_URLS)}
        completed = 0
        for future in as_completed(future_to_url):
            category_ids, cat_gender = future.result()
            completed += 1
            if category_ids:
                product_ids.update(category_ids)
                for pid in category_ids:
                    if pid not in gender_hints or gender_hints[pid] == "unknown":
                        gender_hints[pid] = cat_gender
            
            # Simple, non-crashing progress feedback
            if progress_callback:
                total_cats = len(COACH_CAT_URLS)
                discovery_prog = min(10 + int((completed / total_cats) * 20), 30)
                progress_callback(discovery_prog, f"Discovering: Found {len(product_ids)} IDs...", len(product_ids))

            if scrape_record_id:
                heartbeat_scrape_record(scrape_record_id, len(product_ids))
    
    print(f"✅ Category discovery completed. Total unique IDs: {len(product_ids)}")

    # Enhanced cache saving with metadata
    if product_ids:
        try:
            # Save main cache
            with open(CACHE_FILE, "w") as f:
                json.dump({"ids": list(product_ids), "hints": gender_hints}, f)
            
            # Update metadata
            metadata['last_updated'] = time.time()
            metadata['item_count'] = len(product_ids)
            metadata['source'] = 'sitemap_and_category_crawl'
            metadata['version'] = '2.0'
            
            # Calculate and store performance metrics
            if 'previous_item_count' in metadata:
                change_pct = ((len(product_ids) - metadata['previous_item_count']) /
                             metadata['previous_item_count'] * 100)
                metadata['change_percentage'] = round(change_pct, 2)
            
            metadata['previous_item_count'] = len(product_ids)
            
            # Save metadata
            with open(CACHE_METADATA, "w") as f:
                json.dump(metadata, f)
            
            print(f"💾 Saved {len(product_ids)} product IDs to cache "
                  f"(Size: {os.path.getsize(CACHE_FILE) / 1024:.1f} KB)")
            
            # Optional: Create a compressed backup
            try:
                import gzip
                compressed_file = CACHE_FILE + '.gz'
                with open(CACHE_FILE, 'rb') as f_in:
                    with gzip.open(compressed_file, 'wb') as f_out:
                        f_out.write(f_in.read())
                print(f"📦 Created compressed backup: {os.path.basename(compressed_file)} "
                      f"({os.path.getsize(compressed_file) / 1024:.1f} KB)")
            except ImportError:
                pass  # gzip not available, skip compression
                
        except Exception as e:
            print(f"⚠️ Cache write error: {e}")
            import traceback
            traceback.print_exc()

    return list(product_ids), gender_hints

def _extract_coach_product_from_source(page_source, p_id, url):
    """
    Multi-strategy extraction from a Coach product page source.
    Returns a result dict or None if no usable data found.
    Strategies (in order):
      1. Expanded window object patterns (PDP_DATA, __INITIAL_STATE__, __NEXT_DATA__, digitalData)
      2. JSON-LD structured data (<script type="application/ld+json">)
      3. Broad BS4 CSS selectors as last resort
    """
    # Helper to find product dict in nested JSON
    def find_product_dict(obj):
        """Return a dict with product data (name, price, images, etc.) or None."""
        if isinstance(obj, dict):
            # Check if this dict looks like a product (has name and prices)
            if "name" in obj and ("prices" in obj or "price" in obj):
                return obj
            # Also check for product key
            if "product" in obj and isinstance(obj["product"], dict):
                return obj["product"]
            # Search recursively
            for v in obj.values():
                result = find_product_dict(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = find_product_dict(item)
                if result:
                    return result
        return None

    # --- Strategy 1: Window/global JS objects ---
    patterns = [
        r'window\.PDP_DATA\s*=\s*({.+?});\s*(?:window|</script>)',
        r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*(?:window|</script>)',
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>\s*({.+?})\s*</script>',
        r'window\.digitalData\s*=\s*({.+?});\s*(?:window|</script>)',
        r'App\.init\(\s*({.+?})\s*\)',
        r'"product"\s*:\s*({[^{}]{50,}})',  # Looser match for embedded product blobs
    ]
    for pat in patterns:
        match = re.search(pat, page_source, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                # Try to find product dict within the parsed data
                product = find_product_dict(data)
                if product:
                    # Use the product dict as data for consistency
                    name = product.get("name") or product.get("displayName") or product.get("title")
                    if name:
                        return {"id": p_id, "data": product, "url": url, "strategy": "js_object"}
                else:
                    # Fallback to old logic
                    p = data.get("product", data)
                    name = p.get("name") or p.get("displayName") or p.get("title")
                    if name:
                        return {"id": p_id, "data": data, "url": url, "strategy": "js_object"}
            except (json.JSONDecodeError, ValueError):
                continue

    # --- Strategy 2: JSON-LD structured data ---
    soup = BeautifulSoup(page_source, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            # Handle @graph array or single object
            items = ld.get("@graph", [ld]) if isinstance(ld, dict) else (ld if isinstance(ld, list) else [])
            for item in items:
                if item.get("@type") in ("Product", "IndividualProduct"):
                    name = item.get("name", "")
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") or offers.get("lowPrice")
                    if name and price:
                        return {
                            "id": p_id,
                            "data": {
                                "name": name,
                                "price": {"value": str(price)},
                                "category": item.get("category", "Handbags"),
                                "description": item.get("description", ""),
                                "images": [item.get("image", "")] if item.get("image") else [],
                            },
                            "url": url,
                            "strategy": "json_ld"
                        }
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    # --- Strategy 3: Broad BS4 CSS selectors ---
    # Try multiple known h1/price selector patterns
    name_selectors = [
        ("h1", {"class_": "product-name"}),
        ("h1", {"class_": re.compile(r"product[-_]?name", re.I)}),
        ("h1", {"class_": re.compile(r"pdp[-_]?title", re.I)}),
        ("h1", {}),  # any h1
    ]
    price_selectors = [
        ("span", {"class_": "sales"}),
        ("span", {"class_": re.compile(r"sales|price--sale|sale-price", re.I)}),
        ("span", {"class_": re.compile(r"price", re.I)}),
        ("div", {"class_": re.compile(r"price", re.I)}),
        ("meta", {"itemprop": "price"}),
    ]

    name_tag = None
    for tag, attrs in name_selectors:
        name_tag = soup.find(tag, **attrs)
        if name_tag:
            break

    price_tag = None
    for tag, attrs in price_selectors:
        price_tag = soup.find(tag, **attrs)
        if price_tag:
            break

    if name_tag:
        name = name_tag.get_text(strip=True)
        price_text = "0"
        if price_tag:
            raw = price_tag.get("content") or price_tag.get_text(strip=True)
            raw = raw.replace("$", "").replace(",", "").strip()
            m = re.search(r'[\d.]+', raw)
            if m:
                price_text = m.group(0)
        if name and price_text != "0":
            return {
                "id": p_id,
                "data": {
                    "name": name,
                    "price": {"value": price_text},
                    "category": "Handbags",
                },
                "url": url,
                "strategy": "bs4"
            }

    return None

# ── Shared fetch state ────────────────────────────────────────────────────────
# Thread-safe state shared across all concurrent workers.

import threading as _threading

# Thread-local storage — used to fire the warmup stagger exactly once per
# worker thread (not once per product, which would kill throughput).
_tl = _threading.local()

# Dead-SKU cache: 404 responses are permanent — skip on any re-encounter
_DEAD_SKUS: set = set()
_DEAD_SKUS_LOCK = _threading.Lock()

# Circuit breaker: when Akamai IP-bans the whole pool, pause everyone
_FETCH_STATE = {
    'consecutive_403': 0,       # resets to 0 on any 200
    'cooldown_until': 0.0,      # epoch timestamp; workers sleep until this passes
    'run_id': 0,                # incremented each run so stagger re-fires per run
    'cooldown_cycles': 0,       # how many 90s cooldowns this run has had
    'successes_this_run': 0,    # 200 OK count since run started; resets each run
    'ip_banned': False,         # set True when ban confirmed; workers abort instantly
    'lock': _threading.Lock(),
}

_CIRCUIT_TRIGGER = 8    # consecutive 403s before declaring rate-limit cooldown
_CIRCUIT_COOLDOWN = 90  # seconds to wait before retrying after IP-ban
_BAN_CYCLES = 1         # ONE cooldown cycle with 0 successes = hard IP ban; abort

# Proven user agents that bypass Coach's Akamai WAF (Chrome 120 fingerprint)
_COACH_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]


def _cb_report_403():
    """Tell the circuit breaker a 403 was seen; trigger cooldown if threshold hit."""
    with _FETCH_STATE['lock']:
        if _FETCH_STATE['ip_banned']:
            return
        _FETCH_STATE['consecutive_403'] += 1
        count = _FETCH_STATE['consecutive_403']
        now = time.time()
        if count >= _CIRCUIT_TRIGGER and now >= _FETCH_STATE['cooldown_until']:
            _FETCH_STATE['cooldown_until'] = now + _CIRCUIT_COOLDOWN
            _FETCH_STATE['cooldown_cycles'] += 1
            cycles = _FETCH_STATE['cooldown_cycles']
            successes = _FETCH_STATE['successes_this_run']
            print(f"🚨 [Coach] {count} consecutive 403s — IP cooldown {_CIRCUIT_COOLDOWN}s activated "
                  f"(cycle {cycles}, successes this run: {successes})")
            # Hard ban: multiple cooldown cycles with zero new successes between them
            if cycles >= _BAN_CYCLES and successes == 0:
                _FETCH_STATE['ip_banned'] = True
                print("🚫 [Coach] IP hard-banned by Akamai — aborting run. "
                      "Wait 4-24 h or configure a proxy before retrying.")


def _cb_report_success():
    """Tell the circuit breaker a 200 was received; reset the counter."""
    with _FETCH_STATE['lock']:
        _FETCH_STATE['consecutive_403'] = 0
        _FETCH_STATE['successes_this_run'] += 1


def _cb_wait_if_cooling():
    """Block the calling worker until the circuit-breaker cooldown expires.

    Returns True normally; returns False immediately if the run has been
    aborted due to a confirmed hard IP ban so workers can exit early.
    """
    while True:
        with _FETCH_STATE['lock']:
            if _FETCH_STATE['ip_banned']:
                return False
            remaining = _FETCH_STATE['cooldown_until'] - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 5))


def _fetch_coach_product_single(p_id, worker_id, progress_callback=None,
                                current_count_ref=None, total_count=0):
    """
    Fetches a single Coach product page using curl_cffi (chrome120 impersonation).

    Retry strategy:
      • 200        → parse; reset circuit breaker; return.
      • 404        → permanent; mark dead; no retry.
      • other 4xx  → permanent client error; no retry.
      • 403        → IP block; report to circuit breaker; back-off 30-60 s; retry ×1.
      • 429        → rate-limit; back-off 20-40 s; retry ×1.
      • 5xx        → server error; back-off 5-10 s; retry ×1.
      • exception  → brief pause; retry ×1.

    Startup stagger: worker_id × 0.7 s on the very first attempt — spreads the
    initial burst across several seconds so Akamai does not see a connection flood.
    """
    # Skip instantly if already known dead
    with _DEAD_SKUS_LOCK:
        if p_id in _DEAD_SKUS:
            if current_count_ref is not None:
                with current_count_ref['lock']:
                    current_count_ref['count'] += 1
            return None

    url = f"{BASE_URL}/products/{p_id}.html"

    # IMPORTANT: keep headers byte-for-byte identical to the fingerprint that
    # was proven to bypass Akamai on Coach (chrome120, en-US).
    # Changing Sec-Ch-Ua, Accept-Language, or impersonate version can trigger WAF.
    headers = {
        "User-Agent": random.choice(_COACH_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    MAX_RETRIES = 1  # one retry on transient errors (403, 429, 5xx)

    for attempt in range(MAX_RETRIES + 1):
        # Wait out any active circuit-breaker cooldown; False = IP banned, abort
        if not _cb_wait_if_cooling():
            return None

        # Also abort immediately if ban was confirmed while we were in back-off
        if _FETCH_STATE.get('ip_banned'):
            return None

        try:
            # One-time warmup stagger per worker THREAD per run.
            # Compares the thread-local run_id to the global one so that each
            # new scrape run re-staggers even when threads are reused by the pool.
            current_run = _FETCH_STATE['run_id']
            if attempt == 0 and getattr(_tl, 'last_run_id', -1) != current_run:
                _tl.last_run_id = current_run
                stagger = worker_id * 0.4 + random.uniform(0.1, 0.3)
                time.sleep(stagger)
            # (back-off for retries is applied inside the 4xx/5xx branches below)

            r = cur_requests.get(url, headers=headers, proxies=proxies,
                                 impersonate="chrome120", timeout=20)

            if r.status_code == 200:
                result = _extract_coach_product_from_source(r.text, p_id, url)
                _cb_report_success()
                # Per-request throttle: keeps each worker at ~0.5-1 req/s so
                # 15 workers together stay in the 4-6/s range that Akamai
                # tolerates.  Without this, 15 workers at 2s network latency
                # drive ~7+/s and trigger rate-limiting after ~200 products.
                time.sleep(random.uniform(0.5, 1.5))
                if result:
                    result.pop("strategy", None)
                    if current_count_ref is not None:
                        with current_count_ref['lock']:
                            current_count_ref['count'] += 1
                            current_count_ref['successes'] += 1
                            current = current_count_ref['count']
                            success = current_count_ref['successes']
                            if current % 10 == 0 or current == total_count:
                                elapsed = time.time() - current_count_ref['start_time']
                                rate = current / elapsed if elapsed > 0 else 0
                                eta = (total_count - current) / rate if rate > 0 else 0
                                msg = f"Progress: {current}/{total_count} ({success} success)"
                                print(f"📈 [Coach] {msg}, {rate:.2f}/s, ETA: {eta/60:.1f}m")
                                if progress_callback:
                                    progress_callback(
                                        int(30 + current / total_count * 50), msg, success)
                    return result
                else:
                    print(f"⚠️  [Coach] 200 but no data extracted for {p_id}")
                    break  # parsing failure — retrying same HTML won't help

            elif r.status_code == 404:
                with _DEAD_SKUS_LOCK:
                    _DEAD_SKUS.add(p_id)
                break  # permanent — no retry

            elif r.status_code == 403:
                _cb_report_403()
                if attempt < MAX_RETRIES:
                    wait = random.uniform(30, 60)
                    print(f"🛑 [Coach] 403 on {p_id} — back-off {wait:.0f}s (attempt {attempt+1})")
                    # Interruptible sleep: exit early if a hard IP ban is declared
                    deadline = time.time() + wait
                    while time.time() < deadline:
                        if _FETCH_STATE.get('ip_banned'):
                            return None
                        time.sleep(min(1.0, deadline - time.time()))
                else:
                    print(f"🛑 [Coach] 403 on {p_id} — giving up after {MAX_RETRIES+1} attempts")

            elif r.status_code == 429:
                if attempt < MAX_RETRIES:
                    wait = random.uniform(20, 40)
                    print(f"⏳ [Coach] 429 rate-limit on {p_id} — back-off {wait:.0f}s (attempt {attempt+1})")
                    time.sleep(wait)

            elif r.status_code >= 500:
                if attempt < MAX_RETRIES:
                    wait = random.uniform(5, 10)
                    print(f"🔴 [Coach] {r.status_code} server error on {p_id} — retry in {wait:.0f}s")
                    time.sleep(wait)

            else:
                print(f"❌ [Coach] {r.status_code} on {p_id} (no retry)")
                break  # other 4xx is permanent

        except Exception as exc:
            if attempt < MAX_RETRIES:
                time.sleep(random.uniform(2, 5))
            else:
                print(f"🔥 [Coach] {p_id} failed: {exc}")

    if current_count_ref is not None:
        with current_count_ref['lock']:
            current_count_ref['count'] += 1
    return None


def fetch_coach_product_details(product_ids, gender_hints=None, max_workers=20,
                                progress_callback=None):
    """Fetches details for multiple Coach product IDs with safe concurrency."""
    if not product_ids:
        return []

    product_ids = [pid for pid in product_ids if _is_valid_product_id(pid)]
    if not product_ids:
        return []

    # De-duplicate while preserving order
    seen: set = set()
    unique_ids = []
    for pid in product_ids:
        if pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)
    product_ids = unique_ids

    gender_hints = gender_hints or {}

    # Reset all circuit-breaker state for each fresh run.
    with _FETCH_STATE['lock']:
        _FETCH_STATE['consecutive_403'] = 0
        _FETCH_STATE['cooldown_until'] = 0.0
        _FETCH_STATE['cooldown_cycles'] = 0
        _FETCH_STATE['successes_this_run'] = 0
        _FETCH_STATE['ip_banned'] = False
        _FETCH_STATE['run_id'] += 1

    # 15 workers + 0.5-1.5s per-request throttle after every 200 OK.
    # This keeps aggregate throughput at ~4-5/s — matching the original proven
    # working rate — while the one-time warmup stagger avoids a simultaneous
    # connection flood at startup.
    MAX_REQUEST_WORKERS = 15

    print(f"🚀 [Coach] Fetching {len(product_ids)} products — "
          f"{MAX_REQUEST_WORKERS} workers (throttled, chrome120) ...")

    import threading as _thr
    progress_state = {
        'count': 0,
        'successes': 0,
        'start_time': time.time(),
        'lock': _thr.Lock(),
    }

    results = []

    with ThreadPoolExecutor(max_workers=MAX_REQUEST_WORKERS) as executor:
        future_to_pid = {
            executor.submit(
                _fetch_coach_product_single, pid, i % MAX_REQUEST_WORKERS,
                progress_callback, progress_state, len(product_ids)
            ): pid
            for i, pid in enumerate(product_ids)
        }
        for future in as_completed(future_to_pid):
            pid = future_to_pid[future]
            try:
                res = future.result()
                if res:
                    if pid in gender_hints:
                        res["url_gender_hint"] = gender_hints[pid]
                    results.append(res)
            except Exception as exc:
                print(f"❌ [Coach] Unexpected error on {pid}: {exc}")

    total_time = time.time() - progress_state['start_time']
    rate = len(results) / total_time if total_time > 0 else 0
    skipped = len(_DEAD_SKUS)

    if _FETCH_STATE.get('ip_banned'):
        msg = (f"Coach IP banned by Akamai after {len(results)} products. "
               "Wait 4-24 h for the ban to lift, then try again.")
        print(f"🚫 [Coach] {msg}")
        # Raise so app.py marks the scraper as stopped with a visible error,
        # rather than overwriting the status with "Completed ✅".
        raise RuntimeError(msg)

    print(f"✅ [Coach] {len(results)}/{len(product_ids)} fetched in {total_time:.1f}s "
          f"({rate:.2f}/s) — {skipped} dead SKUs skipped")
    return results

# ── Coach-specific metadata helpers ───────────────────────────────────────────

# Maps common Coach breadcrumb sub-category names → clean Shopify tag slugs
_SUBCAT_SLUG_MAP = {
    "shoulder bags": "shoulder-bag",
    "shoulder bag": "shoulder-bag",
    "crossbody bags": "crossbody-bag",
    "crossbody": "crossbody-bag",
    "totes": "tote",
    "tote bags": "tote",
    "satchels": "satchel",
    "clutches": "clutch",
    "wristlets": "wristlet",
    "bucket bags": "bucket-bag",
    "hobos": "hobo-bag",
    "belt bags": "belt-bag",
    "mini bags": "mini-bag",
    "backpacks": "backpack",
    "wallets": "wallet",
    "card cases": "card-case",
    "cardholders": "card-case",
    "coin purses": "coin-purse",
    "small leather goods": "small-leather-goods",
    "leather goods": "leather-goods",
    "handbags": "handbag",
    "bags": "bag",
    "sneakers": "sneaker",
    "trainers": "sneaker",
    "boots": "boot",
    "booties": "boot",
    "sandals": "sandal",
    "slides": "slide",
    "loafers": "loafer",
    "pumps": "pump",
    "flats": "flat",
    "shoes": "shoe",
    "footwear": "shoe",
    "watches": "watch",
    "sunglasses": "sunglasses",
    "scarves": "scarf",
    "hats": "hat",
    "belts": "belt",
    "jewelry": "jewelry",
    "jewellery": "jewelry",
    # Charms / keyrings / straps (Coach "Straps, Charms & Keyrings" category)
    "straps, charms & keyrings": "charm",
    "straps charms and keyrings": "charm",
    "straps charms keyrings": "charm",
    "charms": "charm",
    "charm": "charm",
    "key rings": "keyring",
    "keyrings": "keyring",
    "keyfobs": "keyfob",
    "straps": "strap",
    "bag straps": "strap",
    "shoulder straps": "strap",
    # Apparel
    "clothing": "apparel",
    "ready-to-wear": "apparel",
    "tops": "top",
    "tees": "top",
    "t-shirts": "top",
    "hoodies": "top",
    "sweatshirts": "top",
    "shirts": "shirt",
    "knitwear": "top",
    "sweaters": "top",
    "dresses": "dress",
    "skirts": "apparel",
    "jackets": "jacket",
    "outerwear": "jacket",
    "coats": "coat",
    "pants": "pant",
    "trousers": "pant",
    "shorts": "shorts",
    "jeans": "pant",
    "blouses": "top",
    "vests": "apparel",
    "cardigans": "top",
}

# Product-type keywords to detect from the title when breadcrumbs are lacking
_TITLE_TYPE_MAP = [
    ("shoulder-bag", ["shoulder bag"]),
    ("crossbody-bag", ["crossbody", "cross body", "cross-body"]),
    ("tote", ["tote"]),
    ("satchel", ["satchel"]),
    ("clutch", ["clutch"]),
    ("wristlet", ["wristlet"]),
    ("bucket-bag", ["bucket bag"]),
    ("hobo-bag", ["hobo"]),
    ("belt-bag", ["belt bag", "beltbag"]),
    ("mini-bag", ["mini bag", "micro bag"]),
    ("backpack", ["backpack", "rucksack"]),
    ("bag", ["messenger bag", "workbag", "messenger"]),   # must come before wallet/card
    ("wallet", ["wallet"]),
    ("card-case", ["card case", "cardholder", "card holder"]),
    ("jewelry", ["necklace", "bracelet", "earring", "bangle", "pendant",
                 "ring", "brooch", "cufflink"]),
    ("sneaker", ["sneaker", "trainer"]),
    ("boot", ["boot", "bootie"]),
    ("sandal", ["sandal", "slide"]),
    ("loafer", ["loafer", "moccasin"]),
    ("pump", ["pump", "stiletto", "kitten heel"]),
    ("flat", ["ballet flat", "flat shoe"]),
    ("watch", ["watch", "chronograph", "timepiece"]),
    ("sunglasses", ["sunglass", "eyewear"]),
    ("scarf", ["scarf", "muffler"]),
    ("belt", ["belt"]),
    ("hat", ["hat", "cap", "beanie", "bucket hat"]),
    ("top", ["tee", "t-shirt", "hoodie", "sweatshirt", "crewneck", "pullover",
             "blouse", "camisole", "tank top", "jersey", "sweater", "knitwear"]),
    ("shirt", ["shirt", "polo", "oxford", "flannel shirt", "button-down"]),
    ("jacket", ["jacket", "blazer", "bomber", "blouson", "puffer", "parka",
                "anorak", "windbreaker", "trench", "varsity", "moto jacket",
                "leather jacket", "shearling jacket", "fleece jacket"]),
    ("coat", ["coat", "trench coat", "overcoat", "duster"]),
    ("pant", ["pant", "trouser", "jean", "chino", "legging", "jogger"]),
    ("dress", ["dress", "gown", "midi dress", "mini dress", "maxi dress"]),
    ("apparel", ["skirt", "shorts", "vest", "cardigan", "swimwear", "swim",
                 "robe", "tracksuit", "jumpsuit", "romper"]),
]


# ── Gender-deterministic product keywords ────────────────────────────────────
# Product types that are almost exclusively tied to one gender.
# Used ONLY when the title/description contains them, as a content-level signal.
_WOMEN_PRODUCT_TYPES = frozenset([
    "handbag", "clutch", "purse", "wristlet", "satchel", "hobo bag",
    "bucket bag", "baguette", "minaudiere", "tote bag", "shoulder bag",
    "crossbody bag", "crossbody", "mini bag", "belt bag",
    "pump", "stiletto", "kitten heel", "ballet flat", "mule",
    "skirt", "dress", "blouse", "camisole",
])
_MEN_PRODUCT_TYPES = frozenset([
    "briefcase", "dopp kit", "shaving kit", "cufflink", "necktie",
    "tie clip", "tie bar", "oxford shoe", "derby shoe", "brogue",
    "messenger bag", "messenger",
])

# Compiled regex patterns with word boundaries for title gender detection.
# CRITICAL: plain "in" substring checks cause "men's" to match inside "women's".
# Word boundaries (\b) prevent that — \b before 'm' in "women's" won't fire
# because it is preceded by the word-char 'o'.
_RE_WOMEN_TITLE = re.compile(
    r"\b(?:women'?s?|womens|woman'?s?|womans|ladies'?|lady'?s?)\b",
    re.IGNORECASE,
)
_RE_MEN_TITLE = re.compile(
    r"\b(?:men'?s?|mens|man'?s?|mans|gents'?|gent'?s?)\b",
    re.IGNORECASE,
)

# Raw API gender field values used by SFCC / Coach backend
_SFCC_GENDER_WOMEN = frozenset(["women", "female", "ladies", "womens", "woman", "f"])
_SFCC_GENDER_MEN   = frozenset(["men", "male", "gents", "mens", "man", "m"])


def _verify_product_gender(title, description, cat_raw_arr,
                           url_gender_hint, url, raw_product=None):
    """
    Multi-signal, verified gender determination.

    Scoring priority (highest → lowest):
      +20/-20  Explicit gender word in title       (e.g. "Women's Tabby Bag")
      +10/-10  Gender-deterministic product type   (e.g. "clutch" → WOMEN)
      +8 /-8   Raw API gender field                (e.g. p.get("gender") = "Women")
      +6 /-6   Breadcrumb first element            (e.g. ["Women", "Bags", ...])
      +4 /-4   URL gender hint                     (set during URL discovery)
      +3 /-3   URL path                            (/shop/women/...)
      +2 /-2   Gender keywords in description

    A score ≥ 3 → WOMEN, ≤ -3 → MEN, otherwise → UNISEX.
    This threshold ensures at least one structural signal (breadcrumb, URL)
    or a content-level signal before committing to a gender.
    """
    score = 0
    title_lower = (title or "").lower()
    desc_lower  = (description or "").lower()

    # ── Signal 1: Explicit gender word in title (DEFINITIVE ±20) ─────────────
    # Use compiled regex with \b word boundaries — prevents "men's" from
    # matching inside "women's" (the classic substring false-positive).
    women_explicit = bool(_RE_WOMEN_TITLE.search(title_lower))
    men_explicit   = bool(_RE_MEN_TITLE.search(title_lower))
    if women_explicit and not men_explicit:
        score += 20
    elif men_explicit and not women_explicit:
        score -= 20
    # If BOTH fire (genuine "Men's and Women's" label) → net 0, fall through

    # ── Signal 2: Deterministic product type in title (NEAR-DEFINITIVE ±10) ──
    # Guarded: only fire when no contradicting explicit gender word is present.
    if not men_explicit:
        for kw in _WOMEN_PRODUCT_TYPES:
            if kw in title_lower:
                score += 10
                break
    if not women_explicit:
        for kw in _MEN_PRODUCT_TYPES:
            if kw in title_lower:
                score -= 10
                break

    # ── Signal 3: Raw API product gender field (STRONG ±8) ────────────────────
    if raw_product and isinstance(raw_product, dict):
        for field in ("gender", "targetAudience", "audienceGender", "genderCode"):
            raw_val = str(raw_product.get(field) or "").strip().lower()
            if raw_val:
                if raw_val in _SFCC_GENDER_WOMEN:
                    score += 8
                elif raw_val in _SFCC_GENDER_MEN:
                    score -= 8
                break  # only use the first populated field

    # ── Signal 4: Breadcrumb first element (STRONG ±6) ────────────────────────
    if cat_raw_arr:
        first = str(cat_raw_arr[0]).strip().lower()
        if first in ("women", "women's", "ladies", "girl"):
            score += 6
        elif first in ("men", "men's", "gents", "boy"):
            score -= 6

    # ── Signal 5: URL gender hint (MEDIUM ±4) ─────────────────────────────────
    if url_gender_hint:
        h = str(url_gender_hint).strip().upper()
        if "WOMEN" in h or "LADIES" in h:
            score += 4
        elif "MEN" in h and "WOMEN" not in h:
            score -= 4

    # ── Signal 6: URL path (WEAK ±3) ──────────────────────────────────────────
    u = (url or "").lower()
    if "/women" in u or "gender=women" in u:
        score += 3
    elif "/men" in u and "women" not in u:
        score -= 3

    # ── Signal 7: Gender-specific keywords in description (WEAK ±2) ───────────
    # Use word-boundary style checks to avoid substring false-positives
    padded_desc = f" {desc_lower} "
    women_desc = any(kw in padded_desc for kw in (
        " she ", " her ", " herself ", "feminine", "womenswear", " ladies "
    ))
    men_desc = any(kw in padded_desc for kw in (
        " he ", " him ", " himself ", "masculine", "menswear", " gents "
    ))
    if women_desc and not men_desc:
        score += 2
    elif men_desc and not women_desc:
        score -= 2

    # ── Final decision (threshold ±3) ─────────────────────────────────────────
    if score >= 3:
        return "WOMEN"
    if score <= -3:
        return "MEN"

    # Only keep UNISEX when the product is explicitly marketed as unisex.
    # Otherwise default to WOMEN because Coach is a women's brand by volume.
    _cat_text = " ".join(str(c).lower() for c in (cat_raw_arr or []))
    if any(k in title_lower or k in _cat_text for k in ("unisex", "gender neutral")):
        return "UNISEX"

    # ── Coach-specific WOMEN bias for unresolved products ─────────────────────
    # Coach's business is ~90% women's. When no strong signal fires:
    #   1. Bag-type products (any title containing a bag keyword) → WOMEN
    #      Messenger bags and briefcases score ≤-10 above, so are already excluded.
    #   2. Coachtopia brand in title → WOMEN (women's sustainability sub-brand)
    #   3. Re Loved / Restored / Disney collections → WOMEN (vintage/collab items are women's)
    #      Guard: score >= 0 (no men's signals at all) to avoid flipping genuine men's items.
    #   4. Cat breadcrumb contains collection keyword → WOMEN
    _BAG_TITLE_TERMS = (
        "bag", "tote", "backpack", "clutch", "wristlet", "pouch",
        "satchel", "purse", "carryall",
    )
    if any(k in title_lower for k in _BAG_TITLE_TERMS):
        return "WOMEN"

    if "coachtopia" in title_lower:
        return "WOMEN"

    if score >= 0 and (
        title_lower.startswith("restored ") or "re loved" in title_lower
    ):
        return "WOMEN"

    for _cat in (cat_raw_arr or []):
        _cl = str(_cat).lower()
        if any(x in _cl for x in ("re loved", "restored", "disney", "coachtopia")):
            return "WOMEN"

    return "WOMEN"


def _extract_coach_categories(cat_raw_arr, title):
    """
    Returns (broad_category, sub_category_slug) from Coach breadcrumbs + title.
    broad_category: 'accessories' | 'footwear' | 'apparel'
    sub_category_slug: e.g. 'shoulder-bag', 'wallet', 'sneaker' or None
    """
    cats_lower = [c.strip().lower() for c in (cat_raw_arr or [])]
    title_lower = (title or "").lower()
    # Use the breadcrumb blob for broad detection, title only as secondary signal
    crumb_blob = " ".join(cats_lower)
    all_blob = crumb_blob + " " + title_lower

    # Broad category — check footwear FIRST to avoid "low top sneaker" → apparel
    # Use word-boundary check to prevent "cap" in "moonscape" or "hat" at end of title
    # Accessories that are NOT apparel even if apparel breadcrumbs bleed through
    _is_accessory_product = bool(re.search(
        r'\b(bag|tote|crossbody|backpack|wallet|hat|cap|bucket hat'
        r'|bootie|sandal|sneaker|loafer|boot|pump|wristlet|clutch'
        r'|charm|keyring|keyfob|keychain|key chain|key fob|strap|lanyard)\b',
        all_blob
    ))
    broad = "accessories"
    # Charms, keyrings, straps are ALWAYS accessories — shield them from both
    # the footwear AND apparel checks even if Coach breadcrumbs include "jackets"
    # or "outerwear" (Coach sometimes places shearling charms under jacket cats).
    _is_charm = bool(re.search(
        r'\b(charm|keyring|keyfob|keychain|key chain|key fob'
        r'|keyfob|strap|lanyard|dogleash|dog charm|bag charm|shoe charm)\b',
        all_blob
    ))
    if not _is_charm and any(x in all_blob for x in ["shoe", "footwear", "sneaker", "boot", "sandal",
                                                      "pump", "heel", "flat", "loafer", "mule", "slide",
                                                      "trainer", "espadrille"]):
        broad = "footwear"
    elif not _is_accessory_product and not _is_charm and any(x in all_blob for x in [
            "clothing", "apparel", "ready-to-wear", "tops", "tees", "t-shirts",
            "hoodies", "sweatshirts", "knitwear", "sweater", "sweaters",
            "dress", "dresses", "jacket", "jackets", "coat", "coats",
            "blazer", "bomber", "blouson", "puffer", "parka", "anorak",
            "windbreaker", "trench", "varsity", "moto",
            "shirt", "shirts", "polo", "blouse", "camisole", "crewneck", "pullover",
            "hoodie", "tee", "t-shirt", "sweatshirt", "fleece",
            "shorts", "pant", "pants", "trouser", "trousers", "skirt",
            "cardigan", "cardigans", "jogger", "joggers",
            "tank top", "tank ", "jumpsuit", "tracksuit", "swimwear", "swim trunk",
            "vest", "robe", "romper"]):
        broad = "apparel"

    # Sub-category: prefer the most specific breadcrumb (last element after gender)
    # Coach breadcrumb: ["Women", "Bags", "Shoulder Bags"] → sub = "Shoulder Bags"
    sub_slug = None
    non_gender = [c for c in cats_lower if c not in ("women", "men", "women's", "men's", "ladies", "gents")]
    if non_gender:
        # Try the most specific (last) breadcrumb first, then work backwards
        for candidate in reversed(non_gender):
            slug = _SUBCAT_SLUG_MAP.get(candidate)
            if slug:
                sub_slug = slug
                break
        # If no exact match, slugify the last breadcrumb directly
        if not sub_slug and non_gender:
            raw = non_gender[-1]
            sub_slug = re.sub(r'[^a-z0-9]+', '-', raw).strip('-') or None

    # Fallback: detect type from title (word boundary + optional trailing 's' for plurals)
    if not sub_slug:
        for type_tag, keywords in _TITLE_TYPE_MAP:
            if any(re.search(r'\b' + re.escape(kw) + r's?\b', title_lower) for kw in keywords):
                sub_slug = type_tag
                break

    # Final catch: generic "\bbag\b" in title (e.g. "Mini Swinger Bag", "Courier Bag")
    if not sub_slug and re.search(r'\bbag\b', title_lower):
        sub_slug = "bag"

    # ── Title-level override: trust the product name over breadcrumbs ─────────
    # Coach files "Restored" / "Re Loved" / "Upcrafted" items under "Bags" in
    # their API even when the product is apparel, footwear, or a scarf.
    _TITLE_FOOTWEAR_RE = re.compile(
        r'\b(bootie|sneaker|trainer|loafer|moccasin|sandal|slide|pump|stiletto'
        r'|kitten heel|ballet flat|mule|espadrille)\b', re.I
    )
    _TITLE_JEWELRY_RE = re.compile(
        r'\b(earring|hoop|necklace|bracelet|pendant|ring|bangle|brooch|cufflink'
        r'|anklet|choker|locket|charm|charme)\b', re.I
    )
    _TITLE_BELT_RE = re.compile(
        r'\b(belts?|reversible\s+belt|buckles?)\b', re.I
    )
    _TITLE_GLOVES_RE = re.compile(
        r'\b(gloves?|mittens?)\b', re.I
    )
    _TITLE_SOCKS_RE = re.compile(
        r'\b(socks?|quarter\s+crew|crew\s+socks?)\b', re.I
    )
    _TITLE_HAT_RE = re.compile(
        r'\b(hats?|caps?|beanies?|berets?|bucket\s+hat|baseball\s+hat|earmuffs?)\b', re.I
    )
    _TITLE_FRAGRANCE_RE = re.compile(
        r'\b(eau de|parfum|toilette|cologne|perfume|fragrance)\b', re.I
    )
    _TITLE_SCARF_RE = re.compile(r'\b(stole|shawl|muffler|bandana|scarf|scarves)\b', re.I)
    _TITLE_APPAREL_RE = re.compile(
        r'\b(t-shirt|tee|hoodie|sweatshirt|crewneck|pullover|sweater|knitwear'
        r'|blouse|camisole|jersey|shirt|polo|oxford|jacket|jackets|blazer|bomber|blouson|puffer'
        r'|parka|anorak|windbreaker|varsity|moto jacket|leather racer|racer'
        r'|coat|overcoat|duster|trouser|jean|chino|legging|jogger|dress|gown'
        r'|midi dress|maxi dress|skirt|cardigan|swimwear|swim trunk|jumpsuit'
        r'|tracksuit|romper)\b', re.I
    )

    # Sunglasses / eyewear are frequently filed under bags by Coach's API.
    _TITLE_SUNGLASSES_RE = re.compile(r'\b(sunglasses?|eyewear|optical)\b', re.I)
    # Wallet / card-case family.
    _TITLE_WALLET_RE = re.compile(r'\b(wallet|zip\s+around|accordion\s+zip|card\s+case|cardholder|valet|3-in-1)\b', re.I)
    # Straps (but not belt straps; keep belts as belt).
    _TITLE_STRAP_RE = re.compile(r'\b(strap)\b', re.I)
    # Backpack / sling pack / pack bags.
    _TITLE_BACKPACK_RE = re.compile(r'\b(backpacks?|sling\s+pack|racer\s+sling|pack\s+bag)\b', re.I)
    # Coat/jacket title disambiguation: trust the full phrase when present.
    _TITLE_COAT_RE = re.compile(r'\b(trench\s+coat|overcoat|duffle\s+coat|topcoat|peacoat)\b', re.I)
    _TITLE_JACKET_RE = re.compile(r'\b(cardigan\s+jacket|shearling\s+jacket|moto\s+jacket|leather\s+jacket|varsity\s+jacket|bomber\s+jacket|puffer\s+jacket|blazer\s+jacket|denim\s+jacket|biker\s+jacket|suede\s+jacket|surplus\s+jacket)\b', re.I)

    if _TITLE_FRAGRANCE_RE.search(title_lower):
        broad, sub_slug = "accessories", "fragrance"
    elif _TITLE_SUNGLASSES_RE.search(title_lower):
        broad, sub_slug = "accessories", "sunglasses"
    elif _TITLE_JEWELRY_RE.search(title_lower):
        broad, sub_slug = "accessories", "jewelry"
    elif _TITLE_BELT_RE.search(title_lower):
        broad, sub_slug = "accessories", "belt"
    elif _TITLE_GLOVES_RE.search(title_lower):
        broad, sub_slug = "accessories", "gloves"
    elif _TITLE_HAT_RE.search(title_lower):
        broad, sub_slug = "accessories", "hat"
    elif _TITLE_SOCKS_RE.search(title_lower):
        broad, sub_slug = "apparel", "socks"
    elif _TITLE_WALLET_RE.search(title_lower) and not _TITLE_APPAREL_RE.search(title_lower):
        # Wallet/card-case family, unless the title is actually apparel (e.g. "wallet tee").
        broad, sub_slug = "accessories", "wallet"
    elif _TITLE_STRAP_RE.search(title_lower) and not re.search(r'\bbelts?\b', title_lower, re.I):
        broad, sub_slug = "accessories", "strap"
    elif _TITLE_BACKPACK_RE.search(title_lower):
        broad, sub_slug = "accessories", "backpack"
    elif _TITLE_COAT_RE.search(title_lower):
        broad, sub_slug = "apparel", "coat"
    elif _TITLE_JACKET_RE.search(title_lower):
        broad, sub_slug = "apparel", "jacket"
    elif re.search(r'\bcamera\s+bag\b', title_lower, re.I):
        broad, sub_slug = "accessories", "bag"
    elif re.search(r'\b(sleeve|cup\s+sleeve|laptop\s+sleeve|glasses\s+case|id\s+case|skinny\s+id|card\s+case|cardholder|tech\s+kit|travel\s+kit|cosmetic\s+pouch|toiletry|passport\s+case|umbrella|pill\s+box|flask)\b', title_lower, re.I):
        # Coach accessories that are frequently filed under "Bags" by the API
        broad, sub_slug = "accessories", "small-leather-goods"
    elif _TITLE_FOOTWEAR_RE.search(title_lower):
        m = _TITLE_FOOTWEAR_RE.search(title_lower)
        kw = m.group(1).lower()
        _fw_slug = {
            "bootie": "boot", "sneaker": "sneaker", "trainer": "sneaker",
            "loafer": "loafer", "moccasin": "loafer", "sandal": "sandal",
            "slide": "slide", "pump": "pump", "stiletto": "pump",
            "kitten heel": "pump", "ballet flat": "flat", "mule": "mule",
            "espadrille": "sandal",
        }.get(kw, kw)
        broad, sub_slug = "footwear", _fw_slug
    elif _TITLE_SCARF_RE.search(title_lower):
        # "scarf" already handled in _TITLE_TYPE_MAP; catch stole/shawl here
        broad, sub_slug = "accessories", "scarf"
    elif sub_slug in ("bag", "handbag", "shoulder-bag", "tote", "crossbody-bag", "mini-bag",
                      "bucket-bag", "satchel", "hobo-bag", "belt-bag", "wristlet", "clutch",
                      "backpack") and _TITLE_APPAREL_RE.search(title_lower):
        # Breadcrumb says "bag" but title clearly names an apparel item
        m = _TITLE_APPAREL_RE.search(title_lower)
        kw = m.group(1).lower()
        _ap_slug = {
            "t-shirt":"top","tee":"top","hoodie":"top","sweatshirt":"top","crewneck":"top",
            "pullover":"top","sweater":"top","knitwear":"top","blouse":"top","camisole":"top",
            "jersey":"top","shirt":"shirt","polo":"shirt","oxford":"shirt","blazer":"jacket",
            "bomber":"jacket","blouson":"jacket","puffer":"jacket","parka":"jacket",
            "anorak":"jacket","windbreaker":"jacket","varsity":"jacket","moto jacket":"jacket",
            "leather racer":"jacket","racer":"jacket","coat":"coat","overcoat":"coat",
            "duster":"coat","trouser":"pant","jean":"pant","chino":"pant","legging":"pant",
            "jogger":"pant","dress":"dress","gown":"dress","midi dress":"dress",
            "maxi dress":"dress","skirt":"apparel","cardigan":"top","swimwear":"apparel",
            "swim trunk":"apparel","jumpsuit":"apparel","tracksuit":"apparel","romper":"apparel",
        }.get(kw, "apparel")
        broad, sub_slug = "apparel", _ap_slug

    # ── Charm/keyring safety net ───────────────────────────────────────────────
    # Coach's API sometimes places shearling charms or keyrings under "Jackets"
    # or "Outerwear" breadcrumbs. If we flagged _is_charm AND the resolved
    # sub_slug is an apparel type, reset it to "charm" / accessories.
    _APPAREL_SLUGS = {
        "jacket", "coat", "top", "shirt", "polo", "pant", "dress",
        "shorts", "apparel", "sweater", "hoodie", "tee", "skirt",
    }
    if _is_charm and (broad == "apparel" or sub_slug in _APPAREL_SLUGS):
        broad = "accessories"
        # Keep a specific slug if breadcrumb gave us one, else use "charm"
        if sub_slug in _APPAREL_SLUGS or not sub_slug:
            sub_slug = "charm"

    return broad, sub_slug


def _verify_coach_tags(tag_set, gender, broad_cat, sub_cat_slug, title, raw_tags=""):
    """
    Verification layer for Coach tag sets.

    Checks performed:
      1. Gender signal cross-check — title + raw_tags keywords used to detect
         contradictions (e.g. title says "women" but gender is "MEN").
      2. Gender tag consistency — "men"/"women"/"unisex" tags must match the
         resolved gender exactly; stray tags are removed.
      3. Taxonomy completeness:
         - UNISEX → BOTH mens-*/men-* AND womens-*/women-* taxonomy tags required.
         - WOMEN  → only womens-*/women-* taxonomy; mens-*/men-* tags stripped.
         - MEN    → only mens-*/men-* taxonomy; womens-*/women-* tags stripped.
      4. Broad category consistency — the display_broad tag must match what the
         sub_cat_slug implies; stale values from earlier logic are corrected.

    Returns the corrected tag_set.
    """
    import re as _re

    g = (gender or "UNISEX").upper()
    combined = f"{(title or '').lower()} {(raw_tags or '').lower()}"

    # ── 1. Gender signal cross-check ──────────────────────────────────────────
    # Use strong lexical markers only — broad terms ("men", "man") excluded to
    # avoid false positives on product names like "mane" or "omen".
    _W_STRONG = r'\b(womens|womenswear|ladies|ladieswear)\b'
    _M_STRONG = r'\b(menswear|gents)\b'

    has_strong_women = bool(_re.search(_W_STRONG, combined))
    has_strong_men   = bool(_re.search(_M_STRONG, combined))

    if has_strong_women and not has_strong_men and g == "MEN":
        g = "WOMEN"
    elif has_strong_men and not has_strong_women and g == "WOMEN":
        g = "MEN"

    # ── 2. Gender tag consistency ─────────────────────────────────────────────
    for _gt in ("men", "women", "unisex"):
        tag_set.discard(_gt)

    if g == "UNISEX":
        # Single neutral gender tag; no mixed mens+womens taxonomy tags.
        tag_set.add("unisex")
    else:
        tag_set.add(g.lower())

    # ── 3. Taxonomy completeness ──────────────────────────────────────────────
    def _has_mens_tax():
        return any(t.lower().startswith(("mens-", "men-")) for t in tag_set)

    def _has_womens_tax():
        return any(t.lower().startswith(("womens-", "women-")) for t in tag_set)

    if g == "UNISEX":
        # Use the UNISEX taxonomy (one neutral/women's-base set), never mix mens and womens.
        if not _has_womens_tax():
            w_main, w_sub = map_to_store_tags("UNISEX", broad_cat, sub_cat_slug)
            for t in (w_main, w_sub):
                if t: tag_set.add(t)

    elif g == "WOMEN":
        # Strip any stray mens taxonomy tags.
        for _t in list(tag_set):
            if _t.lower().startswith(("mens-", "men-")):
                tag_set.discard(_t)
        # Ensure womens taxonomy is present.
        if not _has_womens_tax():
            w_main, w_sub = map_to_store_tags("WOMEN", broad_cat, sub_cat_slug)
            for t in (w_main, w_sub):
                if t: tag_set.add(t)

    elif g == "MEN":
        # Strip any stray womens taxonomy tags.
        for _t in list(tag_set):
            if _t.lower().startswith(("womens-", "women-")):
                tag_set.discard(_t)
        # Ensure mens taxonomy is present.
        if not _has_mens_tax():
            m_main, m_sub = map_to_store_tags("MEN", broad_cat, sub_cat_slug)
            for t in (m_main, m_sub):
                if t: tag_set.add(t)

    # ── 4. Broad category consistency ─────────────────────────────────────────
    # broad_cat is the primary signal; sub_cat_slug is only a fallback.
    _BAG_SUBS_V = {
        "shoulder-bag","tote","crossbody-bag","handbag","bucket-bag","satchel",
        "hobo-bag","mini-bag","backpack","belt-bag","wristlet","clutch","bag",
    }
    _WATCH_SUBS_V = {"watch", "chronograph"}
    _FOOT_SUBS_V  = {
        "sneaker","loafer","flat","sandal","slide","pump","boot","shoe","trainer","mule",
    }

    if broad_cat == "apparel":
        expected_broad = "apparel"
    elif broad_cat == "footwear":
        expected_broad = "footwear"
    elif broad_cat == "watches" or "watch" in (title or "").lower() or "chronograph" in (title or "").lower():
        expected_broad = "watches"
    elif broad_cat == "bags" or sub_cat_slug in _BAG_SUBS_V:
        expected_broad = "bags"
    else:
        expected_broad = "accessories"

    # Remove wrong broad-category tags; add the correct one.
    for _b in ("bags", "footwear", "watches", "accessories", "apparel"):
        if _b != expected_broad:
            tag_set.discard(_b)
    tag_set.add(expected_broad)

    tag_set.discard("")
    tag_set.discard(None)
    return tag_set


def _build_coach_tags(gender, broad_cat, sub_cat_slug, title, vendor="Coach", raw_tags=""):
    """
    Build Cruise Fashion-compatible Shopify tags for a Coach product.

    Format mirrors Cruise:
      source, broad-category, gender(s), gender-category-taxonomy, [sub-brand], product-type

    Example:
      bags, coach, tote, women, womens-handbags, womens-totebags
      accessories, coach, men, mens-accessories, mens-wallets, wallet
      coach, coachtopia, footwear, sandal, women, womens-footwear, womens-flats
    """
    import re as _re

    tag_set = set()

    # 1. Source/brand — always "coach"; add sub-brand when applicable
    tag_set.add("coach")
    vendor_clean = _re.sub(r'[®™©\s]+', ' ', (vendor or "Coach")).strip().lower()
    if "coachtopia" in vendor_clean or "coachtopia" in (title or "").lower():
        tag_set.add("coachtopia")

    # 2. Broad display category — trust the resolved broad category first.
    # Coach's API often leaves sub_cat_slug as a breadcrumb value (e.g. "shoulder-bag")
    # even when the title proves the product is apparel/footwear/accessories. Using
    # broad_cat as the primary signal prevents "bags" tags on jackets or t-shirts.
    _BAG_SUBCATS = {
        "shoulder-bag", "tote", "crossbody-bag", "handbag", "bucket-bag",
        "satchel", "hobo-bag", "mini-bag", "backpack", "belt-bag",
        "wristlet", "clutch", "bag",
    }
    _WATCH_SUBCATS = {"watch", "chronograph"}
    _FOOTWEAR_SUBCATS = {
        "sneaker", "loafer", "flat", "sandal", "slide", "pump",
        "boot", "shoe", "trainer", "mule",
    }

    title_lower = (title or "").lower()

    if broad_cat == "apparel":
        display_broad = "apparel"
    elif broad_cat == "footwear":
        display_broad = "footwear"
    elif broad_cat == "watches" or "watch" in title_lower or "chronograph" in title_lower:
        display_broad = "watches"
    elif broad_cat == "bags" or sub_cat_slug in _BAG_SUBCATS:
        display_broad = "bags"
    else:
        display_broad = "accessories"

    tag_set.add(display_broad)

    # 2b. Refine generic footwear sub-types using title keywords
    if sub_cat_slug in ("shoe", "sneaker") and broad_cat == "footwear":
        if _re.search(r'\bsandal\b', title_lower):           sub_cat_slug = "sandal"
        elif _re.search(r'\b(loafer|moccasin)\b', title_lower): sub_cat_slug = "loafer"
        elif _re.search(r'\b(boot|bootie)\b', title_lower):  sub_cat_slug = "boot"
        elif _re.search(r'\b(pump|stiletto)\b', title_lower): sub_cat_slug = "pump"
        elif _re.search(r'\bmule\b', title_lower):           sub_cat_slug = "mule"
        elif _re.search(r'\bslide\b', title_lower):          sub_cat_slug = "slide"
        elif _re.search(r'\bflat\b', title_lower):           sub_cat_slug = "flat"
        # Recalculate display_broad in case sub changed
        display_broad = "footwear"

    # 3. Human-readable product sub-type (e.g. "tote", "sandal", "wallet")
    # Only add the sub-type when it belongs to the resolved broad category.  Coach's API
    # frequently leaves sub_cat_slug as a breadcrumb value (e.g. "shoulder-bag") even for
    # apparel or accessories, so adding it blindly would tag jackets as "handbag".
    _BAG_SUBCATS_READABLE = {s.replace("-", " ") for s in _BAG_SUBCATS}
    _APPAREL_SUBCATS_READABLE = {
        "top", "shirt", "polo", "t-shirt", "t shirt", "hoodie", "sweatshirt", "crewneck",
        "sweater", "pullover", "blouse", "camisole", "jersey", "tank", "knit top", "bib shirt",
        "cardigan", "tunic", "vest", "jacket", "coat", "overcoat", "duster", "dress", "gown",
        "midi dress", "maxi dress", "skirt", "pant", "trouser", "jean", "chino", "legging",
        "jogger", "shorts", "track pant", "culotte", "swimwear", "swim trunk", "jumpsuit",
        "tracksuit", "romper", "pajama", "socks", "boxer", "bermuda", "apparel",
    }
    _WATCH_SUBCATS_READABLE = _WATCH_SUBCATS

    if sub_cat_slug:
        sub_readable = sub_cat_slug.replace("-", " ").lower().strip()
        if sub_readable and sub_readable not in ("coach", display_broad):
            expected_broad = None
            if sub_readable in _BAG_SUBCATS_READABLE:
                expected_broad = "bags"
            elif sub_readable in _APPAREL_SUBCATS_READABLE:
                expected_broad = "apparel"
            elif sub_readable in _FOOTWEAR_SUBCATS:
                expected_broad = "footwear"
            elif sub_readable in _WATCH_SUBCATS_READABLE:
                expected_broad = "watches"
            else:
                expected_broad = "accessories"
            if expected_broad == display_broad:
                tag_set.add(sub_readable)

    # 4. Gender tags
    g = (gender or "UNISEX").upper()
    if g == "UNISEX":
        tag_set.update(["men", "women", "unisex"])
    else:
        tag_set.add(g.lower())

    # 5. Store taxonomy (gender-prefixed collection tags)
    if g == "UNISEX":
        w_main, w_sub = map_to_store_tags("WOMEN", broad_cat, sub_cat_slug)
        m_main, m_sub = map_to_store_tags("MEN",   broad_cat, sub_cat_slug)
        for t in [w_main, w_sub, m_main, m_sub]:
            if t:
                tag_set.add(t)
    else:
        main_cat, sub_cat = map_to_store_tags(g, broad_cat, sub_cat_slug)
        if main_cat:
            tag_set.add(main_cat)
        if sub_cat:
            tag_set.add(sub_cat)

    # Scraper identity tag — required for Shopify dual-safety delete/update ops
    tag_set.add("RudraScrapper-coach")

    # Remove any empty/None
    tag_set.discard("")
    tag_set.discard(None)

    # ── Verification layer ────────────────────────────────────────────────────
    # Uses title + raw_tags signals to cross-check gender and type, then
    # ensures the final tag set is internally consistent.
    tag_set = _verify_coach_tags(
        tag_set, g, broad_cat, sub_cat_slug, title, raw_tags=raw_tags
    )

    return ", ".join(sorted(tag_set))


# ── Coach description: Mirage template ───────────────────────────────────────

_COACH_USE_CASES = {
    # Bags
    "shoulder-bag":   ("elevating everyday wardrobes, weekend outings, or polished work ensembles",
                       "those who refuse to choose between function and iconic style"),
    "tote":           ("carrying essentials to the office, weekend getaways, or city errands",
                       "those who demand a bag that moves seamlessly from desk to dinner"),
    "crossbody-bag":  ("hands-free city exploring, travel, or casual outings",
                       "those who want effortless style on the move"),
    "backpack":       ("commuting, travel, or weekend adventures",
                       "those who pair practicality with premium craftsmanship"),
    "wristlet":       ("evenings out, travel, or pared-back everyday carry",
                       "those who embrace minimalist elegance"),
    "clutch":         ("evenings out, formal occasions, or statement styling",
                       "those who believe less is always more"),
    "bag":            ("everyday carry, travel, or weekend styling",
                       "those who value timeless craftsmanship in every detail"),
    "belt-bag":       ("hands-free outings, travel, or casual styling",
                       "those who blend utility with iconic New York design"),
    "mini-bag":       ("evening outings, travel, or statement accessorising",
                       "those who believe the boldest statements come in the smallest packages"),
    "bucket-bag":     ("city days, weekend outings, or elevated casual looks",
                       "those who appreciate relaxed silhouettes with premium craftsmanship"),
    "hobo-bag":       ("relaxed weekend styling or everyday carry",
                       "those who pair effortless form with artisanal quality"),
    "satchel":        ("the office, weekend outings, or structured everyday styling",
                       "those who value considered design and premium materials"),
    # Wallets & small leather goods
    "wallet":         ("organising everyday essentials with effortless elegance",
                       "those who invest in craftsmanship even in the smallest details"),
    "card-case":      ("streamlined everyday carry or gifting",
                       "those who appreciate refined minimalism"),
    "small-leather-goods": ("gifting, everyday carry, or accessorising a signature look",
                       "those who value Coach's iconic craftsmanship in every piece"),
    # Footwear
    "sneaker":        ("pairing with casual looks, weekend styling, or city errands",
                       "those who bring the same high standards to everyday footwear"),
    "sandal":         ("warm-weather dressing, resort styling, or elevated casual looks",
                       "those who expect artisanal quality in every step"),
    "boot":           ("cooler-season styling, city looks, or statement footwear moments",
                       "those who invest in footwear that defines the season"),
    "loafer":         ("smart-casual dressing, office styling, or elevated weekend looks",
                       "those who understand that refined shoes complete any look"),
    "pump":           ("formal occasions, power dressing, or evening events",
                       "those who make a statement from the ground up"),
    "flat":           ("everyday elegance, office dressing, or relaxed weekend looks",
                       "those who refuse to sacrifice comfort for style"),
    "mule":           ("effortless styling, warm-weather dressing, or considered casual looks",
                       "those who appreciate understated elegance"),
    "slide":          ("relaxed off-duty moments, resort dressing, or casual outings",
                       "those who bring Coach's iconic quality to every step"),
    "shoe":           ("everyday styling, smart-casual dressing, or considered footwear moments",
                       "those who invest in footwear crafted with artisanal care"),
    # Accessories
    "watch":          ("completing a signature look, gifting, or everyday timekeeping",
                       "those who value precision craftsmanship and iconic design"),
    "jewelry":        ("elevating any outfit, gifting, or adding a signature finishing touch",
                       "those who celebrate considered accessorising"),
    "sunglasses":     ("sun-soaked days, travel, or completing a polished look",
                       "those who invest in eyewear that frames their signature style"),
    "belt":           ("completing tailored looks, smart-casual dressing, or structured styling",
                       "those who believe the right belt defines the whole outfit"),
    "scarf":          ("layering, travel, or adding a finishing touch to any look",
                       "those who understand that a great scarf elevates everything"),
    "hat":            ("completing casual looks, sun protection, or statement styling",
                       "those who believe in considered accessories for every season"),
    "keyring":        ("gifting, bag accessorising, or everyday carry",
                       "those who appreciate artisanal craftsmanship in every detail"),
    "charm":          ("personalising a bag, gifting, or adding a playful finishing touch",
                       "those who believe in expressing personality through considered design"),
    # Apparel
    "jacket":         ("layering for cooler weather, smart-casual dressing, or statement outerwear moments",
                       "those who invest in outerwear that transcends seasons"),
    "coat":           ("cooler-season styling, travel, or timeless outerwear dressing",
                       "those who demand craftsmanship in every layer"),
    "dress":          ("evening occasions, weekend dressing, or relaxed elevated styling",
                       "those who appreciate considered femininity in every detail"),
    "top":            ("everyday styling, casual layering, or relaxed weekend looks",
                       "those who bring the same standards to everyday wear as to occasion dressing"),
    "pant":           ("smart-casual styling, the office, or weekend looks",
                       "those who value considered tailoring for everyday life"),
    "apparel":        ("everyday styling, casual dressing, or completing a signature look",
                       "those who bring Coach's New York aesthetic to every outfit"),
    # Fragrance
    "fragrance":      ("gifting, everyday wear, or completing a signature scent wardrobe",
                       "those who consider fragrance the ultimate finishing touch"),
}

_COACH_TECH_SHOWCASE = {
    "accessories": "premium leather craftsmanship and iconic hardware",
    "bags":        "premium leather construction and considered interior organisation",
    "footwear":    "premium leather upper and artisanal construction",
    "apparel":     "premium fabric quality and considered tailoring",
    "watches":     "precision movement and premium case construction",
}

_COACH_CATEGORY_INTRO = {
    "bags":        "this bag embodies the iconic New York heritage and artisanal spirit that define Coach's legacy",
    "footwear":    "this footwear embodies the iconic New York craftsmanship and considered design that define Coach's legacy",
    "accessories": "this piece embodies the iconic New York heritage and artisanal spirit that define Coach's legacy",
    "apparel":     "this piece embodies the relaxed New York elegance and considered design that define Coach's ready-to-wear legacy",
    "watches":     "this timepiece embodies the precision craftsmanship and considered design that define Coach's accessory legacy",
}

# Collection-type labels that are NOT product types — map to actual type via title
_COACH_COLLECTION_TYPES = {
    "coach re loved", "re loved", "re loved new arrivals", "restored",
    "upcrafted", "remade", "reloved", "all products",
    "disney x coach", "coachtopia bags accessories", "coachtopia clothes",
    "hats scarves gloves", "charms straps", "straps charms and keyrings",
    "tech travel home", "tech desk travel", "tech travel", "tech accessories",
    "jewelry watches", "fragrance", "home accessories",
    # Additional collection/navigation labels
    "search enabled products", "view all", "other", "products", "exclusives",
    "bottoms", "vintage", "bag accessories keychains",
}

_COACH_TITLE_TYPE_MAP = [
    ("Bag Charm",      r'\b(bag|shoe|shoelace)\s+charm\b'),
    ("Bag Strap",      r'\b(crossbody|tote|backpack|shoulder|wristlet|clutch|messenger|duffle|pouch|mini\s+bag|belt\s+bag)\b.*?\bstrap\b'),
    ("Fragrance",      r'\b(eau\s+de|parfum|cologne|perfume|fragrance)\b'),
    ("Small Leather Goods", r'\b(cup\s+sleeve|laptop\s+sleeve|id\s+case|skinny\s+id|card\s+case|cardholder|key\s+case|passport\s+case|phone\s+case|apple\s+watch\s*[\u00ae\u2122]?\s*strap|watch\s+strap)\b'),
    ("Wallet",         r'\b(wallet|zip\s+around|accordion\s+zip)\b'),
    ("Card Case",      r'\b(card\s+case|card\s+holder|cardholder)\b'),
    ("Watch",          r'\bwatch(?:es)?\b'),
    ("Sunglasses",     r'\b(sunglasses?|eyewear|optical|lens)\b'),
    ("Scarf",          r'\b(scarves?|scarf|shawls?|stoles?|mufflers?|wraps?|snoods?)\b'),
    ("Hat",            r'\b(hats?|caps?|beanies?|berets?|bucket\s+hat|baseball\s+hat|earmuffs?|trucker\s+hat)\b'),
    ("Gloves",         r'\bgloves?\b'),
    ("Belt",           r'\bbelts?\b'),
    ("Socks",          r'\bsocks?\b'),
    ("Jewelry",        r'\b(necklaces?|bracelets?|earrings?|(?<![dD]-)rings?|bangles?|pendants?|lockets?|brooches?|anklets?|cufflinks?|studs?|hoops?|charm\s+pendant)\b'),
    ("Home Accessory", r'\b(coaster|candle|notebook|diary|organiser|organizer|comb|dice|game\s+set|tic\s+tac|pill\s+box|flask|pencil\s+case|tray|pouf)\b'),

    # ── Specific bag types (unambiguous before generic "charm" / "strap") ─────
    ("Backpack",       r'\b(backpacks?|packs?(?:\s+bag)?|sling\s+pack|racer\s+sling)\b'),
    ("Tote",           r'\btotes?\b'),
    ("Crossbody Bag",  r'\bcrossbody\b'),
    ("Wristlet",       r'\bwristlets?\b'),
    ("Clutch",         r'\bclutch\b'),
    ("Mini Bag",       r'\bmini\s+(bag|crossbody)\b'),
    ("Belt Bag",       r'\bbelt\s+bag\b'),
    ("Messenger Bag",  r'\b(messenger|brief(?:case)?)\b'),
    ("Duffle Bag",     r'\b(duffle|duffel)\b'),
    ("Pouch",          r'\bpouch(?:es)?\b'),

    # ── Footwear ─────────────────────────────────────────────────────────────
    ("Boot",           r'\b(booties?|boots?|ankle\s+boot|hikers?)\b'),
    ("Sneaker",        r'\b(sneakers?|trainers?|court\s+shoe)\b'),
    ("Sandal",         r'\b(sandals?|flip\s+flops?)\b'),
    ("Loafer",         r'\b(loafers?|moccasins?)\b'),
    ("Pump",           r'\b(pumps?|stilettos?|kitten\s+heel|wedge)\b'),
    ("Flat",           r'\b(ballet\s+flat|flats?(?:\s+shoe)?)\b'),
    ("Mule",           r'\bmules?\b'),
    ("Slide",          r'\bslides?\b'),
    ("Clog",           r'\bclogs?\b'),
    ("Mary Jane",      r'\bmary\s+janes?\b'),
    ("Derby",          r'\b(derby|derbie)\b'),
    ("Oxford",         r'\boxfords?\b'),
    ("Flip Flop",      r'\bflip\s+flops?\b'),

    # ── Apparel (cleaned keywords, no ambiguous collection words) ───────────
    ("Jacket",         r'\b(jackets?|blazers?|bombers?|puffers?|parkas?|anorak|windbreaker|outerwear|capes?|blousons?|fleece)\b'),
    ("Coat",           r'\b(coats?|overcoat|topcoat|peacoat|duffle\s+coat|duffel\s+coat|trench\s+coat)\b'),
    ("Skirt",          r'\bskirts?\b'),
    ("Dress",          r'\b(dress(?:es)?|gown|romper|jumpsuit)\b'),
    ("Top",            r'\b(t-shirt|tees?(?:\s|$)|hoodie|sweatshirt|crewneck|sweaters?|pullover|blouse|camisole|tank|polo(?:\s+shirt)?|knit\s+top|bib\s+shirt|cardigan|shirts?|tunics?|challis|skimmer)\b'),
    ("Pant",           r'\b(pants?|trousers?|jeans?|chinos?|leggings?|joggers?|shorts|track\s+pant|culottes?|sweatpants?)\b'),
    ("Pajama",         r'\bpajama\b'),
    ("Swimwear",       r'\b(bikinis?|swimsuits?|swimwear)\b'),

    # ── Bag straps & generic charms (after specific types) ───────────────────
    ("Bag Strap",      r'\bstrap\b'),
    ("Bag Charm",      r'\b(charms?|collectible)\b'),

    # ── Generic bags (last) ──────────────────────────────────────────────────
    ("Shoulder Bag",   r'\b(shoulder\s+bag|hobo|satchel|bucket\s+bag|top\s+handle(?:\s+bag)?|carryall|convertible\s+bag|parker\b|tabby\b)\b'),
    ("Bag",            r'\bbags?\b'),
    ("Shoulder Bag",   r'\b(saddle|swinger|lana|rogue|dreamer|kat|cargo|beat|ergo|courier|penn|morgan|shoulder|town|quincy|hippie|equestrian|sutton|studio\s+flap|mollie)\b'),
    ("Bag",            r'\b(metropolitan|portfolio|archive|vintage\s+legacy)\b'),

    # ── Catch-alls ───────────────────────────────────────────────────────────
    ("Pant",           r'\b(boxers?|bermuda|culotte|legging)\b'),
    ("Accessories",    r'\b(pack|kit|set|case|box|boxed)\b'),
]


# Canonical (broad_cat, sub_cat_slug) for every resolved Coach product type.
# This ensures tags and descriptions are generated from the *real* product type
# rather than from a breadcrumb value that Coach's API left behind.
_COACH_TYPE_TO_CAT = {
    "Sunglasses":       ("accessories", "sunglasses"),
    "Wallet":           ("accessories", "wallet"),
    "Card Case":        ("accessories", "card-case"),
    "Backpack":         ("accessories", "backpack"),
    "Bag Strap":        ("accessories", "strap"),
    "Belt":             ("accessories", "belt"),
    "Gloves":           ("accessories", "gloves"),
    "Hat":              ("accessories", "hat"),
    "Scarf":            ("accessories", "scarf"),
    "Jewelry":          ("accessories", "jewelry"),
    "Bag Charm":        ("accessories", "jewelry"),
    "Watch":            ("accessories", "watch"),
    "Fragrance":        ("accessories", "fragrance"),
    "Keyring":          ("accessories", "keyring"),
    "Phone Case":       ("accessories", "small-leather-goods"),
    "Luggage Tag":      ("accessories", "small-leather-goods"),
    "Travel Kit":       ("accessories", "small-leather-goods"),
    "Passport Case":    ("accessories", "small-leather-goods"),
    "Umbrella":         ("accessories", "small-leather-goods"),
    "Jewelry Box":      ("accessories", "small-leather-goods"),
    "Home Accessory":   ("accessories", "home-accessory"),
    "Small Leather Goods": ("accessories", "small-leather-goods"),
    "Accessories":      ("accessories", "accessories"),
    "Jacket":           ("apparel", "jacket"),
    "Coat":             ("apparel", "coat"),
    "Top":              ("apparel", "top"),
    "Dress":            ("apparel", "dress"),
    "Skirt":            ("apparel", "skirt"),
    "Pant":             ("apparel", "pant"),
    "Shorts":           ("apparel", "shorts"),
    "Pajama":           ("apparel", "apparel"),
    "Socks":            ("apparel", "socks"),
    "Swimwear":         ("apparel", "swimwear"),
    "Sneaker":          ("footwear", "sneaker"),
    "Boot":             ("footwear", "boot"),
    "Sandal":           ("footwear", "sandal"),
    "Loafer":           ("footwear", "loafer"),
    "Flat":             ("footwear", "flat"),
    "Pump":             ("footwear", "pump"),
    "Mule":             ("footwear", "mule"),
    "Slide":            ("footwear", "slide"),
    "Clog":             ("footwear", "clog"),
    "Mary Jane":        ("footwear", "mary-jane"),
    "Derby":            ("footwear", "derby"),
    "Oxford":           ("footwear", "oxford"),
    "Flip Flop":        ("footwear", "flip-flop"),
    "Slipper":          ("footwear", "slipper"),
    "Bag":              ("bags", "bag"),
    "Handbag":          ("bags", "handbag"),
    "Shoulder Bag":     ("bags", "shoulder-bag"),
    "Tote":             ("bags", "tote"),
    "Crossbody Bag":    ("bags", "crossbody-bag"),
    "Mini Bag":         ("bags", "mini-bag"),
    "Belt Bag":         ("bags", "belt-bag"),
    "Wristlet":         ("bags", "wristlet"),
    "Clutch":           ("bags", "clutch"),
    "Pouch":            ("bags", "pouch"),
    "Messenger Bag":    ("bags", "messenger-bag"),
    "Duffle Bag":       ("bags", "duffle-bag"),
    "Bucket Bag":       ("bags", "bucket-bag"),
    "Hobo Bag":         ("bags", "hobo-bag"),
    "Satchel":          ("bags", "satchel"),
}


def _resolve_coach_type(product_type: str, title: str) -> str:
    """
    Resolve the real product type from the product title.

    Coach's API often files items under breadcrumb-derived collection types
    (e.g. "Shoulder Bag") even when the title clearly identifies a wallet,
    sunglasses, backpack, strap, etc. Always trust the title first, then
    fall back to the API-provided type only when the title is ambiguous.
    """
    tl = (title or "").lower()
    for ptype, pattern in _COACH_TITLE_TYPE_MAP:
        if re.search(pattern, tl, re.I):
            return ptype
    # Generic fallback only when the title is genuinely bag-like
    if "bag" in tl:
        return "Bag"
    return product_type


def _build_coach_description(raw_desc, title, broad_cat, sub_cat_slug, gender="WOMEN"):
    """
    Build a full Mirage-template HTML description for a Coach product.
    Structure:
      1. Opening hook paragraph
      2. <p><strong>Key Features & Characteristics:</strong></p>
      3. <ul><li> feature bullets (from raw spec lines, Style No. stripped)
      4. Closing heritage paragraph
      5. append_brand_message tag
    """
    gender_lc = gender.lower() if gender and gender not in ("UNISEX", "unisex") else ""
    type_label = (sub_cat_slug or broad_cat or "product").replace("-", " ")
    title_clean = (title or "Coach Product").strip()
    # Strip leading "Coach " prefix — it's added explicitly in the opening
    if title_clean.lower().startswith("coach "):
        title_clean = title_clean[6:].strip()

    # ── Opening hook ─────────────────────────────────────────────────────────
    cat_intro = _COACH_CATEGORY_INTRO.get(
        broad_cat,
        "this piece embodies the iconic New York heritage and artisanal spirit that define Coach's legacy"
    )
    # Avoid repeating type_label when the title already contains the same word
    _STRIP_PUNCT = str.maketrans('', '', '.,;:!?"\'-')
    title_words_lower = [w.translate(_STRIP_PUNCT).rstrip('s') for w in title_clean.lower().split()]
    type_last_word = type_label.split()[-1].lower().rstrip('s')
    title_already_has_type = type_last_word in title_words_lower
    # Only show gender when there is also a type suffix (avoids dangling "women's,")
    if title_already_has_type:
        gender_str = ""
        type_suffix = ""
    else:
        gender_str = f" {gender_lc}'s" if gender_lc else ""
        type_suffix = f" {type_label}"
    opening = (
        f"<p>Discover the iconic Coach {title_clean}{gender_str}{type_suffix}, exclusively "
        f"curated by The Mirage. A masterpiece of artisanal craftsmanship, {cat_intro}.</p>"
    )

    # ── Feature bullets ───────────────────────────────────────────────────────
    _STYLE_NO_RE = re.compile(r'\s*Style\s+No\.?\s+\S+', re.I)
    bullets_html = ""
    if raw_desc and raw_desc.strip():
        text = re.sub(r'<[^>]+>', ' ', raw_desc)
        text = re.sub(r'\r\n|\r', '\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        lines = [re.sub(r'^[*\-\u2022]\s*', '', l).strip() for l in text.split('\n') if l.strip()]
        lines = [_STYLE_NO_RE.sub('', l).strip() for l in lines]
        lines = [l for l in lines if l and len(l) > 3]
        if lines:
            bullets_html = (
                "<p><strong>Key Features &amp; Characteristics:</strong></p>"
                "<ul>" + "".join(f"<li>{l}</li>" for l in lines) + "</ul>"
            )
    if not bullets_html:
        type_title = type_label.title()
        bullets_html = (
            "<p><strong>Key Features &amp; Characteristics:</strong></p>"
            f"<ul><li>Expertly crafted {type_title} from Coach's premium material selection</li>"
            f"<li>Considered interior design for everyday practicality</li>"
            f"<li>Coach's iconic hardware and finishing details</li>"
            f"<li>Coach's signature craftsmanship throughout</li></ul>"
        )

    # ── Closing paragraph ─────────────────────────────────────────────────────
    use_case, audience = _COACH_USE_CASES.get(
        sub_cat_slug,
        ("elevating everyday looks or special occasions",
         "those who appreciate considered craftsmanship and iconic New York design")
    )
    tech_phrase = _COACH_TECH_SHOWCASE.get(broad_cat, "premium leather craftsmanship and iconic hardware")
    closing = (
        f"<p>Crafted with the precision expected from Coach's New York heritage, the {title_clean} "
        f"represents more than a {type_label} — it's a statement in considered style. "
        f"The {tech_phrase} showcase Coach's commitment to artisanal excellence without compromise. "
        f"Whether {use_case}, this {type_label} delivers the quality and character that {audience} demand. "
        f"At The Mirage, we celebrate pieces that transcend trends. This Coach creation is a testament "
        f"to purposeful design and uncompromising quality, making it an essential addition to any "
        f"discerning wardrobe.</p>"
    )

    body = opening + bullets_html + closing
    return append_brand_message(body)


# ── Main cleaning worker ───────────────────────────────────────────────────────

def _clean_single_coach_item(item):
    """Worker for parallel Coach cleaning"""
    try:
        data = item.get("data", item)
        product_id = str(item.get("id", "unknown")).strip()
        url = item.get("url") or f"{BASE_URL}/p/{product_id}"

        # Extract the inner product object if nested
        p = data.get("product", data)
        if not p: return None

        # ── 1. Title ──────────────────────────────────────────────────────────
        title = str(p.get("name") or p.get("pageTitle") or "").strip()
        if not title: return None
        cleaned_title = clean_title(title)

        # ── 2. Breadcrumbs / Category ─────────────────────────────────────────
        cat_raw_arr = p.get("item_category") or []
        # Enrich with English category + primary category strings for detection
        extra_cats = []
        if p.get("item_category_english"):
            extra_cats += [str(p["item_category_english"])]
        if p.get("primaryCategory"):
            extra_cats += [str(p["primaryCategory"])]
        # Simplified scraper format stores category as a plain string
        if not cat_raw_arr and not extra_cats and p.get("category"):
            cat_val = p["category"]
            if isinstance(cat_val, str):
                extra_cats += [cat_val]
            elif isinstance(cat_val, list):
                extra_cats += [str(c) for c in cat_val]
        full_cat_arr = list(cat_raw_arr) + extra_cats

        url_gender_hint = item.get("url_gender_hint")

        # ── 3. Raw description (fetched early — used by gender verification) ──
        raw_desc = str(
            p.get("longDescription") or p.get("shortDescription") or p.get("description") or ""
        ).strip()

        # ── 2b. Category extraction ───────────────────────────────────────────
        broad_cat, sub_cat_slug = _extract_coach_categories(full_cat_arr, title)

        # ── 2c. Gender — verified against title, description, raw API fields ──
        # Priority: explicit title words > product-type keywords > raw API gender field
        #           > breadcrumb[0] > URL hint > URL path > description keywords
        gender = _verify_product_gender(
            title=title,
            description=raw_desc,
            cat_raw_arr=full_cat_arr,
            url_gender_hint=url_gender_hint,
            url=url,
            raw_product=p,
        )

        # ── 2d. Resolve the real product type from the title, then normalize
        # broad_cat / sub_cat_slug so tags and descriptions never inherit a wrong
        # breadcrumb value (e.g. a jacket filed under "Shoulder Bags").
        _title_l = cleaned_title.lower()
        _raw_slug = sub_cat_slug or broad_cat
        if _raw_slug in ("shoe", "sneaker") and broad_cat == "footwear":
            for _kw, _ts in [("sandal","Sandal"),("loafer","Loafer"),("moccasin","Loafer"),
                             ("boot","Boot"),("bootie","Boot"),("pump","Pump"),("mule","Mule"),
                             ("slide","Slide"),("flat","Flat"),("ballet flat","Ballet Flat"),
                             ("slingback","Slingback"),("mary jane","Mary Jane"),
                             ("flip flop","Flip Flop"),("clog","Clog"),("slipper","Slipper")]:
                if re.search(r'\b' + re.escape(_kw) + r'\b', _title_l):
                    _raw_slug = _ts.lower().replace(" ", "-")
                    break
        product_type = _raw_slug.replace("-", " ").title() if _raw_slug else broad_cat.title()
        product_type = _resolve_coach_type(product_type, cleaned_title)
        norm_broad, norm_sub = _COACH_TYPE_TO_CAT.get(product_type, (broad_cat, sub_cat_slug))
        broad_cat, sub_cat_slug = norm_broad, norm_sub

        # ── 4. HTML description ───────────────────────────────────────────────
        body_html = _build_coach_description(raw_desc, title, broad_cat, sub_cat_slug, gender=gender)

        # ── 5. Handle ────────────────────────────────────────────────────────
        v_style = str(p.get("masterId", "")).strip() or product_id.split('-')[0].split(' ')[0]
        handle = generate_handle(cleaned_title, v_style, product_id)

        # ── 5. Variants & Images ──────────────────────────────────────────────
        image_urls = []
        all_image_groups = list(p.get("imageGroups", []))
        if isinstance(p.get("selectedVariantGroupData"), dict):
            all_image_groups += p["selectedVariantGroupData"].get("imageGroups", [])

        for grp in all_image_groups:
            for img in grp.get("images", []):
                src = img.get("src") or img.get("url")
                if src and src not in image_urls:
                    image_urls.append(src)
        image_urls = list(dict.fromkeys(image_urls))

        # Fallback: simplified scraper format stores images as a plain list
        if not image_urls:
            for img in (p.get("images") or []):
                src = img if isinstance(img, str) else (img.get("src") or img.get("url") or "")
                if src and src not in image_urls:
                    image_urls.append(src)

        # Coach SFCC uses "prices" (plural) for the window-object strategy and
        # "price" (singular, nested as {"value": ...}) for JSON-LD / BS4 fallbacks.
        # The "or" chain tries every known path before defaulting to 0.
        _p_prices = p.get("prices") or {}
        _p_price  = p.get("price")  or {}
        price_raw = (
            _p_prices.get("currentPrice")                    # modern Coach API (currentPrice: 149)
            or _p_prices.get("sales", {}).get("value")       # SFCC window obj
            or _p_prices.get("list",  {}).get("value")       # SFCC list price
            or (_p_price.get("sales", {}).get("value") if isinstance(_p_price, dict) else None)
            or (_p_price.get("list",  {}).get("value") if isinstance(_p_price, dict) else None)
            or (_p_price.get("value")                  if isinstance(_p_price, dict) else None)
            or (float(_p_price) if isinstance(_p_price, (int, float)) and _p_price > 0 else None)
            or 0.0
        )

        final_variants = []

        # ── Helper: flatten a SFCC variationAttributes list into the internal
        # variantGroup format so both code-paths share the same emit logic.
        def _sfcc_va_to_group(va_list, attr_key):
            """Return a synthetic variantGroup dict for attr_key ('size' or 'color')."""
            for va in (va_list or []):
                aid = (va.get("id") or "").lower()
                aname = (va.get("name") or "").lower()
                if attr_key in aid or attr_key in aname:
                    raw_vals = va.get("values") or va.get("variationAttributes") or []
                    return {
                        "name": attr_key,
                        "variationAttributes": [
                            {
                                "id":           str(vv.get("value", vv.get("id", ""))),
                                "displayValue": (vv.get("displayValue")
                                                 or vv.get("displayName")
                                                 or vv.get("value", "")).strip(),
                            }
                            for vv in raw_vals
                            if vv.get("value") or vv.get("displayValue")
                        ],
                    }
            return None

        # ── Attempt 1: custom variantGroups (Coach India JS-object strategy) ──
        vgs = p.get("variantGroups") or []
        size_attr  = next((vg for vg in vgs if (vg.get("name") or "").lower() == "size"),  None)
        color_attr = next((vg for vg in vgs if (vg.get("name") or "").lower() == "color"), None)

        # ── Attempt 2: standard SFCC variationAttributes at product level ──────
        if not size_attr and not color_attr:
            sfcc_attrs = p.get("variationAttributes") or []
            size_attr  = _sfcc_va_to_group(sfcc_attrs, "size")
            color_attr = _sfcc_va_to_group(sfcc_attrs, "color")

        # ── Attempt 3: Coach DW search-result variant map  ────────────────────
        # SFCC search results may expose variants as p["variants"][id]["attributes"]
        if not size_attr and not color_attr:
            dw_variants = p.get("variants") or {}
            if isinstance(dw_variants, dict):
                _size_vals, _color_vals = [], []
                for vid, vdata in dw_variants.items():
                    if isinstance(vdata, dict):
                        for av in (vdata.get("variationValues") or {}).items():
                            aname_l = av[0].lower()
                            if "size" in aname_l:
                                entry = {"id": str(av[1]), "displayValue": str(av[1])}
                                if entry not in _size_vals:
                                    _size_vals.append(entry)
                            elif "color" in aname_l or "colour" in aname_l:
                                entry = {"id": str(av[1]), "displayValue": str(av[1])}
                                if entry not in _color_vals:
                                    _color_vals.append(entry)
                if _size_vals:
                    size_attr = {"name": "size", "variationAttributes": _size_vals}
                if _color_vals:
                    color_attr = {"name": "color", "variationAttributes": _color_vals}

        # Coach.com serves prices in USD for most items, but geolocation/India
        # pages sometimes return INR values (e.g. ₹225,450).  We detect the
        # currency from the raw price magnitude combined with the product type,
        # because a $500 wallet is plausible USD while a $15,000 pouch is not.
        def _detect_coach_currency(price_value, ptype: str, ptitle: str) -> str:
            try:
                val = float(price_value)
            except (TypeError, ValueError):
                return "USD"
            # Definite INR: raw values above any plausible USD Coach price.
            if val >= 100000:
                return "INR"
            # Exotic / luxury leather items can legitimately exceed normal thresholds in USD.
            _title_l = ptitle.lower()
            if any(k in _title_l for k in ("alligator", "crocodile", "python", "ostrich")):
                return "INR" if val >= 25000 else "USD"
            # Type-aware USD ceiling before we treat the price as INR.
            _ptype_l = ptype.lower()
            _small_accessory_types = {
                "wallet", "wristlet", "pouch", "card case", "small leather goods",
                "charm", "bag charm", "shoelace charm", "key fob", "key ring",
                "belt", "sunglasses", "scarf", "stole", "shawl", "hat", "gloves",
                "lanyard", "fragrance", "bag strap", "home accessory", "accessories",
                "coin purse", "leather goods",
            }
            _footwear_apparel_types = {
                "sneaker", "loafer", "flat", "sandal", "slide", "pump", "boot",
                "shoe", "mule", "clog", "mary jane", "derby", "flip flop", "slingback",
                "slipper", "top", "dress", "leggings", "jacket", "coat", "pant",
                "shorts", "polo", "shirt", "skirt", "swimwear", "apparel", "hoodie",
                "sweater", "tee", "t-shirt", "varsity jacket", "trucker jacket",
                "fleece jacket", "racer jacket", "camp shorts", "crewneck", "tank",
            }
            if _ptype_l in _small_accessory_types:
                threshold = 500
            elif _ptype_l in _footwear_apparel_types:
                threshold = 1000
            elif _ptype_l in {"jewelry", "watch"}:
                threshold = 5000 if _ptype_l == "jewelry" else 10000
            else:
                # Bags and any unmapped type: allow luxury USD up to $10k.
                threshold = 10000
            return "INR" if val > threshold else "USD"

        _variant_currency = _detect_coach_currency(price_raw, product_type, cleaned_title)

        # ── Emit variants ─────────────────────────────────────────────────────
        def _size_display(raw_val):
            if broad_cat == "footwear":
                uk = us_size_to_uk_tag(raw_val, gender)
                return uk if uk else raw_val
            return raw_val

        if size_attr and color_attr:
            # Cross-product Color × Size (e.g. footwear with multiple colorways)
            sizes  = size_attr.get("variationAttributes", [])
            colors = color_attr.get("variationAttributes", [])
            for c_attr in colors:
                c_val = c_attr.get("displayValue", "") or c_attr.get("id", "")
                for s_attr in sizes:
                    s_raw = s_attr.get("displayValue", "") or s_attr.get("id", "")
                    s_val = _size_display(s_raw)
                    final_variants.append({
                        "Variant SKU": f"{product_id}-{c_attr.get('id','')}-{s_attr.get('id','')}",
                        "Option1 Name": "Color", "Option1 Value": c_val,
                        "Option2 Name": "Size",  "Option2 Value": s_val,
                        "Variant Price": float(price_raw),
                        "currency": _variant_currency,
                        "images": image_urls,
                    })
        elif size_attr:
            # Size-only variants (bags/accessories with one colorway)
            for s_attr in size_attr.get("variationAttributes", []):
                s_raw = s_attr.get("displayValue", "") or s_attr.get("id", "")
                s_val = _size_display(s_raw)
                final_variants.append({
                    "Variant SKU": f"{product_id}-{s_attr.get('id', s_raw)}",
                    "Option1 Name": "Size",
                    "Option1 Value": s_val,
                    "Variant Price": float(price_raw),
                    "currency": _variant_currency,
                    "images": image_urls,
                })
        elif color_attr:
            # Color-only variants (bags sold in multiple colorways, no size)
            for c_attr in color_attr.get("variationAttributes", []):
                c_val = c_attr.get("displayValue", "") or c_attr.get("id", "")
                final_variants.append({
                    "Variant SKU": f"{product_id}-{c_attr.get('id', c_val)}",
                    "Option1 Name": "Color",
                    "Option1 Value": c_val,
                    "Variant Price": float(price_raw),
                    "currency": _variant_currency,
                    "images": image_urls,
                })
        else:
            # No size / color data found — Shopify standard single-variant format
            final_variants.append({
                "Variant SKU": product_id,
                "Option1 Name": "Title",
                "Option1 Value": "Default Title",
                "Variant Price": float(price_raw),
                "currency": _variant_currency,
                "images": image_urls,
            })

        # ── 6. Vendor (resolved first so tags can reference it) ───────────────
        _brand_raw = str(p.get("brand") or p.get("manufacturer") or "Coach").strip()
        # Always use "Coach" as the canonical Shopify vendor — Coachtopia is a
        # Coach sub-brand and must not appear as a separate vendor in the store.
        vendor = "Coach"

        # ── 7. Tags ───────────────────────────────────────────────────────────
        # Build raw_tags signal string from Coach API data for gender verification
        _raw_categories = " ".join(str(c) for c in (p.get("categories") or full_cat_arr or []))
        _raw_product_tags = " ".join(str(t) for t in (p.get("tags") or p.get("keywords") or []))
        _raw_tags_signal = f"{_raw_categories} {_raw_product_tags} {url}".strip()
        tags = _build_coach_tags(
            gender, broad_cat, sub_cat_slug, title,
            vendor=vendor, raw_tags=_raw_tags_signal
        )

        return {
            "Handle": handle,
            "Title": cleaned_title,
            "Vendor": vendor,
            "Type": product_type,
            "Body (HTML)": body_html,
            "Tags": tags,
            "Google Shopping / Gender": gender,
            "variants": final_variants,
            "url": url,
            "_gender_refined": True,
        }
    except Exception as e:
        print(f"⚠️ Error cleaning product {item.get('id')}: {e}")
        return None

def clean_coach_data(raw_results):
    """Parallelized cleaning system matching Cruise Fashion methodology"""
    print(f"✨ Starting Parallel Cleaning for {len(raw_results)} items...")
    
    from concurrent.futures import ThreadPoolExecutor
    final_results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(_clean_single_coach_item, raw_results))
        final_results = [r for r in results if r]
            
    print(f"✅ Cleaned {len(final_results)} products.")
    return final_results

def complete_workflow_coach(progress_callback=None, stop_event=None, **kwargs):
    """Main entry point: Match Cruise Fashion robustness"""
    scrape_record_id = start_scrape_record("coach")
    CACHE_DIR = "scraped_files/coach_cache"
    os.makedirs(CACHE_DIR, exist_ok=True)

    def _cancel_run(reason="Cancelled by user"):
        if scrape_record_id:
            update_scrape_record(scrape_record_id, status="cancelled", error_message=reason)
        if progress_callback:
            progress_callback(100, reason)
        return []
    
    try:
        if progress_callback: progress_callback(10, "Starting Coach discovery...")
        p_ids, hints = scrape_coach_ids(progress_callback=progress_callback, scrape_record_id=scrape_record_id, **kwargs)
        
        if stop_event and stop_event.is_set():
            return _cancel_run()
        
        # Details (Resumable)
        DETAILS_FILE = f"{CACHE_DIR}/raw_details.json"
        raw_details = []
        if os.path.exists(DETAILS_FILE) and not kwargs.get("force_refresh"):
            with open(DETAILS_FILE, "r") as f: raw_details = json.load(f)
            print(f"♻️ Loaded {len(raw_details)} from cache.")
        
        remaining_ids = [pid for pid in p_ids if pid not in [d.get("id") for d in raw_details]]
        if remaining_ids:
            if progress_callback: progress_callback(40, f"Fetching {len(remaining_ids)} remainders...")
            new_details = fetch_coach_product_details(remaining_ids, gender_hints=hints, max_workers=25, progress_callback=progress_callback)
            raw_details.extend(new_details)
            with open(DETAILS_FILE, "w") as f: json.dump(raw_details, f)

        if stop_event and stop_event.is_set():
            return _cancel_run()
        
        # Cleaning
        if progress_callback: progress_callback(80, "Applying Tagging Engine v8.4...")
        cleaned = clean_coach_data(raw_details)

        if stop_event and stop_event.is_set():
            return _cancel_run()
        
        # Database & Export
        if cleaned:
            upsert_all_product_data(cleaned, "coach", "USD")
            csv_path = f"scraped_files/coach_latest.csv"
            rows = transform_to_shopify(cleaned)
            if not rows:
                update_scrape_record(
                    scrape_record_id,
                    status="failed",
                    products_count=len(cleaned),
                    error_message="Shopify CSV transform produced zero rows (check variant pricing/images).",
                )
                print("❌ transform_to_shopify returned 0 rows; raw_details kept for retry.")
                return cleaned
            export_shopify_csv(rows, csv_path)
            normalize_shopify_csv_file(csv_path)
            csv_url = upload_csv_to_supabase(csv_path, "coach")
            update_scrape_record(scrape_record_id, status="completed", products_count=len(cleaned), csv_url=csv_url)
            
            # Remove detail cache on success
            if os.path.exists(DETAILS_FILE): os.remove(DETAILS_FILE)
            if progress_callback: progress_callback(100, f"Done! {len(cleaned)} products saved.")
            return cleaned
        else:
            update_scrape_record(scrape_record_id, status="failed", error_message="No products cleaned.")
            return []
            
    except RuntimeError as e:
        # Ban errors must propagate to app.py so it sets is_running=False and
        # shows the error message rather than overwriting with "Completed ✅".
        if "IP banned" in str(e):
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
            raise
        print(f"❌ Workflow Error: {e}")
        update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        return []
    except Exception as e:
        print(f"❌ Workflow Error: {e}")
        update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        return []

if __name__ == "__main__":
    complete_workflow_coach()
