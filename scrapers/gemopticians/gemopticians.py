"""
GEM Opticians Scraper v2.0
===========================
Strategy:
  Phase 1 — Shopify Storefront GraphQL (gem-opticians-delhi.myshopify.com)
             Collections: mens-sunglasses, womens-sunglasses
             Fetches all product GIDs, then bulk-fetches product data.
             Filters by target vendor lists provided by user.

  Phase 2 — Build Mirage descriptions, apply taxonomy tags.

  Phase 3 — Transform to Shopify CSV → scraped_files/gemopticians_latest.csv

Notes:
  - Prices are already in INR (Indian store). No currency conversion needed.
  - Products are single-variant (Default Title) sunglasses.
  - Vendor = luxury brand (Prada, Gucci, etc.)
"""

import os
import sys
import re
import time
import logging

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

SCRAPER_ID = "gemopticians"
CURRENCY   = "INR"
BASE_URL   = "https://gemopticians.com"
CSV_PATH   = "scraped_files/gemopticians_latest.csv"

_GRAPHQL_URL = "https://gem-opticians-delhi.myshopify.com/api/unstable/graphql.json"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
    "x-shopify-storefront-access-token": "2dd4d7d5f8452bb76121dfd4b94f7bc7",
}

# Vendor allowlists per collection (case-insensitive match)
_MENS_VENDORS = {
    "alexander mcqueen", "balenciaga", "balmain", "bottega veneta", "burberry",
    "celine", "christian dior", "coach", "dolce & gabbana", "fendi", "ferragamo",
    "gucci", "maui jim", "off-white", "prada", "saint laurent", "tom ford", "versace",
}
_WOMENS_VENDORS = {
    "alaia", "alexander mcqueen", "balenciaga", "balmain", "bottega veneta", "burberry",
    "bvlgari", "chloe", "coach", "dolce & gabbana", "fendi", "ferragamo", "gucci",
    "jacquemus", "jimmy choo", "kate spade", "loewe", "marc jacobs", "maui jim",
    "maybach", "michael kors", "miu miu", "montblanc", "moschino", "off-white",
    "philipp plein", "prada", "prada sports", "saint laurent", "swarovski",
    "tiffany & co.", "tom ford", "tory burch", "valentino", "versace",
}

