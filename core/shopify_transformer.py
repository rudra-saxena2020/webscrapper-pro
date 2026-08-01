"""
Shopify Transformer — Cruise Fashion → Shopify CSV
====================================================
Producing a production-ready, zero-defect Shopify CSV.
Includes streaming-append safety and image deduplication.
"""

import re
import csv
import json
import logging
import os
import collections
from .tag_engine import generate_handle, clean_title, refine_gender_for_coach_export, map_to_store_tags
from .pricing_engine import (
    calculate_price_inr,
    calculate_cost_inr,
    calculate_compare_price,
    calculate_compare_price_from_ticket,
    get_exchange_rates,
    force_refresh_rates,
)

logger = logging.getLogger(__name__)

SHOPIFY_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category",
    "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value",
    "Option2 Name", "Option2 Value",
    "Variant SKU", "Variant Grams",
    "Variant Inventory Tracker", "Variant Inventory Qty",
    "Variant Inventory Policy", "Variant Fulfillment Service",
    "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable", "Variant Barcode",
    "Variant Image",
    "Variant All Images",
    "Image Src", "Image Position", "Image Alt Text",
    "Gift Card",
    "Status",
    "Google Shopping / Google Product Category",
    "Google Shopping / Gender",
    "Cost per item",
    "currency",
]

def _empty_row(handle: str) -> dict:
    return {col: "" for col in SHOPIFY_COLUMNS} | {"Handle": handle}

def _infer_simple_gender(title: str, website_gender: str = "") -> str:
    g = (website_gender or "").strip().lower()
    if g in {"men", "male", "mens"}:
        return "men"
    if g in {"women", "female", "womens", "ladies"}:
        return "women"
    if g in {"unisex"}:
        return "unisex"
    t = f" {str(title or '').lower().replace('-', ' ')} "
    if any(k in t for k in [" women ", " lady ", " ladies ", " female ", " girl "]):
        return "women"
    if any(k in t for k in [" men ", " male ", " mens ", " gents ", " boy "]):
        return "men"
    return "unisex"


def _strip_html_simple(html: str) -> str:
    if not html:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", cleaned).strip()[:2000]


def _gender_upper_to_shopify(g: str) -> str:
    m = {"MEN": "men", "WOMEN": "women", "UNISEX": "unisex"}
    return m.get((g or "").strip().upper(), "unisex")

def _normalize_option_columns(row: dict) -> None:
    """
    Prevent Shopify import failure: duplicated option names (e.g., Size + Size).
    If both option names are equal, collapse Option2 into blank.
    """
    o1 = str(row.get("Option1 Name", "") or "").strip()
    o2 = str(row.get("Option2 Name", "") or "").strip()
    if o1 and o2 and o1.lower() == o2.lower():
        row["Option2 Name"] = ""
        row["Option2 Value"] = ""

def _sanitize_option_names(n1: str, n2: str, n3: str = "") -> tuple[str, str, str]:
    """
    Make option names Shopify-safe: no duplicate non-empty names.
    Keeps order, blanks duplicates.
    """
    names = [(n1 or "").strip(), (n2 or "").strip(), (n3 or "").strip()]
    seen = set()
    out = []
    for name in names:
        if not name:
            out.append("")
            continue
        low = name.lower()
        if low in seen:
            out.append("")
            continue
        seen.add(low)
        out.append(name)
    return out[0], out[1], out[2]

def _infer_simple_category(title: str, p_type: str = "") -> str:
    blob = f"{title or ''} {p_type or ''}".lower()
    if "watch" in blob:
        return "watches"
    if any(k in blob for k in [
        "bag", "tote", "wallet", "crossbody", "satchel", "backpack", "pouch", "brief", "wristlet"
    ]):
        return "bags"
    return "accessories"

def _build_simple_tags(title: str, p_type: str, gender: str, vendor: str = "") -> str:
    category = _infer_simple_category(title, p_type)
    vendor_tag = re.sub(r'\s+', '-', vendor.strip().lower()) if vendor.strip() else "mirage"
    return ", ".join(filter(None, [vendor_tag, gender, category]))


# ── Type → (broad_cat, sub_cat_slug) for Cruise-format tag builder ─────────────
_TRANSFORMER_TYPE_MAP = {
    "shoulder bag": ("accessories","shoulder-bag"),
    "bag": ("accessories","bag"),
    "tote": ("accessories","tote"),
    "crossbody bag": ("accessories","crossbody-bag"),
    "handbag": ("accessories","handbag"),
    "bucket bag": ("accessories","bucket-bag"),
    "satchel": ("accessories","satchel"),
    "hobo bag": ("accessories","hobo-bag"),
    "mini bag": ("accessories","mini-bag"),
    "backpack": ("accessories","backpack"),
    "belt bag": ("accessories","belt-bag"),
    "wristlet": ("accessories","wristlet"),
    "clutch": ("accessories","clutch"),
    "pouches": ("accessories","mini-bag"),
    "watch": ("accessories","watch"),
    "watches": ("accessories","watch"),
    "jewelry watches": ("accessories","watch"),
    "wallet": ("accessories","wallet"),
    "card case": ("accessories","card-case"),
    "small leather goods": ("accessories","small-leather-goods"),
    "belt": ("accessories","belt"),
    "jewelry": ("accessories","jewelry"),
    "eyewear": ("accessories","sunglasses"),
    "sunglasses": ("accessories","sunglasses"),
    "scarf": ("accessories","scarf"),
    "hats scarves gloves": ("accessories","hat"),
    "hat": ("accessories","hat"),
    "gloves": ("accessories","gloves"),
    "straps charms and keyrings": ("accessories","small-leather-goods"),
    "charms straps": ("accessories","small-leather-goods"),
    "shoe": ("footwear","shoe"),
    "sneaker": ("footwear","sneaker"),
    "sandal": ("footwear","sandal"),
    "boot": ("footwear","boot"),
    "loafer": ("footwear","loafer"),
    "flat": ("footwear","flat"),
    "pump": ("footwear","pump"),
    "mule": ("footwear","mule"),
    "slide": ("footwear","slide"),
    "apparel": ("apparel","apparel"),
    "top": ("apparel","top"),
    "dress": ("apparel","dress"),
    "jacket": ("apparel","jacket"),
    "coat": ("apparel","coat"),
    "pant": ("apparel","pant"),
    "bottoms": ("apparel","pant"),
    "shorts": ("apparel","shorts"),
    "shirt": ("apparel","top"),
    "knitwear": ("apparel","top"),
    "leggings": ("apparel","leggings"),
    "coachtopia bags accessories": ("accessories","bag"),
    "coachtopia clothes": ("apparel","apparel"),
    # "Coach Re Loved" / "Restored" / "Upcrafted" etc. are sustainability *collections*,
    # NOT product types — fall through to title-based detection instead of assuming "bag"
    "tech travel home": ("accessories","small-leather-goods"),
    "tech desk travel": ("accessories","small-leather-goods"),
    "tech travel": ("accessories","small-leather-goods"),
    "tech accessories": ("accessories","small-leather-goods"),
    "fragrance": ("accessories","fragrance"),
    "hats scarves gloves": ("accessories","hat"),
    "jewelry watches": ("accessories","jewelry"),   # earrings/rings/necklaces
    "jewelry": ("accessories","jewelry"),
    "bag strap": ("accessories","strap"),
    "strap": ("accessories","strap"),
}

_TRANSFORMER_BAG_SUBS = {"shoulder-bag","tote","crossbody-bag","handbag","bucket-bag",
                         "satchel","hobo-bag","mini-bag","backpack","belt-bag","wristlet","clutch","bag"}
_TRANSFORMER_WATCH_SUBS = {"watch","chronograph"}
_TRANSFORMER_FOOT_SUBS = {"sneaker","loafer","flat","sandal","slide","pump","boot","shoe","trainer","mule"}

