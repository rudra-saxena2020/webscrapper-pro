import requests
import time
import json
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.tag_engine import (
    clean_title, generate_handle, apply_standardized_tags,
    detect_gender, append_brand_message, sanitize_html_description,
    build_mirage_description,
    build_full_tags,
)
from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv

BASE_URL = "https://www.karllagerfeld.com"
SCRAPER_ID = "karl"
CURRENCY = "INR"

graphql_url = "https://karllagerfeld.com/api/unstable/graphql.json"
headers = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.karllagerfeld.com",
    "Referer": "https://www.karllagerfeld.com",
    "User-Agent": "Mozilla/5.0",
    "x-shopify-storefront-access-token": "6202c7ccffbb3d0c7a365be031619902",
}


def extract_handle_from_url(url):
    import re
    match = re.search(r'/collections/([^/?#]+)', url)
    return match.group(1) if match else None


def fetch_product_ids_from_collection(url):
    collection_handle = extract_handle_from_url(url)
    all_ids = []
    has_next_page = True
    after_cursor = None

    while has_next_page:
        query = """
        query ($handle: String!, $cursor: String) {
          collectionByHandle(handle: $handle) {
            products(first: 250, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              edges { node { id } }
            }
          }
        }
        """
        payload = {"query": query, "variables": {"handle": collection_handle, "cursor": after_cursor}}
        response = requests.post(graphql_url, headers=headers, json=payload)
        data = response.json()
        edges = data["data"]["collectionByHandle"]["products"]["edges"]
        for edge in edges:
            all_ids.append(edge["node"]["id"].split("/")[-1])
        page_info = data["data"]["collectionByHandle"]["products"]["pageInfo"]
        has_next_page = page_info["hasNextPage"]
        after_cursor = page_info["endCursor"]

    return all_ids


def format_shopify_gids(product_ids):
    return [f"gid://shopify/Product/{pid}" for pid in product_ids]


def fetch_shopify_products_batched(product_ids):
    query = """
    query test($ids: [ID!]!, $countryCode: CountryCode!, $languageCode: LanguageCode!)
    @inContext(country: $countryCode, language: $languageCode) {
      nodes(ids: $ids) {
        ... on Product {
          id
          availableForSale
          title
          handle
          description
          descriptionHtml
          productType
          onlineStoreUrl
          totalInventory
          vendor
          tags
          images(first: 50) {
            edges { node { id originalSrc } }
          }
          variants(first: 50) {
            edges {
              node {
                id
                sku
                title
                price { amount currencyCode }
                compareAtPrice { amount currencyCode }
                quantityAvailable
                availableForSale
                currentlyNotInStock
                selectedOptions { name value }
                image { id originalSrc }
              }
            }
          }
        }
      }
    }
    """
    all_responses = {"data": {"nodes": []}}
    for i in range(0, len(product_ids), 250):
        batch = product_ids[i:i+250]
        payload = {
            "query": query,
            "variables": {"ids": batch, "countryCode": "IN", "languageCode": "EN"},
        }
        try:
            response = requests.post(graphql_url, headers=headers, json=payload)
            if response.status_code == 200:
                data = response.json()
                all_responses["data"]["nodes"].extend(data.get("data", {}).get("nodes", []))
                print(f"[✓] Batch {i//250+1} fetched")
            else:
                print(f"[✗] Failed batch {i//250+1}: {response.status_code}")
        except Exception as e:
            print(f"[!] Exception in batch {i//250+1}: {e}")
        time.sleep(1.2)
    return all_responses


