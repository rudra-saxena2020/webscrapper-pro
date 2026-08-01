import requests
import time
import json
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.tag_engine import (
    clean_title, generate_handle,
    build_full_tags,
)
from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv

SCRAPER_ID = "jwpei"
CURRENCY = "INR"
GRAPHQL_URL = "https://www.jwpei.com/api/unstable/graphql.json"
STOREFRONT_TOKEN = "a127a6ad6cee5426ab8d566db57b9b5e"
COLLECTION_HANDLE = "shop-all"
BATCH_SIZE = 50  # products per page

# ── Product-type inference (JW PEI API returns raw SKU codes as productType) ─
_TYPE_KEYWORDS = [
    # Footwear — most specific first
    ("flip-flop",       "Flip-Flops"),
    ("flipflop",        "Flip-Flops"),
    ("espadrille",      "Espadrilles"),
    ("bootie",          "Booties"),
    ("sneaker",         "Sneakers"),
    ("loafer",          "Loafers"),
    ("sandal",          "Sandals"),
    ("pump",            "Pumps"),
    ("mule",            "Mules"),
    ("slide",           "Slides"),
    ("boot",            "Boots"),
    ("heel",            "Heels"),
    ("flat",            "Flats"),
    # Apparel — longer phrases before substrings ("jumpsuit" before "jump")
    ("dress",           "Dress"),
    ("skirt",           "Skirt"),
    ("swimwear",        "Swimwear"),
    ("bodysuit",        "Bodysuit"),
    ("jumpsuit",        "Jumpsuit"),
    ("cardigan",        "Cardigan"),
    ("knitwear",        "Knitwear"),
    ("sweater",         "Knitwear"),
    ("hoodie",          "Hoodie"),
    ("blouse",          "Blouse"),
    ("jacket",          "Jacket"),
    ("coat",            "Coat"),
    ("pant",            "Pants"),
    ("short",           "Shorts"),
    ("shirt",           "Shirt"),
    # Bags — MOST SPECIFIC first so "top handle" beats "top", "shoulder bag" beats "bag"
    ("mini bag",        "Mini Bag"),
    ("micro bag",       "Mini Bag"),
    ("belt bag",        "Belt Bag"),
    ("fanny pack",      "Belt Bag"),
    ("bucket bag",      "Bucket Bag"),
    ("shoulder bag",    "Shoulder Bag"),
    ("top handle",      "Top Handle Bag"),
    ("crossbody",       "Crossbody Bag"),
    ("backpack",        "Backpack"),
    ("satchel",         "Satchel"),
    ("hobo",            "Hobo Bag"),
    ("tote",            "Tote Bag"),
    ("handbag",         "Handbag"),
    ("bag",             "Handbag"),    # catches anything with "bag" not already matched
    # Non-bag accessories — only reached when title has NO "bag"
    ("shawl",           "Shawl"),
    ("cape",            "Shawl"),
    ("scarf",           "Scarf"),
    ("coin purse",      "Coin Purse"),
    ("card holder",     "Card Holder"),
    ("card case",       "Card Holder"),
    ("wallet",          "Wallet"),
    ("pouch",           "Pouch"),
    ("clutch",          "Clutch"),
    ("wristlet",        "Wristlet"),
    ("hat",             "Hat"),
    ("glove",           "Gloves"),
    ("sunglasses",      "Sunglasses"),
    ("sunglass",        "Sunglasses"),
    ("necklace",        "Necklace"),
    ("pendant",         "Necklace"),
    ("earring",         "Earrings"),
    ("bracelet",        "Bracelet"),
    ("bangle",          "Bangle"),
    ("ring",            "Ring"),
    ("charm",           "Charm"),
    ("keyring",         "Keyring"),
    ("keychain",        "Keyring"),
    ("belt",            "Belt"),
    ("top",             "Top"),        # apparel top — only if no bag/shoe matched
]

def _infer_product_type(api_type: str, title: str) -> str:
    """
    Return a meaningful Shopify product type.
    JW PEI's productType field is always a raw SKU code (e.g. '2T68', 'JH306B04').
    We detect this and infer the type from the product title instead.
    """
    # If it looks like a real type (contains spaces and is not all-caps SKU-style)
    t = (api_type or "").strip()
    if t and " " in t and t != t.upper():
        return t  # already a proper human-readable label

    # Title-based inference — ordered from most-specific to least-specific
    title_l = (title or "").lower()
    for keyword, label in _TYPE_KEYWORDS:
        if keyword in title_l:
            return label
    return "Accessories"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.jwpei.com",
    "Referer": "https://www.jwpei.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "x-shopify-storefront-access-token": STOREFRONT_TOKEN,
}

