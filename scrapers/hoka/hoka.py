"""
Hoka Scraper v5.0
=================
DataDome blocks hoka.com/en/us/ pages — AU mirrors are used instead.

Strategy:
  Phase 1 — US product catalogue via hreflang XML sitemap (no DataDome)
             Yields footwear products with PID, name, gender, category, URL.

  Phase 2 — AU women's-shoes + men's-shoes pages only (mirrors US catalogue)
             Svelox state JSON → name, description, AUD price, gender,
             sizes_in_stock (only hasStockOnHand=True), gallery images.
             Sizes MERGED across all colour variants per PID.

  Phase 3 — Convert AU/US sizes → UK(EU) format.
             USD price from known MSRP map or AUD×0.60 fallback.
             Gemini rewrites descriptions in Mirage premium voice.

  Phase 4 — Shopify CSV export: one row per UK size variant.

Sizes displayed: UK 2 – UK 12.5  (EU in brackets, e.g. "UK 6(EU 40)")
Currency: USD → INR via pricing_engine
"""

import os
import re
import sys
import json
import time
import threading

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from core.db import (
    upsert_all_product_data, start_scrape_record, update_scrape_record,
    heartbeat_scrape_record, upload_csv_to_supabase,
)
from core.shopify_transformer import transform_to_shopify, export_shopify_csv
from core.tag_engine import (
    sanitize_html_description, map_to_store_tags,
    detect_sub_cat_slug, append_brand_message,
)

SCRAPER_ID = "hoka"
CURRENCY   = "USD"
BASE_URL   = "https://www.hoka.com"

_HREFLANG_URL = (
    "https://www.hoka.com/on/demandware.static"
    "/Sites-HOKA-US-Site/Sites/en_US/hoka-us/en_US-hreflang.xml"
)

_AU_NZ_CATEGORIES = [
    "/en/au/womens-shoes/",
    "/en/au/mens-shoes/",
    "/en/au/road-running-shoes/",
    "/en/au/trail-running-shoes/",
    "/en/au/stability-running-shoes/",
    "/en/au/hiking-shoes/",
    "/en/au/lifestyle-shoes/",
    "/en/au/recovery-shoes/",
]

_CFFI_HEADERS_AU = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "en-AU,en;q=0.9",
}

# ─── Known USD MSRP prices (2025 catalogue) ──────────────────────────────────
_USD_PRICE_MAP = {
    # Road everyday
    "Bondi 9": 165, "Bondi 9 Wide": 165, "Bondi X": 200, "Bondi 8": 160,
    "Bondi Sr": 165, "Bondi Sr 2": 170, "Bondi Sr Wide": 165,
    "Bondi 7": 150, "Bondi B3Ls": 165, "Bondi Mary Jane": 165,
    "Clifton 10": 150, "Clifton 10 Wide": 150, "Clifton L": 170, "Clifton Ls": 165,
    "Clifton 9": 145, "Clifton Edge": 130, "Clifton One9": 150, "Clifton One9 S": 160,
    "Clifton 10 X-Wide": 150, "Bondi 9 X-Wide": 165, "Clifton 9 Gtx": 155,
    "Clifton L Suede": 175,
    "Mach 6": 145, "Mach 7": 145, "Mach 5": 130,
    "Mach Remastered": 145, "Mach X 3": 220, "Mach X 2": 220, "Mach X Caged": 220,
    "Rincon 4": 140, "Rincon 3": 130,
    "Kawana 3": 140, "Kawana 2": 135, "Kawana": 130, "Kawana Mid": 145,
    "Arahi 8": 140, "Arahi 8 Wide": 140,
    "Arahi Sr": 155, "Arahi Sr Wide": 155, "Arahi 7": 135,
    "Gaviota 6": 165, "Gaviota 6 Wide": 165, "Gaviota 5": 155,
    "Stinson 8": 155, "Stinson Atr 7": 150, "Stinson 7": 150,
    "Stinson Breeze": 140, "Stinson Evo Gtx": 185,
    "Stinson One7": 175, "Stinson One7 + Dover Street Market": 175,
    "Elite Terrain System Stinson Evo Og": 225,
    "Skyward X": 200, "Skyward X 2": 210,
    "Skyward Laceless": 200, "Skyflow": 165,
    "Cielo X1": 220, "Cielo X Ls": 240, "Cielo X1 2.0": 220,
    "Cielo Road": 130,
    "Cielo Flyx": 200, "Cielo Flyx Elite": 225, "Cielo Flyx Lite": 190,
    "Cielo X 3 Ld": 225, "Cielo X 3 Md": 225,
    "Elevon 3": 180, "Elevon 2 Ts": 165, "Elevon X": 185,
    # Trail running
    "Speedgoat 7": 155, "Speedgoat 7 Gtx": 175, "Speedgoat 7 Wide": 155,
    "Speedgoat 6": 145, "Speedgoat 6 Gtx": 165, "Speedgoat 6 Mid Gtx": 175,
    "Speedgoat 2": 120, "Speedgoat 5 Gtx Spike": 175,
    "Torrent 4": 130, "Torrent 3": 120,
    "Challenger Atr 7": 140, "Challenger Atr 7 Gtx": 160,
    "Challenger Atr 7 Wide": 140, "Challenger 8": 145, "Challenger 8 Wide": 145,
    "Challenger 8 Gtx": 160, "Challenger Atr 6": 120,
    "Mafate Speed 4 Lite": 185, "Mafate Speed 2": 120,
    "Mafate Speed 4": 165, "Mafate 5": 165,
    "Mafate X": 250, "Mafate Hike": 200, "Mafate X Hike": 215, "Mafate Three2": 165,
    "Evo Mafate 2": 200,
    "Kaha 3 Gtx": 265, "Kaha 3 Low Gtx": 215, "Kaha 2 Low Gtx": 200,
    "Kaha 2 Frost Gtx": 265, "Kaha 2 Frost Moc Gtx": 265, "Kaha 3": 225,
    "Elite Terrain System Kaha 3 Gtx": 265,
    "Elite Terrain System Kaha 3 Low Gtx": 215,
    "Anacapa 2 Gtx": 210, "Anacapa 2 Mid Gtx": 225,
    "Anacapa 2 Low Gtx": 195, "Anacapa Breeze Low": 175, "Anacapa Breeze": 175,
    "Trail Code Gtx": 180, "Trail Code": 145,
    "Tecton X 3": 250, "Stealth/Tech Tecton X 2": 265,
    "Crescendo Md 2": 165, "Crescendo Xc": 160, "Crescendo Xc Spikeless": 155,
    "Infini Hike Tc": 225, "Restore Tc": 200,
    "Rocket X Trail": 225, "Rocket X 3": 225,
    "Carbon X 3": 215, "Carbon X 2": 175,
    # Road-to-trail
    "Transport Gtx": 180, "Transport": 155,
    "Transport 2": 160, "Transport Hike Gtx": 190,
    "Tor Summit": 240, "Tor Summit +": 240,
    # Recovery / lifestyle
    "Hopara 2": 110, "Hopara": 100,
    "Ora Recovery Shoe 3": 90, "Ora Recovery Shoe 2": 80,
    "Ora Recovery Slide 3": 60, "Ora Recovery Flip": 65,
    "Ora Recovery Mule": 75, "Ora Athletic Slide": 65,
    "Ora Primo Ext": 120, "Ora Primo": 110, "Ora Luxe": 140,
    "Speed Loafer": 120, "Solimar": 130, "Solimar 2": 135,
    # Stealth / limited
    "Stealth/Tech Bondi 8 Caged": 180,
    "Stealth/Tech Mafate Speed 2": 130,
    "Stealth/Tech Mafate Speed 4 Lite": 195,
    "Stealth/Tech Speedgoat 2": 130,
}