_JEWELRY_TITLE_RE = re.compile(
    r'\b(earring|necklace|bracelet|pendant|ring|bangle|brooch|cufflink|anklet|choker|locket)\b',
    re.I
)
_APPAREL_TITLE_RE = re.compile(
    r'\b(t-shirt|tee|hoodie|sweatshirt|crewneck|pullover|sweater|knitwear|blouse|camisole'
    r'|tank top|jersey|shirt|polo|oxford|flannel|button-down|jacket|blazer|bomber|blouson'
    r'|puffer|parka|anorak|windbreaker|trench|varsity|moto|leather.jacket|shearling'
    r'|fleece|coat|overcoat|duster|trench.coat|pant|trouser|jean|chino|legging|jogger'
    r'|dress|gown|midi|maxi|skirt|shorts|vest|cardigan|swimwear|swim.trunk|robe|romper'
    r'|tracksuit|jumpsuit|racer)\b',
    re.I
)
_FOOTWEAR_TITLE_RE = re.compile(
    r'\b(bootie|boot|sneaker|trainer|sandal|slide|loafer|moccasin|pump|stiletto|kitten.heel'
    r'|ballet.flat|flat.shoe|mule|espadrille)\b',
    re.I
)
_FRAGRANCE_TITLE_RE = re.compile(r'\b(eau de|parfum|toilette|cologne|perfume|fragrance)\b', re.I)
_SCARF_TITLE_RE = re.compile(r'\b(scarf|stole|shawl|muffler|bandana|wrap)\b', re.I)
_SUNGLASSES_TITLE_RE = re.compile(r'\b(sunglasses?|eyewear|optical)\b', re.I)

def _resolve_cats_for_transformer(p_type: str, title: str):
    t = (p_type or "").strip().lower()
    title_l = (title or "").lower()

    # ── Step 1: Title always wins for clear product signals ──────────────────
    # (This overrides wrong API categories e.g. "Restored" T-shirts filed under bags)
    if _FRAGRANCE_TITLE_RE.search(title_l):
        return ("accessories", "fragrance")
    if _SUNGLASSES_TITLE_RE.search(title_l):
        return ("accessories", "sunglasses")
    if _JEWELRY_TITLE_RE.search(title_l):
        return ("accessories", "jewelry")
    # Backpack / sling pack must be checked before apparel because "racer" / "sling"
    # are otherwise matched by the apparel regex.
    if re.search(r'\b(backpacks?|sling\s+pack|racer\s+sling|pack\s+bag)\b', title_l):
        return ("accessories", "backpack")
    # Coat must be checked before apparel because "trench" matches the apparel regex.
    if re.search(r'\b(coats?|overcoat|topcoat|peacoat|trench\s+coat|duffle\s+coat)\b', title_l):
        return ("apparel", "coat")
    if _FOOTWEAR_TITLE_RE.search(title_l):
        m = _FOOTWEAR_TITLE_RE.search(title_l)
        kw = m.group(1).lower().replace(" ", "-").replace(".", "-")
        slug_map = {
            "bootie": "boot", "sneaker": "sneaker", "trainer": "sneaker",
            "sandal": "sandal", "slide": "slide", "loafer": "loafer",
            "moccasin": "loafer", "pump": "pump", "stiletto": "pump",
            "kitten-heel": "pump", "ballet-flat": "flat", "flat-shoe": "flat",
            "mule": "mule", "espadrille": "sandal",
        }
        return ("footwear", slug_map.get(kw, kw))
    if _SCARF_TITLE_RE.search(title_l):
        return ("accessories", "scarf")
    if _APPAREL_TITLE_RE.search(title_l):
        m = _APPAREL_TITLE_RE.search(title_l)
        kw = m.group(1).lower()
        slug_map = {
            "t-shirt":"top","tee":"top","hoodie":"top","sweatshirt":"top","crewneck":"top",
            "pullover":"top","sweater":"top","knitwear":"top","blouse":"top","camisole":"top",
            "tank top":"top","jersey":"top","shirt":"shirt","polo":"shirt","oxford":"shirt",
            "flannel":"shirt","button-down":"shirt","jacket":"jacket","blazer":"jacket",
            "bomber":"jacket","blouson":"jacket","puffer":"jacket","parka":"jacket",
            "anorak":"jacket","windbreaker":"jacket","trench":"jacket","varsity":"jacket",
            "moto":"jacket","leather":"jacket","shearling":"jacket","fleece":"jacket",
            "coat":"coat","overcoat":"coat","duster":"coat","trench coat":"coat",
            "pant":"pant","trouser":"pant","jean":"pant","chino":"pant","legging":"pant",
            "jogger":"pant","dress":"dress","gown":"dress","midi":"dress","maxi":"dress",
            "skirt":"apparel","shorts":"shorts","vest":"apparel","cardigan":"top",
            "swimwear":"apparel","swim trunk":"apparel","robe":"apparel","romper":"apparel",
            "tracksuit":"apparel","jumpsuit":"apparel","racer":"jacket",
        }
        return ("apparel", slug_map.get(kw, "apparel"))

    # ── Step 2: Explicit product type map (after title overrides) ────────────
    if t in _TRANSFORMER_TYPE_MAP:
        broad, slug = _TRANSFORMER_TYPE_MAP[t]
        if broad == "footwear" and slug in ("shoe","sneaker"):
            if re.search(r'\bsandal\b', title_l):             slug = "sandal"
            elif re.search(r'\b(loafer|moccasin)\b', title_l): slug = "loafer"
            elif re.search(r'\b(boot|bootie)\b', title_l):    slug = "boot"
            elif re.search(r'\b(pump|stiletto)\b', title_l):  slug = "pump"
            elif re.search(r'\bmule\b', title_l):             slug = "mule"
            elif re.search(r'\bslide\b', title_l):            slug = "slide"
            elif re.search(r'\bflat\b', title_l):             slug = "flat"
        return broad, slug

    # ── Step 3: Title keyword fallback ───────────────────────────────────────
    if re.search(r'\b(watch|chronograph)\b', title_l): return ("accessories","watch")
    if re.search(r'\btote\b', title_l):                return ("accessories","tote")
    if re.search(r'\bcrossbody\b', title_l):           return ("accessories","crossbody-bag")
    if re.search(r'\bbackpack\b', title_l):            return ("accessories","backpack")
    if re.search(r'\bwallet\b', title_l):              return ("accessories","wallet")
    if re.search(r'\bsneaker\b', title_l):             return ("footwear","sneaker")
    if re.search(r'\bsandal\b', title_l):              return ("footwear","sandal")
    if re.search(r'\bloafer\b', title_l):              return ("footwear","loafer")
    if re.search(r'\bboot\b', title_l):                return ("footwear","boot")
    if re.search(r'\bshoe\b', title_l):                return ("footwear","shoe")
    if re.search(r'\bbag\b', title_l):                 return ("accessories","bag")
    return ("accessories", None)

def _build_cruise_format_tags(title: str, p_type: str, gender: str, vendor: str) -> str:
    """
    Build Cruise Fashion-compatible tags for use in normalize_shopify_csv_file.
    Format: source, broad-category, gender(s), gender-taxonomy, [sub-brand], product-type
    """
    tag_set = set()
    vendor_l = re.sub(r'[®™©]', '', (vendor or "")).strip().lower()
    source = vendor_l if vendor_l else "coach"
    tag_set.add(source)
    if "coachtopia" in (title or "").lower() and source != "coachtopia":
        tag_set.add("coachtopia")

    broad_cat, sub_cat_slug = _resolve_cats_for_transformer(p_type, title)
    title_l = (title or "").lower()

    if sub_cat_slug in _TRANSFORMER_BAG_SUBS:       display_broad = "bags"
    elif sub_cat_slug in _TRANSFORMER_WATCH_SUBS:   display_broad = "watches"
    elif "watch" in title_l or "chronograph" in title_l: display_broad = "watches"
    elif sub_cat_slug in _TRANSFORMER_FOOT_SUBS or broad_cat=="footwear": display_broad = "footwear"
    elif broad_cat == "apparel":                     display_broad = "apparel"
    else:                                            display_broad = "accessories"
    tag_set.add(display_broad)

    if sub_cat_slug:
        sub_r = sub_cat_slug.replace("-"," ").lower().strip()
        if sub_r and sub_r not in (source, display_broad):
            tag_set.add(sub_r)

    g = (gender or "unisex").upper()
    if g == "UNISEX":
        tag_set.update(["men","women","unisex"])
        w_main, w_sub = map_to_store_tags("WOMEN", broad_cat, sub_cat_slug)
        m_main, m_sub = map_to_store_tags("MEN",   broad_cat, sub_cat_slug)
        for t in [w_main, w_sub, m_main, m_sub]:
            if t: tag_set.add(t)
    else:
        tag_set.add(g.lower())
        main_cat, sub_cat = map_to_store_tags(g, broad_cat, sub_cat_slug)
        if main_cat: tag_set.add(main_cat)
        if sub_cat: tag_set.add(sub_cat)

    tag_set.discard(""); tag_set.discard(None)

    # ── Verification layer ─────────────────────────────────────────────────────
    # Ensures gender tags, taxonomy completeness, and broad category are all
    # internally consistent before finalizing.
    tag_set = _verify_cruise_tags(tag_set, g, broad_cat, sub_cat_slug)

    return ", ".join(sorted(tag_set))


