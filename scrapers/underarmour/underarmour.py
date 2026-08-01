"""
Under Armour Scraper v2.1
==========================
Strategy:
  Phase 1 — Constructor.io browse API (ac.cnstrc.com)
             group_id/men  → 1,014 products
             group_id/women → 1,043 products
             Full catalog (not outlet). Paginates 200/page.

  Phase 2 — Build Mirage descriptions, apply taxonomy tags.

  Phase 3 — Transform to Shopify CSV → scraped_files/underarmour_latest.csv

Format Rules Applied (matching established scrapers):
  PRICING  — "currency": "USD" on every variant dict so auto_fix_variant
             uses the correct USD→INR formula via pricing_engine.
             (Without this key, transformer defaults to GBP — wrong rate.)
  TAGS     — build_full_tags(title, vendor, gender, p_type,
             extra_tags=["RudraScrapper-underarmour"]) — full Mirage
             taxonomy string + Shopify dual-verification delete safety tag.
  IMAGES   — Scene7 CDN URLs deduplicated at product level. Per-variant
             images include front + hover pair per variant when available.
             build_mirage_description(raw, title, brand, gender) builds the
             full Mirage HTML description including the brand footer.
  GENDER   — "Google Shopping / Gender" + "_gender_refined": True set on every
             product dict so the transformer uses the known gender directly
             without calling Gemini for re-classification.
"""

import os
import re
import sys
import math
import time
import threading
import logging

import requests

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
from core.scraper_config import tag_lookup, validate_size, VALID_UK_SIZES, TAG_ROWS, PRICING

logger = logging.getLogger(__name__)

SCRAPER_ID  = "underarmour"
VENDOR      = "Under Armour"
CURRENCY    = "USD"             # USD → INR via pricing_engine
BASE_URL    = "https://www.underarmour.com"
CSV_PATH    = "scraped_files/underarmour_latest.csv"

_CNSTRC_URL = "https://ac.cnstrc.com/browse/group_id/{group_id}"
# Constructor.io API key and session params.
# Key validated live 2026-06-08: group_id/men → 939 results, group_id/women → 990 results.
# If the key rotates, _validate_cnstrc_key() will raise RuntimeError with instructions.
_CNSTRC_BASE = {
    "c":                    "ciojs-client-2.65.0",
    "key":                  "key_Gz4VzKsXbR7b7fSh",
    "i":                    "773d2e1b-d558-4ea1-976e-712c4aad1471",
    "s":                    "3",
    "num_results_per_page": "200",
    "sort_by":              "relevance",
}

# Group IDs validated live 2026-06-08 against ac.cnstrc.com/browse/group_id/{id}.
# To rediscover if groups change: inspect Network tab on underarmour.com → filter
# ac.cnstrc.com requests → note group_id path param and 'key' query param.
GROUPS = [
    {"id": "men",   "gender": "Men"},
    {"id": "women", "gender": "Women"},
]


def _ensure_https(url: str | None) -> str | None:
    """Normalize Scene7 CDN image URLs from http:// to https://."""
    if url and url.startswith("http://"):
        return "https://" + url[7:]
    return url


# ── Phase 1: Fetch Constructor.io ─────────────────────────────────────────────

def _fetch_group(group_id: str, stop_event=None) -> list:
    all_results: list = []
    params = {**_CNSTRC_BASE, "offset": "0"}
    url    = _CNSTRC_URL.format(group_id=group_id)
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data  = r.json()
        total = data.get("response", {}).get("total_num_results", 0)
        if total == 0:
            logger.warning(f"[UA] group={group_id}: 0 results")
            return []
        logger.info(f"[UA] group={group_id}: {total} total")
        all_results.extend(data.get("response", {}).get("results", []))

        per_page = int(params["num_results_per_page"])
        for page in range(1, math.ceil(total / per_page)):
            if stop_event and stop_event.is_set():
                break
            params["offset"] = str(page * per_page)
            r2 = requests.get(url, params=params, timeout=20)
            r2.raise_for_status()
            more = r2.json().get("response", {}).get("results", [])
            if not more:
                break
            all_results.extend(more)
            time.sleep(0.3)
    except Exception as e:
        logger.warning(f"[UA] group={group_id} error: {e}")
    return all_results