AUD_TO_USD_RATE = 0.60  # module-level fallback; runtime calls use _live_aud_to_usd()

_AUD_RATE_CACHE: dict = {}
_AUD_RATE_TS: float = 0.0


def _live_aud_to_usd() -> float:
    """Return live AUD→USD rate from open.er-api.com (cached 1 hour). Falls back to 0.60."""
    global _AUD_RATE_CACHE, _AUD_RATE_TS
    import time as _t
    now = _t.time()
    if _AUD_RATE_CACHE and (now - _AUD_RATE_TS) < 3600:
        return _AUD_RATE_CACHE["rate"]
    try:
        import requests as _req
        r = _req.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        if r.ok:
            aud_per_usd = float(r.json().get("rates", {}).get("AUD", 0))
            if aud_per_usd > 0:
                rate = round(1.0 / aud_per_usd, 6)
                _AUD_RATE_CACHE = {"rate": rate}
                _AUD_RATE_TS = now
                return rate
    except Exception:
        pass
    return AUD_TO_USD_RATE


def _get_usd_price(name: str, aud_price: float = 0) -> float:
    """Return USD price for a product name, with AUD conversion fallback."""
    # Exact match
    if name in _USD_PRICE_MAP:
        return float(_USD_PRICE_MAP[name])
    # Case-insensitive exact
    nl = name.lower()
    for k, v in _USD_PRICE_MAP.items():
        if k.lower() == nl:
            return float(v)
    # Longest partial match (key contained in name)
    best_k, best_v = None, None
    for k, v in _USD_PRICE_MAP.items():
        if k.lower() in nl and (best_k is None or len(k) > len(best_k)):
            best_k, best_v = k, v
    if best_v is not None:
        return float(best_v)
    # AUD conversion fallback — use live rate
    if aud_price and aud_price > 0:
        return round(float(aud_price) * _live_aud_to_usd())
    return 0.0


# ─── UK / EU size conversion tables ──────────────────────────────────────────
# AU svelox sizes are in US scale (same numbers as US sizes).
# Women's: UK = US - 2;  Men's: UK = US - 1

_WOMENS_UK_EU = {
    "5":    ("UK 2(EU 35)",    2,   35),
    "5.5":  ("UK 2.5(EU 35.5)", 2.5, 35.5),
    "6":    ("UK 3(EU 36)",    3,   36),
    "6.5":  ("UK 3.5(EU 37)", 3.5,  37),
    "7":    ("UK 4(EU 37.5)", 4,   37.5),
    "7.5":  ("UK 4.5(EU 38)", 4.5,  38),
    "8":    ("UK 5(EU 38.5)", 5,   38.5),
    "8.5":  ("UK 5.5(EU 39)", 5.5,  39),
    "9":    ("UK 6(EU 40)",   6,    40),
    "9.5":  ("UK 6.5(EU 40.5)", 6.5, 40.5),
    "10":   ("UK 7(EU 41)",   7,    41),
    "10.5": ("UK 7.5(EU 42)", 7.5,  42),
    "11":   ("UK 8(EU 42.5)", 8,   42.5),
    "11.5": ("UK 8.5(EU 43)", 8.5,  43),
    "12":   ("UK 9(EU 44)",   9,    44),
    "12.5": ("UK 9.5(EU 44.5)", 9.5, 44.5),
}

_MENS_UK_EU = {
    "6":    ("UK 5(EU 39)",   5,    39),
    "6.5":  ("UK 5.5(EU 39.5)", 5.5, 39.5),
    "7":    ("UK 6(EU 40)",   6,    40),
    "7.5":  ("UK 6.5(EU 40.5)", 6.5, 40.5),
    "8":    ("UK 7(EU 41)",   7,    41),
    "8.5":  ("UK 7.5(EU 42)", 7.5,  42),
    "9":    ("UK 8(EU 42.5)", 8,   42.5),
    "9.5":  ("UK 8.5(EU 43)", 8.5,  43),
    "10":   ("UK 9(EU 44)",   9,    44),
    "10.5": ("UK 9.5(EU 44.5)", 9.5, 44.5),
    "11":   ("UK 10(EU 45)",  10,   45),
    "11.5": ("UK 10.5(EU 45.5)", 10.5, 45.5),
    "12":   ("UK 11(EU 46)",  11,   46),
    "12.5": ("UK 11.5(EU 47)", 11.5, 47),
    "13":   ("UK 12(EU 47.5)", 12,  47.5),
    "14":   ("UK 12.5(EU 48)", 12.5, 48),
}


