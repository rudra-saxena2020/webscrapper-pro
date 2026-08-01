"""
Dr. Martens Scraper v1.0
========================
Strategy:
  Phase 1 — Listing pages (HTML, no bot-protection on DM site)
             Women: /us/en/womens/c/01000000?size_w_usa=w_size_5..w_size_12
             Men:   /us/en/mens/c/02000000?size_m_usa=m_size_6..m_size_14
             Parse ACC.productList.initPageLoad JSON embedded in each page.
             Women: ~281 products / 6 pages | Men: ~177 products / 4 pages

  Phase 2 — Product detail pages (parallel, 8 workers)
             Fetch meta description from each product page.
             Images: cdn.media.amplience.net/i/drmartens/{code}.{80-84}.jpg

  Phase 3 — Gemini description rewrite in Mirage premium voice.

  Phase 4 — Shopify CSV: one row per size variant.
             Women US 5-12  → UK 3-10
             Men   US 6-14  → UK 5-13
             USD → INR via pricing pipeline.

Currency: USD
Scraper ID: drmartens
"""

import os
import re
import sys
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import build_full_tags, sanitize_html_description, append_brand_message

SCRAPER_ID = "drmartens"
CURRENCY   = "USD"
BASE_URL   = "https://www.drmartens.com"

WOMEN_SIZES_US = [5, 6, 7, 8, 9, 10, 11, 12]
MEN_SIZES_US   = [6, 7, 8, 9, 10, 11, 12, 13, 14]

WOMEN_US_TO_UK = {5: "3", 6: "4", 7: "5", 8: "6", 9: "7", 10: "8", 11: "9", 12: "10"}
MEN_US_TO_UK   = {6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "10", 12: "11", 13: "12", 14: "13"}

