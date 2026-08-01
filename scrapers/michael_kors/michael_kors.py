import os
import time
import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cur_requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from core.tag_engine import (
    clean_title, generate_handle, apply_standardized_tags,
    detect_gender, append_brand_message, sanitize_html_description,
    build_full_tags, build_mirage_description,
)
from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv

load_dotenv()

SCRAPER_ID = "michael_kors"
DEFAULT_CURRENCY = "USD"
DEFAULT_BASE_URL = "https://www.michaelkors.com"

# Default US SFCC endpoints (region-aware override still works for global URLs)
DEFAULT_MK_GRID_URL    = f"{DEFAULT_BASE_URL}/on/demandware.store/Sites-mk_us-Site/en_US/Search-UpdateGrid"
DEFAULT_MK_PRODUCT_URL = f"{DEFAULT_BASE_URL}/on/demandware.store/Sites-mk_us-Site/en_US/Product-Variation"

# Outlet + markdown sale categories — US defaults
DEFAULT_MK_CATEGORIES = [
    {"cgid": "outlet-all",       "gender": "women", "type": ""},
    {"cgid": "sale-handbags",    "gender": "women", "type": "Handbag"},
    {"cgid": "sale-wallets",     "gender": "women", "type": "Wallet"},
    {"cgid": "sale-shoes",       "gender": "women", "type": "Shoes"},
    {"cgid": "sale-accessories", "gender": "women", "type": "Accessories"},
]

proxy_str = os.getenv("PROXY_URL") or os.getenv("PROXY_CHROME")
if proxy_str and not proxy_str.startswith("http"):
    proxy_str = f"http://{proxy_str}"
proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None


# ── Region / URL configuration ─────────────────────────────────────────────────