# Collection handle → (gender, vendor_set)
COLLECTIONS = [
    ("mens-sunglasses",   "men",   _MENS_VENDORS),
    ("womens-sunglasses", "women", _WOMENS_VENDORS),
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

_PRODUCTS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product {
      id handle title vendor availableForSale
      descriptionHtml productType tags
      images(first: 20) {
        edges { node { url altText } }
      }
      variants(first: 10) {
        edges {
          node {
            id sku availableForSale
            price { amount currencyCode }
            compareAtPrice { amount currencyCode }
            selectedOptions { name value }
            image { url }
          }
        }
      }
    }
  }
}
"""


def _gql(query, variables=None, retries=3):
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(retries):
        try:
            r = requests.post(_GRAPHQL_URL, headers=_HEADERS, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def _fetch_collection_ids(handle: str) -> list:
    ids = []
    cursor = None
    while True:
        data = _gql(_COLLECTION_QUERY, {"handle": handle, "cursor": cursor})
        col = (data.get("data") or {}).get("collectionByHandle") or {}
        prods = col.get("products") or {}
        for edge in prods.get("edges", []):
            ids.append(edge["node"]["id"])
        pi = prods.get("pageInfo", {})
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
        time.sleep(0.5)
    return ids


def _vendor_matches(vendor: str, allowlist: set) -> bool:
    v = vendor.strip().lower()
    return v in allowlist or any(a in v or v in a for a in allowlist)


def _process_products(raw_nodes, gender, vendor_set, stop_event=None):
    products = []
    seen_handles = set()

    for node in raw_nodes:
        if stop_event and stop_event.is_set():
            break
        if not node or not node.get("availableForSale"):
            continue

        vendor = (node.get("vendor") or "").strip()
        if not _vendor_matches(vendor, vendor_set):
            continue

        title = (node.get("title") or "").strip()
        handle = (node.get("handle") or "").strip()
        if not title or not handle or handle in seen_handles:
            continue
        seen_handles.add(handle)

        raw_desc = node.get("descriptionHtml") or ""
        description = build_mirage_description(raw_desc, title, vendor, gender, "sunglasses")

        tags = build_full_tags(
            title, vendor, gender, "sunglasses",
            extra_tags=[
                f"RudraScrapper-{SCRAPER_ID}", "mirage-curated", "premium",
                "eyewear", "sunglasses", "luxury-eyewear",
            ],
        )

        # Images
        all_images = []
        seen_img = set()
        for e in node.get("images", {}).get("edges", []):
            url = e["node"].get("url", "")
            if url and url not in seen_img:
                all_images.append(url)
                seen_img.add(url)

        # Variants — GEM products are mostly single-variant "Default Title"
        variants = []
        seen_vars = set()
        for e in node.get("variants", {}).get("edges", []):
            v = e["node"]
            if not v.get("availableForSale"):
                continue
            sku = (v.get("sku") or "").strip()
            price_inr = float((v.get("price") or {}).get("amount", 0) or 0)
            compare_inr = float((v.get("compareAtPrice") or {}).get("amount", 0) or 0)

            opt_title = ""
            for opt in v.get("selectedOptions", []):
                if opt["name"].lower() == "title":
                    opt_title = opt["value"]
                    break

            vkey = sku or opt_title or "default"
            if vkey in seen_vars:
                continue
            seen_vars.add(vkey)

            vimg = (v.get("image") or {}).get("url", "") or (all_images[0] if all_images else "")

            variants.append({
                "sku": sku,
                "color": "",
                "size": opt_title if opt_title and opt_title.lower() not in ("default title", "default") else "",
                "Variant Price": price_inr,
                "Variant Compare At Price": compare_inr,
                "currency": CURRENCY,
                "images": all_images,
            })

        if not variants:
            continue

        products.append({
            "handle":                    handle,
            "Title":                     title,
            "Vendor":                    vendor,
            "Body (HTML)":               description,
            "Type":                      "Sunglasses",
            "Tags":                      tags,
            "Google Shopping / Gender":  gender.lower(),
            "currency":                  CURRENCY,
            "images":                    all_images,
            "color_image_map":           {},
            "variants":                  variants,
            "_gender_refined":           True,
        })

    return products


def complete_workflow_gemopticians(progress_callback=None, stop_event=None):
    def _cb(pct, msg, cnt=0):
        if progress_callback:
            progress_callback(pct, msg, cnt)

    scrape_id = start_scrape_record(SCRAPER_ID)
    _cb(2, "GEM Opticians: starting scraper…")

    all_gids = {}   # gid → (gender, vendor_set)

    for idx, (col_handle, gender, vendor_set) in enumerate(COLLECTIONS):
        if stop_event and stop_event.is_set():
            break
        _cb(3 + idx * 5, f"[GEM] Fetching collection: {col_handle}…")
        try:
            gids = _fetch_collection_ids(col_handle)
            for gid in gids:
                if gid not in all_gids:
                    all_gids[gid] = (gender, vendor_set)
            logger.info(f"[GEM] {col_handle}: {len(gids)} GIDs")
        except Exception as e:
            logger.warning(f"[GEM] collection {col_handle} error: {e}")

    total_gids = list(all_gids.keys())
    _cb(15, f"[GEM] {len(total_gids)} total GIDs — fetching product data…")

    if not total_gids:
        _cb(100, "GEM Opticians: no products found.")
        update_scrape_record(scrape_id, "completed", 0)
        return

    raw_nodes = []
    for i in range(0, len(total_gids), 100):
        if stop_event and stop_event.is_set():
            break
        batch_gids = total_gids[i:i+100]
        data = _gql(_PRODUCTS_QUERY, {"ids": batch_gids})
        nodes = (data.get("data") or {}).get("nodes") or []
        raw_nodes.extend([n for n in nodes if n])
        pct = 15 + int((i + len(batch_gids)) / len(total_gids) * 45)
        fetched = min(i + 100, len(total_gids))
        _cb(pct, f"[GEM] Fetched {fetched}/{len(total_gids)} products…")
        time.sleep(0.8)
        heartbeat_scrape_record(scrape_id)

    _cb(62, "[GEM] Filtering vendors & building Mirage descriptions…")
    all_products = []
    seen = set()
    for node in raw_nodes:
        if not node:
            continue
        gid = node.get("id", "")
        gender, vendor_set = all_gids.get(gid, ("unisex", set()))
        prods = _process_products([node], gender, vendor_set, stop_event)
        for p in prods:
            h = p["handle"]
            if h not in seen:
                all_products.append(p)
                seen.add(h)

    _cb(75, f"[GEM] {len(all_products)} products after vendor filter — transforming to CSV…")
    rows = transform_to_shopify(all_products)
    export_shopify_csv(rows, CSV_PATH)

    _cb(88, f"[GEM] CSV saved — uploading to DB…")
    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)

    update_scrape_record(scrape_id, "completed", len(all_products))
    _cb(100, f"GEM Opticians: done ✅ — {len(all_products)} products", len(all_products))
    logger.info(f"[GEM] Complete — {len(all_products)} products → {CSV_PATH}")
