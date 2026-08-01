import json
import re
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import requests

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

from core.tag_engine import (
    clean_title, generate_handle, apply_standardized_tags,
    detect_gender, sanitize_html_description,
    build_full_tags, append_brand_message, build_mirage_description,
)
from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv

BASE_URL   = "https://www.mytheresa.com"
SCRAPER_ID = "mytheresa"
CURRENCY   = "EUR"

_MYTHERESA_TYPE_SINGULAR = {
    "Minidresses": "minidress", "Gowns": "gown", "Shirts": "shirt",
    "Sweaters": "sweater", "Miniskirts": "miniskirt", "Blazers": "blazer",
    "Blouses": "blouse", "Loafers": "loafer", "Cardigans": "cardigan",
    "Slides": "slide", "Mules": "mule", "Swimsuits": "swimsuit",
    "Earrings": "earring", "Bikinis": "bikini", "Shorts": "pair of shorts",
    "Bodysuits": "bodysuit", "Scarves": "scarf", "Vests": "vest",
    "Gloves": "glove", "Sunglasses": "sunglasses piece", "Belts": "belt",
    "Leggings": "pair of leggings", "Kaftans": "kaftan", "Chinos": "chino",
    "Shoppers": "shopper", "Sweatshirts": "sweatshirt", "Bombers": "bomber",
    "Bracelets": "bracelet", "Necklaces": "necklace", "Hoodies": "hoodie",
    "Clutches": "clutch", "Beanies": "beanie", "Sandals": "sandal",
    "Sweatpants": "pair of sweatpants", "Pouches": "pouch", "Rings": "ring",
    "Wallets": "wallet", "Caps": "cap", "Bras": "bra", "Parkas": "parka",
    "Socks": "pair of socks", "Business": "business piece",
    "Mini Dresses": "mini dress", "Maxi Dresses": "maxi dress",
    "Mini Skirts": "mini skirt", "Maxi Skirts": "maxi skirt",
    "Square Sunglasses": "square sunglasses", "Round Sunglasses": "round sunglasses",
    "Cat Eye Sunglasses": "cat-eye sunglasses",
    "Platform Sandals": "platform sandal", "Platform Boots": "platform boot",
    "Ankle Boots": "ankle boot", "Chelsea Boots": "Chelsea boot",
    "Knee Boots": "knee boot", "Combat Boots": "combat boot",
    "Lace-Up Shoes": "lace-up shoe", "Slip-On Shoes": "slip-on shoe",
}

MYTHERESA_API = "https://api.mytheresa.com/api"

# ── Per-gender designer lists (from user-supplied sale URLs) ──────────────────
WOMEN_DESIGNERS = [
    "Ala\u00efa", "Ami Paris", "Amina Muaddi", "Amiri", "Aquazzura",
    "Balenciaga", "Balmain", "Burberry", "Chlo\u00e9", "DeMellier",
    "Deveaux New York", "Dolce&Gabbana", "Fendi", "Ferragamo",
    "Givenchy", "Golden Goose", "Gucci", "Jacquemus", "Jimmy Choo",
    "Kenzo", "Mach & Mach", "Maison Margiela", "Off-White",
    "Oscar de la Renta", "Palm Angels", "Prada", "Saint Laurent",
    "Savette", "Self-Portrait", "Stella McCartney", "The Row",
    "Tory Burch", "Valentino", "Valentino Garavani", "Versace",
    "Vivienne Westwood",
]

MEN_DESIGNERS = [
    "Ami Paris", "Amiri", "Brunello Cucinelli", "Burberry",
    "Canada Goose", "Fendi", "Givenchy", "Gucci", "Jacquemus",
    "Kenzo", "Lanvin", "Loewe", "Maison Margiela", "Missoni",
    "New Balance", "Polo Ralph Lauren", "Prada", "Saint Laurent",
    "The North Face", "The Row", "Tod's", "Valentino",
    "Valentino Garavani", "Versace",
]

