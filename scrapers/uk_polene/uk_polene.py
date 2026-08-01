import requests
import time
import os
import re
import sys
import threading

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import (
    sanitize_html_description, build_full_tags, append_brand_message,
    build_mirage_description,
)

SCRAPER_ID = "uk_polene"
CURRENCY   = "GBP"
BASE_URL   = "https://uk.polene-paris.com"

GRAPHQL_URL = "https://uk-polene.myshopify.com/api/unstable/graphql.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin":  BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "x-shopify-storefront-access-token": "e5d2415341bc55e799a3067f538fe766",
}

COLLECTIONS = [
    {"url": "https://uk.polene-paris.com/collections/handbags",          "gender": "women"},
    {"url": "https://uk.polene-paris.com/collections/small-leather-goods", "gender": "women"},
]

_COLLECTION_IDS_QUERY = """
query ($handle: String!, $cursor: String) {
  collectionByHandle(handle: $handle) {
    products(first: 250, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges { node { id } }
    }
  }
}
"""

_PRODUCT_DETAILS_QUERY = """
query ($ids: [ID!]!, $countryCode: CountryCode!, $languageCode: LanguageCode!)
@inContext(country: $countryCode, language: $languageCode) {
  nodes(ids: $ids) {
    ... on Product {
      id availableForSale title handle description descriptionHtml
      productType tags vendor
      images(first: 250) {
        edges { node { originalSrc } }
      }
      variants(first: 250) {
        edges {
          node {
            id sku title
            price { amount currencyCode }
            compareAtPrice { amount currencyCode }
            quantityAvailable availableForSale
            selectedOptions { name value }
          }
        }
      }
    }
  }
}
"""


def _extract_handle(url: str) -> str:
    m = re.search(r'/collections/([^/?#]+)', url)
    return m.group(1) if m else ""


def _fetch_collection_ids(url: str) -> list:
    handle = _extract_handle(url)
    all_ids = []
    cursor  = None

    while True:
        resp = requests.post(
            GRAPHQL_URL, headers=HEADERS,
            json={"query": _COLLECTION_IDS_QUERY,
                  "variables": {"handle": handle, "cursor": cursor}},
            timeout=30,
        )
        resp.raise_for_status()
        data  = resp.json()
        col   = (data.get("data") or {}).get("collectionByHandle") or {}
        prods = col.get("products") or {}
        for edge in prods.get("edges") or []:
            gid = edge["node"]["id"]
            all_ids.append(gid.split("/")[-1])
        pi = prods.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi["endCursor"]
        time.sleep(0.5)

    return all_ids


