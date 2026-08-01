"""
Kate Spade Outlet Scraper v2.0
================================
Strategy:
  Phase 1 — Fetch product URLs from sitemap via ScraperAPI.
             Extract product IDs from URLs.

  Phase 2 — Batch-query katespadeoutlet.com/api/get-products via ScraperAPI
             (50 IDs per request). No browser automation needed.

  Phase 3 — Filter by target departments, clean data.

  Phase 4 — Build Mirage descriptions, apply taxonomy tags.

  Phase 5 — Transform to Shopify CSV → scraped_files/katespadeoutlet_latest.csv

Notes:
  - Prices in USD → INR via pricing_engine.
  - Women's-focused (handbags, accessories, jewelry, shoes, wallets).
  - Uses ScraperAPI (basic plan) for IP rotation.
"""

import os
import sys
import re
import time
import json
import logging
import xml.etree.ElementTree as ET

import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import (
    build_full_tags, build_mirage_description,
)

logger = logging.getLogger(__name__)

SCRAPER_ID  = "katespadeoutlet"
VENDOR      = "Kate Spade"
CURRENCY    = "USD"
BASE_URL    = "https://www.katespadeoutlet.com"
SITEMAP_URL = f"{BASE_URL}/sitemap_0-product.xml"
CSV_PATH    = "scraped_files/katespadeoutlet_latest.csv"

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

# Departments to include (from user URL params)
TARGET_DEPARTMENTS = {
    "accessories", "handbags", "jewelry", "shoes",
    "wallets", "wristlets", "wallets & wristlets",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
    "Accept": "application/json, text/html, */*",
}


def _scraperapi_get(url, timeout=45):
    if SCRAPER_API_KEY:
        return requests.get(
            "http://api.scraperapi.com",
            params={"api_key": SCRAPER_API_KEY, "url": url, "country_code": "us"},
            timeout=timeout,
        )
    return requests.get(url, headers=_HEADERS, timeout=timeout)


def _extract_product_id(url: str) -> str:
    last = url.rstrip("/").split("/")[-1]
    base = last.split(".html")[0]
    pid = base.split("-")[0] if "-" in base else base
    return re.sub(r"[^A-Za-z0-9]", "", pid).upper()