SECTIONS = [
    {
        "section":    "women",
        "slug":       "/",
        "gender_tag": "women",
        "designers":  WOMEN_DESIGNERS,
    },
    {
        "section":    "men",
        "slug":       "/",
        "gender_tag": "men",
        "designers":  MEN_DESIGNERS,
    },
]


# ── HTML browsing helpers ────────────────────────────────────────────────────

_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_API_HEADERS = {
    "Accept-Language": "en",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://www.mytheresa.com",
    "Referer": "https://www.mytheresa.com/",
    "X-Country": "DE",
    "X-Nsu": "false",
    "X-Op": "ntr",
    "X-Region": "DE",
    "X-Section": "women",
    "X-Store": "euro",
}


def _fetch_all_skus_from_sitemap(section: str, max_skus: int = 5000, stop_event=None) -> list:
    """
    Mytheresa sale HTML pages only show ~80–100 products per locale, missing
    many sale items (e.g. Rockstud 60). Instead we grab all product URLs from
    the XML sitemap, extract SKUs, and query the API to find which are actually
    on sale. This yields comprehensive coverage.
    """
    if not _HAS_CFFI:
        raise ImportError(
            "curl_cffi is required for Mytheresa scraping (bot detection). "
            "Install with: pip install curl_cffi"
        )

    sess = cffi_requests.Session()

    # 1. Get sitemap index
    try:
        idx_resp = sess.get("https://www.mytheresa.com/sitemap.xml", impersonate="chrome131", timeout=20)
        idx_resp.raise_for_status()
    except Exception as exc:
        print(f"[MyTheresa] Sitemap index fetch error: {exc}")
        return []

    submaps = re.findall(r"<loc>([^<]+)</loc>", idx_resp.text)

    # Pick a small-region sitemap (AT has ~78 k women products across 3 parts)
    product_maps = [s for s in submaps if "AT_en_Part" in s or "US_en_Part" in s]
    if not product_maps:
        product_maps = [s for s in submaps if "en_Part" in s and "product" in s.lower()]

    # Filter for section—women or men
    path_keyword = f"/{section}/"

    skus: list = []
    seen: set = set()

    for pmap in product_maps:  # All parts — ~78 k women products total
        if stop_event and stop_event.is_set():
            break
        try:
            p_resp = sess.get(pmap, impersonate="chrome131", timeout=30)
            p_resp.raise_for_status()
        except Exception as exc:
            print(f"[MyTheresa] Sitemap {pmap} fetch error: {exc}")
            continue

        urls = re.findall(r"<loc>([^<]+)</loc>", p_resp.text)
        for url in urls:
            if path_keyword not in url:
                continue
            m = re.search(r"-p(\d{8})", url)
            if not m:
                continue
            sku = f"P{m.group(1)}"
            if sku in seen:
                continue
            seen.add(sku)
            skus.append(sku)
            if len(skus) >= max_skus:
                break

        if len(skus) >= max_skus:
            break

        print(f"[MyTheresa] Sitemap {pmap.split('/')[-1]}: {len(skus)} SKUs so far")
        time.sleep(0.3)

    return skus[:max_skus]