COLLECTION_QUERY = """
query($handle: String!, $cursor: String) {
  collectionByHandle(handle: $handle) {
    products(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          handle
          description
          descriptionHtml
          productType
          vendor
          tags
          availableForSale
          onlineStoreUrl
          priceRange {
            minVariantPrice { amount currencyCode }
            maxVariantPrice { amount currencyCode }
          }
          images(first: 20) {
            edges { node { originalSrc altText } }
          }
          variants(first: 50) {
            edges {
              node {
                sku
                availableForSale
                price { amount currencyCode }
                compareAtPrice { amount currencyCode }
                selectedOptions { name value }
                image { originalSrc }
              }
            }
          }
          options { name values }
        }
      }
    }
  }
}
"""


def _fetch_all_products(progress_callback=None, stop_event=None):
    """Paginate through the collection and return all raw product nodes."""
    all_products = []
    cursor = None
    has_next = True
    page = 0

    while has_next:
        if stop_event and stop_event.is_set():
            return all_products

        payload = {
            "query": COLLECTION_QUERY,
            "variables": {"handle": COLLECTION_HANDLE, "cursor": cursor},
        }
        try:
            resp = requests.post(GRAPHQL_URL, headers=HEADERS, json=payload, timeout=45)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"⚠️  Page {page} fetch error: {e} — retrying in 5s")
            time.sleep(5)
            continue

        col = (data.get("data") or {}).get("collectionByHandle") or {}
        products_node = col.get("products") or {}
        edges = products_node.get("edges") or []
        page_info = products_node.get("pageInfo") or {}

        for edge in edges:
            node = edge.get("node")
            if node:
                all_products.append(node)

        page += 1
        has_next = page_info.get("hasNextPage", False)
        cursor = page_info.get("endCursor")

        fetched_so_far = len(all_products)
        if progress_callback:
            pct = min(55, 20 + int(fetched_so_far / 2000 * 35))
            progress_callback(pct, f"Fetched {fetched_so_far} products from JW PEI…")

        print(f"  Page {page}: {len(edges)} products | total so far: {fetched_so_far}")
        time.sleep(0.25)

    return all_products


def _build_description(raw_description, product_type, brand="JW PEI"):
    """Build a Mirage-style description from the raw Shopify description."""
    desc = (raw_description or "").strip()
    if not desc:
        desc = f"Stylish {product_type} from {brand}, crafted with premium quality materials."

    # Extract material info
    material_keywords = [
        "Vegan Leather", "Genuine Leather", "Leather", "Polyester",
        "Nylon", "Cotton", "Canvas", "Suede", "Denim", "PU",
    ]
    key_features = []
    for mat in material_keywords:
        if mat.lower() in desc.lower():
            key_features.append(mat)
            break

    features_html = ""
    if key_features:
        features_html += f'<li>Material: {key_features[0]}</li>'

    # Truncate long descriptions
    desc_snippet = desc[:600] if len(desc) > 600 else desc

    body_html = (
        f'<p>Discover the iconic <strong>{brand} {product_type}</strong>, '
        f'exclusively curated by <strong>The Mirage</strong>. '
        f'A masterpiece of artisanal craftsmanship, this piece embodies '
        f"the brand's distinctive design philosophy and premium quality standards.</p>"
        f'<p><strong>Key Features &amp; Characteristics:</strong></p>'
        f'<ul>{features_html}</ul>'
        f'<p>{desc_snippet}</p>'
        f'<p><em>Exclusively curated for Mirage Retail Collective.</em></p>'
    )
    return body_html