def _build_mk_config(url: str | None = None):
    """Return base URL, grid URL, product URL, headers, currency and categories.

    If a URL is provided, detect whether it is the US site (michaelkors.com) or
    the India global site and build the matching SFCC endpoints.
    """
    if not url:
        return {
            "base_url": DEFAULT_BASE_URL,
            "grid_url": DEFAULT_MK_GRID_URL,
            "product_url": DEFAULT_MK_PRODUCT_URL,
            "headers": {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": f"{DEFAULT_BASE_URL}/in/en/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "x-requested-with": "XMLHttpRequest",
            },
            "currency": DEFAULT_CURRENCY,
            "categories": DEFAULT_MK_CATEGORIES,
            "url_params": {},
        }

    url = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    if hostname.endswith("michaelkors.com"):
        base = "https://www.michaelkors.com"
        site = "mk_us"
        locale = "en_US"
        currency = "USD"
        referer = f"{base}/"
    else:
        base = DEFAULT_BASE_URL
        site = "mk_global"
        locale = "en_IN"
        currency = DEFAULT_CURRENCY
        referer = f"{base}/in/en/"

    grid_url = f"{base}/on/demandware.store/Sites-{site}-Site/{locale}/Search-UpdateGrid"
    product_url = f"{base}/on/demandware.store/Sites-{site}-Site/{locale}/Product-Variation"

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": f"{locale[:2]}-{locale[3:]},en;q=0.9" if len(locale) == 5 else "en-US,en;q=0.9",
        "Referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "x-requested-with": "XMLHttpRequest",
    }

    # Extract category / search filters from the URL path and query.
    # Examples:
    #   /outlet/view-all/                    -> cgid=outlet-view-all
    #   /sale/?prefn1=priceStatus&prefv1=md  -> cgid=sale, plus query params
    path_parts = [p for p in parsed.path.split("/") if p]
    cgid = None
    if "outlet" in path_parts:
        idx = path_parts.index("outlet")
        cgid = "-".join(path_parts[idx:])  # e.g. outlet-view-all
    elif "sale" in path_parts:
        cgid = "sale"

    categories = []
    if cgid:
        categories.append({"cgid": cgid, "gender": "women", "type": ""})

    # Copy search refinement params (prefn*, prefv*) into the grid request.
    # Keep the URL's page size (sz) if given — sale pages often require small sz.
    # Pagination start is handled by the scraper.
    url_params = {}
    for key, values in query.items():
        if key.startswith("prefn") or key.startswith("prefv") or key == "sz":
            url_params[key] = values[0] if len(values) == 1 else values

    return {
        "base_url": base,
        "grid_url": grid_url,
        "product_url": product_url,
        "headers": headers,
        "currency": currency,
        "categories": categories or DEFAULT_MK_CATEGORIES,
        "url_params": url_params,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_pids_from_html(html):
    """Extract product master IDs from SFCC grid HTML."""
    pids = set()
    for m in re.findall(r'data-pid=["\']([^"\']+)["\']', html):
        pids.add(m.strip())
    # Strip trailing colour suffix (last dash segment that looks like a hex/colour code)
    cleaned = set()
    for pid in pids:
        parts = pid.split("-")
        if len(parts) > 1 and re.fullmatch(r"[0-9A-Fa-f]{6}|[A-Z]+", parts[-1]):
            pid = "-".join(parts[:-1])
        cleaned.add(pid)
    return sorted(cleaned)


def _fetch_grid_page(cgid, start=0, sz=100, *, grid_url=None, req_headers=None, url_params=None):
    grid_url = grid_url or DEFAULT_MK_GRID_URL
    req_headers = req_headers or {}
    # URL params (search refinements) are defaults; pagination params are always set by the caller.
    params = {"isAjax": "true"}
    if url_params:
        params.update(url_params)
    params.update({"cgid": cgid, "start": start, "sz": sz})
    resp = cur_requests.get(
        grid_url,
        params=params,
        headers=req_headers,
        proxies=proxies,
        impersonate="chrome131",
        timeout=40,
    )
    resp.raise_for_status()
    return resp.text


def _fetch_product(pid, *, product_url=None, req_headers=None):
    product_url = product_url or DEFAULT_MK_PRODUCT_URL
    req_headers = req_headers or {}
    time.sleep(0.3)  # light pacing to reduce SFCC rate-limiting on detail calls
    resp = cur_requests.get(
        product_url,
        params={"pid": pid, "quantity": 1},
        headers={**req_headers, "Accept": "application/json, */*"},
        proxies=proxies,
        impersonate="chrome131",
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("product", {})


def _fetch_product_safe(pid, *, product_url=None, req_headers=None):
    try:
        product = _fetch_product(pid, product_url=product_url, req_headers=req_headers)
        if product and product.get("productName"):
            print(f"  Fetched: {product['productName']}")
            return product
    except Exception as e:
        print(f"  Failed {pid}: {e}")
    return None


def _fetch_color_data(color_pid: str, *, product_url=None, req_headers=None) -> tuple:
    """Fetch images AND orderable sizes for a specific colour-variant PID via SFCC.

    Returns:
        (images: list, orderable_sizes: set | None)
        orderable_sizes is None when the fetch fails or returns no size data —
        the caller should keep existing variants unchanged (safe fallback).
    """
    try:
        data = _fetch_product(color_pid, product_url=product_url, req_headers=req_headers)
        if not data:
            return [], None
        imgs = [img["url"] for img in (data.get("images") or {}).get("large", [])
                if img.get("url")]
        orderable = set()
        for attr in data.get("variationAttributes", []):
            if attr.get("attributeId") == "size":
                for val in attr.get("values", []):
                    if val.get("selectable") and val.get("displayValue"):
                        orderable.add(val["displayValue"])
                break
        # Return None if orderable is empty — avoids silently wiping all sizes
        # due to an API quirk (e.g. size attribute absent for this colour PID).
        return imgs, (orderable if orderable else None)
    except Exception:
        return [], None


def _enrich_color_images(products: list, stop_event=None, *, product_url=None, req_headers=None) -> list:
    """
    For every product:
    1. Filter the initially-selected colour's size variants using the orderable
       set already captured in _clean_mk_product (no extra API call needed).
    2. Fetch per-colour SFCC data for all non-selected colours (images + sizes).
       Deduplicated per (product, colour_id) so each colour is fetched once.
    3. Apply images and prune OOS size variants per colour.
    4. Image fallback: colours whose fetch failed get the first available gallery.
    5. Build combined product-level image list.
    6. Drop products left with zero variants (all colours fully OOS).

    Safe fallback: if a colour fetch fails or returns no size info, its existing
    variants are kept unchanged — we never wipe sizes on a transient error.
    """
    # ── Step 0: Filter selected colour sizes (already have this data) ─────────
    for p in products:
        sel_cid   = p.get("_selected_color_id", "")
        orderable = p.get("_selected_orderable_sizes")  # set or None
        if not sel_cid or orderable is None:
            continue
        before = len(p["variants"])
        p["variants"] = [
            v for v in p["variants"]
            if v.get("_color_id") != sel_cid
            or not v.get("size")           # single-size (size="") → keep
            or v["size"] in orderable
        ]
        removed = before - len(p["variants"])
        if removed:
            print(f"  ✂️  {p.get('Handle', '?')} [sel {sel_cid}]: {removed} OOS size(s) pruned")

    # ── Step 1: Collect unique (product, colour_id) that need a fetch ─────────
    # Deduplication: one fetch per colour, not one fetch per variant.
    color_tasks = {}   # (p_index, color_id) → (p, color_id, pid)
    for p_idx, p in enumerate(products):
        master_id = p.get("_master_id", "")
        sep       = p.get("_pid_sep", "_")
        for v in p.get("variants", []):
            color_id = v.get("_color_id", "")
            if color_id and not v.get("images"):
                key = (p_idx, color_id)
                if key not in color_tasks:
                    pid = f"{master_id}{sep}{color_id}" if master_id else color_id
                    color_tasks[key] = (p, color_id, pid)

    if color_tasks:
        print(f"  🎨 Fetching per-color data for {len(color_tasks)} colour(s)…")
        with ThreadPoolExecutor(max_workers=10) as pool:
            future_to = {
                pool.submit(_fetch_color_data, pid, product_url=product_url, req_headers=req_headers): (p, color_id)
                for p, color_id, pid in color_tasks.values()
            }
            for fut in as_completed(future_to):
                if stop_event and stop_event.is_set():
                    break
                p, color_id = future_to[fut]
                imgs, orderable = fut.result()
                time.sleep(0.05)

                # Set images on all image-less variants of this colour
                if imgs:
                    for v in p["variants"]:
                        if v.get("_color_id") == color_id and not v.get("images"):
                            v["images"] = imgs

                # Prune OOS sizes for this colour (skip if no orderable data)
                if orderable is not None:
                    before = len(p["variants"])
                    p["variants"] = [
                        v for v in p["variants"]
                        if v.get("_color_id") != color_id
                        or not v.get("size")       # single-size → keep
                        or v["size"] in orderable
                    ]
                    removed = before - len(p["variants"])
                    if removed:
                        print(f"  ✂️  {p.get('Handle', '?')} [{color_id}]: {removed} OOS size(s) pruned")

    # ── Step 2: Fallback — colours with no images get the first available gallery
    for p in products:
        fallback = next(
            (v["images"] for v in p.get("variants", []) if v.get("images")), []
        )
        for v in p.get("variants", []):
            if not v.get("images") and fallback:
                v["images"] = list(fallback)

    # ── Step 3: Build combined product-level image list ───────────────────────
    for p in products:
        seen     = set()
        combined = []
        for v in p.get("variants", []):
            for url in (v.get("images") or []):
                if url and url not in seen:
                    seen.add(url)
                    combined.append(url)
        if combined:
            p["images"] = combined

    # ── Step 4: Drop products with no remaining variants (all colours OOS) ────
    before_total = len(products)
    products = [p for p in products if p.get("variants")]
    dropped = before_total - len(products)
    if dropped:
        print(f"  🗑️  Dropped {dropped} fully-OOS product(s) (no orderable colour/size)")

    return products


def _discover_category(cgid, gender, product_type, progress_callback=None, base_count=0, prog_range=(10, 60), *, grid_url=None, product_url=None, req_headers=None, url_params=None):
    """Paginate through a SFCC category grid and fetch all product details.

    progress_callback(pct, status_str, products_count) is fired after every batch
    so the dashboard shows a live counter instead of a frozen 0.
    """
    discovered = {}
    start = 0
    # Use the page size from the source URL if provided (sale URLs often set sz=12/36).
    page_size = 100
    if url_params and "sz" in url_params:
        try:
            page_size = int(url_params["sz"])
        except ValueError:
            page_size = 100
    prev_ids = None
    prog_lo, prog_hi = prog_range

    while True:
        try:
            html = _fetch_grid_page(cgid, start=start, sz=page_size, grid_url=grid_url, req_headers=req_headers, url_params=url_params)
            batch_ids = _extract_pids_from_html(html)

            if not batch_ids or set(batch_ids) == set(prev_ids or []):
                break
            prev_ids = batch_ids

            new_ids = [p for p in batch_ids if p not in discovered]
            print(f"  [{cgid} +{start}] {len(new_ids)} new IDs")

            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_fetch_product_safe, pid, product_url=product_url, req_headers=req_headers): pid for pid in new_ids}
                for fut in as_completed(futures):
                    prod = fut.result()
                    if prod:
                        pid_key = prod.get("id") or futures[fut]
                        prod["_gender_tag"] = gender
                        prod["_product_type"] = product_type
                        discovered[pid_key] = prod

            # ── live progress update after each 100-product batch ──────────
            if progress_callback:
                running_total = base_count + len(discovered)
                frac = min(start / max(start + page_size, 1), 1.0)
                pct  = int(prog_lo + frac * (prog_hi - prog_lo))
                progress_callback(pct,
                                  f"Fetching {cgid}: {len(discovered)} products (+{start})...",
                                  running_total)

            start += page_size
            time.sleep(0.4)

        except Exception as e:
            print(f"  Error on {cgid} at +{start}: {e}")
            break

    return list(discovered.values())


