import requests
import json
import sys
import os

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

BASE_URL  = "https://www.toryburch.com"
SCRAPER_ID = "tory"
CURRENCY   = "USD"

# Maps Tory Burch productClassName → singular phrase used in description template.
# Tory's API returns plural class names (e.g. "Crossbody Bags", "Sandals") which
# read awkwardly as "is a refined crossbody bags" — this converts them to singular.
TORY_TYPE_SINGULAR = {
    "Sandals":                "sandal",
    "Flats":                  "flat",
    "Crossbody Bags":         "crossbody bag",
    "Shoulder Bags":          "shoulder bag",
    "Sneakers":               "sneaker",
    "Heels":                  "heel",
    "Tote Bags":              "tote bag",
    "Espadrilles":            "espadrille",
    "Sunglasses & Eyewear":   "sunglasses piece",
    "Earrings":               "earring",
    "Belts":                  "belt",
    "Bracelets":              "bracelet",
    "Strap Watches":          "strap watch",
    "Wallets":                "wallet",
    "Necklaces":              "necklace",
    "Scarves":                "scarf",
    "Ankle Boots":            "ankle boot",
    "Card Cases":             "card case",
    "Hobo Bags":              "hobo bag",
    "Satchels":               "satchel",
    "Rings":                  "ring",
    "Belt Bags":              "belt bag",
    "Knee Boots":             "knee boot",
    "Hats":                   "hat",
    "Bag Charms & Key Rings": "bag charm",
    "Smart Watches":          "smart watch",
    "Cosmetic Bags":          "cosmetic bag",
    "Backpacks":              "backpack",
    "Linens":                 "linen piece",
    "Jackets":                "jacket",
    "Coin Purses":            "coin purse",
    "Travel Accessories":     "travel accessory",
    "Perfume":                "perfume",
    "Jewelry Boxes":          "jewelry box",
    "Bottoms":                "bottom",
    "Hair Pins":              "hair pin",
    "Clutches":               "clutch",
    "Tabletop & Drinkware":   "tabletop piece",
    "Tops":                   "top",
    "Socks":                  "pair of socks",
    "Wristlets":              "wristlet",
    "Tights":                 "pair of tights",
    "Handbag":                "handbag",
    "Shoes":                  "shoe",
    "Accessories":            "accessory",
    "Wallet":                 "wallet",
    "Jewelry":                "jewelry piece",
}

TORY_API_BASE = "https://www.toryburch.com/api/prod-r2/v11/categories"
TORY_HEADERS  = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "x-api-key": "yP6bAmceig0QmrXzGfx3IG867h5jKkAs",
}

# ── Categories to scrape ────────────────────────────────────────────────────
# Full-price main catalogs support offset pagination (650/775/791 items each)
# Sale items are smaller (<200 per dept) so one request each
TORY_CATEGORIES = [
    # ── Full-price ────────────────────────────────────────────────────────────
    {"endpoint": "handbags",    "dept_filter": None,          "gender": "women", "type": "Handbag"},
    {"endpoint": "shoes",       "dept_filter": None,          "gender": "women", "type": "Shoes"},
    {"endpoint": "accessories", "dept_filter": None,          "gender": "women", "type": "Accessories"},
    # ── Sale ─────────────────────────────────────────────────────────────────
    {"endpoint": "sale-view-all", "dept_filter": "Handbags",    "gender": "women", "type": "Handbag"},
    {"endpoint": "sale-view-all", "dept_filter": "Wallets",     "gender": "women", "type": "Wallet"},
    {"endpoint": "sale-view-all", "dept_filter": "Shoes",       "gender": "women", "type": "Shoes"},
    {"endpoint": "sale-view-all", "dept_filter": "Accessories", "gender": "women", "type": "Accessories"},
    {"endpoint": "sale-view-all", "dept_filter": "Jewelry",     "gender": "women", "type": "Jewelry"},
]