def _enrich_sibling_images(all_products: list, min_trigger: int = 5, max_images: int = 10) -> list:
    """
    Boost sparse-image products by borrowing gallery shots from colour siblings.

    JW PEI sells each colour as a separate product.  Many colours have only 1-2
    images while a sibling colour of the same style has 5-7.  This function:

      1. Groups products by their BASE TITLE — the part before the first " - "
         (e.g. "Noor Top Handle Bag" groups Taupe Brown + Elephant Gray).
      2. For any product with fewer than `min_trigger` images, appends images
         from its siblings (deduplicated by filename stem).
      3. The product's OWN hero image is always kept first; sibling extras follow.
      4. Total gallery is capped at `max_images` per product.

    Products that already have >= min_trigger images are left untouched.
    Solo products (no siblings) are left untouched.
    """
    from collections import defaultdict

    def _img_stem(url: str) -> str:
        """Return the filename stem for deduplication (strips CDN path + query params)."""
        path = url.split("?")[0]
        return path.split("/")[-1].split(".")[0].lower()

    # ── Step 1: group by base title ──────────────────────────────────────────
    groups: dict = defaultdict(list)
    for prod in all_products:
        title = prod.get("Title", "")
        base = title.split(" - ", 1)[0].strip() if " - " in title else title
        groups[base].append(prod)

    enriched_count = 0

    for base, group in groups.items():
        if len(group) < 2:
            continue

        # ── Step 2: build the full sibling image pool (ordered by frequency) ─
        img_order: list = []
        seen_stems: set = set()
        for prod in group:
            for v in prod.get("variants", []):
                for img in v.get("images", []):
                    stem = _img_stem(img)
                    if stem and stem not in seen_stems:
                        seen_stems.add(stem)
                        img_order.append(img)

        # ── Step 3: enrich sparse products ───────────────────────────────────
        for prod in group:
            # Count unique images this product currently has
            own_stems: set = set()
            for v in prod.get("variants", []):
                for img in v.get("images", []):
                    own_stems.add(_img_stem(img))

            if len(own_stems) >= min_trigger:
                continue

            # Extra images = siblings' images not already owned by this product
            extras = [img for img in img_order if _img_stem(img) not in own_stems]
            if not extras:
                continue

            # Append extras to every variant's image list, up to max_images
            for v in prod.get("variants", []):
                current = list(v.get("images", []))
                slots = max_images - len(current)
                if slots > 0:
                    v["images"] = current + extras[:slots]

            enriched_count += 1

    if enriched_count:
        print(f"🖼️  Sibling image enrichment: {enriched_count} products boosted from siblings")

    return all_products


def _clean_products(raw_nodes):
    """Convert raw GraphQL product nodes into the standard Mirage product dict list."""
    cleaned = {}

    for product in raw_nodes:
        if not product:
            continue

        # Skip unavailable products
        if not product.get("availableForSale", True):
            continue

        title = product.get("title", "").strip()
        cleaned_title = clean_title(title)
        if not cleaned_title:
            continue

        handle_key = generate_handle(cleaned_title)
        if not handle_key:
            continue

        brand = product.get("vendor") or "JW PEI"
        product_type = _infer_product_type(
            product.get("productType") or "", cleaned_title
        )
        raw_desc = product.get("description", "")
        body_html = _build_description(raw_desc, product_type, brand)

        # Extract images
        all_images = [
            e["node"]["originalSrc"]
            for e in (product.get("images") or {}).get("edges", [])
            if e["node"].get("originalSrc")
        ]
        if not all_images:
            continue

        # Gender detection — JW PEI is women's by default
        tags_list = product.get("tags") or []
        tag_str = " ".join(tags_list).lower()
        detected_gender = "women"
        if any(kw in tag_str for kw in ["mens", " men", "male", "masculine"]):
            detected_gender = "men"
        if "unisex" in tag_str:
            detected_gender = "unisex"

        # Build standardised tags
        tags_str = build_full_tags(
            title=cleaned_title,
            vendor=brand,
            gender=detected_gender,
            product_type=product_type,
            url="",
            extra_tags=["RudraScrapper-jwpei"],
        )

        if handle_key not in cleaned:
            cleaned[handle_key] = {
                "Handle": handle_key,
                "Title": cleaned_title,
                "Body (HTML)": body_html,
                "Vendor": brand,
                "Product Category": "",
                "Type": product_type,
                "Tags": tags_str,
                "Google Shopping / Gender": detected_gender,
                "variants": [],
            }

        # Process variants
        seen_fps = set()
        for edge in (product.get("variants") or {}).get("edges", []):
            v = edge["node"]

            if not v.get("availableForSale", False):
                continue

            price_node = v.get("price") or {}
            price_raw = float(price_node.get("amount") or 0)
            if price_raw <= 0:
                continue

            currency_code = price_node.get("currencyCode") or "USD"
            compare_raw = float((v.get("compareAtPrice") or {}).get("amount") or 0)
            sku = v.get("sku") or ""

            # Extract color / size from selectedOptions
            color, size = "", ""
            for opt in v.get("selectedOptions") or []:
                name_lower = (opt.get("name") or "").lower()
                val = opt.get("value") or ""
                if name_lower in ("color", "colour"):
                    color = val
                elif name_lower == "size":
                    # Normalise JW PEI's "6 | US" / "2 | US" format → "US 6"
                    if "|" in val:
                        parts = [p.strip() for p in val.split("|")]
                        num   = next((p for p in parts if p.replace(".", "").isdigit()), "")
                        unit  = next((p for p in parts if not p.replace(".", "").isdigit()), "")
                        val   = f"{unit} {num}".strip() if unit and num else (num or val)
                    size = val

            fp = (sku, size, color)
            if fp in seen_fps:
                continue
            seen_fps.add(fp)

            # Variant image first, then product images
            v_img = (v.get("image") or {}).get("originalSrc") or ""
            if v_img and v_img not in all_images:
                variant_images = [v_img] + all_images
            elif v_img:
                variant_images = [v_img] + [i for i in all_images if i != v_img]
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

        # Remove products that ended up with no valid variants
        if not cleaned.get(handle_key, {}).get("variants"):
            cleaned.pop(handle_key, None)
            continue

        # Append available sizes/colors to description
        avail_sizes = list(dict.fromkeys(
            v["size"] for v in cleaned[handle_key]["variants"]
            if v.get("size") and v["size"] not in ("", "Default", "Default Title")
        ))
        avail_colors = list(dict.fromkeys(
            v["color"] for v in cleaned[handle_key]["variants"]
            if v.get("color") and v["color"] not in ("", "Default", "Default Title")
        ))
        extra = ""
        if avail_sizes:
            extra += f'<p><strong>Available Sizes:</strong> {", ".join(avail_sizes)}</p>'
        if avail_colors:
            extra += f'<p><strong>Available Colors:</strong> {", ".join(avail_colors)}</p>'
        if extra:
            cleaned[handle_key]["Body (HTML)"] += extra

    return list(cleaned.values())


