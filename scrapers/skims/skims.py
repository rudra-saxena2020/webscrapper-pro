"""
SKIMS Body Scraper v2.1
========================
Strategy:
  Phase 1 — Shopify Storefront GraphQL (skimsbody.myshopify.com)
             7 collections: clothing, bras, bralettes, intimates,
             underwear, shapewear, skims-sale.
             Fetches product GIDs per collection, then bulk-fetches
             full product + variant data in batches of 100.

  Phase 2 — Build Mirage descriptions, apply taxonomy tags.

  Phase 3 — Transform to Shopify CSV → scraped_files/skims_latest.csv

Format Rules Applied (matching established scrapers):
  PRICING  — "currency": "INR" on every variant dict. GraphQL is called with
             country: "IN" so Shopify returns prices in INR directly (India
             storefront pricing). The pricing engine applies the standard INR
             passthrough with 1.25× markup.
  TAGS     — build_full_tags(title, vendor, gender, p_type,
             extra_tags=["RudraScrapper-skims"]) — full Mirage taxonomy
             string + Shopify dual-verification delete safety tag.
  IMAGES   — Deduplicated product-level image list. Per-variant images
             point to the same deduplicated list. build_mirage_description(raw,
             title, brand, gender) builds the full Mirage HTML description
             including the brand footer.
  GENDER   — "Google Shopping / Gender" + "_gender_refined": True set on every
             product dict so the transformer uses the known gender directly
             without calling Gemini for re-classification.
"""

import os
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

SCRAPER_ID   = "skims"
VENDOR       = "SKIMS"
CURRENCY     = "INR"              # Fetch from India storefront — prices already in INR
BASE_URL     = "https://skims.com"
CSV_PATH     = "scraped_files/skims_latest.csv"

_GRAPHQL_URL = "https://skimsbody.myshopify.com/api/unstable/graphql.json"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131",
    "x-shopify-storefront-access-token": "2efed5e20dc72ac998fdbbc55ae0649e",
}

COLLECTIONS = [
    "clothing", "bras", "bralettes", "intimates",
    "underwear", "shapewear", "skims-sale",
    "sale-clothing", "best-of-sale", "sale-loungewear",
]

_COLLECTION_QUERY = """
query($handle: String!, $cursor: String) {
  collectionByHandle(handle: $handle) {
    products(first: 250, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges { node { id } }
    }
  }
}
"""

_PRODUCT_QUERY = """
query($ids: [ID!]!, $country: CountryCode!, $lang: LanguageCode!)
@inContext(country: $country, language: $lang) {
  nodes(ids: $ids) {
    ... on Product {
      id title handle availableForSale
      descriptionHtml productType vendor
      images(first: 30) { edges { node { originalSrc } } }
      options { name values }
      variants(first: 100) {
        edges {
          node {
            id sku availableForSale quantityAvailable
            price { amount }
            compareAtPrice { amount }
            selectedOptions { name value }
            image { originalSrc }
          }
        }
      }
    }
  }
}
"""