def _fetch_products_batched(product_ids: list) -> list:
    all_nodes = []
    for i in range(0, len(product_ids), 200):
        batch = [f"gid://shopify/Product/{pid}" for pid in product_ids[i:i+200]]
        try:
            resp = requests.post(
                GRAPHQL_URL, headers=HEADERS,
                json={"query": _PRODUCT_DETAILS_QUERY,
                      "variables": {"ids": batch, "countryCode": "GB", "languageCode": "EN"}},
                timeout=45,
            )
            if resp.status_code == 200:
                nodes = (resp.json().get("data") or {}).get("nodes") or []
                all_nodes.extend(nodes)
                print(f"[Polene] Batch {i//200+1}: {len(nodes)} products")
            else:
                print(f"[Polene] Batch {i//200+1} HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            print(f"[Polene] Batch error: {exc}")
        time.sleep(1.0)
    return all_nodes


def _clean_products(nodes: list, gender_tag: str = "women") -> dict:
    cleaned = {}

    for product in nodes:
        if not product:
            continue
        if not product.get("availableForSale"):
            continue

        handle = (product.get("handle") or "").strip()
        if not handle:
            continue

        title  = (product.get("title") or "").strip()
        vendor = (product.get("vendor") or "Polene").strip()
        _raw_desc = product.get("descriptionHtml") or product.get("description") or ""
        desc      = sanitize_html_description(_raw_desc)
        ptype    = (product.get("productType") or "").strip()
        tags_str = build_full_tags(title, vendor, gender_tag, ptype,
                                   extra_tags=["RudraScrapper-uk_polene"])

        all_images = []
        seen_imgs  = set()
        for edge in (product.get("images") or {}).get("edges") or []:
            src = edge["node"].get("originalSrc")
            if src and src not in seen_imgs:
                all_images.append(src)
                seen_imgs.add(src)

        if handle not in cleaned:
            cleaned[handle] = {
                "Handle":      handle,
                "Title":       title,
                "Body (HTML)": build_mirage_description(_raw_desc, title, vendor or "Polène", gender_tag),
                "Vendor":      vendor,
                "Type":        ptype,
                "Tags":        tags_str,
                "variants":    [],
            }

        seen_variants = set()
        for edge in (product.get("variants") or {}).get("edges") or []:
            v = edge["node"]
            if not v.get("availableForSale"):
                continue
            # qty may be None when inventory is untracked — trust availableForSale
            qty = v.get("quantityAvailable")
            if qty is not None and qty == 0:
                continue

            sku   = (v.get("sku") or "").strip()
            price = float((v.get("price") or {}).get("amount") or 0)
            if price <= 0:
                continue

            compare = 0.0
            if v.get("compareAtPrice"):
                compare = float(v["compareAtPrice"].get("amount") or 0)

            color, size = "", ""
            for opt in v.get("selectedOptions") or []:
                n = opt["name"].lower()
                if "color" in n or "colour" in n or "couleur" in n or "colore" in n:
                    color = opt["value"]
                elif "size" in n or "taille" in n:
                    size = opt["value"]
                elif n == "title":
                    # Polène single-option products expose the colorway as "Title"
                    val = opt.get("value", "")
                    if val.lower() not in ("default title", "default", ""):
                        color = val

            # Polène encodes colorway in the product title after " - "
            # e.g. "Cyme - Textured Camel" → color = "Textured Camel"
            # Use this as fallback when selectedOptions gave nothing useful.
            if not color:
                if " - " in title:
                    color = title.split(" - ", 1)[-1].strip()
                elif title:
                    # handle may encode color: "cyme-textured-camel" → last words
                    # Use title directly as color if it's all one colorway description
                    color = title.strip()

            key = (sku or v.get("title", "")) + color + size
            if key in seen_variants:
                continue
            seen_variants.add(key)

            cleaned[handle]["variants"].append({
                "Variant SKU":              sku,
                "color":                    color,
                "size":                     size,
                "Variant Price":            price,
                "Variant Compare At Price": compare if compare > price else "",
                "images":                   all_images,
            })

    return cleaned


def complete_workflow_uk_polene(progress_callback=None, stop_event=None, **kwargs):
    def _cb(pct, msg, count=None):
        if progress_callback:
            try:
                progress_callback(pct, msg, count)
            except TypeError:
                progress_callback(pct, msg)

    scrape_record_id = start_scrape_record(SCRAPER_ID)

    heart_stop = threading.Event()

    def _heartbeat():
        while not heart_stop.is_set():
            try:
                heartbeat_scrape_record(scrape_record_id)
            except Exception:
                pass
            time.sleep(20)

    threading.Thread(target=_heartbeat, daemon=True).start()

    try:
        _cb(5, "Fetching product IDs from Polene UK collections…")

        id_to_collection = {}
        all_ids = []

        for col in COLLECTIONS:
            if stop_event and stop_event.is_set():
                break
            try:
                ids = _fetch_collection_ids(col["url"])
                for pid in ids:
                    if pid not in id_to_collection:
                        id_to_collection[pid] = col
                all_ids.extend(ids)
                print(f"[Polene] {col['url']}: {len(ids)} IDs")
            except Exception as exc:
                print(f"[Polene] Error {col['url']}: {exc}")

        unique_ids = list(dict.fromkeys(all_ids))
        _cb(25, f"Fetched {len(unique_ids)} product IDs — loading details…")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        nodes = _fetch_products_batched(unique_ids)
        heartbeat_scrape_record(scrape_record_id, len(nodes))
        _cb(60, f"Cleaning {len(nodes)} products…")

        # Merge cleaned products across all collections (dedup by handle)
        merged: dict = {}
        for node in nodes:
            if not node:
                continue
            handle = (node.get("handle") or "").strip()
            col    = id_to_collection.get(node.get("id","").split("/")[-1], COLLECTIONS[0])
            partial = _clean_products([node], gender_tag=col.get("gender","women"))
            for h, p in partial.items():
                if h not in merged:
                    merged[h] = p
                else:
                    merged[h]["variants"].extend(p["variants"])

        cleaned = [p for p in merged.values() if p["variants"]]
        _cb(78, f"{len(cleaned)} in-stock Polene products", len(cleaned))

        if not cleaned:
            update_scrape_record(scrape_record_id, status="failed",
                                 error_message="No in-stock products found.")
            return []

        upsert_all_product_data(cleaned, SCRAPER_ID, CURRENCY)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        os.makedirs("scraped_files", exist_ok=True)

        _cb(88, "Generating Shopify CSV…")
        rows = transform_to_shopify(cleaned)
        export_shopify_csv(rows, csv_path)

        _cb(96, "Uploading CSV…", len(cleaned))
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)

        update_scrape_record(scrape_record_id, status="completed",
                             products_count=len(cleaned), csv_url=csv_url)
        _cb(100, f"Done ✅  {len(cleaned)} products", len(cleaned))
        print(f"✅ Polene UK complete: {len(cleaned)} products")
        return cleaned

    except Exception as exc:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(exc))
        raise

    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_uk_polene()