def _convert_to_uk(us_size: str, size_gender: str) -> str | None:
    """
    Convert AU/US size code → 'UK X(EU Y)' string.
    Returns None if size is outside the valid range (skip that variant).
    """
    s = str(us_size).strip()
    table = _WOMENS_UK_EU if size_gender == "womens" else _MENS_UK_EU
    entry = table.get(s)
    return entry[0] if entry else None


# ─── Gemini description rewriter ──────────────────────────────────────────────
_desc_cache: dict = {}
_DESC_CACHE_FILE = "scraped_files/hoka_desc_cache.json"

if os.path.exists(_DESC_CACHE_FILE):
    try:
        with open(_DESC_CACHE_FILE) as _f:
            _desc_cache = json.load(_f)
    except Exception:
        pass


def _save_desc_cache():
    try:
        with open(_DESC_CACHE_FILE, "w") as f:
            json.dump(_desc_cache, f)
    except Exception:
        pass


_FEATURE_KW = re.compile(
    r'\b(upper|midsole|outsole|foam|rubber|mesh|jacquard|leather|suede|lace|strap|'
    r'closure|zip|cushion|stack|plate|layer|carbon|heel|toe|arch|width|fit|EVA|CMEVA|'
    r'Durabrasion|PROFLY|MTR|Gore-Tex|GTX|waterproof|breathab|gusset|collar|tongue|'
    r'lug|grip|shank|frame|trail|road|drop|weight|engineered|knit|woven|nylon|'
    r'liner|insole|footbed|rocker|meta-rocker|J-Frame|HOKA)\b',
    re.IGNORECASE,
)

_SKIP_STARTS = re.compile(
    r'^(who |what |how |why |our thoughts|say hello|meet the|introducing|now available)',
    re.IGNORECASE,
)


def _svelox_to_bullets(raw_desc: str, name: str, ptype: str) -> str:
    """
    Detect specific HOKA technologies from the svelox description and build
    5 clean, product-specific feature bullets — no fragile noun-phrase parsing.
    """
    t = re.sub(r'<[^>]+>', ' ', raw_desc or '').lower()
    t = re.sub(r'\s+', ' ', t)

    def has(*kws):
        return any(re.search(kw, t) for kw in kws)

    bullets = []

    # ── 1. UPPER ─────────────────────────────────────────────────────────────
    if has(r'gore.?tex', r'\bgtx\b'):
        bullets.append("Gore-Tex® waterproof upper for all-weather protection")
    elif has(r'creel jacquard', r'jacquard upper'):
        if has(r'gusset'):
            bullets.append("Creel jacquard upper with zonal breathability and internal gusset for locked-in fit")
        else:
            bullets.append("Jacquard upper with zonal breathability and adaptive fit")
    elif has(r'jacquard'):
        bullets.append("Jacquard upper with targeted breathability zones")
    elif has(r'engineered knit', r'structured knit'):
        bullets.append("Engineered knit upper for superior breathability and stretch comfort")
    elif has(r'\bknit\b'):
        bullets.append("Knit upper for lightweight, sock-like fit and breathability")
    elif has(r'\bmesh\b'):
        bullets.append("Breathable mesh upper for lightweight ventilated comfort")
    elif has(r'suede'):
        bullets.append("Premium suede upper with protective reinforced overlays")
    elif has(r'ripstop', r'nylon'):
        bullets.append("Ripstop nylon upper for lightweight durability on technical terrain")
    else:
        bullets.append("Durable performance upper engineered for breathability and fit")

    # ── 2. MIDSOLE / CUSHIONING ───────────────────────────────────────────────
    if has(r'carbon fibre', r'carbon fiber', r'carbon plate'):
        bullets.append("Full-length carbon fibre plate with dual-density foam for explosive propulsion")
    elif has(r'profly\+?'):
        if has(r'carbon'):
            bullets.append("PROFLY+™ midsole with carbon fibre plate for a responsive, race-ready ride")
        else:
            bullets.append("PROFLY+™ midsole — soft heel cushioning with a responsive forefoot")
    elif has(r'meta.?rocker') and has(r'j.?frame'):
        bullets.append("EVA foam midsole with meta-rocker geometry and J-Frame™ medial stability")
    elif has(r'j.?frame'):
        bullets.append("HOKA foam midsole with J-Frame™ technology for guided, stable strides")
    elif has(r'meta.?rocker'):
        bullets.append("Full-length EVA foam midsole with meta-rocker geometry for smooth heel-to-toe transitions")
    elif has(r'\beva\b', r'cmeva', r'high-energy foam', r'premium foam'):
        bullets.append("Full-length EVA foam midsole for maximum impact cushioning")
    else:
        bullets.append("HOKA performance foam midsole for all-day cushioning and comfort")

    # ── 3. OUTSOLE ────────────────────────────────────────────────────────────
    if has(r'vibram'):
        bullets.append("Vibram® Megagrip rubber outsole for exceptional all-terrain traction")
    elif has(r'durabrasion'):
        bullets.append("Durabrasion rubber outsole targeting high-wear zones for extended durability")
    elif has(r'\blug\b', r'lug pattern', r'multi.directional'):
        bullets.append("Multi-directional lug rubber outsole for superior off-road grip")
    else:
        bullets.append("Durable rubber outsole for reliable traction on road and trail")

    # ── 4. FIT / CLOSURE ─────────────────────────────────────────────────────
    used_collar = False
    if has(r'\bslide\b', r'slip.on', r'slip on'):
        bullets.append("Slip-on design for effortless on/off convenience")
    elif has(r'wide.toe.?box', r'toe.?box'):
        bullets.append("Wide toe box for natural toe splay and fatigue-free all-day comfort")
    elif has(r'3d mold', r'3d mould', r'molded collar', r'moulded collar'):
        bullets.append("3D moulded collar with padded tongue for a locked-in, irritation-free fit")
        used_collar = True
    elif has(r'double.?lace', r'lace lock'):
        bullets.append("Double-lace lock system for precision fit and secure lockdown")
    elif has(r'adjustable.*strap', r'strap.*adjustable'):
        bullets.append("Adjustable strap closure for a personalised, secure fit")
    else:
        bullets.append("Lace-up closure for a secure, customisable fit")

    # ── 5. SPECIAL TECH / COMFORT ─────────────────────────────────────────────
    drop_m = re.search(r'(\d+)mm\s+(?:heel.to.toe\s+)?drop|drop\s+(?:of\s+)?(\d+)\s*mm', t)
    if drop_m:
        mm = drop_m.group(1) or drop_m.group(2)
        bullets.append(f"{mm}mm heel-to-toe drop for a natural, balanced stride")
    elif has(r'waterproof', r'water.resistant') and not has(r'gore.?tex'):
        bullets.append("Waterproof construction for reliable all-weather performance")
    elif has(r'reinforced toe', r'toe cap', r'toe bumper'):
        bullets.append("Reinforced toe cap for protection against rocks, roots and trail debris")
    elif has(r'recovery', r'anatomical', r'contour'):
        bullets.append("Anatomically contoured footbed for active recovery and comfort")
    elif has(r'padded collar', r'padded tongue', r'collar') and not used_collar:
        bullets.append("Padded collar and breathable textile lining for all-day comfort")
    elif has(r'\bslide\b', r'slip.on'):
        bullets.append("Anatomically shaped footbed with HOKA foam for active recovery support")
    else:
        bullets.append("Lightweight construction for effortless performance from the first step")

    return "<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"