# ── Phase 2: Build product dicts ─────────────────────────────────────────────

def _facet_value(facets: list, name: str) -> str:
    for f in facets or []:
        if f.get("name", "").lower() == name.lower():
            vals = f.get("values", [])
            return str(vals[0]) if vals else ""
    return ""


def _clean_type(sub_header: str) -> str:
    cleaned = re.sub(
        r"\b(men|mens|women|womens|woman|girl|girls|boy|boys)(?:'s|s')?\b",
        "", sub_header or "", flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _build_product(raw: dict, gender: str) -> dict | None:
    data = raw.get("data") or {}
    if not data.get("orderable"):
        return None

    title = clean_title(raw.get("value", ""))
    if not title:
        return None

    handle  = (data.get("id") or generate_handle(title)).lower().strip()
    p_type  = _clean_type(data.get("subHeader") or "")
    desc_raw = data.get("description") or ""

    # ── FORMAT RULE: IMAGES ───────────────────────────────────────────────────
    seen_imgs: set = set()
    prod_images: list = []
    for url in [_ensure_https(data.get("image_url")), _ensure_https(data.get("gridTileHoverImageURL"))]:
        if url and url not in seen_imgs:
            seen_imgs.add(url)
            prod_images.append(url)

    # ── FORMAT RULE: PRICING ─────────────────────────────────────────────────
    variants: list = []
    seen_keys: set = set()
    for var in raw.get("variations", []):
        vd = var.get("data") or {}
        if not vd.get("orderable"):
            continue
        sku   = (vd.get("sku") or "").strip()
        size  = _facet_value(vd.get("facets"), "size")
        color = _facet_value(vd.get("facets"), "colorGroup") or data.get("colorValue", "")
        key   = sku or f"{color}_{size}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        price = float(vd.get("salePrice") or 0)
        cap   = float(vd.get("listPrice") or 0)
        if price <= 0:
            continue

        # ── FORMAT RULE: SIZES (from all_sizes= env var) ────────────────────
        # UA sells both footwear and apparel.
        # Footwear: size must be in VALID_UK_SIZES — skip if outside range.
        # Apparel:  exempt from UK-size rule — keep native sizes (XS/S/M etc).
        _blob = f"{p_type} {title}".lower()
        _is_footwear = any(k in _blob for k in (
            "shoe", "sneaker", "boot", "sandal", "slide", "trainer", "footwear",
        ))
        validated = validate_size(size, is_footwear=_is_footwear)
        if validated is None:
            continue   # footwear size outside UK whitelist — skip variant
        size = validated

        # Per-variant images: front + hover for this specific variant
        v_imgs: list = []
        for u in [_ensure_https(vd.get("image_url")), _ensure_https(vd.get("gridTileHoverImageURL"))]:
            if u and u not in seen_imgs:
                seen_imgs.add(u)
                prod_images.append(u)
            if u:
                v_imgs.append(u)
        if not v_imgs:
            v_imgs = prod_images[:2]

        variants.append({
            "Variant SKU":              sku,
            "size":                     size or "One Size",
            "color":                    color,
            "Variant Price":            price,
            "Variant Compare At Price": cap if cap > price else 0,
            "currency":                 CURRENCY,   # ← PRICING RULE
            "images":                   v_imgs,
        })

    if not variants:
        return None

    # ── FORMAT RULE: DESCRIPTION ─────────────────────────────────────────────
    raw_desc  = sanitize_html_description(desc_raw)
    body_html = build_mirage_description(raw_desc, title, VENDOR, gender)

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
        "images":                   prod_images,
        "variants":                 variants,
    }


# ── Main workflow ─────────────────────────────────────────────────────────────

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