def _fetch_sitemap_urls() -> list:
    try:
        r = _scraperapi_get(SITEMAP_URL, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        return [loc.text for loc in root.findall(".//ns:url/ns:loc", ns) if loc.text]
    except Exception as e:
        logger.error(f"[KSO] Sitemap fetch failed: {e}")
        return []


def _is_target_url(url: str) -> bool:
    lower = url.lower()
    return "/products/" in lower


def _fetch_product_batch(ids: list) -> list:
    ids_str = ",".join(ids)
    api_url = f"{BASE_URL}/api/get-products?ids={ids_str}&includeInventory=true"
    try:
        r = _scraperapi_get(api_url, timeout=45)
        r.raise_for_status()
        data = r.json()
        return data.get("productsData", [])
    except Exception as e:
        logger.warning(f"[KSO] Batch fetch failed: {e}")
        return []


def _department_matches(product) -> bool:
    breadcrumbs = product.get("breadcrumbs", [])
    item_cats = product.get("item_category", [])
    all_text = " ".join(
        str(bc.get("htmlValue", "") or bc.get("displayName", "")).lower()
        for bc in breadcrumbs
    ) + " " + " ".join(str(c).lower() for c in item_cats)

    for dept in TARGET_DEPARTMENTS:
        if dept.lower() in all_text:
            return True
    return False


def _clean_product(product) -> dict | None:
    if product.get("hitType") not in ("master", "variation_group"):
        return None

    handle = (product.get("id") or "").strip()
    title  = (product.get("name") or "").strip()
    if not handle or not title:
        return None

    raw_desc = product.get("longDescription", "") or ""
    description = build_mirage_description(raw_desc, title, VENDOR, "women", "accessories")

    # Product type from breadcrumbs
    item_cats = product.get("item_category", [])
    ptype = item_cats[-1].split("&")[0].strip() if item_cats else "Accessories"

    # Broad category for tags
    ptype_lower = ptype.lower()
    if "bag" in ptype_lower or "handbag" in ptype_lower:
        broad = "bags"
    elif "shoe" in ptype_lower or "heel" in ptype_lower or "flat" in ptype_lower or "sandal" in ptype_lower:
        broad = "footwear"
    elif "jewel" in ptype_lower:
        broad = "accessories"
    else:
        broad = "accessories"

    tags = build_full_tags(
        title, VENDOR, "women", broad,
        extra_tags=[f"RudraScrapper-{SCRAPER_ID}", "mirage-curated", "premium"],
    )

    # Images
    all_images = []
    seen_img = set()
    for group in product.get("imageGroups", []):
        for img in group.get("images", []):
            src = img.get("src", "")
            if src and not src.lower().endswith(".mp4") and src not in seen_img:
                all_images.append(src)
                seen_img.add(src)
    if not all_images:
        for vg in product.get("variationGroup", []):
            for group in vg.get("imageGroups", []):
                for img in group.get("images", []):
                    src = img.get("src", "")
                    if src and not src.lower().endswith(".mp4") and src not in seen_img:
                        all_images.append(src)
                        seen_img.add(src)
            if all_images:
                break

    # Color lookup
    color_map = {c.get("id", ""): c.get("text", "") for c in product.get("colors", [])}

    # Variants
    variants = []
    seen_vars = set()
    for variant in product.get("variant", []):
        avail = (variant.get("offers") or {}).get("availability", "")
        if "InStock" not in avail and "LimitedAvailability" not in avail:
            continue

        sku = (variant.get("id") or "").strip()
        pricing_info = variant.get("pricingInfo") or []
        sale_price = list_price = 0.0
        if pricing_info and isinstance(pricing_info, list) and pricing_info[0]:
            sales = pricing_info[0].get("sales") or {}
            lst   = pricing_info[0].get("list")  or {}
            sale_price = float(sales.get("value", 0) or 0)
            list_price = float(lst.get("value", 0) or 0)

        if not sale_price:
            continue

        size     = (variant.get("variationValues") or {}).get("size") or ""
        color_id = (variant.get("variationValues") or {}).get("color", "")
        color    = color_map.get(color_id, "")
        if not color:
            color = (variant.get("customAttributes") or {}).get("c_colorVal", "")

        # Inventory: Kate Spade API returns inventory per variant.
        # Zero or missing inventory means the size is out of stock.
        inv_qty = 0
        raw_inv = variant.get("inventory") or variant.get("inventoryQuantity") or 0
        try:
            inv_qty = int(float(raw_inv))
        except (ValueError, TypeError):
            inv_qty = 0

        # Skip zero-inventory variants (OOS)
        if inv_qty <= 0:
            continue

        vkey = (sku, size, color)
        if vkey in seen_vars:
            continue
        seen_vars.add(vkey)

        # Variant image: first image from variant's imageGroups if available
        vimg = all_images[0] if all_images else ""
        for vg in product.get("variationGroup", []):
            for ig in vg.get("imageGroups", []):
                imgs = ig.get("images", [])
                if imgs:
                    vimg = imgs[0].get("src", vimg)
                    break
            if vimg != (all_images[0] if all_images else ""):
                break

        # Per-color images: pick images from matching color variationGroup
        color_images = []
        for vg in product.get("variationGroup", []):
            vg_color = (vg.get("variationValues") or {}).get("color", "")
            if vg_color == color_id:
                for ig in vg.get("imageGroups", []):
                    for img in ig.get("images", []):
                        src = img.get("src", "")
                        if src and src not in color_images:
                            color_images.append(src)
                break
        if not color_images:
            color_images = all_images[:5] if all_images else ([vimg] if vimg else [])

        variants.append({
            "sku": sku,
            "color": color,
            "size": size,
            "Variant Price": sale_price,
            "Variant Compare At Price": list_price if list_price > sale_price else 0,
            "currency": CURRENCY,
            "Variant Inventory Qty": inv_qty,
            "images": color_images,
        })

    if not variants:
        return None

    # Append available sizes to product description
    avail_sizes = list(dict.fromkeys(
        v["size"] for v in variants if v.get("size") and v["size"] not in ("", "OS", "One Size")
    ))
    if avail_sizes:
        description = description + f'<p><strong>Available Sizes:</strong> {", ".join(avail_sizes)}</p>'

    return {
        "handle":                    handle,
        "Title":                     title,
        "Vendor":                    VENDOR,
        "Body (HTML)":               description,
        "Type":                      ptype,
        "Tags":                      tags,
        "Google Shopping / Gender":  "women",
        "currency":                  CURRENCY,
        "images":                    all_images[:25],
        "color_image_map":           {},
        "variants":                  variants,
        "_gender_refined":           True,
    }


def complete_workflow_kate_outlet(progress_callback=None, stop_event=None):
    def _cb(pct, msg, cnt=0):
        if progress_callback:
            progress_callback(pct, msg, cnt)

    if not SCRAPER_API_KEY:
        _cb(100, "Kate Spade Outlet: SCRAPER_API_KEY not set — skipping.")
        return

    scrape_id = start_scrape_record(SCRAPER_ID)
    _cb(2, "Kate Spade Outlet: fetching sitemap…")

    urls = _fetch_sitemap_urls()
    product_urls = [u for u in urls if _is_target_url(u)]
    logger.info(f"[KSO] {len(product_urls)} product URLs from sitemap")
    _cb(8, f"[KSO] {len(product_urls)} product URLs found — extracting IDs…")

    ids = list({_extract_product_id(u) for u in product_urls if _extract_product_id(u)})
    logger.info(f"[KSO] {len(ids)} unique product IDs")
    _cb(12, f"[KSO] {len(ids)} unique IDs — fetching product data…")

    if not ids:
        _cb(100, "Kate Spade Outlet: no product IDs found.")
        update_scrape_record(scrape_id, "completed", 0)
        return

    all_raw = []
    batch_size = 50
    total_batches = (len(ids) + batch_size - 1) // batch_size

    for bi, i in enumerate(range(0, len(ids), batch_size)):
        if stop_event and stop_event.is_set():
            break
        batch = ids[i:i+batch_size]
        raw = _fetch_product_batch(batch)
        all_raw.extend(raw)
        pct = 12 + int((bi + 1) / total_batches * 55)
        _cb(pct, f"[KSO] Fetched batch {bi+1}/{total_batches} ({len(all_raw)} products)…")
        time.sleep(1.0)
        if bi % 5 == 0:
            heartbeat_scrape_record(scrape_id)

    _cb(70, f"[KSO] {len(all_raw)} raw products — filtering & processing…")

    all_products = []
    seen = set()
    for raw in all_raw:
        if stop_event and stop_event.is_set():
            break
        if not _department_matches(raw):
            continue
        cleaned = _clean_product(raw)
        if cleaned and cleaned["handle"] not in seen:
            all_products.append(cleaned)
            seen.add(cleaned["handle"])

    _cb(80, f"[KSO] {len(all_products)} products — transforming to CSV…")
    rows = transform_to_shopify(all_products)
    export_shopify_csv(rows, CSV_PATH)

    _cb(90, f"[KSO] CSV saved — uploading to DB…")
    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)

    update_scrape_record(scrape_id, "completed", len(all_products))
    _cb(100, f"Kate Spade Outlet: done ✅ — {len(all_products)} products", len(all_products))
    logger.info(f"[KSO] Complete — {len(all_products)} products → {CSV_PATH}")