_USE_CASES = {
    "Running Shoes":   (
        "tackling marathon training, logging daily miles, or chasing personal bests",
        "runners who refuse to compromise on performance or comfort",
    ),
    "Trail Shoes":     (
        "conquering mountain descents, navigating rugged terrain, or exploring wilderness trails",
        "trail runners and outdoor adventurers who demand grip and durability",
    ),
    "Hiking Shoes":    (
        "ascending alpine peaks, navigating rocky paths, or embarking on multi-day hikes",
        "hikers and trekkers who require stability across unpredictable terrain",
    ),
    "Lifestyle Shoes": (
        "pairing with activewear, relaxed weekend looks, or polished casual outfits",
        "those who bring the same high standards to everyday style as they do to sport",
    ),
    "Sandals":         (
        "beach days, casual outings, or leisurely recovery walks",
        "those seeking comfort-first footwear without sacrificing refined design",
    ),
    "Recovery Footwear": (
        "post-run recovery, gym cool-downs, or relaxed everyday wear",
        "athletes who understand that recovery is as important as training",
    ),
}

_TECH_SHOWCASE = {
    "Running Shoes":   "innovative foam midsole technology and precision-engineered upper",
    "Trail Shoes":     "durable multi-directional outsole and protective upper construction",
    "Hiking Shoes":    "waterproof construction and robust outsole grip technology",
    "Lifestyle Shoes": "premium materials and refined everyday construction",
    "Sandals":         "anatomically contoured footbed and premium comfort materials",
}


def _generate_mirage_desc(name: str, ptype: str, gender: str, raw_desc: str) -> str:
    """
    Build a full Mirage-template product description matching the brand standard:
      [Opening hook paragraph]
      [Key Features & Characteristics heading + <ul> bullets]
      [Closing heritage/styling paragraph]
    append_brand_message() appends the italic brand line afterwards.
    """
    cache_key = f"v4_{name}_{gender}"
    if cache_key in _desc_cache:
        return _desc_cache[cache_key]

    ptype_lower = ptype.lower()
    gender_label = (
        "women's" if gender == "Women"
        else "men's" if gender == "Men"
        else "unisex"
    )

    # ── Strip HTML from svelox text ───────────────────────────────────────────
    text = re.sub(r'<[^>]+>', ' ', raw_desc or '').strip()
    # Normalise whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    # ── Hero sentence: first non-skip sentence ≥ 40 chars ────────────────────
    hero = ""
    for s in sentences:
        if not _SKIP_STARTS.match(s) and len(s) >= 40:
            hero = s
            break
    if not hero:
        hero = sentences[0] if sentences else f"HOKA {name} — premium {ptype_lower}."

    # ── Tech showcase phrase (type-based, grammatically safe) ────────────────
    tech_phrase = _TECH_SHOWCASE.get(ptype, "innovative foam technology and precision construction")

    # ── Bullet list ───────────────────────────────────────────────────────────
    bullets_html = _svelox_to_bullets(raw_desc, name, ptype)

    # ── Use-case strings ──────────────────────────────────────────────────────
    use_cases, audience = _USE_CASES.get(ptype, _USE_CASES["Running Shoes"])

    # ── Assemble opening paragraph ────────────────────────────────────────────
    opening = (
        f"<p>Discover the iconic HOKA {name} {gender_label} {ptype_lower}, "
        f"exclusively curated by The Mirage. "
        f"A masterpiece of performance engineering, these {ptype_lower} embody the "
        f"dynamic innovation and athletic spirit that define premium footwear.</p>"
    )

    # ── Key Features section ──────────────────────────────────────────────────
    features_section = (
        f"<p><strong>Key Features &amp; Characteristics:</strong></p>"
        f"{bullets_html}"
    )

    # ── Closing paragraph ─────────────────────────────────────────────────────
    closing = (
        f"<p>Crafted with the precision expected from HOKA's heritage, the {name} "
        f"represents more than footwear — it's a statement in performance that "
        f"elevates every stride. "
        f"The {tech_phrase} showcase HOKA's commitment to athletic excellence "
        f"without compromise. "
        f"Whether {use_cases}, these {ptype_lower} deliver the responsiveness and "
        f"cushioning that {audience} demand. "
        f"At The Mirage, we celebrate pieces that transcend trends. "
        f"This HOKA creation is a testament to purposeful design and uncompromising "
        f"quality, making it an essential addition to any discerning wardrobe.</p>"
    )

    html = opening + features_section + closing
    _desc_cache[cache_key] = html
    _save_desc_cache()
    return html