def _clean_karl_products(raw_data, gender_tag=None):
    """Clean Karl Lagerfeld Shopify products into Mirage-standard format."""
    products = raw_data.get("data", {}).get("nodes", [])
    cleaned = {}

    for product in products:
        if not product:
            continue

        # ── Out-of-stock filter (product level) ───────────────────────────────
        # availableForSale is Shopify's authoritative in-stock signal.
        # totalInventory check is a belt-and-braces guard; only applied when the
        # value is explicitly present and zero (not when it is missing/None).
        if not product.get("availableForSale", True):
            continue
        total_inv = product.get("totalInventory")
        if total_inv is not None and total_inv <= 0:
            continue

        title = (product.get("title") or "").strip()
        if not title:
            continue

        handle_raw = product.get("handle") or ""
        cleaned_title = clean_title(title)
        product_type = (product.get("productType") or "").strip()
        # Reject Shopify hierarchy markers if present
        if product_type.lower() in {"variationgroup", "variation group", "master", "variant"}:
            product_type = ""
        raw_desc = product.get("descriptionHtml") or f"<p>{product.get('description', '')}</p>"
        body_html = build_mirage_description(raw_desc, cleaned_title, "Karl Lagerfeld", gender_tag or "women")
        # Always normalize to "Karl Lagerfeld" — collapses sub-brand variants
        # ("KARL LAGERFELD PARIS", "KL Jeans", etc.) into a single Shopify Vendor.
        brand = "Karl Lagerfeld"
        url = product.get("onlineStoreUrl") or f"{BASE_URL}/products/{handle_raw}"

        # ── Standardized tags ─────────────────────────────────────────────────
        # Pass raw Shopify tags into tag_meta so detect_gender() can read them
        # (Karl Shopify products are tagged "Men" / "Women" by the source store).
        gender_hint = gender_tag.lower() if gender_tag else None
        tag_meta = {
            "name": title,
            "Title": title,
            "Product Category": product_type,
            "Type": product_type,
            "Vendor": brand,
            "description": product.get("description", ""),
            "url": url,
            "tags": product.get("tags", []),  # source Shopify tags for gender detection
        }
        detected_gender = gender_hint if gender_hint in ("men", "women", "unisex") else detect_gender(tag_meta)
        tags = build_full_tags(cleaned_title, brand, detected_gender, product_type, url,
                               extra_tags=["RudraScrapper-karl"])

        # ── Images ────────────────────────────────────────────────────────────
        all_images = []
        seen_img = set()
        for edge in product.get("images", {}).get("edges", []):
            img_url = edge["node"].get("originalSrc")
            if img_url and img_url not in seen_img:
                all_images.append(img_url)
                seen_img.add(img_url)

        pid = product.get("id", "").split("/")[-1]
        handle_key = handle_raw or generate_handle(cleaned_title, pid)

        if handle_key not in cleaned:
            cleaned[handle_key] = {
                "Handle": handle_key,
                "Title": cleaned_title,
                "Body (HTML)": body_html,
                "Vendor": brand,
                "Product Category": product_type,
                "Type": product_type,
                "Tags": tags,
                "Google Shopping / Gender": detected_gender,
                "variants": [],
            }

        # ── Variants (only in-stock) ───────────────────────────────────────────
        seen = set()
        for edge in product.get("variants", {}).get("edges", []):
            v = edge["node"]
            # Primary gate: Shopify's definitive in-stock signal
            if not v.get("availableForSale", False):
                continue
            # Secondary gate: explicitly marked not-in-stock (covers pre-order / backorder edge cases)
            if v.get("currentlyNotInStock", False):
                continue

            sku = v.get("sku", "")
            price_node = v.get("price") or {}
            price_raw = float(price_node.get("amount", 0) or 0)
            if price_raw <= 0:
                continue

            # Use currency reported by the price node so the main store (countryCode=IN → INR)
            # and Karl Lagerfeld Paris (hardcoded GBP in REST→GQL shim) both price correctly.
            currency_code = price_node.get("currencyCode") or CURRENCY

            compare_raw = float((v.get("compareAtPrice") or {}).get("amount", 0) or 0)
            color, size = "", ""
            for opt in v.get("selectedOptions", []):
                name_lower = opt.get("name", "").lower()
                if "color" in name_lower or "colour" in name_lower:
                    color = opt.get("value", "")
                elif "size" in name_lower:
                    size = opt.get("value", "")

            fp = (sku, size, color)
            if fp in seen:
                continue
            seen.add(fp)

            # Per-variant image: prefer the variant's own image, fall back to product gallery
            v_img_src = (v.get("image") or {}).get("originalSrc") or ""
            if v_img_src:
                variant_images = [v_img_src] + [img for img in all_images if img != v_img_src]
            else:
                variant_images = list(all_images)

            cleaned[handle_key]["variants"].append({
                "Variant SKU": sku,
                "size": size,
                "color": color,
                "Variant Price": price_raw,
                "Variant Compare At Price": compare_raw,
                "currency": currency_code,
                "images": variant_images,
            })

        # Remove products where ALL variants were OOS
        if not cleaned[handle_key]["variants"]:
            del cleaned[handle_key]
            continue

        # Append available sizes to product description
        avail_sizes = list(dict.fromkeys(
            v["size"] for v in cleaned[handle_key]["variants"]
            if v.get("size") and v["size"] not in ("", "Default")
        ))
        if avail_sizes:
            sizes_html = f'<p><strong>Available Sizes:</strong> {", ".join(avail_sizes)}</p>'
            cleaned[handle_key]["Body (HTML)"] = cleaned[handle_key]["Body (HTML)"] + sizes_html

    return list(cleaned.values())


