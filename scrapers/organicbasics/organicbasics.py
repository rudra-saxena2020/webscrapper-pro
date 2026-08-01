"""
Organic Basics UK Scraper v2.1
================================
Strategy:
  Phase 1 — Shopify products.json API (no auth required)
             uk.organicbasics.com/collections/{handle}/products.json
             Paginates 250/page until empty.
             Collections: all-mens-products + all-womens-products

  Phase 2 — Build Mirage description + apply Mirage taxonomy tags.

  Phase 3 — Transform to Shopify CSV, export → scraped_files/organicbasics_latest.csv

Format Rules Applied (matching established scrapers):
  PRICING  — "currency": "GBP" on every variant dict so auto_fix_variant
             uses the correct GBP→INR formula via pricing_engine.
  TAGS     — build_full_tags(title, vendor, gender, p_type,
             extra_tags=["RudraScrapper-organicbasics"]) — returns a
             comma-separated string including all Mirage taxonomy tags
             AND the safety tag required by the dual-verification Shopify
             delete system.
  IMAGES   — Deduplicated product-level list + per-variant image pointer.
             build_mirage_description(raw_desc, title, brand, gender) builds
             the full Mirage HTML description including the brand footer.
  GENDER   — "Google Shopping / Gender" + "_gender_refined": True set on every
             product dict so the transformer uses the known gender directly
             without calling Gemini for re-classification.
"""

import os
import re
import sys
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

SCRAPER_ID = "organicbasics"
VENDOR     = "Organic Basics"
CURRENCY   = "GBP"               # UK store — GBP → INR via pricing_engine
BASE_URL   = "https://uk.organicbasics.com"
CSV_PATH   = "scraped_files/organicbasics_latest.csv"

COLLECTIONS = [
    {"handle": "all-mens-products",  "gender": "Men"},
    {"handle": "all-womens-products", "gender": "Women"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": BASE_URL,
}


# ── Phase 1: Fetch from Shopify products.json ─────────────────────────────────

def _fetch_collection(handle: str) -> list:
    products: list = []
    page = 1
    while True:
        url = f"{BASE_URL}/collections/{handle}/products.json?limit=250&page={page}"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=25)
            r.raise_for_status()
            batch = r.json().get("products", [])
            if not batch:
                break
            products.extend(batch)
            logger.info(f"[OB] {handle} p{page}: {len(batch)} ({len(products)} total)")
            if len(batch) < 250:
                break
            page += 1
            time.sleep(0.4)
        except Exception as e:
            logger.warning(f"[OB] {handle} p{page} error: {e}")
            break
    return products


# ── Phase 2: Build product dict ───────────────────────────────────────────────

def _option_names(product: dict) -> tuple[str, str]:
    opts = product.get("options", [])
    n1 = (opts[0]["name"] if len(opts) > 0 else "").lower()
    n2 = (opts[1]["name"] if len(opts) > 1 else "").lower()
    return n1, n2


def _clean_type(raw: str) -> str:
    """'Bottoms_Pants' → 'Pants'."""
    if not raw:
        return ""
    return raw.split("_")[-1].strip() if "_" in raw else raw.strip()


