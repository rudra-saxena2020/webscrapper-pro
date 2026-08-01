import requests
import time
import json
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from core.db import *
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
import re
import json
import re
from copy import deepcopy
# shopify: {
#     staging: "gymshark-staging",
#     production: "gymshark"
# },
# storefront: {
#     staging: "60b46bd76dd1449a52b294c68961a3b2",
#     production: "b65646444b3639704a6ecb270cd28ce7"
# },



def normalize_color(color_str):
    return color_str.strip().lower().replace(" ", "-")

def remove_color_from_handle(handle, color):
    normalized_color = normalize_color(color)
    if handle.endswith(f"-{normalized_color}"):
        return handle[: -len(f"-{normalized_color}")]
    return handle  # fallback if it doesn't match

def normalize_title(title, color):
    return re.sub(rf" - {re.escape(color)}$", "", title).strip()

def group_by_handle_without_color(products):
    grouped = {}

    for product in products:
        original_handle = product.get("Handle", "")
        for variant in product["variants"]:
            color = variant.get("color", "").strip()
            cleaned_handle = remove_color_from_handle(original_handle, color)
            cleaned_title = normalize_title(product["Title"], color)
            key = f"{cleaned_handle}::{cleaned_title.lower()}"

            if key not in grouped:
                new_product = deepcopy(product)
                new_product["Handle"] = cleaned_handle
                new_product["Title"] = cleaned_title
                new_product["variants"] = [variant]
                grouped[key] = new_product
            else:
                grouped[key]["variants"].append(variant)

    return list(grouped.values())


BASE_URL = "https://www.aloyoga.com"

graphql_url = "https://alo-yoga.myshopify.com/api/2025-01/graphql.json"
headers = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.aloyoga.com",
    "Referer": "https://www.aloyoga.com",
    "User-Agent": "Mozilla/5.0",
    "x-shopify-storefront-access-token": "d7ef45a4f583a78079bfebcb868b5931"
}



def format_shopify_gids(product_ids):
    return [f"gid://shopify/Product/{pid}" for pid in product_ids]


def _parse_collection_url(url: str):
    """Return (collection_handle, inferred_gender) from an Alo Yoga URL.

    Supports:
      https://www.aloyoga.com/collections/womens-shop-all
      https://www.aloyoga.com/pages/mens  -> maps to collection handle 'mens-shop-all'
    """
    if not url:
        return None, None
    url = url.strip().rstrip("/")
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "collections":
        handle = parts[-1]
        inferred = None
        if "women" in handle.lower():
            inferred = "women"
        elif "men" in handle.lower():
            inferred = "men"
        return handle, inferred
    if len(parts) >= 2 and parts[-2] == "pages":
        page = parts[-1].lower()
        if "men" in page:
            return "mens-shop-all", "men"
    return None, None


def fetch_product_ids_by_collection(collection_handle, gender_filter=None, max_pages=20):
    """Paginate through a Shopify collection and return available product IDs."""
    all_ids = set()
    has_next_page = True
    after_cursor = None
    pages = 0

    while has_next_page and pages < max_pages:
        query = """
        query ($handle: String!, $cursor: String) {
          collectionByHandle(handle: $handle) {
            products(first: 250, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              edges { node { id availableForSale tags } }
            }
          }
        }
        """
        variables = {"handle": collection_handle, "cursor": after_cursor}
        try:
            response = requests.post(graphql_url, headers=headers, json={"query": query, "variables": variables}, timeout=30)
            data = response.json()
            coll = data.get("data", {}).get("collectionByHandle")
            if not coll:
                print(f"❌ Collection '{collection_handle}' not found.")
                break
            edges = coll["products"]["edges"]
            for edge in edges:
                node = edge["node"]
                if not node.get("availableForSale", True):
                    continue
                if gender_filter and gender_filter not in node.get("tags", []):
                    continue
                gid = node["id"]
                numeric_id = gid.split("/")[-1]
                all_ids.add(numeric_id)
            page_info = coll["products"]["pageInfo"]
            has_next_page = page_info["hasNextPage"]
            after_cursor = page_info["endCursor"]
            pages += 1
            print(f"  [Collection {collection_handle}] page {pages}: {len(all_ids)} IDs so far")
        except Exception as e:
            print(f"[!] Collection fetch error: {e}")
            break

    return list(all_ids)