def _gql(query: str, variables: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.post(
                _GRAPHQL_URL, headers=_HEADERS,
                json={"query": query, "variables": variables},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.warning(f"[SKIMS] GQL error: {e}")
    return {}


def _fetch_collection_ids(handle: str) -> list[str]:
    ids: list = []
    cursor = None
    while True:
        data = _gql(_COLLECTION_QUERY, {"handle": handle, "cursor": cursor})
        coll = (data.get("data") or {}).get("collectionByHandle")
        if not coll:
            break
        for e in coll["products"]["edges"]:
            ids.append(e["node"]["id"])
        pi = coll["products"]["pageInfo"]
        if not pi["hasNextPage"]:
            break
        cursor = pi["endCursor"]
        time.sleep(0.3)
    return ids


def _fetch_products_batched(gids: list[str]) -> list[dict]:
    results: list = []
    for i in range(0, len(gids), 100):
        batch = gids[i:i + 100]
        data = _gql(_PRODUCT_QUERY, {"ids": batch, "country": "IN", "lang": "EN"})
        nodes = (data.get("data") or {}).get("nodes") or []
        results.extend([n for n in nodes if n])
        time.sleep(0.5)
    return results


def _strip_color_suffix(title: str) -> str:
    """
    SKIMS API returns titles like 'Cotton Jersey T-Shirt | Onyx'.
    Strip the ' | Color' suffix so _merge_color_variants() can group
    all color variants of the same base product together.
    """
    if " | " in title:
        return title.split(" | ")[0].strip()
    return title


def _build_product(raw: dict) -> dict | None:
    if not raw.get("availableForSale"):
        return None
    raw_title = clean_title(raw.get("title", ""))
    if not raw_title:
        return None
    # Strip ` | Color` suffix — color is already captured per-variant.
    # Also extract the suffix itself as a fallback color for products that have
    # no explicit "Color" option (e.g. bra per-color products where the color
    # only lives in the title, e.g. "Weightless Scoop Bra | Espresso").
    title = _strip_color_suffix(raw_title)
    if not title:
        return None
    suffix_color = raw_title.split(" | ")[1].strip() if " | " in raw_title else ""

    vendor = raw.get("vendor") or VENDOR
    p_type = raw.get("productType") or "Apparel"
    # Always generate handle from the clean base title (not raw API handle
    # which includes the color slug e.g. "cotton-jersey-t-shirt-onyx")
    handle = generate_handle(title)
    gender = "Women"

    # ── FORMAT RULE: IMAGES ───────────────────────────────────────────────────
    # Collect ALL images for this colour product.  SKIMS API returns each
    # colour as a separate product with its full gallery (front, back, detail,
    # lifestyle, model, zoom).  Every image is stored in variant["images"] and
    # written to "Variant All Images" (pipe-separated) so the publisher can
    # POST each one with variant_ids — ensuring the correct photos appear when
    # a colour swatch is selected.  Never limit gallery size.
    seen_imgs: set = set()
    prod_images: list = []
    for e in raw.get("images", {}).get("edges", []):
        src = e["node"].get("originalSrc", "")
        if src and src not in seen_imgs:
            seen_imgs.add(src)
            prod_images.append(src)

    # Map option name → axis
    # SKIMS bras have 3 options: Color (actual colour name), Band Size (30/32/…),
    # Cup Size (A/B/C/D/DD/…).  Regular apparel has 2: Color + Size.
    color_key    = None
    size_key     = None
    band_size_key = None
    cup_size_key  = None
    for opt in raw.get("options", []):
        n = opt["name"].lower()
        if any(k in n for k in ("color", "colour", "shade")):
            color_key = opt["name"]
        elif "band" in n and "size" in n:
            band_size_key = opt["name"]   # e.g. "Band Size"
        elif "cup" in n and "size" in n:
            cup_size_key = opt["name"]    # e.g. "Cup Size"
        elif "size" in n:
            size_key = opt["name"]        # generic "Size" for normal apparel

    # ── FORMAT RULE: PRICING ─────────────────────────────────────────────────
    variants: list = []
    seen_keys: set = set()
    for e in raw.get("variants", {}).get("edges", []):
        v = e["node"]
        if not v.get("availableForSale"):
            continue
        qty = v.get("quantityAvailable")
        if qty is not None and qty <= 0:
            continue
        price = float((v.get("price") or {}).get("amount") or 0)
        if price <= 0:
            continue

        cap_raw = v.get("compareAtPrice")
        cap     = float((cap_raw or {}).get("amount") or 0) if cap_raw else 0

        opts_vals: dict = {so["name"]: so["value"] for so in v.get("selectedOptions", [])}
        # Use explicit Color option if present; fall back to the title suffix
        # color (e.g. "Espresso" from "Weightless Scoop Bra | Espresso") for
        # products like bras that expose each color as a separate API product
        # with no Color option of their own.
        color = opts_vals.get(color_key, "") if color_key else suffix_color

        # ── BRA SIZE HANDLING ────────────────────────────────────────────────
        # For bras: encode BOTH Band Size and Cup Size as "{band} / {cup}" so
        # neither axis is discarded (e.g. "32 / B").  The dedup key likewise
        # uses the full combined size so distinct band+cup combos aren't folded.
        if band_size_key and cup_size_key:
            band = opts_vals.get(band_size_key, "") or ""
            cup  = opts_vals.get(cup_size_key, "") or ""
            if band and cup:
                size = f"{band} / {cup}"
            elif band:
                size = band
            elif cup:
                size = cup
            else:
                size = "One Size"
        elif size_key:
            size = opts_vals.get(size_key, "") or "One Size"
        else:
            size = v.get("title") or "One Size"

        sku = (v.get("sku") or "").strip()

        # ── FORMAT RULE: SIZES (from all_sizes= env var) ────────────────────
        # SKIMS sells intimates/apparel — exempt from UK footwear size rule.
        if not (band_size_key and cup_size_key):
            size = validate_size(size or "One Size", is_footwear=False) or "One Size"

        # For bras: dedup by (color, full-size) — size now encodes both band and
        # cup as "{band} / {cup}", so each distinct combination is preserved.
        # For regular apparel: dedup by SKU (unique per variant).
        if band_size_key and cup_size_key:
            key = f"{color}_{size}"
        else:
            key = sku or f"{color}_{size}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # ── PER-VARIANT IMAGE ────────────────────────────────────────────────
        # All sizes of a given colour share the full image set (ALL images).
        # These are written to Variant All Images (pipe-separated) in the CSV
        # so the publisher can link every image to this colour's variant_ids.
        variant_images: list = list(prod_images)  # ALL images for this colour

        variants.append({
            "Variant SKU":              sku,
            "size":                     size or "One Size",
            "color":                    color,
            "Variant Price":            price,
            "Variant Compare At Price": cap,
            "currency":                 CURRENCY,   # ← PRICING RULE
            "images":                   variant_images,
        })

    if not variants:
        return None

    # ── FORMAT RULE: DESCRIPTION ─────────────────────────────────────────────
    raw_desc  = sanitize_html_description(raw.get("descriptionHtml") or "")
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


def complete_workflow_skims(progress_callback=None, stop_event=None):
    logger.info(
        f"[SKIMS] Format rules loaded — "
        f"sizes={len(VALID_UK_SIZES)} | tag_rows={len(TAG_ROWS)} | "
        f"pricing=offset+{PRICING['RATE_OFFSET']} fee+{PRICING['FIXED_FEE']} "
        f"INR×1.25 "
        f"GBP×{PRICING['MARKUP_GBP_BELOW']}"
    )
    scrape_id = start_scrape_record(SCRAPER_ID)

    def cb(pct, msg, count=None):
        logger.info(f"[SKIMS] {pct}% — {msg}")
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

    cb(5, "Fetching SKIMS collection IDs…")

    all_gids: list = []
    seen_gids: set = set()
    for i, col in enumerate(COLLECTIONS):
        if stop_event and stop_event.is_set():
            break
        pct = 5 + int(i / len(COLLECTIONS) * 30)
        cb(pct, f"Collection: {col}…")
        for gid in _fetch_collection_ids(col):
            if gid not in seen_gids:
                seen_gids.add(gid)
                all_gids.append(gid)
        logger.info(f"[SKIMS] {col}: {len(seen_gids)} unique GIDs so far")

    cb(35, f"Fetching {len(all_gids)} product details…")

    raw_products = _fetch_products_batched(all_gids)
    cb(60, f"Building {len(raw_products)} products…")

    all_products: list = []
    for raw in raw_products:
        if stop_event and stop_event.is_set():
            break
        prod = _build_product(raw)
        if prod:
            # Do NOT dedup by handle here — same-title products with different
            # colors will share the same handle after _strip_color_suffix().
            # _merge_color_variants() in the transformer collapses them correctly.
            all_products.append(prod)

    cb(72, f"Built {len(all_products)} products — transforming…", len(all_products))

    if not all_products:
        update_scrape_record(scrape_id, status="failed", products_count=0)
        cb(100, "No products found.")
        return

    rows = transform_to_shopify(all_products)
    cb(86, f"Exporting {len(rows)} CSV rows…", len(all_products))

    export_shopify_csv(rows, CSV_PATH)
    upsert_all_product_data(all_products, BASE_URL, CURRENCY)
    cb(95, "Uploading to Supabase…")

    try:
        upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)
    except Exception as e:
        logger.warning(f"[SKIMS] Supabase upload: {e}")

    update_scrape_record(scrape_id, status="completed", products_count=len(all_products))
    cb(100, f"Done — {len(all_products)} products, {len(rows)} CSV rows.", len(all_products))
    logger.info(f"[SKIMS] ✅ Complete → {CSV_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    complete_workflow_skims()