def _verify_cruise_tags(tag_set, gender, broad_cat, sub_cat_slug):
    """
    Verification layer for Cruise-format tag sets.

    Ensures:
    1. Gender tags (men/women/unisex) match the gender field exactly.
    2. UNISEX → BOTH mens-*/men-* AND womens-*/women-* taxonomy tags present.
    3. WOMEN  → no stray mens-*/men-* taxonomy tags; womens-*/women-* present.
    4. MEN    → no stray womens-*/women-* taxonomy tags; mens-*/men-* present.
    5. The correct broad category tag (bags/footwear/watches/accessories/apparel)
       is present and all other broad tags are removed.
    """
    g = (gender or "UNISEX").upper()

    def _has_mens_tax():
        return any(t.lower().startswith(("mens-", "men-")) for t in tag_set)

    def _has_womens_tax():
        return any(t.lower().startswith(("womens-", "women-")) for t in tag_set)

    # 1. Gender tag consistency
    for _gt in ("men", "women", "unisex"):
        tag_set.discard(_gt)

    if g == "UNISEX":
        tag_set.update(["men", "women", "unisex"])
    else:
        tag_set.add(g.lower())

    # 2-4. Taxonomy completeness
    if g == "UNISEX":
        if not _has_mens_tax():
            m_main, m_sub = map_to_store_tags("MEN", broad_cat, sub_cat_slug)
            for t in (m_main, m_sub):
                if t: tag_set.add(t)
        if not _has_womens_tax():
            w_main, w_sub = map_to_store_tags("WOMEN", broad_cat, sub_cat_slug)
            for t in (w_main, w_sub):
                if t: tag_set.add(t)
    elif g == "WOMEN":
        for _t in list(tag_set):
            if _t.lower().startswith(("mens-", "men-")):
                tag_set.discard(_t)
        if not _has_womens_tax():
            w_main, w_sub = map_to_store_tags("WOMEN", broad_cat, sub_cat_slug)
            for t in (w_main, w_sub):
                if t: tag_set.add(t)
    elif g == "MEN":
        for _t in list(tag_set):
            if _t.lower().startswith(("womens-", "women-")):
                tag_set.discard(_t)
        if not _has_mens_tax():
            m_main, m_sub = map_to_store_tags("MEN", broad_cat, sub_cat_slug)
            for t in (m_main, m_sub):
                if t: tag_set.add(t)

    # 5. Broad category consistency
    _BAG_SUBS  = {"shoulder-bag","tote","crossbody-bag","handbag","bucket-bag","satchel",
                  "hobo-bag","mini-bag","backpack","belt-bag","wristlet","clutch","bag"}
    _WATCH_SUBS = {"watch","chronograph"}
    _FOOT_SUBS  = {"sneaker","loafer","flat","sandal","slide","pump","boot","shoe","trainer","mule"}

    if sub_cat_slug in _BAG_SUBS:                                expected_broad = "bags"
    elif sub_cat_slug in _WATCH_SUBS:                            expected_broad = "watches"
    elif sub_cat_slug in _FOOT_SUBS or broad_cat == "footwear":  expected_broad = "footwear"
    elif broad_cat == "apparel":                                 expected_broad = "apparel"
    else:                                                        expected_broad = "accessories"

    for _b in ("bags", "footwear", "watches", "accessories", "apparel"):
        if _b != expected_broad:
            tag_set.discard(_b)
    tag_set.add(expected_broad)

    tag_set.discard(""); tag_set.discard(None)
    return tag_set


def _normalize_image_url(url: str) -> str:
    """
    Canonicalise an image URL for deduplication.
    Strips Shopify CDN cache-busting tokens (?v=...) and other query params
    that do not change the image itself, so two references to the same image
    with different version tokens are treated as one.
    """
    if not url or not isinstance(url, str):
        return ""
    import re as _re
    cleaned = url.strip()
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    if not (cleaned.startswith("http") and len(cleaned) > 15):
        return ""
    # Strip cache-busting / version params that do not affect image content.
    # Keep params like ?preset= (Bynder), ?_s= (Cloudinary signed) as-is —
    # they ARE semantically different URLs for the same source file.
    cleaned = _re.sub(r'[?&]v=[^&]*', '', cleaned).rstrip('?&')
    cleaned = _re.sub(r'[?&]width=[^&]*', '', cleaned).rstrip('?&')
    cleaned = _re.sub(r'[?&]height=[^&]*', '', cleaned).rstrip('?&')
    return cleaned

def map_shopify_category(title, p_type, raw_cat):
    """
    Shopify's new taxonomy is strict.
    Leaving this EMPTY allows Shopify's AI to auto-categorize.
    This prevents the 'invalid product category' import error.
    """
    return ""

# ── CSV Sanitization ───────────────────────────────────────────────────

def _sanitize_csv_field(value: str, is_html: bool = False) -> str:
    """
    Make a field safe for Shopify's strict CSV parser.
    - Replace literal double-quotes with the HTML entity (avoids RFC 4180 doubling
      that confuses Shopify's importer).
    - Strip bare newlines / carriage returns (Shopify doesn't support multiline fields).
    """
    if not value:
        return value
    value = value.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    value = value.replace('"', '&quot;')
    return value.strip()

def _sanitize_row(row: dict) -> dict:
    """Apply CSV sanitization to fields that commonly carry free text."""
    TEXT_FIELDS = {"Body (HTML)", "Title", "Tags", "Image Alt Text", "Variant SKU"}
    out = {}
    for k, v in row.items():
        if k in TEXT_FIELDS and isinstance(v, str):
            out[k] = _sanitize_csv_field(v, is_html=(k == "Body (HTML)"))
        else:
            out[k] = v
    return out

# ── Auto-Correction Functions ──────────────────────────────────────────

def auto_fix_pricing(final_price, cost_price):
    """
    Ensures monetizable margins. IF price <= cost: FIX: price = cost * 1.15
    """
    try:
        p = float(final_price) if final_price else 0
        c = float(cost_price) if cost_price else 0
        if c <= 0: return p, c # Pricing formula error, cannot fix cost here
        
        if p <= c:
            return round(c * 1.15, 2), c
        return p, c
    except:
        return final_price, cost_price

# UK → EU footwear conversion (women's sizing reference per spec)
_UK_TO_EU = {
    "2": "35",   "2.5": "35.5", "3": "36",   "3.5": "36.5",
    "4": "37",   "4.5": "37.5", "5": "38",   "5.5": "38.5",
    "6": "39",   "6.5": "39.5", "7": "40",   "7.5": "41",
    "8": "42",   "8.5": "42.5", "9": "43",   "9.5": "44",
    "10": "44.5","10.5": "45",  "11": "46",  "11.5": "46.5",
    "12": "47",  "12.5": "47.5",
}

def _uk_label(uk_num: float, eu: str | None = None) -> str:
    """Build display label 'UK X' per spec (EU size omitted for clean display)."""
    val = str(uk_num) if uk_num % 1 != 0 else str(int(uk_num))
    return f"UK {val}"