# ─── Phase 1: hreflang XML product URL discovery ─────────────────────────────

def _fetch_hreflang_xml() -> str:
    """Fetch the US hreflang XML — accessible without DataDome."""
    try:
        from curl_cffi import requests as cffi
        r = cffi.get(
            _HREFLANG_URL,
            headers={"User-Agent": "Mozilla/5.0 Chrome/131", "Accept": "*/*"},
            impersonate="chrome131",
            timeout=30,
        )
        if r.status_code == 200:
            return r.text
        print(f"[Hoka] hreflang XML returned status {r.status_code}")
    except Exception as e:
        print(f"[Hoka] hreflang fetch error: {e}")
    return ""


def _parse_hreflang_products(xml: str) -> list:
    """
    Parse hreflang XML to extract unique US footwear product stubs.
    Each URL: /en/us/{category}/{product-slug}/{pid}.html
    Returns: [{"url", "product_id", "name", "gender", "category"}]
    """
    import urllib.parse

    products  = []
    seen_ids  = set()
    url_pat   = re.compile(
        r'https://www\.hoka\.com(/en/us/([^/]+)/([^/]+)/(\d{5,12})\.html)'
    )
    _SKIP = (
        "tops", "bottoms", "shorts", "tights", "leggings", "bra",
        "outerwear", "apparel", "accessories", "socks", "kids", "children",
    )

    for m in url_pat.finditer(xml):
        path, cat_slug, name_slug, pid = m.group(1), m.group(2), m.group(3), m.group(4)
        if pid in seen_ids or any(kw in cat_slug for kw in _SKIP):
            continue
        seen_ids.add(pid)

        if "womens" in cat_slug or "womens" in name_slug:
            gender = "Women"
        elif "mens" in cat_slug or "mens" in name_slug:
            gender = "Men"
        else:
            gender = "Unisex"

        name     = urllib.parse.unquote(name_slug).replace("-", " ").title()
        category = urllib.parse.unquote(cat_slug).replace("-", " ").title()

        products.append({
            "url":        BASE_URL + path,
            "product_id": pid,
            "name":       name,
            "gender":     gender,
            "category":   category,
        })

    print(f"[Hoka] hreflang XML → {len(products)} unique US footwear product URLs")
    return products


# ─── Phase 2: AU / NZ category page scraping ─────────────────────────────────

def _extract_svelox_products(html: str) -> tuple[dict, dict]:
    """
    Parse the svelox state JSON embedded in a Hoka AU/NZ category page.

    Returns:
        products: {pid: {pid, name, description, gender, aud_price,
                         sizes_in_stock, size_gender}}
        images:   {pid: [img_url, ...]}   all gallery images per product
    """
    # Use re.DOTALL so the regex captures full script content including any
    # '<' characters that appear inside the 982KB svelox JSON blob.
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    blob    = next((s for s in scripts if '"$sveloxProducts"' in s), None)
    if not blob:
        return {}, {}

    try:
        state = json.loads(blob)
    except Exception:
        return {}, {}

    # Find the container with '$sveloxProducts'
    prod_container = None
    for el in state:
        if isinstance(el, dict) and '$sveloxProducts' in el:
            idx = el['$sveloxProducts']
            if isinstance(state[idx], dict):
                prod_container = state[idx]
            break
    if not prod_container:
        return {}, {}

    pb_idx = prod_container.get('productsById')
    if pb_idx is None:
        return {}, {}
    products_by_id = state[pb_idx] if pb_idx < len(state) else {}
    if not isinstance(products_by_id, dict):
        return {}, {}

    products, images = {}, {}

    for _uuid, state_idx in products_by_id.items():
        if not isinstance(state_idx, int) or state_idx >= len(state):
            continue
        prod = state[state_idx]
        if not isinstance(prod, dict):
            continue

        def rv(v):
            return state[v] if isinstance(v, int) and 0 <= v < len(state) else v

        epc = rv(prod.get("erpProductParentCode", ""))
        pid = epc.split("-")[0] if isinstance(epc, str) and epc else ""
        if not pid:
            continue

        # ── Sizes: UNION across all colour variants (hasStockOnHand=True only) ──
        sizes_this_variant = set()
        variants_raw = rv(prod.get("variants"))
        if isinstance(variants_raw, int):
            variants_raw = rv(variants_raw)
        if isinstance(variants_raw, list):
            for vref in variants_raw:
                v = rv(vref)
                if not isinstance(v, dict):
                    continue
                code  = rv(v.get("code", ""))
                stock = rv(v.get("hasStockOnHand", False))
                if code and stock:
                    sizes_this_variant.add(str(code))

        # ── Size gender label (womens / mens / unisex) ─────────────────────────
        scg = rv(prod.get("sizeChartGender", "")) or rv(prod.get("erpGenderCode", ""))
        scg = str(scg).lower() if scg else ""
        if "women" in scg:
            size_gender = "womens"
        elif "men" in scg:
            size_gender = "mens"
        else:
            size_gender = "unisex"

        # ── Gallery images: resolve images[] array → url field ─────────────────
        # Collect ALL gallery images (front, back, detail, lifestyle, model,
        # zoom).  Never limit gallery size — every image is passed through.
        if pid not in images:
            gallery = []
            imgs_raw = rv(prod.get("images"))
            if isinstance(imgs_raw, int):
                imgs_raw = rv(imgs_raw)
            if isinstance(imgs_raw, list):
                for iref in imgs_raw:
                    img_dict = rv(iref)
                    if isinstance(img_dict, dict):
                        url = rv(img_dict.get("url", ""))
                        if isinstance(url, int):
                            url = rv(url)
                        if isinstance(url, str) and url.startswith("http"):
                            gallery.append(url)
            if gallery:
                images[pid] = gallery

        if pid not in products:
            products[pid] = {
                "pid":            pid,
                "name":           rv(prod.get("name", "")),
                "description":    rv(prod.get("description", "")) or "",
                "gender":         rv(prod.get("erpGenderCode", "")),
                "aud_price":      rv(prod.get("price", 0)) or 0,
                "sizes_in_stock": set(),          # will union across all colours
                "size_gender":    size_gender,
            }
        # Union in-stock sizes from this colour variant
        products[pid]["sizes_in_stock"] |= sizes_this_variant

    return products, images


