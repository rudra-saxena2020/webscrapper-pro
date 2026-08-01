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

SCRAPER_ID = "thedesignerboxuk"
CURRENCY   = "GBP"
BASE_URL   = "https://thedesignerboxuk.com"

GRAPHQL_URL = "https://thedesignerbox.myshopify.com/api/2025-01/graphql.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin":  BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "x-shopify-storefront-access-token": "d2e6ee62da9d5158adadada8c59c4bb1",
}

# Only keep products from these luxury vendors
ALLOWED_VENDORS = {
    "Alexander McQueen", "Ami Paris", "Amiri", "Balenciaga", "Balmain",
    "Burberry", "Dolce & Gabbana", "Dsquared2", "Fendi", "Gallery Dept",
    "Givenchy", "Gucci", "Jacquemus", "Kenzo", "Lanvin", "Moschino",
    "Loewe", "Love Moschino", "Off-White", "Rhude", "Valentino",
    "Palm Angels", "Tom Ford",
}

COLLECTIONS = [
    {"url": "https://thedesignerboxuk.com/collections/sale", "gender": None},
]

# ─── GraphQL helpers ────────────────────────────────────────────────────────

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
      productType tags vendor totalInventory
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


def _format_tdb_desc(text: str) -> str:
    """
    Convert thedesignerboxuk comma-separated feature strings into a clean
    HTML paragraph. Strips any residual <br>/<p> tags that slip through
    sanitize_html_description, then wraps in <p>.
    """
    if not text:
        return ""
    import re as _re
    # Strip any HTML tags that weren't cleaned (e.g. <br>, <p>)
    cleaned = _re.sub(r'<[^>]+>', ' ', text)
    # Collapse whitespace
    cleaned = _re.sub(r'[ \t]+', ' ', cleaned).strip()
    if not cleaned:
        return ""
    # Wrap in paragraph
    return f"<p>{cleaned}</p>"


def _extract_collection_handle(url: str) -> str:
    m = re.search(r'/collections/([^/?#]+)', url)
    return m.group(1) if m else "sale"


def _fetch_collection_ids(url: str) -> list:
    handle   = _extract_collection_handle(url)
    all_ids  = []
    cursor   = None

    while True:
        resp = requests.post(
            GRAPHQL_URL, headers=HEADERS,
            json={"query": _COLLECTION_IDS_QUERY,
                  "variables": {"handle": handle, "cursor": cursor}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        col  = (data.get("data") or {}).get("collectionByHandle") or {}
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
                print(f"[TDBUK] Batch {i//200+1}: fetched {len(nodes)} products")
            else:
                print(f"[TDBUK] Batch {i//200+1} HTTP {resp.status_code}")
        except Exception as exc:
            print(f"[TDBUK] Batch {i//200+1} error: {exc}")
        time.sleep(1.2)
    return all_nodes


def _clean_products(nodes: list) -> list:
    cleaned = {}

    for product in nodes:
        if not product:
            continue
        if not product.get("availableForSale"):
            continue

        vendor = (product.get("vendor") or "").strip()
        if vendor not in ALLOWED_VENDORS:
            continue

        handle = (product.get("handle") or "").strip()
        if not handle:
            continue

        title = (product.get("title") or "").strip()
        _raw_desc = product.get("descriptionHtml") or product.get("description") or ""
        desc      = sanitize_html_description(_raw_desc)
        ptype     = (product.get("productType") or "").strip()
        raw_tags  = product.get("tags") or []
        raw_lower = " ".join(raw_tags).lower()
        if "women" in raw_lower or "womens" in raw_lower:
            gender_hint = "women"
        elif " men" in raw_lower or raw_lower.startswith("men"):
            gender_hint = "men"
        else:
            gender_hint = "women"

        all_images = []
        seen_imgs  = set()
        for edge in (product.get("images") or {}).get("edges") or []:
            src = edge["node"].get("originalSrc")
            if src and src not in seen_imgs:
                all_images.append(src)
                seen_imgs.add(src)

        tags_str = build_full_tags(title, vendor, gender_hint, ptype,
                                   extra_tags=["RudraScrapper-thedesignerboxuk"])

        if handle not in cleaned:
            cleaned[handle] = {
                "Handle":      handle,
                "Title":       title,
                "Body (HTML)": build_mirage_description(_raw_desc, title, vendor, gender_hint),
                "Vendor":      "The Designer Box UK",
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
                if n == "color":
                    color = opt["value"]
                elif n == "size":
                    size = opt["value"]

            key = (sku or v.get("title","")) + color + size
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

    return [p for p in cleaned.values() if p["variants"]]


def complete_workflow_thedesignerboxuk(progress_callback=None, stop_event=None, **kwargs):
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
        _cb(5, "Fetching product IDs from sale collection…")

        all_ids = []
        for col in COLLECTIONS:
            if stop_event and stop_event.is_set():
                break
            try:
                ids = _fetch_collection_ids(col["url"])
                all_ids.extend(ids)
                print(f"[TDBUK] {col['url']}: {len(ids)} IDs")
            except Exception as exc:
                print(f"[TDBUK] Error fetching {col['url']}: {exc}")

        unique_ids = list(dict.fromkeys(all_ids))
        _cb(25, f"Fetched {len(unique_ids)} product IDs — loading details…")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        nodes = _fetch_products_batched(unique_ids)
        _cb(60, f"Cleaning {len(nodes)} products (filtering to allowed vendors)…")

        heartbeat_scrape_record(scrape_record_id, len(nodes))

        cleaned = _clean_products(nodes)
        _cb(78, f"{len(cleaned)} in-stock luxury products found", len(cleaned))

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
        print(f"✅ TheDesignerBoxUK complete: {len(cleaned)} products")
        return cleaned

    except Exception as exc:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(exc))
        raise

    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_thedesignerboxuk()