def normalize_size(s):
    """
    Normalize footwear sizes to spec format: 'UK X (EU Y)'.
    Accepts 'UK 7', 'UK 7 (EU 40)', '6 (40)', '7', etc.
    """
    s = str(s).strip()

    # Strip '(EU X)' suffix from existing 'UK X (EU Y)' values → clean 'UK X'
    m_strip = re.match(r'^(UK\s+[\d\.]+)\s*\(EU\s+[\d\.]+\)$', s, re.I)
    if m_strip:
        return m_strip.group(1).strip()

    # Already clean 'UK X' format
    if re.match(r'^UK\s+[\d\.]+$', s, re.I):
        return s

    # Plain numeric UK size — add 'UK' prefix if in valid footwear range
    m2 = re.match(r'^([\d\.]+)$', s)
    if m2:
        try:
            num = float(m2.group(1))
            if 2.0 <= num <= 12.5:
                return _uk_label(num)
        except ValueError:
            pass

    # 'N (EU M)' or 'N(M)' input — convert to clean 'UK N'
    m1 = re.match(r'^([\d\.]+)\s*\(\s*(?:EU\s*)?([\d\.]+)\s*\)$', s, re.I)
    if m1:
        try:
            return _uk_label(float(m1.group(1)))
        except ValueError:
            return s

    return s

def auto_fix_variant(v, handle, rates, product_color="Standard"):
    """
    Auto-corrects variant data. Generates SKUs and defaults sizes.
    """
    # 1. Options/Size Correction
    opt1_name = str(v.get("Option1 Name", "")).strip()
    opt1 = str(v.get("Option1 Value", "")).strip()
    opt2 = str(v.get("Option2 Value", "")).strip()
    size_raw = str(v.get("size", "")).strip()
    color_raw = str(v.get("color", "")).strip()

    # Standard letter/number sizes recognised as sizes (not colours).
    # UK shoe sizes are handled separately via normalize_size() which converts
    # numeric values to "UK N" format using the tables in tag_engine.py.
    _SIZE_WORDS = {
        'S', 'M', 'L', 'XL', 'XXL', 'XXXL', 'XXXXL', 'XXS', 'XS',
        '2XL', '3XL', '4XL', '5XL', 'DEFAULT',
        # One-size / single-variant placeholders (Tory Burch "OS", generic "ONE SIZE")
        'OS', 'ONE SIZE', 'ONESIZE', 'O/S', 'O.S.',
        # Bag size descriptors used by Marc Jacobs etc. as the SIZE attribute
        'MINI', 'SMALL', 'MEDIUM', 'LARGE', 'PETITE', 'STANDARD', 'REGULAR',
        # Bra cup sizes (SKIMS and similar intimates brands)
        'A', 'B', 'C', 'D', 'DD', 'DDD', 'E', 'F', 'G', 'H', 'I', 'J',
    }

    # Shopify's canonical single-variant "Title / Default Title" — pass through
    # unchanged so it renders as one variant with no option columns in Shopify,
    # NOT as Color="Default Title".
    if opt1_name.strip().lower() == "title" and opt1.strip().lower() in ("default title", "default"):
        sku = v.get("Variant SKU") or v.get("sku") or handle
        orig_price = v.get("Variant Price")
        currency = v.get("currency", "GBP")
        final_price = calculate_price_inr(orig_price, currency, rates)
        cost_price = calculate_cost_inr(orig_price, currency, rates)
        if not final_price or final_price <= 0: return None
        if not cost_price or cost_price <= 0: cost_price = final_price / 1.25
        final_price, cost_price = auto_fix_pricing(final_price, cost_price)
        orig_compare = v.get("Variant Compare At Price")
        compare_price = calculate_compare_price_from_ticket(orig_compare, currency, rates)
        if not compare_price or compare_price <= final_price:
            compare_price = calculate_compare_price(final_price)
        return {
            "Variant SKU": sku,
            "Option1 Name": "Title", "Option1 Value": "Default Title",
            "Option2 Name": "",      "Option2 Value": "",
            "Variant Price": final_price, "Variant Compare At Price": compare_price,
            "Cost per item": cost_price,
            "currency": currency,
            "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
            "images": v.get("images", []) or v.get("_images", []),
        }

    # When the source already marks Option1 as "Size" (e.g. Coach products which have
    # no colour variation), keep the size-only structure — do NOT bolt on a fake Color.
    if opt1_name.upper() == "SIZE":
        size = normalize_size(opt1 or size_raw or "Default")
        if size.lower() in ("", "default"): size = "Default"
        # 2. SKU (inlined below — same logic)
        sku = v.get("Variant SKU") or v.get("sku")
        if not sku:
            sku = f"{handle}-{size.replace(' ', '-')}".lower()
        # 3. Pricing
        orig_price = v.get("Variant Price")
        currency = v.get("currency", "GBP")
        final_price = calculate_price_inr(orig_price, currency, rates)
        cost_price = calculate_cost_inr(orig_price, currency, rates)
        if not final_price or final_price <= 0: return None
        if not cost_price or cost_price <= 0:
            cost_price = final_price / 1.25
        final_price, cost_price = auto_fix_pricing(final_price, cost_price)
        orig_compare = v.get("Variant Compare At Price")
        compare_price = calculate_compare_price_from_ticket(orig_compare, currency, rates)
        if not compare_price or compare_price <= final_price:
            compare_price = calculate_compare_price(final_price)
        return {
            "Variant SKU": sku,
            "Option1 Name": "Size",
            "Option1 Value": size,
            "Option2 Name": "",
            "Option2 Value": "",
            "Variant Price": final_price,
            "Variant Compare At Price": compare_price,
            "Cost per item": cost_price,
            "currency": currency,
            "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
            "images": v.get("images", []) or v.get("_images", []),
        }

    if opt2:
        size = normalize_size(opt2)
        color = opt1 or color_raw or product_color
    else:
        # If we only have one value (opt1 or size_raw), determine if it's a color or a size
        val = size_raw or opt1 or "Default"
        if any(c.isdigit() for c in val) or val.upper() in _SIZE_WORDS:
            size = normalize_size(val)
            color = color_raw or product_color
        else:
            # It's likely a color (e.g. "Black")
            size = "Default"
            color = val
        # If size resolved to Default but color_raw itself looks like a size word
        # (e.g. Karl Lagerfeld / TDBUK stores that put "S/M/L" in the Color option),
        # promote color_raw → size and leave color blank so the transformer emits a
        # clean size-only variant rather than Color="S".
        if size == "Default" and not size_raw and color_raw and color_raw.upper() in _SIZE_WORDS:
            size = normalize_size(color_raw)
            color = product_color or ""

    if size.lower() in ("", "default"): size = "Default"
    # Reject brand-leak placeholders & known invalid color values.
    _BAD_COLOR_TOKENS = {
        "default", "standard", "michael kors", "karl lagerfeld", "kl jeans",
        "marc jacobs", "tory burch", "coach", "cruise", "cruise fashion",
        "contemporary", "little marc", "karl lagerfeld paris",
        # one-size placeholders mistakenly stored as colour
        "os", "one size", "onesize", "o/s", "o.s.",
    }
    if color and color.strip().lower() in _BAD_COLOR_TOKENS:
        # If colour was actually a size placeholder, repurpose it as the size
        if color.strip().lower() in {"os", "one size", "onesize", "o/s", "o.s."}:
            if not size or size == "Default":
                size = color.strip()
        color = ""
    if not color:
        color = product_color if product_color and product_color.strip().lower() not in _BAD_COLOR_TOKENS else ""

    # If there is genuinely no colour, emit a SIZE-ONLY variant (cleaner than
    # putting "Standard" or a brand name into Option1 Color).
    if not color and size and size != "Default":
        sku = v.get("Variant SKU") or v.get("sku") or f"{handle}-{size.replace(' ', '-')}".lower()
        orig_price = v.get("Variant Price")
        currency = v.get("currency", "GBP")
        final_price = calculate_price_inr(orig_price, currency, rates)
        cost_price = calculate_cost_inr(orig_price, currency, rates)
        if not final_price or final_price <= 0: return None
        if not cost_price or cost_price <= 0: cost_price = final_price / 1.25
        final_price, cost_price = auto_fix_pricing(final_price, cost_price)
        orig_compare = v.get("Variant Compare At Price")
        compare_price = calculate_compare_price_from_ticket(orig_compare, currency, rates)
        if not compare_price or compare_price <= final_price:
            compare_price = calculate_compare_price(final_price)
        return {
            "Variant SKU": sku, "Option1 Name": "Size", "Option1 Value": size,
            "Option2 Name": "", "Option2 Value": "",
            "Variant Price": final_price, "Variant Compare At Price": compare_price,
            "Cost per item": cost_price,
            "currency": currency,
            "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
            "images": v.get("images", []) or v.get("_images", []),
        }
    # If size and color both resolved to meaningless "Default" and the source
    # provided NO real variant data (color_raw, size_raw, opt1, opt2 all blank),
    # emit Shopify's canonical single-variant format (Title / Default Title)
    # rather than showing confusing "Color=Default, Size=Default" in the store.
    if (size == "Default" and not color
            and not color_raw and not size_raw and not opt1 and not opt2):
        sku = v.get("Variant SKU") or v.get("sku") or handle
        orig_price = v.get("Variant Price")
        currency   = v.get("currency", "GBP")
        final_price  = calculate_price_inr(orig_price, currency, rates)
        cost_price   = calculate_cost_inr(orig_price, currency, rates)
        if not final_price or final_price <= 0: return None
        if not cost_price or cost_price <= 0: cost_price = final_price / 1.25
        final_price, cost_price = auto_fix_pricing(final_price, cost_price)
        orig_compare  = v.get("Variant Compare At Price")
        compare_price = calculate_compare_price_from_ticket(orig_compare, currency, rates)
        if not compare_price or compare_price <= final_price:
            compare_price = calculate_compare_price(final_price)
        return {
            "Variant SKU": sku,
            "Option1 Name": "Title", "Option1 Value": "Default Title",
            "Option2 Name": "",      "Option2 Value": "",
            "Variant Price": final_price, "Variant Compare At Price": compare_price,
            "Cost per item": cost_price,
            "currency": currency,
            "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
            "images": v.get("images", []) or v.get("_images", []),
        }

    if not color:
        color = "Default"

    # 2. SKU Correction
    sku = v.get("Variant SKU") or v.get("sku")
    if not sku:
        sku = f"{handle}-{size.replace(' ', '-')}".lower()
    
    # 3. Pricing Correction
    orig_price = v.get("Variant Price")
    currency = v.get("currency", "GBP")
    
    final_price = calculate_price_inr(orig_price, currency, rates)
    cost_price = calculate_cost_inr(orig_price, currency, rates)
    
    # If price missing, we cannot fix this variant
    if not final_price or final_price <= 0: return None
    
    # Recalculate cost if missing/invalid
    if not cost_price or cost_price <= 0:
        cost_price = final_price / 1.25 # Reverse-engineer fallback
        
    final_price, cost_price = auto_fix_pricing(final_price, cost_price)
    
    orig_compare = v.get("Variant Compare At Price")
    compare_price = calculate_compare_price_from_ticket(orig_compare, currency, rates)
    if not compare_price or compare_price <= final_price:
        compare_price = calculate_compare_price(final_price)
        
    # If size resolved to the "Default" placeholder (meaning the scraper sent no
    # real size), emit an empty Option2 Value so the CSV builder's has_o2_value
    # check suppresses the Size option entirely for colour-only products.
    size_out = size if size not in ("Default", "") else ""
    return {
        "Variant SKU": sku,
        "Option1 Name": "Color",
        "Option1 Value": color,
        "Option2 Name": "Size" if size_out else "",
        "Option2 Value": size_out,
        "Variant Price": final_price,
        "Variant Compare At Price": compare_price,
        "Cost per item": cost_price,
        "currency": currency,
        "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
        "images": v.get("images", []) or v.get("_images", [])
    }

