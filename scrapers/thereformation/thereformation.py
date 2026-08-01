"""
The Reformation Scraper v2.2
==============================
Strategy:
    Phase 1 — SFCC Search-ShowAjax API paginates /clothing category
               sequentially with early-stop (empty page = end of catalog).
               Sequential avoids the HTTP 429 rate-limits from concurrent bursts.

    Phase 2 — Product-ShowQuickAdd endpoint fetches per-product details:
               colors, sizes, prices, availability, images.
               Concurrent fetch with ThreadPoolExecutor(20 workers).

    Phase 3 — Build Mirage descriptions, apply taxonomy tags.
               Export to scraped_files/thereformation_latest.csv

Format Rules:
    PRICING  — currency: USD on every variant dict (USD->INR via pricing_engine).
    TAGS     — build_full_tags + RudraScrapper-thereformation safety tag.
    IMAGES   — Deduplicated, per-color image list. Per-variant images match colour.
    GENDER   — _gender_refined: True skips Gemini re-classification.
"""

import os
import re
import sys
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as _cf_requests
    _CURL_CFFI_OK = True
except (ImportError, OSError):
    _CURL_CFFI_OK = False
    import httpx as _httpx

from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
      upsert_all_product_data, start_scrape_record, update_scrape_record,
      heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import (
      clean_title, generate_handle,
      build_full_tags, build_mirage_description,
      sanitize_html_description,
)
from core.scraper_config import tag_lookup, validate_size, TAG_ROWS, PRICING

load_dotenv()
logger = logging.getLogger(__name__)

SCRAPER_ID = "thereformation"
VENDOR     = "The Reformation"
CURRENCY   = "USD"
BASE_URL   = "https://www.thereformation.com"
CSV_PATH   = "scraped_files/thereformation_latest.csv"

_PROXY_STR = os.getenv("PROXY_URL")
_PROXIES   = {"http": _PROXY_STR, "https": _PROXY_STR} if _PROXY_STR else None

_SFCC_SEARCH = (
      "https://www.thereformation.com/on/demandware.store"
      "/Sites-reformation-us-Site/en_US/Search-ShowAjax"
      "?cgid=clothing&pmpt=qualifying"
      "&prefn1=subclass"
      "&prefv1=Dresses%7cTops%7cTees%7cJeans%7cJumpsuits"
      "%7cPants%7cTwo%20pieces%7cSweatshirts%7cSweaters"
      "%7cSkirts%7cOuterwear%7cOne%2bPiece"
      "&srule=Best%20of"
)
_QUICK_ADD = (
      "https://www.thereformation.com/on/demandware.store"
      "/Sites-reformation-us-Site/en_US/Product-ShowQuickAdd"
      "?pid={pid}&gtmListAttribute=Category%3A%20Clothing"
      "&pageTypeContext=Search-Show"
)

_HEADERS = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
      ),
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": f"{BASE_URL}/",
}

RETRIES     = 4
BACKOFF     = 2
TIMEOUT     = 60
MAX_PAGES   = 100
PAGE_SIZE   = 24
MAX_WORKERS = 20
PAGE_DELAY  = 0.8   # seconds between sequential page requests (avoids 429s)


def _http_get(url: str, *, headers=None, proxies=None, timeout=TIMEOUT) -> object:
    """
    HTTP GET with curl_cffi (TLS fingerprint impersonation) when available,
    falling back to httpx with HTTP/2 enabled if curl_cffi is not importable
    or its shared library is missing. Both return a response-like object with
    .status_code, .headers, .text, .raise_for_status(), and .json().
    """
    if _CURL_CFFI_OK:
        return _cf_requests.get(
            url, headers=headers, proxies=proxies,
            timeout=timeout, impersonate="chrome131",
        )
    # httpx fallback — http2=True gives better TLS handshake parity than plain http
    # httpx.Response already exposes .status_code, .headers, .text, .json(),
    # and .raise_for_status() so it is a drop-in for the response object used here.
    _httpx_proxies = proxies.get("https") if proxies else None
    with _httpx.Client(http2=True, headers=headers, proxy=_httpx_proxies, timeout=timeout) as client:
        return client.get(url)


# ── Phase 1: Collect product IDs (SEQUENTIAL) ─────────────────────────────────
#
# The Reformation SFCC endpoint rate-limits hard when hit with concurrent
# bursts. The old approach fired 5 workers x 100 pages simultaneously and got
# mass 429s, collecting 0 products. Sequential pagination with early-stop
# (empty page = catalog end) collects all ~400-500 products in ~18-21 requests.

def _extract_pids(html: str) -> set:
      soup = BeautifulSoup(html, "html.parser")
      pids: set = set()
      for a in soup.find_all("a", href=re.compile(r"/products/[^/]+/[^/]+\.html")):
          m = re.search(r"/products/[^/]+/([^/]+)\.html", a["href"])
          if m:
              pids.add(m.group(1))
      return pids


