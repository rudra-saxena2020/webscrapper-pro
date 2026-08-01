import requests
import time
import json
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from core.db import upsert_all_product_data
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
import re
# shopify: {
#     staging: "gymshark-staging",
#     production: "gymshark"
# },
# storefront: {
#     staging: "60b46bd76dd1449a52b294c68961a3b2",
#     production: "b65646444b3639704a6ecb270cd28ce7"
# },

BASE_URL = "https://www.gymshark.com"

url = "https://gymsharkusa.myshopify.com/api/2025-01/graphql.json"
headers = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.gymshark.com",
    "Referer": "https://www.gymshark.com",
    "User-Agent": "Mozilla/5.0",
    "x-shopify-storefront-access-token": "527c7a1ab34f53ca4dc301924baee65d"
}



def format_shopify_gids(product_ids):
    return [f"gid://shopify/Product/{pid}" for pid in product_ids]


def _parse_collection_url(url: str):
    """Return (collection_handle, gender_filter_tag) from a Gymshark collection URL.

    Supports URLs like:
      https://www.gymshark.com/collections/all-products/womens
      https://www.gymshark.com/collections/all-products/mens
    The last path segment is interpreted as a gender filter when it is 'womens' or 'mens'.
    """
    if not url:
        return None, None
    url = url.strip().rstrip("/")
    parts = [p for p in url.split("/") if p]
    # Find the 'collections' segment index
    try:
        coll_idx = parts.index("collections")
    except ValueError:
        return None, None
    # After 'collections' we expect at least one segment (the handle)
    if coll_idx + 1 >= len(parts):
        return None, None
    handle = parts[coll_idx + 1]
    # If there is a trailing gender filter segment, use it
    if coll_idx + 2 < len(parts) and parts[coll_idx + 2].lower() in ("womens", "mens"):
        gender_tag = parts[coll_idx + 2].lower()
        filter_tag = gender_tag.capitalize()  # Shopify tags: Womens, Mens
        return handle, filter_tag
    return handle, None


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
            response = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=30)
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

        response = requests.post(url, headers=headers, json=payload)
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
          variants(first: 250) {
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
            response = requests.post(url, headers=headers, json=payload)
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

        if not product.get("availableForSale", True):
            continue
        handle = product.get("handle")
        title = product.get("title")
        description = product.get("descriptionHtml") or f"<p>{product.get('description', '')}</p>"
        brand = product.get("vendor", "")
        product_tags = list(set(product.get("tags", [])))


        all_images = []
        seen_images = set()
        for edge in product.get("images", {}).get("edges", []):
            url = edge["node"].get("originalSrc")
            if url and url not in seen_images:
                all_images.append(url)
            seen_images.add(url)

        # Category is just gender
        category_val = gender_tag.lower() if gender_tag else ""
        type_val = product.get("productType")
        type_val = re.sub(r'\bwomens\b', '', type_val, flags=re.IGNORECASE).strip()
        gender_tags = set()
        if gender_tag:
            if gender_tag.lower() == "men":
                gender_tags = {"all clothing men", "mens", "men clothing", "men"}
            elif gender_tag.lower() == "women":
                gender_tags = {"all clothing women", "womens", "women clothing", "women"}
            else:
                gender_tags = {"men", "women", "unisex","shoes", "unisex"}

        all_tags = product_tags + list(gender_tags)
        tags_str = ', '.join(sorted(all_tags))

        if handle not in cleaned_products:
            cleaned_products[handle] = {
                "Handle": handle,
                "Title": title,
                "Body (HTML)": description,
                "Vendor": brand,
                "Product Category": category_val,
                "Type": type_val,
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

    # Return as a list of product dicts
    return list(cleaned_products.values())



def complete_workflow_gymshark(collection_url=None, base_url=None, gender_tag=None, max_pages=20):
    """
    Scrape Gymshark products.

    Args:
        collection_url: Optional single Gymshark collection URL. Still supported
            for backwards compatibility.
        base_url: Optional URL or comma-separated URLs. If provided, it overrides
            the default womens + mens collections. Example:
            "https://www.gymshark.com/collections/all-products/womens,https://www.gymshark.com/collections/all-products/mens"
        gender_tag: Optional "women" or "men" override. Only used when a single
            collection URL is supplied and gender cannot be inferred from the URL.
    """
    # Build the list of URLs to scrape
    if collection_url:
        urls = [collection_url]
    elif base_url:
        urls = [u.strip() for u in base_url.split(",") if u.strip()]
    else:
        urls = [
            "https://www.gymshark.com/collections/all-products/womens",
            "https://www.gymshark.com/collections/all-products/mens",
        ]

    all_products = []
    seen_handles = set()

    for url in urls:
        handle, gender_filter = _parse_collection_url(url)
        if not handle:
            print(f"❌ Could not parse collection URL: {url}")
            continue
        inferred_gender = gender_tag or ("women" if gender_filter == "Womens" else "men" if gender_filter == "Mens" else None)
        print(f"🔍 Scraping collection '{handle}' with gender filter '{gender_filter}'...")
        unique_ids = fetch_product_ids_by_collection(handle, gender_filter=gender_filter, max_pages=max_pages)
        print(f"🎯 Total Unique Product IDs for {url}: {len(unique_ids)}")

        if not unique_ids:
            print(f"❌ No product IDs found for {url}. Skipping.")
            continue

        gids = format_shopify_gids(unique_ids)
        print("📦 Fetching product data in batches...")
        raw_data = fetch_shopify_products_batched(gids)

        products = clean_and_save_product_data_only_available_with_all_images_from_data(raw_data, inferred_gender)

        # Remove duplicate products by handle within this batch
        for prod in products:
            if prod["Handle"] not in seen_handles:
                all_products.append(prod)
                seen_handles.add(prod["Handle"])

    print(f"📊 Total unique products collected across {len(urls)} URL(s): {len(all_products)}")

    if not all_products:
        print("❌ No products found. Exiting.")
        return

    # Unified upsert for products, tags, and colors
    upsert_all_product_data(all_products, BASE_URL, "USD")

    # Generate Shopify CSV
    csv_path = "scraped_files/gymshark_latest.csv"
    rows = transform_to_shopify(all_products)
    export_shopify_csv(rows, csv_path)
    print(f"✅ Gymshark complete: {len(all_products)} products, CSV -> {csv_path}")
    return all_products


# 🔧 Run Everything
if __name__ == "__main__":
    complete_workflow_gymshark()