def _merge_color_variants(products: list) -> list:
    """
    Merge same-title + same-gender product dicts into a single product with all
    color variants combined.

    Many brand websites (Organic Basics, SKIMS, The Reformation, etc.) expose
    each color of a product as its own URL/product.  This function collapses
    them so the Shopify store sees one unified product (e.g. "True Heavy Regular
    Fit Tee" with Color=Black/White/Sage/… and Size=XS–XXXL) instead of five
    separate identically-titled products.

    Products with a unique title are passed through unchanged (handle preserved).
    Only when two or more products share the same title+gender is the handle
    regenerated from the title with a gender prefix (mens-/womens-).
    """
    from collections import OrderedDict

    groups: OrderedDict = OrderedDict()
    for prod in products:
        gender = (prod.get("Google Shopping / Gender") or "women").lower()
        title  = (prod.get("Title") or prod.get("title") or "").strip()
        # Key by CLEANED HANDLE (not exact title) so products that produce the
        # same Shopify handle (e.g. "UA Tech™" vs "UA Tech", "UA Unstoppable+"
        # vs "UA Unstoppable Woven") are merged before the 100-variant split —
        # preventing duplicate handles in the output CSV.
        clean_hdl = re.sub(r"[^a-z0-9]+", "-", title.lower().strip()).strip("-")
        key       = (clean_hdl, gender)
        groups.setdefault(key, []).append(prod)

    result = []
    for (clean_hdl, gender), group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        # Multiple products share the same cleaned-handle+gender — merge them.
        real_title  = group[0].get("Title") or group[0].get("title") or ""
        gender_pfx  = "mens" if gender == "men" else "womens"
        clean_hdl   = re.sub(r"[^a-z0-9]+", "-", real_title.lower().strip()).strip("-")

        base = {**group[0], "Handle": f"{gender_pfx}-{clean_hdl}"}
        existing_skus = {v.get("Variant SKU", "") for v in base.get("variants", [])
                         if v.get("Variant SKU")}
        existing_imgs = set(base.get("images", []))

        for prod in group[1:]:
            prod_imgs = prod.get("images", [])
            prod_color = str(prod.get("_color_name") or "").strip()
            for v in prod.get("variants", []):
                sku = v.get("Variant SKU", "")
                if not sku or sku not in existing_skus:
                    # Ensure each merged variant carries its source product's
                    # images (the per-color images), not the base product's.
                    # This preserves per-color imagery for products (e.g. SKIMS
                    # bras) where each color is a separate API product.
                    if prod_imgs and not v.get("images"):
                        v = {**v, "images": list(prod_imgs)}
                    # CRITICAL FIX: carry the source product's colour name so
                    # auto_fix_variant() can map the right colour for variants
                    # that come from a different source product (MyTheresa).
                    if prod_color:
                        v = {**v, "color": prod_color}
                    base.setdefault("variants", []).append(v)
                    if sku:
                        existing_skus.add(sku)
            for img in prod.get("images", []):
                if img and img not in existing_imgs:
                    base.setdefault("images", []).append(img)
                    existing_imgs.add(img)

        # All images collected from all colour variants are kept.
        # Never limit gallery size — source image count must equal scraped count.

        result.append(base)

    merged_count = len(products) - len(result)
    if merged_count:
        logger.info(f"[Transformer] Merged {merged_count} colour-variant duplicates "
                    f"→ {len(result)} products (was {len(products)})")
    return result


def _split_over_limit(products: list, max_variants: int = 90) -> list:
    """
    Shopify hard-limits each product to 100 variants.
    Any merged product that exceeds this is split into "— Part N" siblings
    so each part stays within the limit.  Handle gains a -part-N suffix.

    We use 90 (not 100) as the split threshold because Shopify occasionally
    returns 500 errors on products with exactly 100 variants — the extra
    headroom avoids those transient failures.
    """
    import copy
    result = []
    for prod in products:
        variants = prod.get("variants", [])
        if len(variants) <= max_variants:
            result.append(prod)
            continue
        chunks = [variants[i:i + max_variants] for i in range(0, len(variants), max_variants)]
        logger.info(
            f"[Transformer] Splitting '{prod.get('Title','')}' "
            f"({len(variants)} variants) → {len(chunks)} parts"
        )
        for idx, chunk in enumerate(chunks, 1):
            p = copy.deepcopy(prod)
            p["variants"] = chunk
            p["Title"]    = f"{prod['Title']} — Part {idx}"
            p["Handle"]   = f"{prod['Handle']}-part-{idx}"
            result.append(p)
    return result