def _fetch_page_pids(offset: int) -> set:
      """Fetch one page of product IDs. Respects Retry-After header on 429."""
      url = f"{_SFCC_SEARCH}&start={offset}&sz={PAGE_SIZE}"
      for attempt in range(RETRIES):
          try:
              r = _http_get(url, headers=_HEADERS, proxies=_PROXIES, timeout=TIMEOUT)
              if r.status_code == 429:
                  wait = int(r.headers.get("Retry-After", 30))
                  wait = min(wait, 90)
                  logger.info(f"[REF] 429 at offset={offset} — waiting {wait}s")
                  time.sleep(wait)
                  continue
              r.raise_for_status()
              pids = _extract_pids(r.text)
              if pids or attempt == RETRIES - 1:
                  return pids
              time.sleep(BACKOFF ** attempt)
          except Exception as e:
              if attempt < RETRIES - 1:
                  time.sleep(BACKOFF ** attempt)
              else:
                  logger.warning(f"[REF] offset={offset} error: {e}")
      return set()


def _collect_all_pids(stop_event=None) -> set:
      """
      Walk pages sequentially until an empty page signals end-of-catalog.
      Avoids the burst-rate-limit that concurrent fetching triggers on SFCC.
      """
      all_pids: set = set()
      for page_num in range(MAX_PAGES):
          if stop_event and stop_event.is_set():
              break
          pids = _fetch_page_pids(page_num * PAGE_SIZE)
          if not pids:
              logger.info(f"[REF] Catalog end at offset={page_num * PAGE_SIZE} (page {page_num})")
              break
          all_pids.update(pids)
          logger.info(f"[REF] Page {page_num}: +{len(pids)} pids, total={len(all_pids)}")
          time.sleep(PAGE_DELAY)
      return all_pids


# ── Phase 2: Fetch product details ────────────────────────────────────────────

def _fetch_product(pid: str) -> tuple[str, dict | None]:
    url = _QUICK_ADD.format(pid=pid)
    for attempt in range(RETRIES):
        try:
            r = _http_get(url, headers=_HEADERS, proxies=_PROXIES, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if data and data.get("product"):
                return pid, data
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF ** attempt)
        except Exception as e:
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF ** attempt)
            else:
                logger.warning(f"[REF] pid={pid} error: {e}")
    return pid, None


def _build_product(pid: str, raw_data: dict) -> dict | None:
    product = raw_data.get("product", {})
    if not product.get("purchasable"):
        return None

    title = clean_title(product.get("productName", ""))
    if not title:
        return None

    vendor  = product.get("brand") or VENDOR
    p_type  = product.get("item_class") or "Clothing"
    handle  = generate_handle(title)
    gender  = "Women"

    material = product.get("material_description") or ""
    country  = product.get("country_of_origin_sustainabilitytext") or ""

    variation_attrs = product.get("variationAttributes", [])
    if not variation_attrs:
        return None

    color_variants = variation_attrs[0].get("values", [])

    # ── FORMAT RULE: IMAGES ───────────────────────────────────────────────────
    seen_imgs: set = set()
    all_images: list = []

    def _collect_img(url: str):
        if url and url not in seen_imgs:
            seen_imgs.add(url)
            all_images.append(url)

    # Build color → image map first (for variant image assignment).
    # Collect ALL images per colour (front, back, detail, model shots…)
    # not just the first one — the full list is passed into each variant so
    # the transformer can write them as gallery rows.
    color_images: dict = {}
    for cv in color_variants:
        color_id = cv.get("id", "")
        imgs: list = []
        for img in cv.get("images", {}).get("medium", []):
            if img.get("hasImage"):
                url = img.get("absURL", "")
                if url:
                    imgs.append(url)
                    _collect_img(url)
        color_images[color_id] = imgs

    size_attr = next(
        (a for a in variation_attrs if a.get("attributeId") == "sizeByColor"), {}
    )

    # ── FORMAT RULE: PRICING ─────────────────────────────────────────────────
    variants: list = []
    seen_skus: set = set()
    for cv in color_variants:
        color    = cv.get("displayValue", "")
        color_id = cv.get("id", "")
        c_imgs   = color_images.get(color_id, all_images[:2])

        for sv in size_attr.get("values", []):
            if (sv.get("color") or {}).get("id") != color_id:
                continue
            for size_entry in sv.get("sizes", []):
                vp = size_entry.get("product", {})
                if not (vp.get("available") and vp.get("purchasable")):
                    continue
                avail = vp.get("availability", {})
                if "Out of Stock" in (avail.get("messages") or []):
                    continue
                sku   = (vp.get("id") or "").strip()
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)
                size  = size_entry.get("displayValue", "")
                # ── FORMAT RULE: SIZES (from all_sizes= env var) ────────────
                # Reformation sells women's clothing (US dress sizes 0-16+).
                # Apparel is exempt from UK footwear size rule — pass through.
                size = validate_size(size or "One Size", is_footwear=False) or "One Size"
                price = (vp.get("price") or {}).get("sales", {}).get("value")
                if not price or float(price) <= 0:
                    continue
                variants.append({
                    "Variant SKU":              sku,
                    "size":                     size or "One Size",
                    "color":                    color,
                    "Variant Price":            float(price),
                    "Variant Compare At Price": 0,
                    "currency":                 CURRENCY,   # ← PRICING RULE
                    "images":                   c_imgs,
                })

    if not variants:
        return None

    # ── FORMAT RULE: DESCRIPTION ─────────────────────────────────────────────
    desc_plain = f"{material.lower()} {country.lower()}".strip()
    raw_desc   = sanitize_html_description(desc_plain)
    body_html  = build_mirage_description(raw_desc, title, VENDOR, gender)

    # ── FORMAT RULE: TAGS (from all_Tags= env var) ───────────────────────────
    env_main, env_sub = tag_lookup(gender, p_type, title)
    tags = build_full_tags(
        title, VENDOR, gender, p_type,
        extra_tags=[f"RudraScrapper-{SCRAPER_ID}", env_main, env_sub],
    )

    return {
        "Title":                    title,
        "Handle":                   handle,
        "Body (HTML)":              body_html,
        "Vendor":                   VENDOR,
        "Product Category":         p_type,
        "Type":                     p_type,
        "Tags":                     tags,
        "Google Shopping / Gender": gender.lower(),
        "_gender_refined":          True,
        "images":                   all_images,
        "variants":                 variants,
    }