PARIS_BASE_URL = "https://www.karllagerfeldparis.com"

PARIS_COLLECTIONS = [
    {"url": f"{PARIS_BASE_URL}/collections/select-styles", "gender": "women"},
    {"url": f"{PARIS_BASE_URL}/collections/mens-sale",     "gender": "men"},
]


def _fetch_paris_products_rest(collection_url):
    """Fetch all products from a Karl Lagerfeld Paris collection via Shopify REST."""
    handle = collection_url.rstrip("/").split("/collections/")[-1]
    products = []
    page = 1
    while True:
        url = f"{PARIS_BASE_URL}/collections/{handle}/products.json?limit=250&page={page}"
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                break
            batch = r.json().get("products", [])
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  Paris REST error (page {page}): {e}")
            break
    return products


def _paris_rest_to_graphql(p, base_url=PARIS_BASE_URL):
    """Convert a Shopify REST product to the GraphQL format used by _clean_karl_products."""
    variants_rest = p.get("variants", [])
    images_rest   = p.get("images", [])

    # Map variant image_id → image src for easy lookup
    img_id_to_src = {img.get("id"): img.get("src", "") for img in images_rest}

    return {
        "id": f"gid://shopify/Product/{p.get('id','')}",
        "availableForSale": any(v.get("available") for v in variants_rest),
        "totalInventory": sum(max(v.get("inventory_quantity", 0), 0) for v in variants_rest),
        "title": p.get("title", ""),
        "handle": p.get("handle", ""),
        "description": p.get("body_html", ""),
        "descriptionHtml": p.get("body_html", ""),
        "productType": p.get("product_type", ""),
        "vendor": "Karl Lagerfeld",
        "onlineStoreUrl": f"{base_url}/products/{p.get('handle', '')}",
        "tags": (p.get("tags") or "").split(", ") if isinstance(p.get("tags"), str) else [],
        "images": {
            "edges": [
                {"node": {"id": str(img.get("id", "")), "originalSrc": img.get("src", "")}}
                for img in images_rest
            ]
        },
        "variants": {
            "edges": [
                {
                    "node": {
                        "id": f"gid://shopify/ProductVariant/{v.get('id','')}",
                        "sku": v.get("sku", "") or "",
                        "title": v.get("title", ""),
                        "price": {
                            "amount": str(v.get("price", "0")),
                            "currencyCode": "GBP",
                        },
                        "compareAtPrice": (
                            {"amount": str(v.get("compare_at_price", "0")), "currencyCode": "GBP"}
                            if v.get("compare_at_price") else None
                        ),
                        "quantityAvailable": max(v.get("inventory_quantity", 0), 0),
                        "availableForSale": v.get("available", False),
                        "currentlyNotInStock": not v.get("available", False),
                        "selectedOptions": [
                            {
                                "name": opt.get("name", ""),
                                "value": v.get(f"option{i+1}", "") or ""
                            }
                            for i, opt in enumerate(p.get("options", []))
                        ],
                        "image": (
                            {"id": str(v.get("image_id", "")),
                             "originalSrc": img_id_to_src.get(v.get("image_id"), "")}
                            if v.get("image_id") else None
                        ),
                    }
                }
                for v in variants_rest
            ]
        },
    }