def _validate_cnstrc_key() -> None:
    """
    Probe the Constructor.io API key and group IDs before starting a full run.
    Raises RuntimeError with a clear message if the key is invalid/rotated or
    every configured group returns 0 results (group IDs changed upstream).

    This replaces silent failures where an expired key would silently produce an
    empty catalog with no diagnostic information.
    """
    probe_url   = _CNSTRC_URL.format(group_id=GROUPS[0]["id"])
    probe_params = {**_CNSTRC_BASE, "offset": "0", "num_results_per_page": "1"}
    try:
        r = requests.get(probe_url, params=probe_params, timeout=20)
    except Exception as e:
        raise RuntimeError(
            f"[UA] Constructor.io probe request failed — network error: {e}"
        ) from e

    if r.status_code == 401:
        raise RuntimeError(
            f"[UA] Constructor.io API key '{_CNSTRC_BASE['key']}' is invalid or has been "
            "rotated. Inspect current UA site traffic (Network tab → ac.cnstrc.com) to "
            "find the live key and update _CNSTRC_BASE['key'] in underarmour.py."
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"[UA] Constructor.io probe returned HTTP {r.status_code} for "
            f"group={GROUPS[0]['id']}. Response: {r.text[:200]}"
        )

    try:
        total = r.json().get("response", {}).get("total_num_results", 0)
    except Exception:
        total = 0

    if total == 0:
        raise RuntimeError(
            f"[UA] Constructor.io group '{GROUPS[0]['id']}' returned 0 results with key "
            f"'{_CNSTRC_BASE['key']}'. The group_id may have changed upstream — inspect "
            "UA site traffic to find the current browse group IDs and update "
            "GROUPS in underarmour.py."
        )

    logger.info(
        f"[UA] Constructor.io health check passed — key={_CNSTRC_BASE['key']} "
        f"group={GROUPS[0]['id']} total={total}"
    )


def complete_workflow_underarmour(progress_callback=None, stop_event=None):
    logger.info(
        f"[UA] Format rules loaded — "
        f"footwear_sizes={len(VALID_UK_SIZES)} | tag_rows={len(TAG_ROWS)} | "
        f"pricing=offset+{PRICING['RATE_OFFSET']} fee+{PRICING['FIXED_FEE']} "
        f"USD×{PRICING['MARKUP_USD_BELOW']}/{PRICING['MARKUP_USD_ABOVE']} "
        f"GBP×{PRICING['MARKUP_GBP_BELOW']}"
    )
    scrape_id = start_scrape_record(SCRAPER_ID)

    def cb(pct, msg, count=None):
        logger.info(f"[UA] {pct}% — {msg}")
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

    # ── API key / group health check ────────────────────────────────────────
    cb(3, "Validating Constructor.io API key and group IDs…")
    try:
        _validate_cnstrc_key()
    except RuntimeError as e:
        update_scrape_record(scrape_id, status="failed", products_count=0)
        cb(0, f"Failed: {e}")
        raise   # re-raise so scrapers_run.py / app.py mark the job as failed,
                # not silently completed (a bare return would look like success to callers)
    # ────────────────────────────────────────────────────────────────────────

    all_raw: list = []
    for i, grp in enumerate(GROUPS):
        if stop_event and stop_event.is_set():
            break
        cb(5 + i * 25, f"Fetching {grp['gender']} catalog…")
        raw = _fetch_group(grp["id"], stop_event=stop_event)
        for item in raw:
            item["_gender"] = grp["gender"]
        all_raw.extend(raw)
        logger.info(f"[UA] {grp['id']}: {len(raw)} results")

    cb(55, f"Building products from {len(all_raw)} results…")

    all_products: list = []
    seen_handles: set = set()
    for item in all_raw:
        if stop_event and stop_event.is_set():
            break
        gender = item.pop("_gender", "Women")
        prod   = _build_product(item, gender)
        if prod and prod["Handle"] not in seen_handles:
            seen_handles.add(prod["Handle"])
            all_products.append(prod)

    all_products = _split_large_products(all_products)
    cb(70, f"Built {len(all_products)} products — transforming…", len(all_products))

    if not all_products:
        update_scrape_record(scrape_id, status="failed", products_count=0)
        cb(100, "No products found.")
        return

    rows = transform_to_shopify(all_products)
    cb(85, f"Exporting {len(rows)} CSV rows…", len(all_products))

    export_shopify_csv(rows, CSV_PATH)
    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    cb(95, "Uploading to Supabase…")

    try:
        upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)
    except Exception as e:
        logger.warning(f"[UA] Supabase upload: {e}")

    update_scrape_record(scrape_id, status="completed", products_count=len(all_products))
    cb(100, f"Done — {len(all_products)} products, {len(rows)} CSV rows.", len(all_products))
    logger.info(f"[UA] ✅ Complete → {CSV_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    complete_workflow_underarmour()