def _split_large_products(products: list, max_variants: int = 100) -> list:
    import copy
    result = []
    for prod in products:
        variants = prod.get("variants", [])
        if len(variants) <= max_variants:
            result.append(prod)
            continue
        chunks = [variants[i:i + max_variants] for i in range(0, len(variants), max_variants)]
        for idx, chunk in enumerate(chunks, 1):
            p = copy.deepcopy(prod)
            p["variants"] = chunk
            p["Title"]  = f"{prod['Title']} — Part {idx}"
            p["Handle"] = f"{prod['Handle']}-part-{idx}"
            result.append(p)
    return result


# ── Main workflow ─────────────────────────────────────────────────────────────

def complete_workflow_thereformation(progress_callback=None, stop_event=None):
    logger.info(
        f"[REF] Format rules loaded — "
        f"tag_rows={len(TAG_ROWS)} | "
        f"pricing=offset+{PRICING['RATE_OFFSET']} fee+{PRICING['FIXED_FEE']} "
        f"USD×{PRICING['MARKUP_USD_BELOW']}/{PRICING['MARKUP_USD_ABOVE']} "
        f"GBP×{PRICING['MARKUP_GBP_BELOW']}"
    )
    scrape_id = start_scrape_record(SCRAPER_ID)

    def cb(pct, msg, count=None):
        logger.info(f"[REF] {pct}% — {msg}")
        if progress_callback:
            try:
                progress_callback(pct, msg, count)
            except Exception:
                pass

    _hb_count = [0]

    def _heartbeat():
        while not (stop_event and stop_event.is_set()):
            try:
                heartbeat_scrape_record(scrape_id, _hb_count[0])
            except Exception:
                pass
            time.sleep(15)

    threading.Thread(target=_heartbeat, daemon=True).start()

    cb(5, "Collecting The Reformation product IDs…")

    all_pids = _collect_all_pids(stop_event=stop_event)

    cb(22, f"Found {len(all_pids)} product IDs — fetching details…")

    raw_map: dict = {}
    failed: list = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_fetch_product, pid): pid for pid in all_pids}
        done = 0
        for fut in as_completed(futs):
            if stop_event and stop_event.is_set():
                break
            pid, data = fut.result()
            done += 1
            if data:
                raw_map[pid] = data
            else:
                failed.append(pid)
            if done % 25 == 0:
                pct = 22 + int(done / max(len(all_pids), 1) * 40)
                cb(pct, f"Fetched {done}/{len(all_pids)} products…")

    for pid in failed:
        if stop_event and stop_event.is_set():
            break
        _, data = _fetch_product(pid)
        if data:
            raw_map[pid] = data

    cb(65, f"Building {len(raw_map)} products…")

    all_products: list = []
    seen_handles: set = set()
    for pid, data in raw_map.items():
        if stop_event and stop_event.is_set():
            break
        prod = _build_product(pid, data)
        if prod and prod["Handle"] not in seen_handles:
            seen_handles.add(prod["Handle"])
            all_products.append(prod)

    all_products = _split_large_products(all_products)
    cb(76, f"Transforming {len(all_products)} products…", len(all_products))

    if not all_products:
        update_scrape_record(scrape_id, status="failed", products_count=0)
        cb(100, "No products found.")
        return

    rows = transform_to_shopify(all_products)
    cb(88, f"Exporting {len(rows)} CSV rows…", len(all_products))

    export_shopify_csv(rows, CSV_PATH)
    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    cb(95, "Uploading to Supabase…")

    try:
        upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)
    except Exception as e:
        logger.warning(f"[REF] Supabase upload: {e}")

    update_scrape_record(scrape_id, status="completed", products_count=len(all_products))
    cb(100, f"Done — {len(all_products)} products, {len(rows)} CSV rows.", len(all_products))
    logger.info(f"[REF] ✅ Complete → {CSV_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    complete_workflow_thereformation()