def complete_workflow_karl(progress_callback=None, stop_event=None, **kwargs):
    """Main entry point for Karl Lagerfeld scraper."""
    scrape_record_id = start_scrape_record(SCRAPER_ID)

    def _cb(p, s, c=0):
        if progress_callback:
            progress_callback(p, s, c)

    # karllagerfeld.com collections (Shopify GraphQL)
    # NOTE: "sale-all" contains both men's and women's products.
    # Do NOT pass a gender override here — let detect_gender() classify each
    # product individually using title, tags, and category data.
    collections = [
        {"url": "https://www.karllagerfeld.com/en-gb/collections/sale-all", "gender": None},
    ]

    try:
        _cb(5, "Discovering Karl Lagerfeld collections...")

        all_scraped_ids = []
        product_id_to_collection = {}

        for i, collection in enumerate(collections):
            if stop_event and stop_event.is_set():
                update_scrape_record(scrape_record_id, status="cancelled")
                return []
            try:
                ids = fetch_product_ids_from_collection(collection["url"])
                for pid in ids:
                    product_id_to_collection[str(pid)] = collection
                all_scraped_ids.extend(ids)
                print(f"✓ {len(ids)} IDs from {collection['url']}")
            except Exception as e:
                print(f"✗ Error scraping {collection['url']}: {e}")

        unique_ids = list(set(all_scraped_ids))
        _cb(25, f"Found {len(unique_ids)} products — fetching details...", len(unique_ids))
        print(f"🎯 Total unique IDs: {len(unique_ids)}")

        _cb(40, f"Found {len(unique_ids)} IDs from karllagerfeld.com — fetching details...")

        all_products = []
        seen_handles = set()

        if unique_ids:
            gids = format_shopify_gids(unique_ids)
            raw_data = fetch_shopify_products_batched(gids)

            if stop_event and stop_event.is_set():
                update_scrape_record(scrape_record_id, status="cancelled")
                return []

            _cb(55, "Cleaning karllagerfeld.com products...")
            for product in raw_data.get("data", {}).get("nodes", []):
                if not product:
                    continue
                pid = product.get("id", "").split("/")[-1]
                coll = product_id_to_collection.get(str(pid))
                if coll:
                    product["_gender_tag"] = coll.get("gender")
                    product["_coll_url"] = coll["url"]

            products_by_collection = {}
            for product in raw_data.get("data", {}).get("nodes", []):
                if not product:
                    continue
                pid = product.get("id", "").split("/")[-1]
                coll = product_id_to_collection.get(str(pid))
                if coll:
                    key = coll["url"]
                    if key not in products_by_collection:
                        products_by_collection[key] = {"products": [], "collection": coll}
                    products_by_collection[key]["products"].append(product)

            for key, cd in products_by_collection.items():
                temp = {"data": {"nodes": cd["products"]}}
                gender = cd["collection"].get("gender")
                cleaned = _clean_karl_products(temp, gender_tag=gender)
                for p in cleaned:
                    if p["Handle"] not in seen_handles:
                        all_products.append(p)
                        seen_handles.add(p["Handle"])

        # ── Karl Lagerfeld Paris (REST API — different Shopify store) ─────────
        total_paris = len(PARIS_COLLECTIONS)
        for j, paris_coll in enumerate(PARIS_COLLECTIONS):
            if stop_event and stop_event.is_set():
                break
            prog2 = 60 + int(((j + 1) / total_paris) * 15)
            _cb(prog2, f"Fetching Paris: {paris_coll['url'].split('/')[-1]}...", len(all_products))
            print(f"\n📡 Paris collection: {paris_coll['url']}")
            try:
                paris_rest = _fetch_paris_products_rest(paris_coll["url"])
                print(f"  REST products: {len(paris_rest)}")
                paris_gql = [_paris_rest_to_graphql(p) for p in paris_rest]
                paris_temp = {"data": {"nodes": paris_gql}}
                paris_cleaned = _clean_karl_products(paris_temp, gender_tag=paris_coll.get("gender"))
                added = 0
                for p in paris_cleaned:
                    if p["Handle"] not in seen_handles:
                        all_products.append(p)
                        seen_handles.add(p["Handle"])
                        added += 1
                print(f"  → {added} new products added from Paris")
            except Exception as e:
                print(f"  ✗ Paris collection error: {e}")

        heartbeat_scrape_record(scrape_record_id, len(all_products))
        _cb(80, f"Saving {len(all_products)} products to database...", len(all_products))

        if not all_products:
            update_scrape_record(scrape_record_id, status="failed", error_message="No in-stock products after cleaning.")
            return []

        upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        os.makedirs("scraped_files", exist_ok=True)
        _cb(92, "Generating Shopify CSV...")
        rows = transform_to_shopify(all_products)
        export_shopify_csv(rows, csv_path)

        _cb(97, "Uploading CSV...")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)
        update_scrape_record(scrape_record_id, status="completed", products_count=len(all_products), csv_url=csv_url)

        _cb(100, f"Done! {len(all_products)} in-stock products.", len(all_products))
        print(f"✅ Karl complete: {len(all_products)} products")
        return all_products

    except Exception as e:
        print(f"❌ Karl Error: {e}")
        try:
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        except Exception:
            pass
        if progress_callback:
            progress_callback(0, f"Error: {e}", 0)
        return []


if __name__ == "__main__":
    complete_workflow_karl()