def _filter_sale_skus(all_skus: list, stop_event=None) -> list:
    """
    Batch-query xProductList to determine which SKUs are on sale.
    A product is considered on sale when discount < original price.
    """
    if not all_skus:
        return []

    BATCH_SIZE = 50
    sale_skus: list = []
    sess = cffi_requests.Session()

    try:
        sess.get("https://www.mytheresa.com/", impersonate="chrome131", timeout=10)
    except Exception:
        pass

    total_batches = (len(all_skus) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(all_skus), BATCH_SIZE):
        if stop_event and stop_event.is_set():
            break

        batch = all_skus[i : i + BATCH_SIZE]
        q = f"""
        query {{
            xProductList(skus: {json.dumps(batch)}) {{
                sku
                price {{ original discount }}
                hasStock
            }}
        }}
        """

        try:
            resp = sess.post(
                MYTHERESA_API,
                headers=_API_HEADERS,
                json={"query": q},
                timeout=30,
                impersonate="chrome131",
            )
            data = resp.json()
        except Exception as exc:
            print(f"[MyTheresa] Batch {i // BATCH_SIZE + 1}/{total_batches} error: {exc}")
            continue

        if data.get("data") and data["data"].get("xProductList"):
            for p in data["data"]["xProductList"]:
                if not p.get("hasStock", False):
                    continue
                price = p.get("price") or {}
                orig = price.get("original") or 0
                disc = price.get("discount") or 0
                # On sale = discount price is strictly lower than original
                if disc > 0 and disc < orig:
                    sale_skus.append(p["sku"])

        if (i // BATCH_SIZE + 1) % 10 == 0:
            print(
                f"[MyTheresa] Checked {i + len(batch)}/{len(all_skus)} SKUs, "
                f"{len(sale_skus)} on sale so far"
            )

        time.sleep(0.2)

    print(f"[MyTheresa] Total sale SKUs: {len(sale_skus)} out of {len(all_skus)}")
    return sale_skus


def _fetch_section_skus(section: str, max_pages: int = 30, stop_event=None) -> list:
    """
    Discover sale SKUs for a section (women / men).

    Strategy:
      1. Extract ALL product SKUs from the XML sitemap (comprehensive coverage).
      2. Batch-query the API to find which SKUs are actually on sale.
    """
    all_skus = _fetch_all_skus_from_sitemap(section, max_skus=30000, stop_event=stop_event)
    if not all_skus:
        return []
    sale_skus = _filter_sale_skus(all_skus, stop_event=stop_event)
    return sale_skus


_XPRODUCT_LIST_QUERY = """
query ProductList($skus: [String]) {
    xProductList(skus: $skus) {
        name
        slug
        designer
        color
        hasStock
        isPurchasable
        combinedCategoryName
        description
        material
        price { original discount percentage currencyCode }
        displayImages
        sku
    }
}
"""


def _fetch_products_by_skus(skus: list, stop_event=None) -> list:
    """
    Batch-query xProductList GraphQL API for SKU details.
    Mytheresa API limits batch size; we use 25 per request with brief delays.
    """
    if not skus:
        return []

    BATCH_SIZE = 50
    all_products: list = []

    sess = cffi_requests.Session()
    # Prime cookies with a main-page visit (helps API auth)
    try:
        sess.get("https://www.mytheresa.com/", impersonate="chrome131", timeout=10)
    except Exception:
        pass

    total_batches = (len(skus) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(skus), BATCH_SIZE):
        if stop_event and stop_event.is_set():
            break

        batch = skus[i : i + BATCH_SIZE]
        try:
            resp = sess.post(
                MYTHERESA_API,
                headers=_API_HEADERS,
                json={"query": _XPRODUCT_LIST_QUERY, "variables": {"skus": batch}},
                timeout=30,
                impersonate="chrome131",
            )
            data = resp.json()
        except Exception as exc:
            print(f"[MyTheresa] Batch {i // BATCH_SIZE + 1}/{total_batches} network error: {exc}")
            time.sleep(1.0)
            continue

        if data.get("data") and data["data"].get("xProductList"):
            products = data["data"]["xProductList"]
            all_products.extend(products)
            print(
                f"[MyTheresa] Batch {i // BATCH_SIZE + 1}/{total_batches}: "
                f"{len(products)} products (total {len(all_products)})"
            )
        else:
            errors = data.get("errors", [])
            msg = errors[0].get("message") if errors else "unknown error"
            print(f"[MyTheresa] Batch {i // BATCH_SIZE + 1}/{total_batches} API error: {msg}")

        time.sleep(0.3)

    return all_products


def _normalize_img_url(url: str, size: int = 1000, quality: int = 95) -> str:
    """Upscale an img.mytheresa.com URL to the requested resolution."""
    decoded = url.replace("\\u002F", "/").replace("\\u002f", "/")
    result = re.sub(
        r"https://img\.mytheresa\.com/\d+/\d+/\d+/\w+/catalog/product/",
        f"https://img.mytheresa.com/{size}/{size}/{quality}/jpeg/catalog/product/",
        decoded, count=1,
    )
    return result


# All known Mytheresa CDN image angle suffixes (confirmed by CDN HEAD probing).
_CDN_SUFFIXES = [
    ".jpg", "_b1.jpg", "_d1.jpg", "_d2.jpg", "_d3.jpg",
    "_d4.jpg", "_b2.jpg", "_d5.jpg", "_d6.jpg", "_d7.jpg",
    "_b3.jpg", "_b4.jpg", "_d8.jpg", "_d9.jpg",
]

_CDN_BASE_RE = re.compile(
    r"catalog/product/(\w{2})/([\w]+?)(?:_\w+)?\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)


def _probe_product_images(display_images: list) -> list:
    """
    Root-cause fix: Mytheresa's listing API only exposes 2 displayImages per
    product; extra gallery angles are loaded client-side by their Vue SPA.

    Instead we hit the CDN directly with HEAD requests for every known angle
    suffix (_b1, _d1 … _d7, _b2). The CDN returns 200 for existing angles and
    403/404 for missing ones — no rate-limits, very fast (~80 ms per probe).
    With 30 workers we process 6 k products in under 3 minutes.
    """
    if not display_images:
        return []

    first = display_images[0] if isinstance(display_images[0], str) else ""
    m = _CDN_BASE_RE.search(first)
    if not m:
        return [_normalize_img_url(u) for u in display_images if u]

    folder  = m.group(1)   # e.g. "4d"
    base_id = m.group(2)   # e.g. "P01165736"
    cdn_pfx = (
        f"https://img.mytheresa.com/1000/1000/95/jpeg/catalog/product/{folder}"
    )

    found: list = []
    sess = requests.Session()
    for suf in _CDN_SUFFIXES:
        url = f"{cdn_pfx}/{base_id}{suf}"
        try:
            r = sess.head(url, timeout=6, allow_redirects=True)
            if r.status_code == 200:
                found.append(url)
        except Exception:
            continue

    return found if found else [_normalize_img_url(u) for u in display_images if u]


def _enhance_product_images(products: list, stop_event=None, progress_callback=None) -> list:
    """
    For each product, probe the Mytheresa CDN for every image angle and
    replace the 2-image displayImages with the full gallery.
    Uses 30 workers (HEAD requests are very lightweight).
    """
    total = len(products)
    done  = 0
    enhanced: dict = {}

    def _work(product):
        slug = product["Handle"]
        display_imgs: list = []
        for v in product.get("variants", []):
            imgs = v.get("images") or []
            if imgs:
                display_imgs = imgs
                break
        probed = _probe_product_images(display_imgs)
        return slug, probed

    with ThreadPoolExecutor(max_workers=30) as pool:
        futures = {pool.submit(_work, p): p for p in products}
        for fut in as_completed(futures):
            if stop_event and stop_event.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            slug, imgs = fut.result()
            if imgs:
                enhanced[slug] = imgs
            done += 1
            if progress_callback and done % 100 == 0:
                pct = 55 + int((done / total) * 25)
                progress_callback(pct, f"Fetching product images: {done}/{total}…", total)

    for p in products:
        slug = p["Handle"]
        imgs = enhanced.get(slug)
        if imgs:
            for v in p.get("variants", []):
                v["images"] = imgs
    return products


def _clean_api_products(products: list, gender_tag: str) -> list:
    """
    Convert xProductList API responses into cleaned product dicts
    ready for Shopify CSV generation.

    xProductList returns one entry per SKU (no per-size variants), so each
    product becomes a single-variant entry with Default Title.
    """
    cleaned: dict = {}

    for product in products:
        if not product.get("hasStock", False):
            continue
        if not product.get("isPurchasable", True):
            continue

        slug = (product.get("slug") or "").lstrip("/")
        if not slug:
            continue

        title  = product.get("name", "").strip()
        brand  = (product.get("designer") or "").strip()
        combined = product.get("combinedCategoryName", "") or ""
        parts    = [p.strip() for p in combined.split("::") if p.strip()]
        p_type   = parts[-1] if parts else ""

        raw_desc = (product.get("description") or "").strip()
        color  = (product.get("color") or "").strip()

        tags_str = build_full_tags(
            title, brand, gender_tag, p_type,
            extra_tags=["RudraScrapper-mytheresa"],
        )

        images: list = []
        seen_imgs: set = set()
        for img in (product.get("displayImages") or []):
            if img and img not in seen_imgs:
                seen_imgs.add(img)
                images.append(_normalize_img_url(img))

        # Price: Mytheresa returns EUR cents (e.g. 71200 = €712.00)
        price_data = product.get("price") or {}
        sale_cents = price_data.get("discount") or price_data.get("original") or 0
        orig_cents = price_data.get("original") or sale_cents

        sale_eur = round(sale_cents / 100, 2) if sale_cents else 0.0
        orig_eur = round(orig_cents / 100, 2) if orig_cents else 0.0

        if sale_eur <= 0:
            continue

        sku = (product.get("sku") or "").strip()
        if not sku:
            continue

        if slug not in cleaned:
            cleaned[slug] = {
                "Handle":      slug,
                "Title":       title,
                "Body (HTML)": build_mirage_description(raw_desc, title, brand or "Mytheresa", gender_tag),
                "Vendor":      brand or "Mytheresa",
                "Type":        p_type,
                "Tags":        tags_str,
                "_color_name": color,
                "variants":    [],
            }

        # xProductList does not return per-size variants; emit single variant
        cleaned[slug]["variants"].append({
            "Variant SKU":              sku,
            "Variant Price":            sale_eur,
            "Variant Compare At Price": orig_eur if orig_eur > sale_eur else "",
            "currency":                 "EUR",
            "size":                     "Default",
            "color":                    color,
            "images":                   images,
        })

    return [p for p in cleaned.values() if p["variants"]]


def complete_workflow_mytheresa(progress_callback=None, stop_event=None, **kwargs):
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
        if not _HAS_CFFI:
            raise ImportError(
                "curl_cffi is required for Mytheresa scraping (bot detection). "
                "Install with: pip install curl_cffi"
            )

        all_products: list = []
        total_sections = len(SECTIONS)

        for i, section_cfg in enumerate(SECTIONS):
            if stop_event and stop_event.is_set():
                update_scrape_record(scrape_record_id, status="cancelled")
                return

            section    = section_cfg["section"]
            gender_tag = section_cfg["gender_tag"]
            base_pct   = int(i * 80 / total_sections) + 5

            _cb(base_pct, f"Fetching MyTheresa {section} sale SKUs…")

            skus = _fetch_section_skus(section, stop_event=stop_event)
            _cb(base_pct + int(30 / total_sections), f"Fetching details for {len(skus)} SKUs…")

            api_products = _fetch_products_by_skus(skus, stop_event=stop_event)

            _cb(base_pct + int(35 / total_sections), f"Cleaning {section} data…")
            section_products = _clean_api_products(api_products, gender_tag)

            _cb(
                base_pct + int(35 / total_sections),
                f"{section}: {len(section_products)} raw products — fetching full images…",
                len(all_products) + len(section_products),
            )
            section_products = _enhance_product_images(
                section_products, stop_event=stop_event, progress_callback=_cb
            )
            all_products.extend(section_products)

            _cb(
                base_pct + int(38 / total_sections),
                f"{section}: {len(section_products)} products with full images",
                len(all_products),
            )

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return

        _cb(85, f"Saving {len(all_products)} products to database…", len(all_products))
        upsert_all_product_data(all_products, SCRAPER_ID, CURRENCY)

        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"
        os.makedirs("scraped_files", exist_ok=True)

        _cb(90, "Generating Shopify CSV…", len(all_products))
        rows = transform_to_shopify(all_products)
        export_shopify_csv(rows, csv_path)

        _cb(96, "Uploading CSV…", len(all_products))
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)

        update_scrape_record(
            scrape_record_id,
            status="completed",
            products_count=len(all_products),
            csv_url=csv_url,
        )
        _cb(100, "Done ✅", len(all_products))

    except Exception as exc:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(exc))
        raise
    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_mytheresa()
