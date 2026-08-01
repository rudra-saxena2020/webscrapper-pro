from curl_cffi import requests
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

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

BASE_URL = "https://www.marcjacobs.com"
GRID_URL = "https://www.marcjacobs.com/on/demandware.store/Sites-mjsfra-Site/en_US/Search-UpdateGrid"
PRODUCT_URL = "https://www.marcjacobs.com/on/demandware.store/Sites-mjsfra-Site/en_US/Product-Variation"
SCRAPER_ID = "marcjacobs"
CURRENCY = "USD"

proxy_str = os.getenv("PROXY_URL")
proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None


def _clean_mj_product(data, gender_tag=None, product_type_override=None):
    """Clean a single Marc Jacobs SFCC product into Mirage-standard format."""
    if not data:
        return None

    # ── Out-of-stock filter ───────────────────────────────────────────────────
    availability = data.get("availability") or {}
    if not availability.get("orderable", True):
        return None
    if data.get("available") is False:
        return None

    handle = data.get("id")
    if not handle:
        return None

    title = data.get("productName", "")
    cleaned_title_str = clean_title(title)
    if not cleaned_title_str:
        return None

    raw_desc = data.get("longDescription") or data.get("shortDescription") or ""
    body_html = build_mirage_description(raw_desc, cleaned_title_str, "Marc Jacobs", gender_tag or "women")

    # Always use the canonical vendor name. SFCC's data.get("brand") returns
    # internal sub-brand classifications like "CONTEMPORARY"/"LITTLE MARC"
    # which leak into Shopify Vendor & Tags if not overridden.
    brand = "Marc Jacobs"
    raw_ptype = (data.get("productType") or "").strip()
    # Reject SFCC hierarchy markers ("variationGroup", "master", "variant")
    if raw_ptype.lower() in {"variationgroup", "variation group", "master", "variant"}:
        raw_ptype = ""
    product_type = product_type_override or raw_ptype or ""
    category_val = gender_tag.lower() if gender_tag else data.get("productParentCategory", "")
    url = f"{BASE_URL}/products/{handle}"

    # ── Standardized tags ─────────────────────────────────────────────────────
    tag_meta = {
        "name": title,
        "Title": title,
        "Product Category": category_val,
        "Type": product_type,
        "Vendor": brand,
        "description": raw_desc,
        "url": url,
    }
    gender_hint = gender_tag.lower() if gender_tag else None
    detected_gender = gender_hint if gender_hint in ("men", "women", "unisex") else detect_gender(tag_meta)
    tags = build_full_tags(cleaned_title_str, brand, detected_gender, product_type, url,
                           extra_tags=["RudraScrapper-marcjacobs"])

    # ── Images ────────────────────────────────────────────────────────────────
    # MJ API v1 had a flat "url" key; v2 uses mapResults.pdpMainImg[].url.
    # We handle both so old-cached products and new fetches both work.
    all_images = []
    for img in (data.get("images") or {}).get("large") or []:
        img_url = img.get("url")
        if not img_url:
            # New nested format — pick the largest available preset
            pdp_imgs = (img.get("mapResults") or {}).get("pdpMainImg") or []
            # Prefer PDP_CAROUSEL_1280 quality; fall back to first entry
            for entry in pdp_imgs:
                preset = entry.get("imgPreset", "")
                if preset in ("PDP_CAROUSEL_1280", "PDP_CAROUSEL_1600"):
                    img_url = entry.get("url")
                    break
            if not img_url and pdp_imgs:
                img_url = pdp_imgs[0].get("url")
        if img_url:
            # Strip Bynder preset params — they force AVIF which Shopify cannot import.
            # Without the preset, Bynder returns JPEG by default.
            img_url = re.sub(r'[?&]preset=[^&]*', '', img_url).rstrip('?&/')
            img_url = img_url + '/'   # keep trailing slash (Bynder canonical form)
            all_images.append(img_url)

    # ── Price ─────────────────────────────────────────────────────────────────
    # Guard: .get("sales", {}) returns None (not {}) when the key exists with a
    # null value in the API response, so we must use "or {}" after the get.
    price_obj = data.get("price") or {}
    selected_price = (price_obj.get("sales") or {}).get("value") or 0
    selected_compare = (price_obj.get("list") or {}).get("value") or 0
    if not selected_price or float(selected_price) <= 0:
        return None

    # ── Sizes — collect ALL available size options ────────────────────────────
    _MJ_SIZE_MAP = {
        "1SZ": "One Size", "1": "One Size", "OS": "One Size",
        "O/S": "One Size", "ONESIZE": "One Size",
    }
    all_sizes = []
    for attr in data.get("variationAttributes", []):
        if attr.get("attributeId") == "size":
            for val in attr.get("values", []):
                if val.get("selectable") and val.get("displayValue"):
                    raw_sz = val["displayValue"].strip()
                    normalized = _MJ_SIZE_MAP.get(raw_sz.upper(), raw_sz.upper())
                    all_sizes.append(normalized)
            break
    if not all_sizes:
        all_sizes = [""]  # no size variation → single Default variant

    # ── Colors — collect ALL selectable color options ─────────────────────────
    master_id = data.get("masterID", "") or str(handle)
    selected_color_display = data.get("selectedColorValue") or ""
    color_entries = []  # (color_id, display_name, is_selected)
    for attr in data.get("variationAttributes", []):
        if attr.get("attributeId") == "color":
            for val in attr.get("values", []):
                if not val.get("selectable"):
                    continue
                vid  = val.get("id", "")
                disp = val.get("displayValue", "")
                sel  = bool(val.get("selected"))
                if sel:
                    selected_color_display = disp
                color_entries.append((vid, disp, sel))
            break
    if not color_entries:
        color_entries = [("", selected_color_display, True)]

    product_entry = {
        "Handle": generate_handle(cleaned_title_str, handle),
        "Title": cleaned_title_str,
        "Body (HTML)": body_html,
        "Vendor": brand,
        "Product Category": category_val,
        "Type": product_type,
        "Tags": tags,
        "Google Shopping / Gender": detected_gender,
        "variants": [],
        "_master_id": master_id,
    }

    # Color × Size cross-product — non-selected color images enriched later
    for color_id, color_disp, is_selected in color_entries:
        sku_base = str(handle) if is_selected else (f"{master_id}-{color_id}" if master_id else color_id)
        variant_images = all_images if is_selected else []
        color_label    = color_disp or selected_color_display

        for size in all_sizes:
            size_sfx = (f"-{size.replace(' ', '-').lower()}"
                        if size and len(all_sizes) > 1 else "")
            sku = f"{sku_base}{size_sfx}" if size_sfx else sku_base
            product_entry["variants"].append({
                "Variant SKU":              sku,
                "size":                     size,
                "color":                    color_label,
                "Variant Price":            float(selected_price),
                "Variant Compare At Price": float(selected_compare) if selected_compare else 0,
                "currency":                 CURRENCY,
                "images":                   variant_images,
                "_color_id":                color_id,
            })

    return product_entry