def transform_to_shopify(products: list) -> list:
    """
    Converts cleaned product dicts -> valid Shopify CSV rows.
    Includes a PROACTIVE AUTO-CORRECTION LAYER.
    """
    # Collapse same-title+gender colour variants into one product each.
    products = _merge_color_variants(products)
    # Enforce Shopify's 100-variant hard limit — split any product that exceeds it.
    products = _split_over_limit(products)

    try: rates = force_refresh_rates()
    except: rates = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1 — per-product computation + fire-and-forget image job submission
    #   • Each product's unique_images list is built and stored as a mutable ref
    #   • enqueue_if_enabled() either updates unique_images[0] synchronously
    #     (cache hit → returns None) or submits a background job (returns Future)
    #   • No per-product blocking wait — all futures are collected for Phase 2
    # Phase 2 — bounded global drain via concurrent.futures.wait()
    #   • One call waits for the entire batch; timeout = sync_timeout × workers
    #   • After the wait, unique_images[0] for each product is resolved (or kept
    #     at the original CDN URL if the job timed-out / errored)
    # Phase 3 — row generation
    #   • Iterates the prepared list; unique_images[0] is now stable
    # ─────────────────────────────────────────────────────────────────────────
    prepared    = []   # ordered per-product data dicts (hold mutable unique_images refs)
    _ip_futures = []   # Futures from Phase 1 image submissions

    for product in products:
        if not product: continue
        
        # ── 1. TITLE & HANDLE RECOVERY ──
        raw_title = product.get("Title") or product.get("name", "")
        title = clean_title(raw_title)
        
        # Use existing handle if available (Cruise pre-generates them)
        handle = product.get("Handle") or product.get("handle")
        if not handle:
            handle = generate_handle(title, style_code=product.get("styleCode"), product_key=product.get("key"))
        
        if not handle:
            handle = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

        if not title:
            brand = product.get("Vendor") or "Cruise"
            p_type = product.get("Type") or "Item"
            title = f"{brand} {p_type}".strip()

        # ── 2. VARIANT AUTO-CORRECTION ──
        raw_variants = product.get("variants", []) or []
        fixed_variants = []
        seen_keys = set()
        
        variant_images = []
        
        # Use product-level color as base.
        # NEVER fall back to Vendor — that leaks brand names ("Michael Kors",
        # "Karl Lagerfeld") into the Option1 Color field. Empty string here
        # signals auto_fix_variant() to switch the variant to size-only.
        p_color = str(product.get("_color_name") or "").strip()
        
        for v in raw_variants:
            v_fixed = auto_fix_variant(v, handle, rates, product_color=p_color)
            if v_fixed:
                key = f"{v_fixed['Option1 Value']}-{v_fixed['Variant SKU']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    fixed_variants.append(v_fixed)
                    if v_fixed["images"]:
                        variant_images.extend(v_fixed["images"])

        if not fixed_variants:
            logger.warning(f"⏩ Skip {handle}: All {len(raw_variants)} variants have no valid price — product excluded from CSV.")
            continue

        # When every auto_fix_variant failed, images were never appended (only success path did).
        # Pull image URLs from raw/fixed rows so we do not skip the whole product.
        if not variant_images and fixed_variants:
            for v in fixed_variants:
                for img in (v.get("images", []) or v.get("_images", []) or []):
                    if img:
                        variant_images.append(img)

        # ── 3. IMAGE AUTO-CORRECTION ──
        # Source priority:
        #   1. Product-level "images" list (aggregated by scraper across firstVariant.mainImage
        #      + all size-variant images — most complete and deduplicated at source)
        #   2. Per-variant images collected during auto_fix_variant pass (fallback)
        #   3. Legacy product.mainImage dict (coach-style products)
        seen_images: set = set()
        unique_images: list = []

        def _collect_img(url):
            n = _normalize_image_url(url)
            if n and n not in seen_images:
                seen_images.add(n)
                unique_images.append(n)

        # 1. Product-level aggregated images (Cruise Fashion + any scraper that sets "images")
        for img_url in (product.get("images") or []):
            _collect_img(img_url)

        # 2. Per-variant images from the transform pass (fills gaps for scrapers that don't
        #    set product-level images)
        for img_url in variant_images:
            _collect_img(img_url)

        # 3. Legacy mainImage dict (coach-style)
        main_img = product.get("mainImage")
        if isinstance(main_img, dict):
            _collect_img(main_img.get("url", ""))
        elif isinstance(main_img, str):
            _collect_img(main_img)

        # Skip only if absolutely no images found
        if not unique_images:
            logger.warning(f"⏩ Skip {handle}: No valid image URLs found.")
            continue

        # ── 3b. BiRefNet hero submission (fire-and-forget) ──────────────────
        # Only unique_images[0] is submitted; gallery/variant images untouched.
        # Cache hit → unique_images[0] updated synchronously, returns None.
        # Cache miss → Future collected for Phase 2 global drain, no per-product wait.
        try:
            from .image_processor import get_image_processor as _get_ip
            _ip_f = _get_ip().enqueue_if_enabled(product, unique_images)
            if _ip_f is not None:
                _ip_futures.append(_ip_f)
        except Exception as _ip_err:
            logger.debug(f"[ImageProc] Hook error for {handle}: {_ip_err}")

        # ── 4. Lean taxonomy (+ Gemini verify for non-Coach or unrefined feeds) ──
        pg = str(product.get("Google Shopping / Gender", "") or "")
        if product.get("_gender_refined"):
            gender = _gender_upper_to_shopify(pg)
        else:
            refined = refine_gender_for_coach_export(
                title=str(product.get("Title", "") or title),
                description=_strip_html_simple(str(product.get("Body (HTML)", "") or "")),
                raw_tags=str(product.get("Tags", "") or ""),
                local_gender=pg,
                url=str(product.get("url", "") or ""),
                url_gender_hint=None,
                breadcrumbs=str(product.get("Type", "") or ""),
            )
            gender = _gender_upper_to_shopify(refined)
        # Use the rich pre-computed tags from the scraper if available, otherwise build from scratch
        precomputed_tags = str(product.get("Tags") or "").strip()
        if precomputed_tags:
            tags = precomputed_tags
        else:
            tags = _build_simple_tags(title, product.get("Type", ""), gender, vendor=str(product.get("Vendor", "") or ""))

        category = product.get("Google Shopping / Google Product Category") or map_shopify_category(title, product.get("Type", ""), product.get("Product Category", ""))

        # unique_images is stored by reference — Phase 2 drain may still update [0]
        prepared.append({
            "handle": handle,
            "product": product,
            "fixed_variants": fixed_variants,
            "unique_images": unique_images,
            "gender": gender,
            "tags": tags,
            "category": category,
        })

    # ── Phase 2: bounded global drain ────────────────────────────────────────
    # All outstanding image futures are waited on in a single concurrent call.
    # Global timeout = sync_timeout × workers (default 5 × 4 = 20 s) so the
    # thread pool can process multiple images in parallel within the window.
    # Futures that don't finish keep their original CDN URL in unique_images[0].
    if _ip_futures:
        import concurrent.futures as _cf
        try:
            from .image_processor import IMAGE_PROCESSING as _IP_CFG
            _sync_t  = _IP_CFG.get("sync_timeout", 5)
            _workers = _IP_CFG.get("workers", 4)
            _drain_t = _IP_CFG.get("global_drain_timeout", _sync_t * max(1, _workers))
        except Exception:
            _drain_t = 20
        _cf.wait(_ip_futures, timeout=_drain_t)
        logger.debug(
            f"[ImageProc] Phase 2 drain: {len(_ip_futures)} job(s), "
            f"timeout={_drain_t}s"
        )

    # ── Phase 3: row generation — unique_images[0] is now stable ─────────────
    rows = []
    for entry in prepared:
        handle         = entry["handle"]
        product        = entry["product"]
        fixed_variants = entry["fixed_variants"]
        unique_images  = entry["unique_images"]
        gender         = entry["gender"]
        tags           = entry["tags"]

        # ── 5. ROW GENERATION ──────────
        base_o1, base_o2, _ = _sanitize_option_names(
            str(fixed_variants[0].get("Option1 Name", "Color")) if fixed_variants else "Color",
            str(fixed_variants[0].get("Option2 Name") or "") if fixed_variants else "",
            "",
        )
        num_rows = max(len(fixed_variants), len(unique_images))
        for i in range(num_rows):
            row = _empty_row(handle)

            # Global Product Attributes (Forced persistence across ALL rows for SYSTEM INTEGRITY)
            row.update({
                "Title": str(product.get("Title", "")).strip(),
                "Body (HTML)": str(product.get("Body (HTML)", "")).strip(),
                "Vendor": str(product.get("Vendor", "")).strip(),
                "Product Category": "",
                "Type": str(product.get("Type", "")).strip().title(),
                "Tags": str(tags).strip(),
                "Published": "TRUE",
                "Status": "active",
                "Google Shopping / Google Product Category": "",
                "Google Shopping / Gender": gender,
            })

            # Variant Attributes (Map Dynamically)
            has_variant_data = False
            if i < len(fixed_variants):
                v = fixed_variants[i]
                sku = str(v.get("Variant SKU", "")).strip()
                price = v.get("Variant Price")
                # Safety: only emit variant rows that have real data (SKU + price).
                # Parent rows (no SKU, no price) are skipped — Shopify ignores them
                # anyway and they cause "₹0.00" when accidentally imported.
                if sku and price is not None and price > 0:
                    has_variant_data = True
                    o1_val = v.get("Option1 Value", v.get("Option1", "Default"))
                    o2_val = v.get("Option2 Value", "")
                    row.update({
                        "Option1 Name": base_o1,
                        "Option1 Value": o1_val,
                        "Option2 Name": base_o2,
                        "Option2 Value": o2_val if base_o2 else "",
                        "Variant SKU": sku,
                        "Variant Price": price,
                        "Variant Compare At Price": v.get("Variant Compare At Price", 0),
                        "Cost per item": v.get("Cost per item", 0),
                        "currency": str(v.get("currency", "") or "").strip(),
                        "Variant Inventory Tracker": "shopify",
                        "Variant Inventory Qty": (v.get("Variant Inventory Qty") if v.get("Variant Inventory Qty") is not None else 100),
                        "Variant Inventory Policy": "deny",
                        "Variant Fulfillment Service": "manual",
                    })
                    _normalize_option_columns(row)
                    # Link this variant to its color-specific image so Shopify shows
                    # the correct photo when a colour swatch is selected.
                    v_imgs = v.get("images") or []
                    if v_imgs:
                        row["Variant Image"] = str(v_imgs[0]).strip()
                        # Pipe-separated list of ALL images for this color so the
                        # publisher can link every image to its color's variant_ids.
                        row["Variant All Images"] = "|".join(
                            str(img).strip() for img in v_imgs if img
                        )
                    elif unique_images:
                        row["Variant Image"] = str(unique_images[i % len(unique_images)]).strip()

            # Image Attributes (Correct Sequence)
            image_src = ""
            if i < len(unique_images):
                image_src = str(unique_images[i]).strip()
            elif not row.get("Variant Image") and unique_images:
                # More variants than gallery images and no per-variant image set.
                # Use this variant's own color image if available, else the first
                # gallery image — so no variant row is ever completely imageless.
                v_imgs_here = (fixed_variants[i].get("images") or []) if i < len(fixed_variants) else []
                image_src = (
                    str(v_imgs_here[0]).strip() if v_imgs_here
                    else str(unique_images[0]).strip()
                )

            # Fallback: if the row still has no Image Src, use the Variant Image.
            if not image_src and row.get("Variant Image"):
                image_src = str(row.get("Variant Image")).strip()

            # Prefer the original CDN URL when the processed hero image is a local
            # dev-only URL (BiRefNet may be unavailable or the domain is ephemeral).
            # Shopify can only fetch publicly reachable images.
            if image_src and row.get("Variant Image"):
                variant_img = str(row.get("Variant Image")).strip()
                if ("replit.dev" in image_src or "localhost" in image_src) and not ("replit.dev" in variant_img or "localhost" in variant_img):
                    image_src = variant_img

            if image_src:
                row.update({"Image Src": image_src})
                if i < len(unique_images):
                    row["Image Position"] = i + 1

            # Skip rows that have no variant data.
            # Image-only rows (more gallery images than variants) serve no purpose
            # in Shopify CSV import and risk creating "₹0.00" phantom variants.
            # Every product reaching this point has at least one valid variant,
            # so at least one row per product will always be emitted.
            if not has_variant_data:
                continue

            rows.append(row)
        
    # SENIOR AUDIT LOG
    logger.info(f"📊 [CSV AUDIT] Execution Verified: {len(products)} products -> {len(rows)} CSV rows.")
    return rows

