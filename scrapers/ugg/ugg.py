"""
UGG Scraper v1.0
================
UGG.com is protected by Cloudflare + PerimeterX — all direct HTTP returns 403
even with curl_cffi TLS impersonation.

Strategy:
  Phase 1 — Product URL discovery via SFCC hreflang XML (static CDN, no Cloudflare).
             URL: demandware.static/Sites-UGG-US-Site/…/en_US-hreflang.xml
             Filters for adult women/men footwear; excludes kids, apparel, accessories.
             ~120-150 footwear products.

  Phase 2 — Product pages via SeleniumBase UC mode (bypasses Cloudflare/PerimeterX).
             For each product URL, extract: name, price, sizes, images, description.
             Data sources in priority order:
               1. JSON-LD Product schema (<script type="application/ld+json">)
               2. window.dataLayer push with ecommerce.detail
               3. SFCC page HTML (h1, .sales-price, .size-btn, .product-img)

  Phase 3 — Build Shopify products with size variants.
             USD → INR via pricing pipeline.
             Tags via build_full_tags + RudraScrapper-ugg identity tag.

  Phase 4 — Export Shopify CSV + upsert DB.

Currency: USD
Scraper ID: ugg
"""

import os
import re
import sys
import json
import time
import threading

import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import build_full_tags, append_brand_message

SCRAPER_ID = "ugg"
CURRENCY   = "USD"
BASE_URL   = "https://www.ugg.com"

_HREFLANG_XML = (
    "https://www.ugg.com/on/demandware.static/Sites-UGG-US-Site"
    "/Sites/en_US/ugg-us/en_US-hreflang.xml"
)