# ── Cleaning ──────────────────────────────────────────────────────────────────

def _clean_mk_product(data, gender_tag=None, product_type_override=None, base_url=None, currency=None):
    if not data:
        return None

    base_url = base_url or DEFAULT_BASE_URL
    currency = currency or DEFAULT_CURRENCY

    # ── Out-of-stock filter ───────────────────────────────────────────────────
    if data.get("available") is False:
        return None
    avail = data.get("availability") or {}
    if avail.get("orderable") is False:
        return None

    handle = data.get("id")
    title  = (data.get("productName") or data.get("name") or "").strip()
    if not handle or not title:
        return None

    cleaned_title_str = clean_title(title)
    raw_desc  = (
        data.get("longDescription") or
        data.get("shortDescription") or
        data.get("description") or ""
    )
    # Always use canonical vendor — SFCC's brand field returns sub-brand junk.
    brand     = "Michael Kors"
    body_html = build_mirage_description(raw_desc, cleaned_title_str, brand, gender_tag or "women")
    raw_ptype = (data.get("productType") or "").strip()
    if raw_ptype.lower() in {"variationgroup", "variation group", "master", "variant"}:
        raw_ptype = ""
    ptype     = product_type_override or raw_ptype or ""
    cat_val   = gender_tag.lower() if gender_tag else ""
    url       = f"{base_url}/in/en/p/{handle}.html" if "michaelkors.global" in base_url else f"{base_url}/p/{handle}.html"

    # ── Tags ─────────────────────────────────────────────────────────────────
    tag_meta = {
        "name": title, "Title": title,
        "Product Category": cat_val, "Type": ptype,
        "Vendor": brand, "description": raw_desc, "url": url,
    }
    detected_gender = detect_gender(tag_meta)
    if detected_gender.lower() == "unisex" and gender_tag and gender_tag.lower() in ("men", "women"):
        detected_gender = gender_tag.lower()
    tags = build_full_tags(cleaned_title_str, brand, detected_gender, ptype, url,
                           extra_tags=["RudraScrapper-michael_kors"])

    # ── Price — guard against null price objects ──────────────────────────────
    price_obj        = data.get("price") or {}
    selected_price   = (price_obj.get("sales") or {}).get("value") or 0
    selected_compare = (price_obj.get("list")  or {}).get("value") or 0
    if not selected_price or float(selected_price) <= 0:
        return None

    # ── Images ───────────────────────────────────────────────────────────────
    all_images = []
    for img in (data.get("images") or {}).get("large", []):
        img_url = img.get("url")
        if img_url:
            all_images.append(img_url)

    # ── Sizes — collect ALL available size options ────────────────────────────
    all_sizes = []
    for attr in data.get("variationAttributes", []):
        if attr.get("attributeId") == "size":
            for val in attr.get("values", []):
                if val.get("selectable") and val.get("displayValue"):
                    all_sizes.append(val["displayValue"])
            break
    if not all_sizes:
        all_sizes = [""]  # no size variation → single Default variant

    # ── Colors — collect ALL selectable color options ─────────────────────────
    master_id = data.get("masterID", "") or str(handle)
    # Detect separator between master ID and colour code in MK's SFCC PIDs.
    # e.g. "CF5409I4YT_0250" → master="CF5409I4YT", sep="_", colour="0250"
    _handle_str = str(handle)
    if master_id and _handle_str.startswith(master_id) and len(_handle_str) > len(master_id):
        _pid_sep = _handle_str[len(master_id)]   # "_" or "-"
    else:
        _pid_sep = "_"                            # MK SFCC default
    selected_color_display = data.get("selectedColorValue") or ""
    color_entries = []  # (color_id, display_name, is_selected)
    for attr in data.get("variationAttributes", []):
        if attr.get("attributeId") == "color":
            for val in attr.get("values", []):
                if not val.get("selectable"):
                    continue
                vid  = val.get("id", "")
                disp = val.get("displayValue", "")
                sel  = bool(val.get("selected"))
                if sel:
                    selected_color_display = disp
                color_entries.append((vid, disp, sel))
            break
    if not color_entries:
        color_entries = [("", selected_color_display, True)]

    # ── Build product entry ───────────────────────────────────────────────────
    # Identify the selected colour and its orderable sizes so _enrich_color_images
    # can prune OOS sizes for the selected colour without an extra API call.
    _selected_cid = next((vid for vid, _d, sel in color_entries if sel), "")
    # all_sizes already contains only selectable sizes for the selected colour.
    # Use None for single-size products (size="") so the fallback is skipped.
    _sel_orderable = list(set(all_sizes)) if all_sizes != [""] else None

    product_entry = {
        "Handle":           generate_handle(cleaned_title_str, handle),
        "Title":            cleaned_title_str,
        "Body (HTML)":      body_html,
        "Vendor":           brand,
        "Product Category": cat_val,
        "Type":             ptype,
        "Tags":             tags,
        "Google Shopping / Gender": detected_gender,
        "variants": [],
        "_master_id":                master_id,
        "_pid_sep":                  _pid_sep,
        "_selected_color_id":        _selected_cid,
        "_selected_orderable_sizes": _sel_orderable,
    }

    # Color × Size cross-product — images for non-selected colors are enriched later
    for color_id, color_disp, is_selected in color_entries:
        sku_base = str(handle) if is_selected else (f"{master_id}{_pid_sep}{color_id}" if master_id else color_id)
        variant_images = all_images if is_selected else []
        color_label    = color_disp or selected_color_display

        for size in all_sizes:
            size_sfx = (f"-{size.replace(' ', '-').lower()}"
                        if size and len(all_sizes) > 1 else "")
            sku = f"{sku_base}{size_sfx}" if size_sfx else sku_base
            product_entry["variants"].append({
                "Variant SKU":              sku,
                "size":                     size,
                "color":                    color_label,
                "Variant Price":            float(selected_price),
                "Variant Compare At Price": float(selected_compare) if selected_compare else 0,
                "currency":                 currency,
                "images":                   variant_images,
                "_color_id":                color_id,
            })

    return product_entry