WOMEN_LISTING_URL = (
    "https://www.drmartens.com/us/en/womens/c/01000000"
    "?size_w_usa=w_size_5&size_w_usa=w_size_6&size_w_usa=w_size_7"
    "&size_w_usa=w_size_8&size_w_usa=w_size_9&size_w_usa=w_size_10"
    "&size_w_usa=w_size_11&size_w_usa=w_size_12"
)
MEN_LISTING_URL = (
    "https://www.drmartens.com/us/en/mens/c/02000000"
    "?size_m_usa=m_size_7&size_m_usa=m_size_6&size_m_usa=m_size_8"
    "&size_m_usa=m_size_9&size_m_usa=m_size_10&size_m_usa=m_size_11"
    "&size_m_usa=m_size_12&size_m_usa=m_size_13&size_m_usa=m_size_14"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_IMAGE_BASE    = "https://cdn.media.amplience.net/i/drmartens"
_IMAGE_INDICES = [80, 81, 82, 83, 84]
_IMAGE_PRESET  = "$medium$"   # ?$pdp$ is invalid (404); ?$medium$ confirmed working

_PTYPE_MAP = {
    "mary jane": "Mary Jane Shoes",
    "chelsea":   "Chelsea Boots",
    "loafer":    "Loafers",
    "oxford":    "Oxford Shoes",
    "mule":      "Mules",
    "slide":     "Slides",
    "sandal":    "Sandals",
    "platform":  "Platform Boots",
    "wedge":     "Wedge Boots",
    "ankle":     "Ankle Boots",
    "lace-up":   "Lace-Up Boots",
    "lace up":   "Lace-Up Boots",
    "boot":      "Boots",
    "shoe":      "Shoes",
}


def _get_product_type(name: str) -> str:
    nl = name.lower()
    for kw, pt in _PTYPE_MAP.items():
        if kw in nl:
            return pt
    if "sandal" in nl:
        return "Sandals"
    if "boot" in nl:
        return "Boots"
    return "Shoes"


def _parse_usd(formatted: str) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", formatted))
    except Exception:
        return 0.0


def _build_images(code: str) -> list:
    return [f"{_IMAGE_BASE}/{code}.{idx}.jpg?{_IMAGE_PRESET}" for idx in _IMAGE_INDICES]


def _fetch_listing_page(base_url: str, page: int) -> tuple:
    """Return (results_list, n_pages)."""
    url = f"{base_url}&page={page}" if page > 0 else base_url
    try:
        r = requests.get(url, headers=_HEADERS, timeout=25)
        r.raise_for_status()
        m = re.search(r"ACC\.productList\.initPageLoad\((\{.*?\})\s*\);", r.text, re.DOTALL)
        if not m:
            return [], 1
        data    = json.loads(m.group(1))
        results = data.get("results", [])
        n_pages = data.get("pagination", {}).get("numberOfPages", 1)
        return results, n_pages
    except Exception as e:
        print(f"[DrMartens] Listing page={page} error: {e}")
        return [], 1


def _collect_gender_products(gender: str, base_url: str, stop_event=None) -> list:
    """Collect all unique products for one gender from all listing pages."""
    results_p0, n_pages = _fetch_listing_page(base_url, 0)
    print(f"[DrMartens] {gender.capitalize()}: {n_pages} pages to fetch")

    seen, products = set(), []

    def _process(results_list):
        for raw in results_list:
            # ACC.productList.initPageLoad wraps each item under a "current" key:
            # {"results": [{"current": {"code": "…", "name": "…", …}}, …]}
            p = raw.get("current", raw)
            if not isinstance(p, dict):
                continue
            code = (p.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            products.append({
                "code":           code,
                "name":           p.get("name", ""),
                "colour":         p.get("colour", ""),
                "formattedPrice": p.get("formattedPrice", "$0"),
                "url":            p.get("url", ""),
                "thumbnailUrl":   p.get("thumbnailImgUrl", ""),
                "gender":         gender,
            })

    _process(results_p0)

    for page in range(1, n_pages):
        if stop_event and stop_event.is_set():
            break
        results, _ = _fetch_listing_page(base_url, page)
        _process(results)
        time.sleep(0.4)

    print(f"[DrMartens] {gender.capitalize()}: {len(products)} unique products")
    return products


def _fetch_meta_description(product_url: str) -> str:
    """Fetch the meta description from a Dr. Martens product page."""
    full_url = f"{BASE_URL}{product_url}" if product_url.startswith("/") else product_url
    try:
        r = requests.get(full_url, headers=_HEADERS, timeout=20)
        html = r.text
        m = re.search(r'<meta name="description" content="([^"]{10,})"', html)
        if m:
            return m.group(1).strip()
    except Exception as e:
        print(f"[DrMartens] Detail fetch error: {e}")
    return ""


def _generate_description(name: str, colour: str, ptype: str, gender: str, raw_desc: str) -> str:
    """Rewrite in Mirage premium voice via Gemini, or return a rich fallback."""
    try:
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("No GEMINI_API_KEY")
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        prompt = (
            f"Write a premium 2–3 sentence product description for The Mirage, a luxury fashion curator.\n"
            f"Product: Dr. Martens {name} in {colour}\n"
            f"Type: {ptype} | Gender: {gender}\n"
            f"Context: {raw_desc}\n\n"
            f"Voice: confident, editorial, rebellious heritage meets luxury curation.\n"
            f"Highlight: iconic DM craftsmanship, the specific style name, colour/material, and the customer it's for.\n"
            f"Return ONLY the description text — no HTML tags, no quotation marks."
        )
        resp  = model.generate_content(prompt)
        text  = resp.text.strip()
        brand_msg = append_brand_message("Dr. Martens")
        return f"<p>{text}</p>{brand_msg}"
    except Exception as e:
        print(f"[DrMartens] Gemini error: {e}")
        brand_msg = append_brand_message("Dr. Martens")
        base = raw_desc or (
            f"Discover the iconic {name} by Dr. Martens in {colour}. "
            f"Exclusively curated by The Mirage — where rebellious craftsmanship meets premium style."
        )
        return f"<p>{base}</p>{brand_msg}"


def _build_product(p: dict, desc: str) -> dict | None:
    """Build a Shopify-compatible product dict."""
    code   = p["code"]
    name   = p["name"]
    colour = p.get("colour", "")
    gender = p["gender"]

    usd_price = _parse_usd(p.get("formattedPrice", "$0"))
    if usd_price <= 0:
        print(f"[DrMartens] Skip {name} — no price")
        return None

    ptype  = _get_product_type(name)
    title  = f"{name}" + (f" — {colour}" if colour else "")

    handle_base  = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    colour_slug  = re.sub(r"[^a-z0-9]+", "-", colour.lower()).strip("-")
    handle       = f"{handle_base}-{colour_slug}-drm-{code}"
    handle       = re.sub(r"-{2,}", "-", handle).strip("-")

    gender_tag  = "women" if gender == "women" else "men"
    tags        = build_full_tags(
        title, "Dr. Martens", gender_tag, ptype,
        extra_tags=["RudraScrapper-drmartens", "dr-martens", "footwear-brand"],
    )

    images = _build_images(code)

    sizes_us  = WOMEN_SIZES_US  if gender == "women" else MEN_SIZES_US
    us_to_uk  = WOMEN_US_TO_UK  if gender == "women" else MEN_US_TO_UK
    gender_cap = gender.capitalize()

    variants = []
    for us_sz in sizes_us:
        uk_sz = us_to_uk.get(us_sz, str(us_sz))
        variants.append({
            "Option1 Name":  "Size",
            "Option1 Value": f"UK {uk_sz} (US {gender_cap} {us_sz})",
            "Variant SKU":   f"{code}-UK{uk_sz}-{'W' if gender == 'women' else 'M'}",
            "Variant Price": usd_price,
            "currency":      CURRENCY,
            "images":        images,
        })

    return {
        "Handle":                   handle,
        "Title":                    title,
        "Body (HTML)":              desc,
        "Vendor":                   "Dr. Martens",
        "Type":                     ptype,
        "Tags":                     tags,
        "Google Shopping / Gender": gender_tag,
        "images":                   images,
        "variants":                 variants,
        "url":                      f"{BASE_URL}{p.get('url', '')}",
        "_gender_refined":          True,
    }


def complete_workflow_drmartens(progress_callback=None, stop_event=None, **kwargs):
    """
    Main entry point — mirrors the established scraper pattern
    (start_scrape_record → heartbeat → phases 1-4 → update_scrape_record).
    """
    scrape_record_id = start_scrape_record(SCRAPER_ID)
    heart_stop       = threading.Event()

    def _cb(pct, status, count=None):
        if progress_callback:
            progress_callback(SCRAPER_ID, pct, status, count)
        print(f"[DrMartens] {pct}% — {status}")

    _hb_count = [0]

    def _heartbeat():
        while not heart_stop.is_set():
            try:
                heartbeat_scrape_record(scrape_record_id, _hb_count[0])
            except Exception:
                pass
            time.sleep(30)

    threading.Thread(target=_heartbeat, daemon=True).start()

    try:
        # ── Phase 1: Collect product listings ─────────────────────────────
        _cb(5, "Collecting women's listing pages...")
        women = _collect_gender_products("women", WOMEN_LISTING_URL, stop_event)

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        _cb(20, "Collecting men's listing pages...")
        men = _collect_gender_products("men", MEN_LISTING_URL, stop_event)

        all_raw = women + men
        _cb(30, f"{len(all_raw)} products found — fetching descriptions...")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        # ── Phase 2: Fetch product detail pages in parallel ────────────────
        detail_cache: dict[str, str] = {}

        def _fetch_detail(item):
            code    = item["code"]
            raw     = _fetch_meta_description(item.get("url", ""))
            return code, raw

        total  = len(all_raw)
        done   = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_detail, p): p for p in all_raw}
            for fut in as_completed(futs):
                if stop_event and stop_event.is_set():
                    break
                code, raw_desc = fut.result()
                detail_cache[code] = raw_desc
                done += 1
                if done % 25 == 0:
                    prog = 30 + int(done / total * 35)
                    _cb(prog, f"Details: {done}/{total}...")

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        # ── Phase 3: Gemini descriptions + build products ──────────────────
        _cb(68, "Generating Mirage-voice descriptions...")
        final_products = []
        for item in all_raw:
            if stop_event and stop_event.is_set():
                break
            code   = item["code"]
            raw    = detail_cache.get(code, "")
            ptype  = _get_product_type(item["name"])
            desc   = _generate_description(
                item["name"], item.get("colour", ""),
                ptype, item["gender"], raw
            )
            prod = _build_product(item, desc)
            if prod:
                final_products.append(prod)

        if not final_products:
            msg = "No products built."
            update_scrape_record(scrape_record_id, status="failed", error_message=msg)
            _cb(0, f"Failed: {msg}")
            return []

        _cb(80, f"{len(final_products)} products — saving to DB...", len(final_products))

        # ── Phase 4: Persist + export ──────────────────────────────────────
        upsert_all_product_data(final_products, SCRAPER_ID, CURRENCY)

        os.makedirs("scraped_files", exist_ok=True)
        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"

        _cb(90, "Generating Shopify CSV...")
        export_shopify_csv(transform_to_shopify(final_products), csv_path)

        _cb(97, "Uploading CSV to Supabase...")
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)

        update_scrape_record(
            scrape_record_id,
            status="completed",
            products_count=len(final_products),
            csv_url=csv_url,
        )
        _cb(100, f"Done ✅  {len(final_products)} Dr. Martens products", len(final_products))
        print(f"✅ Dr. Martens complete: {len(final_products)} products")
        return final_products

    except Exception as e:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        raise

    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_drmartens()