def fetch_products_paginated(endpoint, dept_filter=None):
    """Fetch all products from a category endpoint with offset pagination."""
    all_products = []
    offset = 0
    limit  = 200

    while True:
        params = {
            "site": "ToryBurch_US",
            "locale": "en-us",
            "pip": "true",
            "limit": str(limit),
            "layout": "flex",
            "offset": str(offset),
        }
        if dept_filter:
            params["filter[c_productDepartment]"] = dept_filter

        try:
            url = f"{TORY_API_BASE}/{endpoint}/products"
            r = requests.get(url, headers=TORY_HEADERS, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  ❌ {endpoint} offset={offset}: HTTP {r.status_code}")
                break

            data     = r.json()
            products = data.get("products", [])
            total    = data.get("total", 0)

            if not products:
                break

            all_products.extend(products)
            offset += limit
            print(f"  [{endpoint}] offset={offset-limit}..{offset} → {len(products)} (total reported: {total})")

            if offset >= total:
                break

        except Exception as e:
            print(f"  ❌ Error {endpoint} offset={offset}: {e}")
            break

    return all_products


def _clean_tory_product(product, gender_tag="women"):
    """Clean a single Tory Burch product into Mirage-standard format."""
    if not product:
        return None

    product_id = product.get("id")
    title      = (product.get("name") or "").strip()
    if not title or not product_id:
        return None

    cleaned_title_str = clean_title(title)
    product_type  = (product.get("productClassName") or "").strip()
    category_dept = (product.get("productDepartmentName") or "").strip()
    url = f"{BASE_URL}/product/{product_id}"

    raw_desc = (product.get("longDescription") or product.get("description") or "").strip()
    body_html = build_mirage_description(raw_desc, cleaned_title_str, "Tory Burch", gender_tag or "women")

    tag_meta = {
        "name": title,
        "Title": title,
        "Product Category": category_dept or product_type,
        "Type": product_type,
        "Vendor": "Tory Burch",
        "description": raw_desc,
        "url": url,
    }
    gender_hint     = gender_tag.lower() if gender_tag else None
    detected_gender = gender_hint if gender_hint in ("men", "women", "unisex") else detect_gender(tag_meta)
    tags            = build_full_tags(cleaned_title_str, "Tory Burch", detected_gender, product_type, url,
                                      extra_tags=["RudraScrapper-tory"])

    product_entry = {
        "Handle": generate_handle(cleaned_title_str, str(product_id)),
        "Title":            cleaned_title_str,
        "Body (HTML)":      body_html,
        "Vendor":           "Tory Burch",
        "Product Category": category_dept or product_type,
        "Type":             product_type,
        "Tags":             tags,
        "Google Shopping / Gender": detected_gender,
        "variants": [],
    }

    sizes = [s.get("value", "") for s in product.get("sizes", []) if s.get("value")]
    if not sizes:
        sizes = ["OS"]

    # Pre-pass: collect the union of all swatch image URLs for use as fallback
    # when an individual swatch has no images (API sometimes omits them).
    all_swatch_images: list = []
    _seen_all: set = set()
    for _sw in product.get("swatches", []):
        for _img in _sw.get("images", []):
            url = f"https://s7.toryburch.com/is/image/ToryBurch/style/{_img}.pdp-1534x1744.jpg"
            if url not in _seen_all:
                _seen_all.add(url)
                all_swatch_images.append(url)

    seen_fp = set()
    for swatch in product.get("swatches", []):
        color   = swatch.get("colorName", "")
        sku     = swatch.get("_id", "") or str(product_id)
        price   = float(swatch.get("price", {}).get("min") or 0)
        compare = float(swatch.get("price", {}).get("max") or 0)

        if price <= 0:
            continue
        if swatch.get("inStock") is False or swatch.get("available") is False:
            continue

        images = [
            f"https://s7.toryburch.com/is/image/ToryBurch/style/{img}.pdp-1534x1744.jpg"
            for img in swatch.get("images", [])
        ]
        # Fallback: if this swatch has no images, use the first available
        # swatch image from the same product so it's not invisible.
        if not images and all_swatch_images:
            images = all_swatch_images[:1]

        # Per-swatch available sizes: the API may expose an "availableSizes" list
        # on each swatch. If present, only include sizes that are actually in-stock
        # for this color. Falls back to all product sizes if not provided.
        swatch_avail_sizes = swatch.get("availableSizes") or swatch.get("sizesAvailable")
        active_sizes = sizes
        if swatch_avail_sizes and isinstance(swatch_avail_sizes, list):
            avail_set = {str(s).strip() for s in swatch_avail_sizes}
            active_sizes = [s for s in sizes if str(s).strip() in avail_set] or sizes

        for size in active_sizes:
            fp = (sku, size)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)

            product_entry["variants"].append({
                "Variant SKU":              f"{sku}-{size}" if size != "OS" else sku,
                "size":                     size,
                "color":                    color,
                "Variant Price":            price,
                "Variant Compare At Price": compare if compare > price else 0,
                "currency":                 CURRENCY,
                "images":                   images,
            })

    if not product_entry["variants"]:
        return None

    # Append available sizes to product description
    avail_sizes = list(dict.fromkeys(
        v["size"] for v in product_entry["variants"]
        if v.get("size") and v["size"] not in ("OS", "")
    ))
    if avail_sizes:
        sizes_html = f'<p><strong>Available Sizes:</strong> {", ".join(avail_sizes)}</p>'
        product_entry["Body (HTML)"] = product_entry["Body (HTML)"] + sizes_html

    return product_entry