def fetch_product_ids_by_type(product_types):
    all_ids = set()
    has_next_page = True
    after_cursor = None
    print(product_types)

    type_query = " OR ".join(f"product_type:'{ptype}'" for ptype in product_types)

    while has_next_page:
        query = """
        query ($queryStr: String!, $cursor: String) {
          products(first: 250, after: $cursor, query: $queryStr) {
            pageInfo {
              hasNextPage
              endCursor
            }
            edges {
              node {
                id
                availableForSale
              }
            }
          }
        }
        """

        variables = {
            "queryStr": type_query,
            "cursor": after_cursor
        }

        payload = {
            "query": query,
            "variables": variables
        }

        response = requests.post(graphql_url, headers=headers, json=payload)
        data = response.json()
        edges = data["data"]["products"]["edges"]

        for edge in edges:
            node = edge["node"]
            if node["availableForSale"]:
                gid = node["id"]
                numeric_id = gid.split("/")[-1]
                all_ids.add(numeric_id)

        page_info = data["data"]["products"]["pageInfo"]
        has_next_page = page_info["hasNextPage"]
        after_cursor = page_info["endCursor"]

    return list(all_ids)


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
          createdAt
          description
          descriptionHtml
          productType
          onlineStoreUrl
          options { id name values }
          featuredImage {
            id
            originalSrc
            transformedSrc(maxWidth: 800, maxHeight: 800, crop: CENTER)
          }
          updatedAt
          tags
          totalInventory
          vendor
          requiresSellingPlan
          compareAtPriceRange {
            maxVariantPrice { amount currencyCode }
            minVariantPrice { amount currencyCode }
          }
          priceRange {
            maxVariantPrice { amount currencyCode }
            minVariantPrice { amount currencyCode }
          }
          images(first: 100) {
            edges {
              node {
                id
                originalSrc
                transformedSrc(maxWidth: 800, maxHeight: 800, crop: CENTER)
              }
            }
          }
          variants(first: 100) {
            edges {
              node {
                id
                sku
                title
                price { amount currencyCode }
                weight
                weightUnit
                requiresShipping
                currentlyNotInStock
                compareAtPrice { amount currencyCode }
                quantityAvailable
                selectedOptions { name value }
                availableForSale
                image {
                  id
                  originalSrc
                  transformedSrc(maxWidth: 800, maxHeight: 800, crop: CENTER)
                }
              }
            }
          }
        }
      }
    }
    """  # omitted for brevity (use your full query here)
    all_responses = {"data": {"nodes": []}}

    for i in range(0, len(product_ids), 250):
        batch = product_ids[i:i+250]
        payload = {
            "query": query,
            "variables": {
                "ids": batch,
                "countryCode": "US",
                "languageCode": "EN"
            }
        }

        try:
            response = requests.post(graphql_url, headers=headers, json=payload)
            if response.status_code == 200:
                data = response.json()
                all_responses["data"]["nodes"].extend(data.get("data", {}).get("nodes", []))
                print(f"[✓] Batch {i//250+1} fetched")
            else:
                print(f"[✗] Failed batch {i//250+1}: {response.status_code}")
                print(response.text)
        except Exception as e:
            print(f"[!] Exception in batch {i//250+1}: {e}")
        time.sleep(1.2)
    # # Save the results to a JSON file
    # with open("output.json", "w", encoding="utf-8") as f:
    #     json.dump(all_responses, f, ensure_ascii=False, indent=4)
    return all_responses

def clean_and_save_product_data_only_available_with_all_images_from_data(
    data, gender_tag=None, product_type=None
):
    products = data.get("data", {}).get("nodes", [])
    cleaned_products = {}

    for product in products:
        if product is None:
            continue

        handle = product.get("handle")
        title = product.get("title")
        description = product.get("descriptionHtml") or f"<p>{product.get('description', '')}</p>"
        brand = product.get("vendor", "")
        product_tags = set(product.get("tags", []))

        # Gender-based tags
        gender_tags = set()
        if gender_tag:
            if gender_tag.lower() == "men":
                gender_tags = {"all clothing men", "mens", "men clothing", "men"}
            elif gender_tag.lower() == "women":
                gender_tags = {"all clothing women", "womens", "women clothing", "women"}

        all_tags = product_tags | gender_tags

        # Collect all image URLs
        all_images = []
        seen_images = set()
        for edge in product.get("images", {}).get("edges", []):
            url = edge["node"].get("originalSrc")
            if url and url not in seen_images:
                all_images.append(url)
            seen_images.add(url)

        # Skip if no images found
        if not all_images:
            continue

        # Category is just gender
        category_val = gender_tag.lower() if gender_tag else ""

        # Use provided product_type if available
        type_val = product.get("productType")
        product_type = type_val.split(":")[-1].strip().lower() if type_val else ""
        type_tags = set([t.strip().lower() for t in type_val.split(":") if t.strip()])
        all_tags |= type_tags
        tags_str = ', '.join(sorted(all_tags))

        if handle not in cleaned_products:
            cleaned_products[handle] = {
                "Handle": handle,
                "Title": title,
                "Body (HTML)": description,
                "Vendor": brand,
                "Product Category": category_val,
                "Type": product_type,
                "Tags": tags_str,
                "variants": []
            }

        seen = set()
        for edge in product.get("variants", {}).get("edges", []):
            variant = edge["node"]
            if not variant.get("availableForSale", False):
                continue

            sku = variant.get("sku", "")
            price = float(variant.get("price", {}).get("amount", 0))
            compare_price = float(variant.get("compareAtPrice", {}).get("amount", 0)) if variant.get("compareAtPrice") else 0
            color, size = "", ""
            for opt in variant.get("selectedOptions", []):
                if opt["name"].lower() == "color":
                    color = opt["value"]
                elif opt["name"].lower() == "size":
                    size = opt["value"]

            if (size, sku) not in seen:
                cleaned_products[handle]["variants"].append({
                    "Variant SKU": sku,
                    "size": size,
                    "color": color,
                    "Variant Price": price,
                    "Variant Compare At Price": compare_price,
                    "images": all_images
                })
                seen.add((size, sku))

    return list(cleaned_products.values())


def complete_workflow_aloyoga(collection_url=None, base_url=None, gender_tag=None, max_pages=20):
    """
    Scrape Alo Yoga products.

    Args:
        collection_url: Optional single Alo Yoga URL. Still supported for
            backwards compatibility.
        base_url: Optional URL or comma-separated URLs. If provided, it overrides
            the default womens + mens collections. Example:
            "https://www.aloyoga.com/collections/womens-shop-all,https://www.aloyoga.com/pages/mens"
        gender_tag: Optional "women" or "men" override.
    """
    if collection_url:
        urls = [collection_url]
    elif base_url:
        urls = [u.strip() for u in base_url.split(",") if u.strip()]
    else:
        urls = [
            "https://www.aloyoga.com/collections/womens-shop-all",
            "https://www.aloyoga.com/pages/mens",
        ]

    all_products = []
    seen_handles = set()

    for url in urls:
        handle, inferred_gender = _parse_collection_url(url)
        if not handle:
            print(f"❌ Could not parse collection URL: {url}")
            continue
        final_gender = gender_tag or inferred_gender or "women"
        print(f"🔍 Scraping collection '{handle}'...")
        unique_ids = fetch_product_ids_by_collection(handle, gender_filter=None, max_pages=max_pages)
        print(f"🎯 Total Unique Product IDs for {url}: {len(unique_ids)}")

        if not unique_ids:
            print(f"❌ No product IDs found for {url}. Skipping.")
            continue

        gids = format_shopify_gids(unique_ids)
        print("📦 Fetching product data in batches...")
        raw_data = fetch_shopify_products_batched(gids)

        products = clean_and_save_product_data_only_available_with_all_images_from_data(raw_data, final_gender)

        # Remove duplicate products by handle within this batch
        for prod in products:
            if prod["Handle"] not in seen_handles:
                all_products.append(prod)
                seen_handles.add(prod["Handle"])

    print(f"📊 Total unique products collected across {len(urls)} URL(s): {len(all_products)}")

    if not all_products:
        print("❌ No products found. Exiting.")
        return

    # Alo Yoga exposes each colour as its own product; collapse them back into one product.
    all_products = group_by_handle_without_color(all_products)

    # Unified upsert for products, tags, and colors
    upsert_all_product_data(all_products, BASE_URL, "USD")

    # Generate Shopify CSV
    csv_path = "scraped_files/aloyoga_latest.csv"
    rows = transform_to_shopify(all_products)
    export_shopify_csv(rows, csv_path)
    print(f"✅ Alo Yoga complete: {len(all_products)} products, CSV -> {csv_path}")
    return all_products


# 🔧 Run Everything
if __name__ == "__main__":
    complete_workflow_aloyoga()