def _scrape_au_nz_categories() -> tuple[dict, dict]:
    """
    Scrape all AU and NZ category pages.
    Returns merged (au_products, au_images) dicts keyed by SFCC PID.
    """
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        print("[Hoka] curl_cffi not available — skipping AU scrape")
        return {}, {}

    all_products: dict = {}
    all_images:   dict = {}

    for path in _AU_NZ_CATEGORIES:
        url = BASE_URL + path
        try:
            r = cffi.get(
                url,
                headers=_CFFI_HEADERS_AU,
                impersonate="chrome131",
                timeout=25,
            )
            if r.status_code != 200:
                print(f"[Hoka] AU/NZ {path} → HTTP {r.status_code}, skipping")
                continue

            prods, imgs = _extract_svelox_products(r.text)
            new_p = sum(1 for k in prods if k not in all_products)
            new_i = sum(1 for k in imgs  if k not in all_images)
            all_products.update({k: v for k, v in prods.items() if k not in all_products})
            all_images.update(  {k: v for k, v in imgs.items()  if k not in all_images})

            if new_p or new_i:
                print(f"[Hoka] {path:46} +{new_p:2}p +{new_i:2}i → "
                      f"{len(all_products)}p {len(all_images)}i")
        except Exception as e:
            print(f"[Hoka] Error scraping {path}: {e}")
        time.sleep(0.5)

    print(f"[Hoka] AU/NZ scrape complete: {len(all_products)} products, "
          f"{len(all_images)} images")
    return all_products, all_images


# ─── Phase 3: Merge & build Shopify product list ─────────────────────────────

# Keywords that indicate non-footwear items to exclude
_APPAREL_KW   = {"tight", "tee", "shirt", "bra", "pant", "zip", "polo",
                  "vest", "sleeve", "jersey", "shorts", "jacket", "hood",
                  "windbreaker", "crop", "glide", "skybreeze", "airoli",
                  "airolite", "skyrun", "novafly", "glidetec", "hupana",
                  "woodland", "raceda", "raceday"}
_ACCESSORY_KW = {"sock", "hat", "trucker", "cap", "pack", "bag",
                  "bottle", "visor", "buff", "glove"}
_KIDS_KW      = {"kids", "kid", "little kids", "youth", "junior",
                  "toddler", "infant", "little", "big kid"}


def _is_footwear(name: str, gender: str) -> bool:
    """Return True if product is footwear (shoes/slides/sandals), False for apparel/accessories/kids."""
    nl = name.lower()
    gl = gender.lower()
    if any(kw in gl for kw in ("kids", "kid")):
        return False
    if any(kw in nl for kw in _KIDS_KW):
        return False
    if any(kw in nl for kw in _APPAREL_KW):
        return False
    if any(kw in nl for kw in _ACCESSORY_KW):
        return False
    return True


def _clean_name(raw: str) -> str:
    """Strip internal gender/width prefixes (e.g. 'M CLIFTON 10 X-WIDE' → 'Clifton 10 X-Wide')."""
    # Remove leading single-letter gender prefix followed by a space
    cleaned = re.sub(r'^[MWK]\s+', '', raw.strip())
    # Title-case
    return cleaned.title()