# ── Main entry point ──────────────────────────────────────────────────────────

def complete_workflow_michael_kors(progress_callback=None, stop_event=None, url=None, base_url=None, **kwargs):
    """
    Scrape Michael Kors products.

    Args:
        url: Optional Michael Kors URL. If it points to michaelkors.com, the US
            SFCC endpoints and USD currency are used. Otherwise the India/global
            site and INR are used.
    """
    effective_url = url or base_url
    urls = [u.strip() for u in effective_url.split(",") if u.strip()] if effective_url else [None]
    multi_url = len(urls) > 1

    CACHE_FILE = "scraped_files/michael_kors_discovery.json"
    os.makedirs("scraped_files", exist_ok=True)

    # Build primary config from the first URL so variables are always defined,
    # even when we use the cached discovery file.
    primary_cfg = _build_mk_config(urls[0])
    base_url    = primary_cfg["base_url"]
    grid_url    = primary_cfg["grid_url"]
    product_url = primary_cfg["product_url"]
    req_headers = primary_cfg["headers"]
    currency    = primary_cfg["currency"]
    region_label = "US" if "michaelkors.com" in base_url else "India"
    scrape_record_id = start_scrape_record(SCRAPER_ID)

    def _cb(p, s, c=0):
        if progress_callback:
            progress_callback(p, s, c)

    try:
        _cb(5, f"Starting Michael Kors scraper ({region_label})...")

        # ── PHASE 1: DISCOVERY ────────────────────────────────────────────────
        all_raw = []

        if not multi_url and os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f:
                    all_raw = json.load(f)
                print(f"♻️  Loaded {len(all_raw)} products from cache")
                _cb(35, f"Loaded {len(all_raw)} from cache...", len(all_raw))
            except Exception:
                all_raw = []

        if not all_raw:
            for url_idx, single_url in enumerate(urls):
                if stop_event and stop_event.is_set():
                    if scrape_record_id:
                        update_scrape_record(scrape_record_id, status="cancelled")
                    return []

                cfg = _build_mk_config(single_url)

                loop_base_url    = cfg["base_url"]
                loop_grid_url    = cfg["grid_url"]
                loop_product_url = cfg["product_url"]
                loop_req_headers = cfg["headers"]
                loop_categories  = cfg["categories"]
                loop_url_params  = cfg["url_params"]

                total = len(loop_categories)
                for i, cat in enumerate(loop_categories):
                    if stop_event and stop_event.is_set():
                        update_scrape_record(scrape_record_id, status="cancelled")
                        return []

                    cgid   = cat["cgid"]
                    gender = cat["gender"]
                    ptype  = cat["type"]

                    # Progress band for this category: divide 10–60 range evenly across all URLs
                    prog_lo = 10 + int(((url_idx * total + i) / (len(urls) * total)) * 50)
                    prog_hi = 10 + int((((url_idx * total) + i + 1) / (len(urls) * total)) * 50)

                    print(f"\n📡 Scraping: {cgid} ({gender}) — URL {url_idx + 1}/{len(urls)}")
                    products = _discover_category(
                        cgid, gender, ptype,
                        progress_callback=_cb,
                        base_count=len(all_raw),
                        prog_range=(prog_lo, prog_hi),
                        grid_url=loop_grid_url,
                        product_url=loop_product_url,
                        req_headers=loop_req_headers,
                        url_params=loop_url_params,
                    )
                    all_raw.extend(products)
                    print(f"  → {len(products)} from {cgid} (total: {len(all_raw)})")
                    heartbeat_scrape_record(scrape_record_id, len(all_raw))

            if all_raw and not multi_url:
                with open(CACHE_FILE, "w") as f:
                    json.dump(all_raw, f)
                print(f"💾 Cached {len(all_raw)} raw products")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        if not all_raw:
            update_scrape_record(scrape_record_id, status="failed", error_message="No products discovered.")
            _cb(0, "Failed: No products found.", 0)
            return []

        # ── PHASE 2: CLEAN ────────────────────────────────────────────────
        _cb(65, f"Cleaning {len(all_raw)} products...")
        cleaned = []
        seen_handles = set()

        for item in all_raw:
            if stop_event and stop_event.is_set():
                break
            gender = item.pop("_gender_tag", "women")
            ptype  = item.pop("_product_type", "")
            try:
                result = _clean_mk_product(item, gender_tag=gender, product_type_override=ptype,
                                           base_url=base_url, currency=currency)
                if result and result["Handle"] not in seen_handles:
                    cleaned.append(result)
                    seen_handles.add(result["Handle"])
            except Exception as e:
                print(f"  Cleaning error on {item.get('id', '?')}: {e}")

        _cb(75, f"Fetching per-color images for {len(cleaned)} products…", len(cleaned))
        cleaned = _enrich_color_images(cleaned, stop_event=stop_event,
                                       product_url=product_url, req_headers=req_headers)

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            _cb(0, "Cancelled after enrichment.", 0)
            return []

        _cb(80, f"Saving {len(cleaned)} in-stock products...", len(cleaned))

        if not cleaned:
            update_scrape_record(scrape_record_id, status="failed",
                                 error_message="No in-stock products after cleaning.")
            return []

        # ── PHASE 3: SAVE + CSV ───────────────────────────────────────────────
        upsert_all_product_data(cleaned, SCRAPER_ID, currency)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        _cb(92, "Generating Shopify CSV...")
        rows = transform_to_shopify(cleaned)
        export_shopify_csv(rows, csv_path)

        _cb(97, "Uploading CSV...")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)
        update_scrape_record(scrape_record_id, status="completed",
                             products_count=len(cleaned), csv_url=csv_url)

        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

        _cb(100, f"Done! {len(cleaned)} in-stock products.", len(cleaned))
        print(f"✅ Michael Kors complete: {len(cleaned)} products")
        return cleaned

    except Exception as e:
        print(f"❌ Michael Kors Error: {e}")
        import traceback; traceback.print_exc()
        try:
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        except Exception:
            pass
        if progress_callback:
            progress_callback(0, f"Error: {e}", 0)
        return []


if __name__ == "__main__":
    # Example: pass a US outlet or sale URL directly
    # complete_workflow_michael_kors(url="https://www.michaelkors.com/outlet/view-all/")
    complete_workflow_michael_kors()