_FOOTWEAR_CATS = {
    "women-footwear", "women-boots", "women-boots-classic-boots",
    "women-shoes", "women-sandals", "women-sandals-slides",
    "women-sandals-flip-flops", "women-sandals-wedges",
    "women-slippers", "women-sneakers", "women-casuals-sneakers",
    "women-casuals-moccasins", "women-clogs-mules", "women-heels",
    "women-platforms", "women-rain-boots", "women-snow-boots",
    "women-cold-weather-boots", "women-fashion-boots",
    "men-footwear", "men-boots", "men-boots-classic-boots",
    "men-shoes", "men-slippers", "men-sneakers", "men-sandals",
    "men-shoes-moccasins", "men-shoes-sneakers", "men-flip-flops",
    "men-indoor-outdoor-slippers", "men-moccasins",
    "men-cold-weather-boots",
}
_EXCLUDE_CATS = {
    "kids", "sale", "apparel", "socks", "accessories",
    "home", "gift", "care", "cleaning", "cozy",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# ── ScraperAPI key (optional) ───────────────────────────────────────────────
# UGG.com is protected by Cloudflare + DataDome — both block Replit's datacenter
# IP regardless of browser automation technique. ScraperAPI uses residential IPs
# and JS rendering to bypass both layers transparently.
# Free tier: 1,000 requests/month → enough for a full UGG run (134 URLs).
# Sign up: https://www.scraperapi.com  → copy your API key → set as secret.
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

_PTYPE_MAP = {
    # Multi-word / specific model names first (highest priority)
    "classic ultra mini platform": "Platform Boot",
    "classic ultra mini":          "Ankle Boot",
    "classic mini platform":       "Platform Boot",
    "classic mini":                "Ankle Boot",
    "classic short":               "Ankle Boot",
    "classic tall":                "Tall Boot",
    "ultra mini":                  "Ankle Boot",
    "mini bailey":                 "Ankle Boot",
    "bailey button":               "Ankle Boot",
    "fluff yeah":                  "Slide",
    "disquette":                   "Slide",
    "tazz platform":               "Platform Boot",
    "tasman platform":             "Platform Boot",
    "neumel platform":             "Platform Boot",
    "classic platform":            "Platform Boot",
    # Generic product type keywords (checked after specifics)
    "platform":                    "Platform Boot",
    "tasman":                      "Slipper",
    "tazz":                        "Loafer",
    "neumel":                      "Ankle Boot",
    "classic":                     "Boot",
    "moccasin":                    "Moccasin",
    "sneaker":                     "Sneaker",
    "slipper":                     "Slipper",
    "sandal":                      "Sandal",
    "slide":                       "Slide",
    "loafer":                      "Loafer",
    "mule":                        "Mule",
    "boot":                        "Boot",
    "shoe":                        "Shoes",
}


def _get_product_type(name: str) -> str:
    nl = name.lower()
    for kw, pt in _PTYPE_MAP.items():
        if kw in nl:
            return pt
    return "Footwear"


def _parse_usd(val) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", str(val)))
    except Exception:
        return 0.0


def _fetch_footwear_urls() -> list:
    """
    Parse the SFCC hreflang XML (static CDN, no Cloudflare)
    and return all adult footwear product URLs.
    """
    try:
        r = requests.get(_HREFLANG_XML, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        all_urls = re.findall(r"<loc>(https://www\.ugg\.com/[^<]+)</loc>", r.text)
        footwear = []
        for url in all_urls:
            path  = url.replace(BASE_URL + "/", "")
            cat   = path.split("/")[0] if "/" in path else ""
            if not cat:
                continue
            if any(ex in cat for ex in _EXCLUDE_CATS):
                continue
            if cat in _FOOTWEAR_CATS or any(fc in cat for fc in ("women-boots", "men-boots", "women-shoes", "men-shoes", "women-sandals", "men-sandal", "women-slippers", "men-slippers", "women-sneakers", "men-sneakers")):
                footwear.append(url)
        print(f"[UGG] Found {len(footwear)} footwear URLs from hreflang XML")
        return footwear
    except Exception as e:
        print(f"[UGG] Hreflang XML fetch error: {e}")
        return []


def _extract_json_ld(html: str) -> dict:
    """Extract Product schema from JSON-LD script tags."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Product"), {})
            if data.get("@type") == "Product":
                return data
        except Exception:
            pass
    return {}


def _extract_datalayer(html: str) -> dict:
    """Extract product info from window.dataLayer."""
    m = re.search(r"window\.dataLayer\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        return {}
    try:
        dl = json.loads(m.group(1))
        for entry in dl:
            if isinstance(entry, dict):
                prods = (
                    entry.get("ecommerce", {}).get("detail", {}).get("products")
                    or entry.get("ecommerce", {}).get("impressions")
                    or []
                )
                if prods:
                    return prods[0]
    except Exception:
        pass
    return {}


def _extract_sfcc_html(html: str, url: str) -> dict:
    """
    Fallback HTML extraction for SFCC product pages.
    Returns: {name, price, description, images, sizes, gender, pid}
    """
    result = {}

    # Name
    m = re.search(r'<h1[^>]*class="[^"]*product-name[^"]*"[^>]*>([^<]{3,100})</h1>', html)
    if not m:
        m = re.search(r'<h1[^>]*itemprop="name"[^>]*>([^<]{3,100})</h1>', html)
    if not m:
        m = re.search(r'<h1[^>]*>([^<]{5,100})</h1>', html)
    if m:
        result["name"] = m.group(1).strip()

    # Price
    for pat in [
        r'itemprop="price"[^>]*content="([0-9]+\.?[0-9]*)"',
        r'"price"\s*:\s*"?\$?([0-9]+\.?[0-9]*)"?',
        r'class="[^"]*sales[^"]*"[^>]*>\$([0-9]+\.?[0-9]*)',
    ]:
        pm = re.search(pat, html)
        if pm:
            result["price"] = _parse_usd(pm.group(1))
            break

    # PID from URL (last numeric segment)
    pid_m = re.search(r"/(\d{6,8})\.html", url)
    if pid_m:
        result["pid"] = pid_m.group(1)

    # Description
    for pat in [
        r'<div[^>]+class="[^"]*product-short-description[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+class="[^"]*product-description[^"]*"[^>]*>(.*?)</div>',
        r'itemprop="description"[^>]*>(.*?)</(?:div|p|span)',
    ]:
        dm = re.search(pat, html, re.DOTALL)
        if dm:
            txt = re.sub(r"<[^>]+>", " ", dm.group(1)).strip()
            txt = re.sub(r"\s+", " ", txt)
            if len(txt) > 20:
                result["description"] = txt
                break

    # Images
    imgs = re.findall(
        r'(?:data-src|src)="(https://[^"]+ugg[^"]+(?:\.jpg|\.png|\.webp)[^"]*)"',
        html
    )
    imgs += re.findall(
        r'"(https://scene7\.com/is/image/[^"]+)"',
        html
    )
    imgs += re.findall(
        r'"(https://[^"]*ugg[^"]*(?:_main|_alt|_hero|_lifestyle)[^"]*\.jpg[^"]*)"',
        html
    )
    result["images"] = list(dict.fromkeys(i for i in imgs if "thumbnail" not in i.lower() and len(i) < 300))

    # Sizes (SFCC size swatches / selectable list items)
    sizes = re.findall(r'data-attr-value="([0-9]+(?:\.[05])?)"', html)
    if not sizes:
        sizes = re.findall(r'data-size="([0-9]+(?:\.[05])?)"', html)
    if not sizes:
        sizes = re.findall(r'"value"\s*:\s*"([0-9]+(?:\.[05]?)")', html)
    result["sizes"] = sorted(set(sizes), key=lambda x: float(x))

    # Gender from URL
    if "women" in url.lower():
        result["gender"] = "women"
    elif "men" in url.lower():
        result["gender"] = "men"
    else:
        result["gender"] = "unisex"

    return result


def _extract_sizes_from_dom(sb) -> list:
    """Pull size option values from the live DOM after JS hydration."""
    try:
        sizes = sb.execute_script("""
            var els = Array.from(document.querySelectorAll(
                '[data-attr-value], [data-size], .size-btn, '
                + '[class*="size-option"], [class*="swatch--size"] button, '
                + 'li[class*="size"] button, li[class*="size"] span'
            ));
            return els.map(e => (e.getAttribute('data-attr-value') || e.getAttribute('data-size') || e.innerText || '').trim())
                      .filter(v => /^[0-9]+(\\.5)?$/.test(v));
        """) or []
        return sorted(set(sizes), key=lambda x: float(x))
    except Exception:
        return []


def _scrape_via_scraperapi(url: str) -> dict:
    """
    Fetch a UGG product page via ScraperAPI (handles Cloudflare + DataDome).
    Uses the existing HTML parsers (_extract_json_ld, _extract_sfcc_html, etc.)
    on the fully-rendered HTML returned by ScraperAPI's JS engine.
    Returns:
      {"_blocked": True}     — ScraperAPI still got a bot-protection block page
      {"_plan_error": True}  — ScraperAPI plan doesn't support premium proxies
      {}                     — other transient failure (skip this URL)
    """
    pid_m  = re.search(r"/(\d{6,8})\.html", url)
    pid    = pid_m.group(1) if pid_m else ""
    gender = "women" if "women" in url.lower() else ("men" if "men" in url.lower() else "unisex")

    try:
        r = requests.get(
            "http://api.scraperapi.com",
            params={
                "api_key":      SCRAPER_API_KEY,
                "url":          url,
                "render":       "true",
                "country_code": "us",
                "device_type":  "desktop",
            },
            timeout=90,
        )
        # Detect ScraperAPI plan-upgrade errors before raise_for_status so we
        # can surface a clear, actionable message instead of a generic HTTP 500.
        # UGG.com requires the premium residential proxy pool (DataDome blocks
        # standard datacenter IPs instantly). Without premium the API returns an
        # instant HTTP 500 with "Protected domains may require adding premium=true".
        if r.status_code in (403, 500) and r.text:
            body_lower = r.text.lower()
            if any(kw in body_lower for kw in ("premium", "upgrade", "your current plan")):
                print(f"[UGG] ScraperAPI plan error: {r.text.strip()[:200]}")
                return {"_plan_error": True, "message": r.text.strip()}
        r.raise_for_status()
    except Exception as e:
        print(f"[UGG] ScraperAPI fetch error for {url}: {e}")
        return {}

    html = r.text or ""
    if len(html) < 500:
        return {}

    # Detect block pages even through ScraperAPI
    if "#cmsg" in html or (
        "access denied" in html[:2000].lower()
        and "datadome" in html[:6000].lower()
    ):
        print(f"[UGG] ScraperAPI still got a block page for {url}")
        return {"_blocked": True}

    # Priority 1: JSON-LD Product schema
    ld = _extract_json_ld(html)
    if ld.get("name") and ld.get("offers"):
        offers = ld["offers"]
        if isinstance(offers, list):
            offers = offers[0]
        price = _parse_usd(offers.get("price", 0))
        if price > 0:
            raw_imgs = ld.get("image") or []
            if isinstance(raw_imgs, str):
                raw_imgs = [raw_imgs]
            images = [img.get("url", "") if isinstance(img, dict) else img for img in raw_imgs]
            sfcc   = _extract_sfcc_html(html, url)
            return {
                "name":        ld["name"],
                "price":       price,
                "description": ld.get("description", ""),
                "images":      [i for i in images if i] or sfcc.get("images", []),
                "sizes":       sfcc.get("sizes", []),
                "gender":      gender,
                "pid":         pid,
                "url":         url,
            }

    # Priority 2: window.dataLayer
    dl = _extract_datalayer(html)
    if dl.get("name") and dl.get("price"):
        sfcc = _extract_sfcc_html(html, url)
        sfcc["name"]   = sfcc.get("name") or dl.get("name", "")
        sfcc["price"]  = sfcc.get("price") or _parse_usd(dl.get("price", 0))
        sfcc["gender"] = gender
        sfcc["pid"]    = pid
        sfcc.setdefault("url", url)
        return sfcc

    # Priority 3: raw HTML patterns
    result = _extract_sfcc_html(html, url)
    result["gender"] = gender
    result["pid"]    = pid
    result.setdefault("url", url)
    return result


def _scrape_product_page(sb, url: str) -> dict:
    """
    Extract product data from a UGG product page using an EXISTING
    SeleniumBase session (sb).  Caller is responsible for opening the URL.

    UGG is a React SPA — initial server HTML contains no product data.
    All JSON-LD / price / name is injected by JavaScript after hydration.
    We use execute_script() to pull data from the LIVE DOM, not raw HTML.
    Returns an empty dict on failure so the caller can skip and continue.
    """
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=8)
    except Exception as e:
        print(f"[UGG] Page load error for {url}: {e}")
        return {}

    # ── Fast-fail: detect Cloudflare challenge OR DataDome IP-block ──────────
    # Layer 1 — Cloudflare Bot Management: headless Chrome gets a JS challenge
    #   page with title "ugg.com" and CSS `#cmsg{animation: A 1.5s;}`.
    #   This challenge never resolves in headless mode (Replit datacenter detected).
    # Layer 2 — DataDome: blocks direct HTTP requests with "Access Denied" page.
    # Both patterns mean 0 product data — abort immediately instead of waiting 30s.
    try:
        _title = sb.get_title() or ""
        _src   = sb.get_page_source() or ""
        _is_cf_challenge = "#cmsg" in _src or "keyframes A{0%{opacity:0" in _src
        _is_datadome = "access denied" in _title.lower() or (
            "access denied" in _src[:2000].lower()
            and ("datadome" in _src[:6000].lower() or "geo.captcha-delivery" in _src[:6000].lower())
        )
        if _is_cf_challenge or _is_datadome:
            _kind = "Cloudflare challenge" if _is_cf_challenge else "DataDome IP-block"
            print(f"[UGG] {_kind} detected at {url} — aborting page immediately")
            return {"_blocked": True}
    except Exception:
        pass

    pid_m  = re.search(r"/(\d{6,8})\.html", url)
    pid    = pid_m.group(1) if pid_m else ""
    gender = "women" if "women" in url.lower() else ("men" if "men" in url.lower() else "unisex")

    # ── Wait for React hydration: poll for product price in live DOM ─────────
    # UGG is a React SPA. Product data (JSON-LD, price, name) is injected by
    # JavaScript. We poll via execute_script until price appears or we give up.
    hydrated = False
    for _ in range(15):          # up to 30 s (15 × 2s)
        try:
            found = sb.execute_script(
                "return !!(document.querySelector('[itemprop=\"price\"], "
                ".product-price, [class*=\"ProductPrice\"], "
                "[class*=\"product-price\"], "
                ".sales .value, [data-testid*=\"price\"]'));"
            )
            if found:
                hydrated = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not hydrated:
        print(f"[UGG] Hydration timeout for {url} — trying anyway")

    time.sleep(0.5)  # small buffer after hydration detected

    # ── Priority 1: JSON-LD from live DOM (post-hydration) ───────────────────
    try:
        ld_texts = sb.execute_script(
            "return Array.from(document.querySelectorAll"
            "('script[type=\"application/ld+json\"]'))"
            ".map(s => s.textContent);"
        ) or []
        for text in ld_texts:
            try:
                data = json.loads(text.strip())
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") == "Product"), {})
                if data.get("@type") == "Product" and data.get("name") and data.get("offers"):
                    offers = data["offers"]
                    if isinstance(offers, list):
                        offers = offers[0]
                    price = _parse_usd(offers.get("price", 0))
                    if price <= 0:
                        continue
                    raw_imgs = data.get("image") or []
                    if isinstance(raw_imgs, str):
                        raw_imgs = [raw_imgs]
                    images = []
                    for img in raw_imgs:
                        if isinstance(img, dict):
                            images.append(img.get("url", ""))
                        elif isinstance(img, str):
                            images.append(img)
                    sizes = _extract_sizes_from_dom(sb)
                    return {
                        "name":        data["name"],
                        "price":       price,
                        "description": data.get("description", ""),
                        "images":      [i for i in images if i],
                        "sizes":       sizes,
                        "gender":      gender,
                        "pid":         pid,
                        "url":         url,
                    }
            except Exception:
                pass
    except Exception:
        pass

    # ── Priority 2: DOM direct extraction ────────────────────────────────────
    try:
        dom = sb.execute_script("""
            var nameEl = document.querySelector(
                'h1[class*="ProductName"], h1[class*="product-name"], '
                + 'h1[itemprop="name"], h1[class*="pdp"], h1');
            var priceEl = document.querySelector(
                '[itemprop="price"], [class*="ProductPrice"], '
                + '[class*="product-price"], .sales .value, '
                + '[data-testid*="price"]');
            var name  = nameEl  ? nameEl.innerText.trim()  : '';
            var price = priceEl
                ? (priceEl.getAttribute('content') || priceEl.innerText || '').replace(/[^0-9.]/g, '')
                : '';
            var imgs = Array.from(document.querySelectorAll(
                'img[src*="ugg"], img[data-src*="ugg"], '
                + '[class*="ProductImage"] img, [class*="product-image"] img'
            )).map(i => i.src || i.dataset.src || '').filter(Boolean);
            return {name: name, price: price, images: imgs};
        """) or {}
        if dom.get("name") and dom.get("price"):
            sizes = _extract_sizes_from_dom(sb)
            print(f"[UGG]  ↳ DOM extraction: {dom['name']} @ ${dom['price']}")
            return {
                "name":    dom["name"],
                "price":   _parse_usd(dom["price"]),
                "images":  dom.get("images", []),
                "sizes":   sizes,
                "gender":  gender,
                "pid":     pid,
                "url":     url,
            }
    except Exception:
        pass

    # ── Priority 3: fall back to raw HTML ─────────────────────────────────────
    try:
        html = sb.get_page_source()
    except Exception:
        return {}
    if not html or len(html) < 500:
        return {}
    # Diagnostic: log first 200 chars to help debug page structure changes
    print(f"[UGG]  ↳ HTML fallback — page snippet: {html[:200].replace(chr(10),' ')}")
    ld = _extract_json_ld(html)
    if ld.get("name") and ld.get("offers"):
        offers = ld["offers"]
        if isinstance(offers, list):
            offers = offers[0]
        price = _parse_usd(offers.get("price", 0))
        raw_imgs = ld.get("image") or []
        if isinstance(raw_imgs, str):
            raw_imgs = [raw_imgs]
        images = [img.get("url", "") if isinstance(img, dict) else img for img in raw_imgs]
        sfcc   = _extract_sfcc_html(html, url)
        return {
            "name":        ld["name"],
            "price":       price,
            "description": ld.get("description", ""),
            "images":      [i for i in images if i],
            "sizes":       sfcc.get("sizes", []),
            "gender":      gender,
            "pid":         pid,
            "url":         url,
        }
    dl = _extract_datalayer(html)
    if dl.get("name") and dl.get("price"):
        sfcc = _extract_sfcc_html(html, url)
        sfcc["name"]  = sfcc.get("name") or dl.get("name", "")
        sfcc["price"] = sfcc.get("price") or _parse_usd(dl.get("price", 0))
        return sfcc
    return _extract_sfcc_html(html, url)


def _generate_description(name: str, gender: str, ptype: str, raw: str) -> str:
    """Mirage-voice description via Gemini."""
    try:
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("No key")
        genai.configure(api_key=api_key)
        model  = genai.GenerativeModel("gemini-2.0-flash")
        prompt = (
            f"Write a 2–3 sentence premium product description for The Mirage, a luxury fashion curator.\n"
            f"Product: UGG {name} | Type: {ptype} | For: {gender}\n"
            f"Context: {raw}\n\n"
            f"Voice: warm, aspirational, lifestyle-focused. Mention the iconic UGG comfort, the specific model, "
            f"and who this is for. No HTML tags, no quotation marks."
        )
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        brand_msg = append_brand_message("UGG")
        return f"<p>{text}</p>{brand_msg}"
    except Exception as e:
        print(f"[UGG] Gemini error: {e}")
        brand_msg = append_brand_message("UGG")
        base = raw or f"Discover the {name} by UGG — iconic comfort and craftsmanship, exclusively curated by The Mirage."
        return f"<p>{base}</p>{brand_msg}"


def _build_product(raw: dict) -> dict | None:
    """Convert raw scraped data → Shopify product dict."""
    name  = (raw.get("name") or "").strip()
    price = _parse_usd(raw.get("price", 0))
    url   = raw.get("url", "")

    if not name or price <= 0:
        return None

    pid    = raw.get("pid", "") or re.search(r"/(\d{6,8})\.html", url)
    if hasattr(pid, "group"):
        pid = pid.group(1)

    gender    = raw.get("gender", "women")
    ptype     = _get_product_type(name)
    gender_tag = "women" if gender == "women" else ("men" if gender == "men" else "unisex")

    handle_base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    handle = f"{handle_base}-ugg-{pid}" if pid else handle_base
    handle = re.sub(r"-{2,}", "-", handle).strip("-")

    images = [i for i in (raw.get("images") or []) if i and i.startswith("http")]

    title  = name
    desc   = _generate_description(name, gender, ptype, raw.get("description", ""))

    tags = build_full_tags(
        title, "UGG", gender_tag, ptype,
        extra_tags=["RudraScrapper-ugg", "ugg", "comfort-fit"],
    )

    # Build size variants
    raw_sizes = raw.get("sizes", [])
    if raw_sizes:
        variants = []
        for sz in raw_sizes:
            try:
                sz_float = float(sz)
            except Exception:
                continue
            # US → UK: Women -2, Men -1
            if gender == "women":
                uk = sz_float - 2
            else:
                uk = sz_float - 1
            uk_label = f"{uk:.0f}" if uk == int(uk) else f"{uk}"
            variants.append({
                "Option1 Name":  "Size",
                "Option1 Value": f"UK {uk_label} (US {gender.capitalize()} {sz})",
                "Variant SKU":   f"{pid}-UK{uk_label}" if pid else f"ugg-{handle_base}-UK{uk_label}",
                "Variant Price": price,
                "currency":      CURRENCY,
                "images":        images,
            })
    else:
        # No sizes extracted — create a single default variant
        variants = [{
            "Option1 Name":  "Title",
            "Option1 Value": "Default Title",
            "Variant SKU":   str(pid) if pid else handle_base,
            "Variant Price": price,
            "currency":      CURRENCY,
            "images":        images,
        }]

    if not images:
        print(f"[UGG] Skip {name} — no images")
        return None

    return {
        "Handle":                   handle,
        "Title":                    title,
        "Body (HTML)":              desc,
        "Vendor":                   "UGG",
        "Type":                     ptype,
        "Tags":                     tags,
        "Google Shopping / Gender": gender_tag,
        "images":                   images,
        "variants":                 variants,
        "url":                      url,
        "_gender_refined":          True,
    }


def complete_workflow_ugg(progress_callback=None, stop_event=None, **kwargs):
    """
    Main entry point — mirrors the established scraper pattern.
    """
    scrape_record_id = start_scrape_record(SCRAPER_ID)
    heart_stop       = threading.Event()

    def _cb(pct, status, count=None):
        if progress_callback:
            try:
                progress_callback(pct, status, count)
            except Exception:
                pass
        print(f"[UGG] {pct}% — {status}")

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
        # ── Phase 1: Product URL discovery ─────────────────────────────────
        _cb(5, "Fetching product URLs from hreflang XML...")
        footwear_urls = _fetch_footwear_urls()

        if not footwear_urls:
            msg = "No footwear URLs found in hreflang XML."
            update_scrape_record(scrape_record_id, status="failed", error_message=msg)
            _cb(0, f"Failed: {msg}")
            return []

        _cb(10, f"{len(footwear_urls)} footwear URLs found — scraping product pages...")

        # ── Phase 2: Scrape product pages ──────────────────────────────────
        raw_products = []
        total        = len(footwear_urls)
        _blocked_count = 0
        _BLOCK_LIMIT   = 3

        if SCRAPER_API_KEY:
            # ── ScraperAPI path ─────────────────────────────────────────────
            # Bypasses both Cloudflare Bot Management and DataDome via
            # residential IPs + JS rendering. No Chrome required.
            print(f"[UGG] ScraperAPI key found — using ScraperAPI for product pages")
            try:
                for i, url in enumerate(footwear_urls):
                    if stop_event and stop_event.is_set():
                        break
                    print(f"[UGG] Scraping {i+1}/{total}: {url}")
                    raw = _scrape_via_scraperapi(url)

                    if raw.get("_plan_error"):
                        raise RuntimeError(
                            "UGG scraper requires a ScraperAPI Premium or Ultra-Premium plan "
                            "to bypass DataDome protection on ugg.com. Your current plan only "
                            "supports the standard proxy pool, which DataDome blocks. "
                            "Upgrade at https://www.scraperapi.com/pricing — the Developer plan "
                            "($49/mo) includes premium residential proxies. "
                            f"ScraperAPI error: {raw.get('message', '')[:200]}"
                        )

                    if raw.get("_blocked"):
                        _blocked_count += 1
                        print(f"[UGG] ScraperAPI block {_blocked_count}/{_BLOCK_LIMIT}")
                        if _blocked_count >= _BLOCK_LIMIT:
                            raise RuntimeError(
                                "ScraperAPI could not bypass UGG's bot protection after "
                                f"{_BLOCK_LIMIT} attempts. Check that your SCRAPER_API_KEY "
                                "is valid and has remaining credits."
                            )
                        continue
                    _blocked_count = 0

                    if not raw.get("url"):
                        raw["url"] = url
                    if raw.get("name") and raw.get("price", 0) > 0:
                        raw_products.append(raw)
                        _hb_count[0] = len(raw_products)
                    else:
                        print(f"[UGG]  ↳ Skipped (missing name/price)")

                    prog = 10 + int((i + 1) / total * 60)
                    _cb(prog, f"Scraped {i+1}/{total} pages ({len(raw_products)} products)...",
                        len(raw_products))

                    time.sleep(0.5)
            except Exception as api_err:
                print(f"[UGG] ScraperAPI error: {api_err}")
                if not raw_products:
                    msg = str(api_err)
                    update_scrape_record(scrape_record_id, status="failed", error_message=msg)
                    _cb(0, f"Failed: {msg}")
                    return []

        else:
            # ── Chrome / SeleniumBase fallback ──────────────────────────────
            # Note: UGG.com blocks Replit datacenter IPs via Cloudflare +
            # DataDome. This path will fast-fail after 3 blocked URLs.
            # Set the SCRAPER_API_KEY secret to use the working path above.
            from seleniumbase import SB

            _NIX_CHROME = (
                "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100"
                "/bin/chromium-browser"
            )
            _chrome_bin = _NIX_CHROME if os.path.exists(_NIX_CHROME) else None

            _chrome_args = (
                "--no-sandbox "
                "--disable-dev-shm-usage "
                "--disable-gpu "
                "--disable-setuid-sandbox "
                "--disable-background-networking "
                "--memory-pressure-off"
            )

            _sb_kwargs = dict(uc=True, headless=True, locale_code="en", chromium_arg=_chrome_args)
            if _chrome_bin:
                _sb_kwargs["binary_location"] = _chrome_bin
                print(f"[UGG] Using Chromium at {_chrome_bin}")

            try:
                with SB(**_sb_kwargs) as sb:
                    for i, url in enumerate(footwear_urls):
                        if stop_event and stop_event.is_set():
                            break
                        print(f"[UGG] Scraping {i+1}/{total}: {url}")
                        raw = _scrape_product_page(sb, url)

                        if raw.get("_blocked"):
                            _blocked_count += 1
                            print(f"[UGG] Bot-protection block {_blocked_count}/{_BLOCK_LIMIT}")
                            if _blocked_count >= _BLOCK_LIMIT:
                                raise RuntimeError(
                                    "UGG.com is protected by Cloudflare + DataDome and is "
                                    "blocking requests from this server's datacenter IP. "
                                    "Set the SCRAPER_API_KEY secret (free at scraperapi.com) "
                                    "to enable the working scrape path."
                                )
                            continue
                        _blocked_count = 0

                        if not raw.get("url"):
                            raw["url"] = url
                        if raw.get("name") and raw.get("price", 0) > 0:
                            raw_products.append(raw)
                            _hb_count[0] = len(raw_products)
                        else:
                            print(f"[UGG]  ↳ Skipped (missing name/price)")

                        prog = 10 + int((i + 1) / total * 60)
                        _cb(prog, f"Scraped {i+1}/{total} pages ({len(raw_products)} products)...",
                            len(raw_products))

                        time.sleep(1.0)
            except Exception as chrome_err:
                print(f"[UGG] Chrome session error: {chrome_err}")
                if not raw_products:
                    msg = str(chrome_err)
                    update_scrape_record(scrape_record_id, status="failed", error_message=msg)
                    _cb(0, f"Failed: {msg}")
                    return []

        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        _cb(72, f"{len(raw_products)} products scraped — building Shopify products...")

        # ── Phase 3: Build Shopify products ────────────────────────────────
        final_products = []
        for raw in raw_products:
            if stop_event and stop_event.is_set():
                break
            prod = _build_product(raw)
            if prod:
                final_products.append(prod)

        if not final_products:
            msg = "No valid products could be built."
            update_scrape_record(scrape_record_id, status="failed", error_message=msg)
            _cb(0, f"Failed: {msg}")
            return []

        _cb(82, f"{len(final_products)} products — saving to DB...", len(final_products))

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
        _cb(100, f"Done ✅  {len(final_products)} UGG products", len(final_products))
        print(f"✅ UGG complete: {len(final_products)} products")
        return final_products

    except Exception as e:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(e))
        raise

    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_ugg()