def _build_products(stubs: list, au_products: dict, au_images: dict) -> list:
    """
    Build Shopify products from ALL hreflang US stubs (primary source — 151 products).
    AU/NZ svelox data enriches price, images, sizes, and description where the PID
    matches; stubs without AU/NZ data fall back to the static price map and a
    standard UK size run so the full US catalogue is always represented.

    Filters out apparel, accessories and kids items.
    Returns a list of Shopify-compatible product dicts.
    """
    # Lookup: pid → AU/NZ product data
    au_by_pid = au_products  # already keyed by pid

    # Default US size runs (used when AU/NZ stock data is absent)
    _WOMENS_DEFAULT_US = [
        "5", "5.5", "6", "6.5", "7", "7.5", "8",
        "8.5", "9", "9.5", "10", "10.5", "11",
    ]
    _MENS_DEFAULT_US = [
        "7", "7.5", "8", "8.5", "9", "9.5", "10",
        "10.5", "11", "11.5", "12", "12.5", "13", "14",
    ]

    products = []
    seen     = set()   # deduplicate by (cleaned_name, gender_label)

    def _sort_key(s):
        try: return float(s)
        except: return 999

    for stub in stubs:
        raw_name   = stub.get("name", "").strip()
        raw_gender = stub.get("gender", "Unisex")
        pid        = stub.get("product_id", "")
        category   = stub.get("category", "")
        url        = stub.get("url", "")

        # Filter non-footwear from stub name/category
        if not _is_footwear(raw_name, raw_gender):
            continue

        # Clean name and resolve gender
        name = _clean_name(raw_name)
        if "Women" in raw_gender:
            gender = gender_label = "Women"
        elif "Men" in raw_gender:
            gender = gender_label = "Men"
        else:
            gender = gender_label = "Unisex"

        # Deduplicate (same cleaned name + same gender)
        key = (name.lower(), gender_label)
        if key in seen:
            continue
        seen.add(key)

        # ── AU/NZ enrichment (if PID matched) ──────────────────────────────
        au = au_by_pid.get(pid, {})
        has_au = bool(au)

        aud_price   = float(au.get("aud_price", 0) or 0) if has_au else 0.0
        desc_raw    = au.get("description", "") or ""
        raw_sizes   = au.get("sizes_in_stock", set()) if has_au else set()
        size_gender = au.get("size_gender", "") if has_au else (
            "womens" if gender == "Women" else "mens"
        )
        gallery     = au_images.get(pid, []) if has_au else []
        if isinstance(gallery, str):
            gallery = [gallery] if gallery else []

        # ── USD price ───────────────────────────────────────────────────────
        usd_price = _get_usd_price(name, aud_price)
        if not usd_price or usd_price <= 0:
            print(f"[Hoka] ⚠ No price for '{name}' — skipping")
            continue

        # ── Product type ────────────────────────────────────────────────────
        nl_combined = f"{name} {category}".lower()
        if any(w in nl_combined for w in ("trail", "speedgoat", "mafate", "tecton", "torrent")):
            ptype = "Trail Running Shoes"
        elif any(w in nl_combined for w in ("hike", "hiking", "kaha", "anacapa", "transport", "tor summit", "infini")):
            ptype = "Hiking Shoes"
        elif any(w in nl_combined for w in ("slide", "flip", "recovery", "ora recovery", "hopara")):
            ptype = "Recovery Footwear"
        elif any(w in nl_combined for w in ("loafer", "solimar", "primo", "luxe", "speed loafer")):
            ptype = "Lifestyle Shoes"
        else:
            ptype = "Running Shoes"

        # ── Description ─────────────────────────────────────────────────────
        mirage_txt = _generate_mirage_desc(name, ptype, gender, desc_raw)
        _raw_body  = (
            f"<p>{mirage_txt}</p>"
            if mirage_txt and not mirage_txt.startswith('<')
            else mirage_txt or f"<p>HOKA {name} — premium {ptype.lower()}.</p>"
        )
        body_html = append_brand_message(_raw_body)

        # ── Sizes ───────────────────────────────────────────────────────────
        if raw_sizes:
            sizes_us = sorted(raw_sizes, key=_sort_key)
        else:
            # Fall back to a standard size run for this gender
            sizes_us = _WOMENS_DEFAULT_US if gender == "Women" else _MENS_DEFAULT_US

        uk_variants = []
        for s in sizes_us:
            uk_label = _convert_to_uk(s, size_gender)
            if uk_label is None:
                continue
            uk_variants.append({
                "Variant SKU":           f"{pid}-{s}",
                "size":                  uk_label,
                "Variant Price":         usd_price,
                "currency":              "USD",
                "Variant Inventory Qty": 100,
            })

        if not uk_variants:
            print(f"[Hoka] ⚠ {name} ({gender_label}) — no UK sizes mappable, skipping")
            continue

        # ── Tags ────────────────────────────────────────────────────────────
        tags_set = {"hoka", "running-shoes", "mirage-curated", "premium",
                    "comfort-fit", "footwear", "RudraScrapper-hoka"}
        if gender == "Women":
            tags_set.update(["womens", "women"])
        elif gender == "Men":
            tags_set.update(["mens", "men"])
        else:
            tags_set.update(["womens", "women", "mens", "men"])
        if "trail" in nl_combined:
            tags_set.update(["trail", "trail-running"])
        if "hik" in nl_combined:
            tags_set.add("hiking")

        sub_slug = detect_sub_cat_slug(name, ptype)
        for g_key in (["WOMEN", "MEN"] if gender == "Unisex"
                      else ["WOMEN"] if gender == "Women" else ["MEN"]):
            main_tag, sub_tag = map_to_store_tags(g_key, "footwear", sub_slug or "shoe")
            if main_tag: tags_set.add(main_tag)
            if sub_tag:  tags_set.add(sub_tag)
        tags = sorted(tags_set)

        # Strip leading "hoka" from name so products like "Hoka X Xlim …"
        # don't become "hoka-hoka-x-xlim-…"
        _name_for_handle = re.sub(r'^hoka[\s\-]+', '', name, flags=re.IGNORECASE).strip()
        handle = re.sub(r'[^a-z0-9]+', '-',
                        f"hoka-{_name_for_handle}-{gender}".lower()).strip('-')

        product = {
            "Handle":      handle,
            "Title":       name,
            "Body (HTML)": body_html,
            "Vendor":      "HOKA",
            "Type":        ptype,
            "Tags":        ", ".join(tags),
            "gender":      gender,
            "source_url":  url,
            "gallery":     gallery,
            "variants":    uk_variants,
        }

        if not gallery:
            print(
                f"[Hoka] ⚠ {name:<40} ${usd_price:>6.2f} "
                f"{gender_label:<7} — skipped (no AU images)"
            )
            continue

        src = "AU✓" if has_au else "map"
        print(
            f"[Hoka] ✅ {name:<40} ${usd_price:>6.2f} "
            f"{gender_label:<7} UK sizes={len(uk_variants)} imgs={len(gallery)} [{src}]"
        )
        products.append(product)

    print(f"[Hoka] Built {len(products)} products "
          f"({sum(1 for p in products if au_by_pid.get(p['Handle'].split('-',1)[-1].rsplit('-',1)[0],{}))}"
          f" AU-enriched) from {len(stubs)} stubs")
    return products


# ─── Shopify CSV export (USD — no INR conversion) ────────────────────────────

_SHOPIFY_COLS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags",
    "Published", "Option1 Name", "Option1 Value",
    "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
    "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
    "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable",
    "Image Src", "Image Position", "Image Alt Text",
    "Gift Card", "SEO Title", "SEO Description",
    "Variant Image", "Variant Weight Unit", "Cost per item", "Status",
]