def _build_product(raw: dict, gender: str) -> dict | None:
    title = clean_title(raw.get("title", ""))
    if not title:
        return None

    vendor   = raw.get("vendor") or VENDOR
    p_type   = _clean_type(raw.get("product_type") or "")
    handle   = raw.get("handle") or generate_handle(title)

    # Discover which axis is color vs size
    o1_name, o2_name = _option_names(raw)
    o1_is_color = any(k in o1_name for k in ("color", "colour", "shade"))
    o1_is_size  = any(k in o1_name for k in ("size", "fit"))

    # ── FORMAT RULE: IMAGES ───────────────────────────────────────────────────
    # Deduplicate product-level images. Variant images point into this list.
    seen_imgs: set = set()
    prod_images: list = []
    for img in raw.get("images", []):
        src = img.get("src", "")
        if src and src not in seen_imgs:
            seen_imgs.add(src)
            prod_images.append(src)

    # Build image_id → url map for variant-specific image lookup
    img_by_id = {img["id"]: img["src"] for img in raw.get("images", []) if img.get("id")}

    # ── FORMAT RULE: PRICING ─────────────────────────────────────────────────
    # "currency" key required on every variant — defaults to "GBP" in
    # auto_fix_variant if absent, but we set it explicitly for clarity.
    variants: list = []
    seen_keys: set = set()
    for v in raw.get("variants", []):
        price = float(v.get("price") or 0)
        if price <= 0:
            continue

        opt1 = (v.get("option1") or "").strip()
        opt2 = (v.get("option2") or "").strip()

        if o1_is_color:
            color, size = opt1, opt2
        elif o1_is_size:
            color, size = opt2, opt1
        else:
            color, size = opt1, opt2   # fallback: treat opt1=color, opt2=size

        # ── FORMAT RULE: SIZES (from all_sizes= env var) ────────────────────
        # OB sells apparel (XS/S/M/L/XL) — exempt from UK footwear size rule.
        # validate_size(size, is_footwear=False) always passes apparel sizes through.
        size = validate_size(size or "One Size", is_footwear=False) or "One Size"

        key = v.get("sku") or f"{color}_{size}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        cap = float(v["compare_at_price"]) if v.get("compare_at_price") else 0

        # Per-variant image: use the image assigned by Shopify if present,
        # otherwise fall back to featured_image, then the full product gallery.
        v_img = []
        if v.get("image_id") and v["image_id"] in img_by_id:
            v_img = [img_by_id[v["image_id"]]]
        if not v_img and isinstance(v.get("featured_image"), dict):
            src = v["featured_image"].get("src", "")
            if src:
                v_img = [src]
        if not v_img:
            v_img = prod_images

        variants.append({
            "Variant SKU":              (v.get("sku") or "").strip(),
            "size":                     size or "One Size",
            "color":                    color,
            "Variant Price":            price,
            "Variant Compare At Price": cap,
            "currency":                 CURRENCY,   # ← PRICING RULE
            "images":                   v_img,
        })

    if not variants:
        return None

    # ── FORMAT RULE: DESCRIPTION ─────────────────────────────────────────────
    raw_desc  = sanitize_html_description(raw.get("body_html") or "")
    body_html = build_mirage_description(raw_desc, title, VENDOR, gender)

    # ── FORMAT RULE: TAGS (from all_Tags= env var) ───────────────────────────
    # tag_lookup reads the canonical Mirage taxonomy from the all_Tags= env var.
    # Results are merged into build_full_tags so the final string includes both
    # the env-var taxonomy tags AND the RudraScrapper safety tag.
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

def complete_workflow_organicbasics(progress_callback=None, stop_event=None):
    logger.info(
        f"[OB] Format rules loaded — "
        f"sizes={len(VALID_UK_SIZES)} | tag_rows={len(TAG_ROWS)} | "
        f"pricing=offset+{PRICING['RATE_OFFSET']} fee+{PRICING['FIXED_FEE']} "
        f"USD×{PRICING['MARKUP_USD_BELOW']}/{PRICING['MARKUP_USD_ABOVE']} "
        f"GBP×{PRICING['MARKUP_GBP_BELOW']}"
    )
    scrape_id = start_scrape_record(SCRAPER_ID)

    def cb(pct, msg, count=None):
        logger.info(f"[OB] {pct}% — {msg}")
        if progress_callback:
            try:
                progress_callback(pct, msg, count)
            except Exception:
                pass

    def _heartbeat():
        while not (stop_event and stop_event.is_set()):
            heartbeat_scrape_record(scrape_id)
            time.sleep(15)

    threading.Thread(target=_heartbeat, daemon=True).start()

    cb(5, "Fetching Organic Basics UK collections…")

    all_products: list = []
    seen_handles: set = set()

    for i, col in enumerate(COLLECTIONS):
        if stop_event and stop_event.is_set():
            break
        cb(10 + i * 20, f"Fetching {col['handle']}…")
        raw_list = _fetch_collection(col["handle"])
        logger.info(f"[OB] {col['handle']}: {len(raw_list)} raw products")
        for raw in raw_list:
            if stop_event and stop_event.is_set():
                break
            prod = _build_product(raw, col["gender"])
            if prod and prod["Handle"] not in seen_handles:
                seen_handles.add(prod["Handle"])
                all_products.append(prod)

    cb(55, f"Built {len(all_products)} products — transforming to Shopify CSV…")

    if not all_products:
        update_scrape_record(scrape_id, status="failed", products_count=0)
        cb(100, "No products found — check collection handles.")
        return

    rows = transform_to_shopify(all_products)
    cb(75, f"Exporting {len(rows)} CSV rows…", len(all_products))

    export_shopify_csv(rows, CSV_PATH)
    logger.info(f"[OB] CSV → {CSV_PATH}")

    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    cb(90, "Uploading CSV to Supabase…")

    try:
        upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)
    except Exception as e:
        logger.warning(f"[OB] Supabase upload: {e}")

    update_scrape_record(scrape_id, status="completed", products_count=len(all_products))
    cb(100, f"Done — {len(all_products)} products, {len(rows)} CSV rows.", len(all_products))
    logger.info(f"[OB] ✅ Complete → {CSV_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    complete_workflow_organicbasics()