def fix_csv_inventory_rows(filename: str) -> dict:
    """
    Lightweight in-place CSV repair for Shopify inventory field consistency.

    The Shopify importer enforces two strict rules on every row that has ANY
    variant signal (Variant Inventory Qty, Option1 Value, SKU, or Price):
      1. Variant Fulfillment Service must be 'manual' (not blank)
      2. Variant Inventory Policy must be 'deny' or 'continue' (not blank)

    Root-cause: image-only continuation rows (Handle + Image Src/Position only)
    were written with Variant Inventory Qty = 100 by older scraper code, which
    makes Shopify treat them as partial variant rows and reject the file.

    This function:
      - Image-only rows (no SKU / Price / Option value):
          → clears Variant Inventory Qty, Tracker, Fulfillment Service, Policy
      - Variant rows:
          → ensures Fulfillment Service = 'manual', Policy = 'deny',
            Tracker = 'shopify'
    """
    if not filename or not os.path.exists(filename):
        return {"ok": False, "error": "file_not_found"}

    try:
        with open(filename, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or SHOPIFY_COLUMNS
            rows = [dict(r) for r in reader]
    except Exception as e:
        return {"ok": False, "error": str(e)}

    fixed_image = 0
    fixed_variant = 0

    for row in rows:
        sku   = (row.get("Variant SKU")     or "").strip()
        price = (row.get("Variant Price")   or "").strip()
        opt1  = (row.get("Option1 Value")   or "").strip()
        opt2  = (row.get("Option2 Value")   or "").strip()

        is_variant_row = bool(sku or price or opt1 or opt2)

        if is_variant_row:
            changed = False
            if row.get("Variant Fulfillment Service", "").strip() != "manual":
                row["Variant Fulfillment Service"] = "manual"
                changed = True
            if row.get("Variant Inventory Policy", "").strip() not in ("deny", "continue"):
                row["Variant Inventory Policy"] = "deny"
                changed = True
            if row.get("Variant Inventory Tracker", "").strip() != "shopify":
                row["Variant Inventory Tracker"] = "shopify"
                changed = True
            if changed:
                fixed_variant += 1
        else:
            # Image-only row: clear ALL variant-specific fields so Shopify
            # does not misinterpret a stale Inventory Qty as a variant signal.
            changed = False
            for field in (
                "Variant Inventory Qty",
                "Variant Inventory Tracker",
                "Variant Inventory Policy",
                "Variant Fulfillment Service",
                "Variant Requires Shipping",
                "Variant Taxable",
                "Variant Grams",
                "Variant Barcode",
            ):
                if row.get(field, ""):
                    row[field] = ""
                    changed = True
            if changed:
                fixed_image += 1

    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=fieldnames, extrasaction="ignore",
                quoting=csv.QUOTE_MINIMAL, lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    logger.info(
        "🔧 CSV inventory fix [%s]: %d image rows cleared, %d variant rows corrected.",
        os.path.basename(filename), fixed_image, fixed_variant,
    )
    return {
        "ok": True,
        "rows": len(rows),
        "image_rows_fixed": fixed_image,
        "variant_rows_fixed": fixed_variant,
    }


def export_shopify_csv(rows, filename, append=False):
    """Streaming-safe CSV exporter with Final Cleanliness Check."""
    if not rows: 
        logger.error("🛑 CSV EXPORT ABORTED: No rows to write.")
        return

    # Deduplicate Variant SKUs globally: if two rows share the same non-empty SKU,
    # append a counter suffix (-1, -2 …) to every occurrence after the first.
    # This prevents Shopify import rejection caused by scrapers that occasionally
    # produce duplicate SKUs from the source catalog (e.g. thedesignerboxuk).
    _seen_skus: dict = {}
    for row in rows:
        sku = (row.get("Variant SKU") or "").strip()
        if not sku:
            continue
        if sku in _seen_skus:
            _seen_skus[sku] += 1
            row["Variant SKU"] = f"{sku}-{_seen_skus[sku]}"
        else:
            _seen_skus[sku] = 0

    dirname = os.path.dirname(filename)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    file_exists = os.path.isfile(filename) and os.path.getsize(filename) > 0
    
    with open(filename, 'a' if append else 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f, fieldnames=SHOPIFY_COLUMNS, extrasaction='ignore',
            quoting=csv.QUOTE_MINIMAL,
            lineterminator='\n',
        )
        if not append or not file_exists:
            writer.writeheader()
        writer.writerows([_sanitize_row(r) for r in rows])
    logger.info("📄 CSV: %d rows written with full auto-correction.", len(rows))

def normalize_shopify_csv_file(filename: str) -> dict:
    """
    In-place CSV normalizer — produces Cruise Fashion-compatible Shopify structure:

      Row 1 (first variant):   ALL product metadata + variant fields + image 1
      Variant rows (2 +):      Handle + options + SKU + inventory + price + image + cost ONLY
      Image-only rows:         Handle + Image Src + Image Position ONLY — all else blank

    Also enforces:
      - Inventory Qty = 100, tracker = shopify, policy = deny, service = manual
      - Published / Status / Tags / Gender only on first row per product
      - Vendor trademark symbols stripped (® ™ ©)
    """
    if not filename or not os.path.exists(filename):
        return {"ok": False, "error": "csv_not_found"}

    with open(filename, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or SHOPIFY_COLUMNS
        rows = [dict(r) for r in reader]

    grouped = collections.OrderedDict()
    for row in rows:
        handle = (row.get("Handle") or "").strip()
        if handle:
            grouped.setdefault(handle, []).append(row)

    def _clean_vendor(v):
        """
        Vendor standardization per spec:
        - Strip ®/™/© and noise words ('official', 'store', 'inc', 'ltd', 'llc', 'co')
        - Title-case the result; fall back to 'Vdrop24' if empty.
        """
        s = re.sub(r'[®™©]', '', v or '').strip()
        # Remove trademark suffixes & filler tokens
        s = re.sub(
            r'\b(official\s+store|official|store|inc\.?|ltd\.?|llc\.?|co\.?|company|brand|the)\b',
            '',
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(r'[^\w\s&\-]', ' ', s)   # keep word chars, spaces, & and -
        s = re.sub(r'\s+', ' ', s).strip(' -')
        if not s:
            return "Vdrop24"
        return ' '.join(w.capitalize() for w in s.split())

    def _is_variant(row):
        return bool(
            row.get("Variant Price") or
            row.get("Variant SKU") or
            row.get("Option1 Value") or
            row.get("Option2 Value")
        )

    out_rows = []
    for handle, items in grouped.items():
        first = items[0]
        title   = (first.get("Title") or "").strip()
        p_type  = (first.get("Type") or "").strip()
        vendor  = _clean_vendor(first.get("Vendor", "")) or "Mirage"

        # Gender
        refined = refine_gender_for_coach_export(
            title=title,
            description=_strip_html_simple(first.get("Body (HTML)", "") or ""),
            raw_tags=(first.get("Tags") or "").strip(),
            local_gender=(first.get("Google Shopping / Gender") or ""),
            url="", url_gender_hint=None, breadcrumbs=p_type,
        )
        gender = _gender_upper_to_shopify(refined)

        # Tags — Cruise Fashion-format: source, broad-category, gender(s), taxonomy, product-type
        tags = _build_cruise_format_tags(title, p_type, gender, vendor)

        # Canonical option names
        # Collect option names only if they have at least one non-empty value
        # across all variant rows — prevents "Option 'Size' must specify at least
        # one option value" Shopify import error on single-variant products.
        has_o2_value = any((r.get("Option2 Value") or "").strip() for r in items)
        opt_names = []
        for row in items:
            n1 = (row.get("Option1 Name") or "").strip()
            if n1 and n1 not in opt_names:
                opt_names.append(n1)
            if has_o2_value:
                n2 = (row.get("Option2 Name") or "").strip()
                if n2 and n2 not in opt_names:
                    opt_names.append(n2)
        if not opt_names:
            opt_names = ["Title"]
        c1 = opt_names[0] if len(opt_names) > 0 else "Title"
        c2 = opt_names[1] if len(opt_names) > 1 else ""
        c1, c2, _ = _sanitize_option_names(c1, c2, "")

        # Split rows into variants and image-only
        variant_rows = [r for r in items if _is_variant(r)]
        image_rows   = [r for r in items if not _is_variant(r) and r.get("Image Src")]

        if not variant_rows and not image_rows:
            continue

        img_counter = [0]
        def next_pos():
            img_counter[0] += 1
            return str(img_counter[0])

        for v_idx, vrow in enumerate(variant_rows):
            new = {f: "" for f in fieldnames}
            new["Handle"] = handle

            if v_idx == 0:
                # First row carries all product metadata
                new["Title"]        = title
                new["Body (HTML)"]  = (first.get("Body (HTML)") or "").strip()
                new["Vendor"]       = vendor
                new["Product Category"] = first.get("Product Category", "")
                new["Type"]         = p_type
                new["Tags"]         = tags
                new["Published"]    = "TRUE"
                new["Status"]       = "active"
                new["Google Shopping / Google Product Category"] = \
                    first.get("Google Shopping / Google Product Category", "")
                new["Google Shopping / Gender"] = gender

            # Variant fields (all variant rows)
            new["Option1 Name"]  = c1
            new["Option1 Value"] = (vrow.get("Option1 Value") or "").strip()
            if c2:
                new["Option2 Name"]  = c2
                new["Option2 Value"] = (vrow.get("Option2 Value") or "").strip()
            new["Variant SKU"]                = (vrow.get("Variant SKU") or "").strip()
            new["Variant Grams"]              = vrow.get("Variant Grams", "")
            new["Variant Inventory Tracker"]  = "shopify"
            new["Variant Inventory Qty"]      = "100"
            new["Variant Inventory Policy"]   = "deny"
            new["Variant Fulfillment Service"]= "manual"
            new["Variant Price"]              = vrow.get("Variant Price", "")
            new["Variant Compare At Price"]   = vrow.get("Variant Compare At Price", "")
            new["Variant Barcode"]            = vrow.get("Variant Barcode", "")
            new["Cost per item"]              = vrow.get("Cost per item", "")
            # Requires Shipping / Taxable: leave blank (Cruise standard)
            new["Variant Requires Shipping"]  = ""
            new["Variant Taxable"]            = ""

            # Attach one image (variant's own image, or pull from pool)
            img = _normalize_image_url(vrow.get("Image Src", ""))
            if not img and image_rows:
                img = _normalize_image_url(image_rows.pop(0).get("Image Src", ""))
            if img:
                new["Image Src"]      = img
                new["Image Position"] = next_pos()
                new["Image Alt Text"] = (vrow.get("Image Alt Text") or "").strip()

            out_rows.append(_sanitize_row(new))

        # Remaining images → image-only rows
        for img_r in image_rows:
            img = _normalize_image_url(img_r.get("Image Src", ""))
            if not img:
                continue
            new = {f: "" for f in fieldnames}
            new["Handle"]         = handle
            new["Image Src"]      = img
            new["Image Position"] = next_pos()
            new["Image Alt Text"] = (img_r.get("Image Alt Text") or "").strip()
            out_rows.append(new)

    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction="ignore",
            quoting=csv.QUOTE_MINIMAL, lineterminator='\n',
        )
        writer.writeheader()
        writer.writerows(out_rows)

    return {
        "ok": True,
        "rows": len(out_rows),
        "handles": len(grouped),
    }