def _build_shopify_csv_rows(products: list) -> list:
    """
    Build Shopify import CSV rows with INR pricing.
    - One row per size variant (first row has all product fields)
    - Extra image-only rows for gallery images 2-6
    """
    from core.pricing_engine import (
        get_exchange_rates, calculate_price_inr,
        calculate_cost_inr, calculate_compare_price,
    )
    rates = get_exchange_rates()
    rows  = []
    _EMPTY = {col: "" for col in _SHOPIFY_COLS}

    for p in products:
        variants = p.get("variants", [])
        gallery  = p.get("gallery", [])
        handle   = p["Handle"]
        body     = p.get("Body (HTML)", "")
        seo      = re.sub(r'<[^>]+>', '', body)[:155]

        # Compute INR once (price is the same per product, not per size)
        v0       = variants[0]
        usd      = v0["Variant Price"]
        currency = v0.get("currency", "USD")
        inr_price   = calculate_price_inr(usd, currency, rates)
        inr_cost    = calculate_cost_inr(usd, currency, rates)
        inr_compare = calculate_compare_price(inr_price)
        if not inr_price or inr_price <= 0:
            continue

        first_img = gallery[0] if gallery else ""

        # ── One row per size variant ──────────────────────────────────────────
        for i, v in enumerate(variants):
            is_first = (i == 0)
            row = _EMPTY.copy()
            row["Handle"]                      = handle
            row["Option1 Name"]                = "Size"
            row["Option1 Value"]               = v["size"]
            row["Variant SKU"]                 = v["Variant SKU"]
            row["Variant Grams"]               = "312"
            row["Variant Inventory Tracker"]   = "shopify"
            row["Variant Inventory Qty"]       = str(v.get("Variant Inventory Qty", 100))
            row["Variant Inventory Policy"]    = "deny"
            row["Variant Fulfillment Service"] = "manual"
            row["Variant Price"]               = str(int(inr_price))
            row["Variant Compare At Price"]    = str(int(inr_compare))
            row["Variant Requires Shipping"]   = "TRUE"
            row["Variant Taxable"]             = "TRUE"
            row["Variant Weight Unit"]         = "kg"
            row["Cost per item"]               = str(int(inr_cost)) if inr_cost else ""
            row["Status"]                      = "active"
            row["Variant Image"]               = first_img

            if is_first:
                # Full product fields only on first row
                row["Title"]        = p["Title"]
                row["Body (HTML)"]  = body
                row["Vendor"]       = p["Vendor"]
                row["Type"]         = p["Type"]
                row["Tags"]         = p["Tags"]
                row["Published"]    = "TRUE"
                row["Gift Card"]    = "FALSE"
                row["SEO Title"]    = p["Title"]
                row["SEO Description"] = seo
                row["Image Src"]    = first_img
                row["Image Position"] = "1" if first_img else ""
                row["Image Alt Text"] = p["Title"] if first_img else ""

            rows.append(row)

        # ── Extra image rows for gallery images 2-6 ───────────────────────────
        for img_pos, img_url in enumerate(gallery[1:6], start=2):
            row = _EMPTY.copy()
            row["Handle"]         = handle
            row["Image Src"]      = img_url
            row["Image Position"] = str(img_pos)
            row["Image Alt Text"] = p["Title"]
            rows.append(row)

    return rows


def _write_shopify_csv(rows: list, path: str) -> None:
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=_SHOPIFY_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"[Hoka] CSV written: {len(rows)} rows → {path}")


# ─── Main orchestration ───────────────────────────────────────────────────────

def complete_workflow_hoka(progress_callback=None, stop_event=None, **kwargs):
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
        # ── Phase 1: US product catalogue ──────────────────────────────────
        _cb(5, "Fetching Hoka product catalogue from hreflang XML…")
        xml = _fetch_hreflang_xml()
        if not xml:
            update_scrape_record(scrape_record_id, status="failed",
                                 error_message="Could not fetch hreflang XML.")
            _cb(0, "Failed: hreflang XML inaccessible.")
            return []

        stubs = _parse_hreflang_products(xml)
        if not stubs:
            update_scrape_record(scrape_record_id, status="failed",
                                 error_message="No product URLs in hreflang XML.")
            _cb(0, "Failed: no products in hreflang XML.")
            return []

        _cb(15, f"Found {len(stubs)} US product URLs — scraping AU/NZ for details…",
            len(stubs))

        # ── Phase 2: AU / NZ category scraping ────────────────────────────
        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        _cb(20, "Scraping AU/NZ category pages for descriptions and images…")
        au_products, au_images = _scrape_au_nz_categories()

        _cb(60, f"AU/NZ data: {len(au_products)} products, {len(au_images)} images — "
                "merging with US catalogue…")

        # ── Phase 3: Merge & price resolution ─────────────────────────────
        if stop_event and stop_event.is_set():
            update_scrape_record(scrape_record_id, status="cancelled")
            return []

        final_products = _build_products(stubs, au_products, au_images)

        if not final_products:
            msg = "No products could be built (all missing prices?)."
            update_scrape_record(scrape_record_id, status="failed", error_message=msg)
            _cb(0, "Failed: " + msg)
            return []

        _cb(80, f"{len(final_products)} in-stock Hoka products — saving to DB…",
            len(final_products))

        # ── Phase 4: Persist + export ──────────────────────────────────────
        upsert_all_product_data(final_products, SCRAPER_ID, CURRENCY)

        os.makedirs("scraped_files", exist_ok=True)
        csv_path = f"scraped_files/{SCRAPER_ID}_latest.csv"

        _cb(93, "Generating Shopify CSV…")
        rows = _build_shopify_csv_rows(final_products)
        _write_shopify_csv(rows, csv_path)

        _cb(97, "Uploading CSV to Supabase…", len(final_products))
        csv_url = upload_csv_to_supabase(csv_path, SCRAPER_ID)

        update_scrape_record(
            scrape_record_id,
            status="completed",
            products_count=len(final_products),
            csv_url=csv_url,
        )
        _cb(100, f"Done ✅  {len(final_products)} Hoka products", len(final_products))
        print(f"✅ Hoka complete: {len(final_products)} products")
        return final_products

    except Exception as exc:
        import traceback
        traceback.print_exc()
        update_scrape_record(scrape_record_id, status="failed", error_message=str(exc))
        raise

    finally:
        heart_stop.set()


if __name__ == "__main__":
    complete_workflow_hoka()