def complete_workflow_jwpei(progress_callback=None, stop_event=None, **kwargs):
    """Main entry point — called by app.py scraper dispatcher."""
    scrape_record_id = start_scrape_record(SCRAPER_ID)

    def _cb(p, s, c=0):
        if progress_callback:
            progress_callback(p, s, c)

    try:
        _cb(5, "Connecting to JW PEI store…")

        # Fetch all products via collection pagination
        raw_nodes = _fetch_all_products(
            progress_callback=progress_callback,
            stop_event=stop_event,
        )
        print(f"🎯 Total JW PEI raw nodes: {len(raw_nodes)}")
        _cb(60, f"Fetched {len(raw_nodes)} raw products. Cleaning…")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        # Clean
        all_products = _clean_products(raw_nodes)
        print(f"✓ {len(all_products)} products after cleaning")

        # Boost sparse-image products using sibling colour images
        all_products = _enrich_sibling_images(all_products, min_trigger=5, max_images=10)

        _cb(75, f"Cleaned {len(all_products)} JW PEI products. Saving to DB…", len(all_products))

        if not all_products:
            update_scrape_record(
                scrape_record_id, status="failed",
                error_message="No products survived cleaning.",
            )
            return []

        # Save to DB
        heartbeat_scrape_record(scrape_record_id, len(all_products))
        upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)

        # Export CSV
        os.makedirs("scraped_files", exist_ok=True)
        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        _cb(90, "Generating Shopify CSV…")
        rows = transform_to_shopify(all_products)
        export_shopify_csv(rows, csv_path)

        # Upload CSV to Supabase
        _cb(96, "Uploading CSV…")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)

        update_scrape_record(
            scrape_record_id, status="completed",
            products_count=len(all_products), csv_url=csv_url,
        )
        _cb(100, f"Done! {len(all_products)} JW PEI products.", len(all_products))
        print(f"✅ JW PEI complete: {len(all_products)} products → {csv_path}")
        return all_products

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ JW PEI Error: {e}")
        try:
            update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        except Exception:
            pass
        _cb(0, f"Error: {e}", 0)
        return []


if __name__ == "__main__":
    complete_workflow_jwpei()