def extract_product_ids(html_content):
    """Extract product IDs from grid HTML.

    Smart suffix stripping: only remove the trailing segment when it looks like
    a colour-code shorthand (2+ uppercase letters such as NRF, BLK, POOL).
    3-digit numeric suffixes like -001, -307 are part of the variant ID and are
    intentionally kept so each colour variant is fetched as its own product.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    ids = set()
    for button in soup.find_all("button", attrs={"data-product-id": True}):
        ids.add(button["data-product-id"])
    processed = []
    for pid in ids:
        parts = str(pid).split("-")
        if len(parts) > 1 and re.fullmatch(r"[A-Z]{2,}", parts[-1]):
            pid = "-".join(parts[:-1])
        processed.append(pid)
    return sorted(set(processed))


def fetch_grid_page(cgid, start=0, sz=18, pgNo=1):
    params = {
        "cgid": cgid, "start": start, "sz": sz,
        "pgNo": pgNo, "enableInfiniteScroll": "true",
    }
    response = requests.get(GRID_URL, params=params, impersonate="chrome131")
    response.raise_for_status()
    return response.text


def fetch_product(pid):
    params = {"pid": pid, "quantity": 1, "isQuickView": "false", "isEditCart": "false"}
    response = requests.get(PRODUCT_URL, params=params, proxies=proxies, impersonate="chrome131")
    response.raise_for_status()
    data = response.json()
    return data.get("product", {})


def fetch_product_wrapper(pid):
    try:
        product_data = fetch_product(pid)
        if product_data:
            print(f"Fetched: {product_data.get('productName', 'Unknown')}")
            return product_data
    except Exception as e:
        print(f"Failed {pid}: {e}")
    return None


def _fetch_mj_color_images(color_pid: str) -> list:
    """Fetch large images for a specific color-variant PID via SFCC."""
    try:
        data = fetch_product(color_pid)
        if not data:
            return []
        imgs = []
        for img in (data.get("images") or {}).get("large") or []:
            img_url = img.get("url")
            if not img_url:
                pdp_imgs = (img.get("mapResults") or {}).get("pdpMainImg") or []
                for entry in pdp_imgs:
                    if entry.get("imgPreset") in ("PDP_CAROUSEL_1280", "PDP_CAROUSEL_1600"):
                        img_url = entry.get("url")
                        break
                if not img_url and pdp_imgs:
                    img_url = pdp_imgs[0].get("url")
            if img_url:
                img_url = re.sub(r'[?&]preset=[^&]*', '', img_url).rstrip('?&/')
                img_url = img_url + '/'
                imgs.append(img_url)
        return imgs
    except Exception:
        return []


def _enrich_mj_color_images(products: list, stop_event=None) -> list:
    """
    For every merged product, find color variants without images and fetch
    their images from the SFCC Product-Variation endpoint in a thread pool.
    """
    import time as _time
    tasks = []
    for p in products:
        master_id = p.get("_master_id", "")
        for v in p.get("variants", []):
            color_id = v.get("_color_id", "")
            if color_id and not v.get("images"):
                pid = f"{master_id}-{color_id}" if master_id else color_id
                tasks.append((p, v, pid))

    if not tasks:
        return products

    print(f"  🎨 MJ: Fetching per-color images for {len(tasks)} color variants…")

    with ThreadPoolExecutor(max_workers=10) as pool:
        future_to = {pool.submit(_fetch_mj_color_images, pid): (p, v) for p, v, pid in tasks}
        for fut in as_completed(future_to):
            if stop_event and stop_event.is_set():
                break
            p, v = future_to[fut]
            imgs = fut.result()
            if imgs:
                v["images"] = imgs
            _time.sleep(0.05)

    for p in products:
        seen = set()
        combined = []
        for v in p.get("variants", []):
            for url in (v.get("images") or []):
                if url and url not in seen:
                    seen.add(url)
                    combined.append(url)
        if combined:
            p["images"] = combined

    # Positional fallback: color variants that still have no images after enrichment
    # get assigned an image from the product's combined pool by color index, so no
    # variant row in the CSV will be imageless when the product has any images at all.
    for p in products:
        pool_imgs = p.get("images") or []
        if not pool_imgs:
            continue
        for idx, v in enumerate(p.get("variants", [])):
            if not v.get("images"):
                v["images"] = [pool_imgs[idx % len(pool_imgs)]]

    return products


def complete_workflow_marcjacobs(progress_callback=None, stop_event=None, **kwargs):
    """Main entry point for Marc Jacobs scraper."""
    scrape_record_id = start_scrape_record(SCRAPER_ID)

    def _cb(p, s, c=0):
        if progress_callback:
            progress_callback(p, s, c)

    categories = [
        {"cgid": "bags-view-all",        "type": "Bag",         "gender": "women"},
        {"cgid": "wallets-view-all",     "type": "Wallet",      "gender": "women"},
        {"cgid": "accessories-view-all", "type": "Accessories", "gender": "women"},
        {"cgid": "shoes-view-all",       "type": "Shoes",       "gender": "women"},
        {"cgid": "jewelry-view-all",     "type": "Jewelry",     "gender": "women"},
        {"cgid": "sale-view-all",        "type": "",            "gender": "women"},
    ]

    total_cats = len(categories)

    try:
        _cb(5, "Discovering Marc Jacobs products...")
        all_raw   = []
        seen_pids = set()   # cross-category dedup — avoid re-fetching the same PID

        for cat_idx, cat in enumerate(categories):
            if stop_event and stop_event.is_set():
                update_scrape_record(scrape_record_id, status="cancelled")
                return []

            cgid         = cat["cgid"]
            product_type = cat["type"]
            gender       = cat.get("gender", "women")
            prog_lo = 5  + int((cat_idx       / total_cats) * 60)
            prog_hi = 5  + int(((cat_idx + 1) / total_cats) * 60)

            print(f"\nScraping category: {cgid} ({product_type})...")
            _cb(prog_lo, f"Discovering {cgid}...", len(all_raw))

            last_batch = None
            start      = 0
            pgNo       = 1
            page_size  = 100

            while True:
                if stop_event and stop_event.is_set():
                    break
                try:
                    html      = fetch_grid_page(cgid, start=start, sz=page_size, pgNo=pgNo)
                    batch_ids = extract_product_ids(html)
                    if not batch_ids or batch_ids == last_batch:
                        break
                    last_batch = batch_ids

                    # Only fetch PIDs we haven't seen yet across all categories
                    new_ids = [pid for pid in batch_ids if pid not in seen_pids]
                    print(f"  [{cgid} +{start}] {len(new_ids)} new IDs ({len(batch_ids)-len(new_ids)} dupes skipped)")

                    with ThreadPoolExecutor(max_workers=10) as executor:
                        futures = {executor.submit(fetch_product_wrapper, pid): pid for pid in new_ids}
                        for future in as_completed(futures):
                            product_data = future.result()
                            if product_data:
                                pid_key = str(product_data.get("id") or futures[future])
                                if pid_key in seen_pids:
                                    continue
                                seen_pids.add(pid_key)
                                product_data["_gender_tag"] = gender
                                product_data["_product_type"] = product_type
                                all_raw.append(product_data)

                    # Mark ALL batch_ids as seen (even those we didn't fetch details for)
                    seen_pids.update(batch_ids)

                    # Live progress update
                    _cb(prog_lo, f"Fetching {cgid}: {len(all_raw)} products (+{start})...", len(all_raw))

                    start += page_size
                    pgNo  += 1
                except Exception as e:
                    print(f"Error fetching grid page {cgid}: {e}")
                    break

            _cb(prog_hi, f"Done {cgid}: {len(all_raw)} products so far", len(all_raw))

        heartbeat_scrape_record(scrape_record_id, len(all_raw))
        _cb(68, f"Cleaning {len(all_raw)} products...")

        cleaned = []
        for item in all_raw:
            gender = item.pop("_gender_tag", "women")
            ptype = item.pop("_product_type", "")
            _raw_pid = str(item.get("id") or "")
            result = _clean_mj_product(item, gender_tag=gender, product_type_override=ptype)
            if result:
                result["_mj_pid"] = _raw_pid
                cleaned.append(result)

        # ── Merge color variants: same title → one Shopify product ──────────────
        # Each API call returns one color. Group by normalized title so all
        # colorways of e.g. "The Zodiac Tote Bag" merge into a single product
        # with multiple Color option values instead of 12 separate products.
        merged_map = {}   # merge_key → merged product dict
        merge_order = []  # preserve insertion order
        for product in cleaned:
            title_key = product.get("Title", "").lower().strip()
            # Require shared SFCC product-ID prefix as tie-breaker so two genuinely
            # different products that happen to share a display name (rare but possible)
            # are NOT incorrectly collapsed into one.  Strip trailing color/size suffix
            # (e.g. W26327_BLK → W26327) before comparing.
            raw_pid = product.get("_mj_pid", "")
            pid_prefix = re.sub(r'[_-][A-Za-z0-9]+$', '', raw_pid) if raw_pid else ""
            merge_key = (title_key, pid_prefix)

            if merge_key not in merged_map:
                # First time: assign a clean handle derived from the title only
                clean_slug = re.sub(r"[^a-z0-9]+", "-", title_key).strip("-")
                product = dict(product)  # shallow copy so we don't mutate original
                product["Handle"] = clean_slug
                merged_map[merge_key] = product
                merge_order.append(merge_key)
            else:
                # Subsequent color: append its variants to the first product
                merged_map[merge_key]["variants"].extend(product.get("variants", []))

        # Deduplicate variants within each merged product by (color, size) key.
        # Multiple grid PIDs for the same product each return ALL colors from the
        # SFCC API, causing the same (color, size) pair to be added multiple times.
        for merge_key in merge_order:
            seen_variants = set()
            deduped = []
            for v in merged_map[merge_key].get("variants", []):
                vkey = (v.get("color", ""), v.get("size", ""))
                if vkey not in seen_variants:
                    seen_variants.add(vkey)
                    deduped.append(v)
            merged_map[merge_key]["variants"] = deduped

        cleaned = [merged_map[k] for k in merge_order]
        print(f"[MJ] After color-merge: {len(cleaned)} unique products from {len(merged_map)} title+pid groups")
        # ────────────────────────────────────────────────────────────────────────

        _cb(76, f"Fetching per-color images for {len(cleaned)} products…", len(cleaned))
        cleaned = _enrich_mj_color_images(cleaned, stop_event=stop_event)

        _cb(80, f"Saving {len(cleaned)} in-stock products...", len(cleaned))

        if not cleaned:
            update_scrape_record(scrape_record_id, status="failed", error_message="No in-stock products found.")
            return []

        upsert_all_product_data(cleaned, SCRAPER_ID, CURRENCY)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        os.makedirs("scraped_files", exist_ok=True)
        _cb(92, "Generating Shopify CSV...")
        rows = transform_to_shopify(cleaned)
        export_shopify_csv(rows, csv_path)

        _cb(97, "Uploading CSV...")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)
        update_scrape_record(scrape_record_id, status="completed", products_count=len(cleaned), csv_url=csv_url)

        _cb(100, f"Done! {len(cleaned)} in-stock products.", len(cleaned))
        print(f"✅ Marc Jacobs complete: {len(cleaned)} products")
        return cleaned

    except Exception as e:
        print(f"❌ Marc Jacobs Error: {e}")
        try:
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        except Exception:
            pass
        if progress_callback:
            progress_callback(0, f"Error: {e}", 0)
        return []


if __name__ == "__main__":
    complete_workflow_marcjacobs()