def complete_workflow_tory(progress_callback=None, stop_event=None, **kwargs):
    """Main entry point for Tory Burch scraper."""
    scrape_record_id = start_scrape_record(SCRAPER_ID)

    def _cb(p, s, c=0):
        if progress_callback:
            progress_callback(p, s, c)

    total_cats = len(TORY_CATEGORIES)

    try:
        _cb(5, "Discovering Tory Burch products...")

        all_raw = []
        seen_ids = set()

        for i, cat in enumerate(TORY_CATEGORIES):
            if stop_event and stop_event.is_set():
                update_scrape_record(scrape_record_id, status="cancelled")
                return []

            ep     = cat["endpoint"]
            dept   = cat.get("dept_filter")
            gender = cat.get("gender", "women")
            ptype  = cat.get("type", "")
            label  = f"{ep}/{dept}" if dept else ep

            prog = 5 + int(((i) / total_cats) * 55)
            _cb(prog, f"Fetching {label}...", len(all_raw))
            print(f"\n📡 Tory Burch: {label}")

            products = fetch_products_paginated(ep, dept_filter=dept)
            new_added = 0
            for p in products:
                pid = p.get("id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    p["_gender_tag"] = gender
                    p["_product_type"] = ptype
                    all_raw.append(p)
                    new_added += 1

            print(f"  → {new_added} new unique (running total: {len(all_raw)})")
            _cb(prog + int(55 / total_cats), f"Got {len(all_raw)} unique products so far...", len(all_raw))
            heartbeat_scrape_record(scrape_record_id, len(all_raw))

        _cb(62, f"Cleaning {len(all_raw)} products...")
        print(f"\n🧹 Cleaning {len(all_raw)} unique raw products...")

        cleaned = []
        for item in all_raw:
            if stop_event and stop_event.is_set():
                break
            gender = item.pop("_gender_tag", "women")
            ptype  = item.pop("_product_type", "")
            result = _clean_tory_product(item, gender_tag=gender)
            if result:
                if ptype and not result.get("Type"):
                    result["Type"] = ptype
                cleaned.append(result)

        _cb(80, f"Saving {len(cleaned)} in-stock products...", len(cleaned))

        if not cleaned:
            update_scrape_record(scrape_record_id, status="failed",
                                 error_message="No in-stock products found.")
            return []

        upsert_all_product_data(cleaned, SCRAPER_ID, CURRENCY)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        os.makedirs("scraped_files", exist_ok=True)
        _cb(92, "Generating Shopify CSV...")
        rows = transform_to_shopify(cleaned)
        export_shopify_csv(rows, csv_path)

        _cb(97, "Uploading CSV...")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)
        update_scrape_record(scrape_record_id, status="completed",
                             products_count=len(cleaned), csv_url=csv_url)

        _cb(100, f"Done! {len(cleaned)} in-stock products.", len(cleaned))
        print(f"✅ Tory Burch complete: {len(cleaned)} products")
        return cleaned

    except Exception as e:
        print(f"❌ Tory Burch Error: {e}")
        import traceback; traceback.print_exc()
        try:
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        except Exception:
            pass
        if progress_callback:
            progress_callback(0, f"Error: {e}", 0)
        return []


if __name__ == "__main__":
    complete_workflow_tory()
