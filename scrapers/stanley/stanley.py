"""
Stanley 1913 Scraper v2.0
=========================
Strategy:
  Phase 1 — Shopify Storefront GraphQL (stanley-pmi.myshopify.com)
             Collections: drinkware, accessories
             Fetches product GIDs per collection, then bulk-fetches
             full product + variant + image data in batches of 100.

  Phase 2 — Build Mirage descriptions, apply taxonomy tags.

  Phase 3 — Transform to Shopify CSV → scraped_files/stanley1913_latest.csv
"""

import os
import sys
import re
import time
import logging
import threading

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

SCRAPER_ID = "stanley1913"
VENDOR     = "Stanley 1913"
CURRENCY   = "USD"
BASE_URL   = "https://www.stanley1913.com"
CSV_PATH   = "scraped_files/stanley1913_latest.csv"

_GRAPHQL_URL = "https://stanley-pmi.myshopify.com/api/unstable/graphql.json"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
    "x-shopify-storefront-access-token": "2a1ffada6512bf238885987120eba877",
}

# Collection handle → (gender, broad_type)
COLLECTIONS = [
    ("drinkware",   "unisex", "accessories"),
    ("accessories", "unisex", "accessories"),
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
      options { name values }
      images(first: 30) {
        edges { node { id url altText } }
      }
      variants(first: 250) {
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
            gid = edge["node"]["id"]
            ids.append(gid)
        pi = prods.get("pageInfo", {})
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
        time.sleep(0.5)
    return ids


def _fetch_products_batch(gids: list) -> list:
    results = []
    for i in range(0, len(gids), 100):
        batch = gids[i:i+100]
        data = _gql(_PRODUCTS_QUERY, {"ids": batch})
        nodes = (data.get("data") or {}).get("nodes") or []
        results.extend([n for n in nodes if n])
        time.sleep(0.8)
    return results


def _clean_title(raw: str) -> str:
    t = re.sub(r'\|.*$', '', raw).strip()
    t = re.sub(r'\s*\d+\s*(oz|OZ|Oz)\b', '', t).strip()
    return t


def _process_products(raw_nodes, gender, broad_type, stop_event=None):
    products = []
    seen_handles = set()

    for node in raw_nodes:
        if stop_event and stop_event.is_set():
            break
        if not node or not node.get("availableForSale"):
            continue

        raw_title = node.get("title", "")
        title = _clean_title(raw_title)
        if not title:
            continue

        # Canonical handle: strip oz suffix for grouping
        orig_handle = node.get("handle", "")
        handle = re.sub(r'-\d+-?oz(?:-[a-zA-Z0-9-]+)?$|-?\d+-?pack$', '', orig_handle, flags=re.IGNORECASE).rstrip('-')
        if not handle:
            handle = orig_handle

        if handle in seen_handles:
            continue
        seen_handles.add(handle)

        raw_desc = node.get("descriptionHtml") or ""
        description = build_mirage_description(raw_desc, title, VENDOR, gender, broad_type)

        # Product type
        ptype = node.get("productType", "") or ""
        ptype = re.sub(r'\b\d+\s*oz\b', '', ptype, flags=re.IGNORECASE).strip()
        if not ptype or ptype.lower() == "normal":
            ptype = "Accessories"

        tags = build_full_tags(
            title, VENDOR, gender, broad_type,
            extra_tags=[f"RudraScrapper-{SCRAPER_ID}", "mirage-curated", "premium"],
        )

        # Images
        all_images = []
        seen_img = set()
        for e in node.get("images", {}).get("edges", []):
            url = e["node"].get("url", "")
            if url and url not in seen_img:
                all_images.append(url)
                seen_img.add(url)

        # Color → images map
        color_image_map = {}
        for e in node.get("variants", {}).get("edges", []):
            v = e["node"]
            color = ""
            for opt in v.get("selectedOptions", []):
                if opt["name"].lower() in ("color", "colour"):
                    color = opt["value"]
                    break
            vimg = (v.get("image") or {}).get("url", "")
            if color and vimg:
                color_image_map.setdefault(color, [])
                if vimg not in color_image_map[color]:
                    color_image_map[color].append(vimg)

        # Variants
        variants = []
        seen_vars = set()
        for e in node.get("variants", {}).get("edges", []):
            v = e["node"]
            if not v.get("availableForSale"):
                continue
            sku = v.get("sku", "") or ""
            price_raw = float((v.get("price") or {}).get("amount", 0) or 0)
            compare_raw = float((v.get("compareAtPrice") or {}).get("amount", 0) or 0)

            color, size = "", ""
            for opt in v.get("selectedOptions", []):
                n = opt["name"].lower()
                if n in ("color", "colour"):
                    color = opt["value"]
                elif n in ("size", "capacity"):
                    size = opt["value"]

            if not size:
                m = re.search(r'(\d+)\s*(oz|OZ)', raw_title)
                if m:
                    size = m.group(1) + "oz"

            vkey = (sku or color or size, color, size)
            if vkey in seen_vars:
                continue
            seen_vars.add(vkey)

            vimg = (v.get("image") or {}).get("url", "")

            variants.append({
                "sku": sku,
                "color": color,
                "size": size,
                "Variant Price": price_raw,
                "Variant Compare At Price": compare_raw,
                "currency": CURRENCY,
                "images": [vimg] if vimg else (all_images[:1] if all_images else []),
            })

        if not variants:
            continue

        products.append({
            "handle":                    handle,
            "Title":                     title,
            "Vendor":                    VENDOR,
            "Body (HTML)":               description,
            "Type":                      ptype,
            "Tags":                      tags,
            "Google Shopping / Gender":  gender.lower(),
            "currency":                  CURRENCY,
            "images":                    all_images[:20],
            "color_image_map":           color_image_map,
            "variants":                  variants,
            "_gender_refined":           True,
        })

    return products


def complete_workflow_stanley1913(progress_callback=None, stop_event=None):
    def _cb(pct, msg, cnt=0):
        if progress_callback:
            progress_callback(pct, msg, cnt)

    scrape_id = start_scrape_record(SCRAPER_ID)
    _cb(2, "Stanley 1913: starting scraper…")

    all_gids = {}   # gid → (gender, broad_type)

    for idx, (handle, gender, broad_type) in enumerate(COLLECTIONS):
        if stop_event and stop_event.is_set():
            break
        _cb(3 + idx * 4, f"[Stanley] Fetching collection: {handle}…")
        try:
            gids = _fetch_collection_ids(handle)
            for gid in gids:
                if gid not in all_gids:
                    all_gids[gid] = (gender, broad_type)
            logger.info(f"[Stanley] {handle}: {len(gids)} product GIDs")
        except Exception as e:
            logger.warning(f"[Stanley] collection {handle} error: {e}")

    total_gids = list(all_gids.keys())
    _cb(12, f"[Stanley] {len(total_gids)} unique products — fetching details…")

    if not total_gids:
        _cb(100, "Stanley 1913: no products found.")
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
        pct = 12 + int((i + len(batch_gids)) / len(total_gids) * 50)
        _cb(pct, f"[Stanley] Fetched {min(i+100, len(total_gids))}/{len(total_gids)} products…")
        time.sleep(0.8)
        heartbeat_scrape_record(scrape_id)

    _cb(65, "[Stanley] Processing & building Mirage descriptions…")
    all_products = []
    seen = set()
    for node in raw_nodes:
        if not node:
            continue
        gid = node.get("id", "")
        gender, broad_type = all_gids.get(gid, ("unisex", "accessories"))
        prods = _process_products([node], gender, broad_type, stop_event)
        for p in prods:
            h = p["handle"]
            if h not in seen:
                all_products.append(p)
                seen.add(h)

    _cb(75, f"[Stanley] {len(all_products)} products — transforming to CSV…")
    rows = transform_to_shopify(all_products)
    export_shopify_csv(rows, CSV_PATH)

    _cb(85, f"[Stanley] CSV saved ({len(all_products)} products) — uploading to DB…")
    upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)
    upload_csv_to_supabase(CSV_PATH, SCRAPER_ID)

    update_scrape_record(scrape_id, "completed", len(all_products))
    _cb(100, f"Stanley 1913: done ✅ — {len(all_products)} products", len(all_products))
    logger.info(f"[Stanley] Complete — {len(all_products)} products → {CSV_PATH}")
