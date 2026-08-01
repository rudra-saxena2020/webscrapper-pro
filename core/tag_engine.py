import os
import re
import json
import itertools
import logging
import time
import warnings
import hashlib
from dotenv import load_dotenv

# Optional Gemini Integration (new google-genai SDK)
try:
    from google import genai as _genai_mod
except ImportError:
    _genai_mod = None

load_dotenv(override=True)
logger = logging.getLogger(__name__)

# --- v11.0: GEMINI HYBRID INTELLIGENCE ENGINE ---
# Goal: Use High-Speed Local Rules + LLM Verification for Edge Cases.

# GEMINI CONFIG
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_CACHE_FILE = "scraped_files/gemini_intelligence_cache.json"
GEMINI_GENDER_VERIFY_CACHE = "scraped_files/gemini_gender_verify_cache.json"
_gemini_cache = {}
_gemini_gender_verify_cache = {}
_gemini_backoff_until = 0.0

gemini_client = None
if _genai_mod and GEMINI_KEY:
    try:
        gemini_client = _genai_mod.Client(api_key=GEMINI_KEY)
        print("[INTEL] Gemini Intelligence Engine: ACTIVE")

        if os.path.exists(GEMINI_CACHE_FILE):
            with open(GEMINI_CACHE_FILE, 'r') as f:
                _gemini_cache = json.load(f)
            print(f"[CACHE] Loaded {len(_gemini_cache)} entries from Gemini Intelligence Cache.")
        if os.path.exists(GEMINI_GENDER_VERIFY_CACHE):
            with open(GEMINI_GENDER_VERIFY_CACHE, 'r') as f:
                _gemini_gender_verify_cache = json.load(f)
            print(f"[CACHE] Loaded {len(_gemini_gender_verify_cache)} entries from Gemini gender verify cache.")
    except Exception as e:
        print(f"[ERROR] Gemini initialization failed: {e}")
        gemini_client = None
else:
    print("[WARN] Gemini Engine: DISABLED (Reason: Missing Key or Library)")

# Alias for callers that check gemini_model
gemini_model = gemini_client

# --- LOCAL AUTHORITY TABLES (v10.2 Base) ---
CATEGORIES = {
    "apparel": [
        "shirt", "polo", "hoodie", "tee", "top", "dress", "skirt", "pant", "short", "jacket",
        "coat", "sweater", "suit", "blazer", "trousers", "jeans", "cardigan", "blouse", "tank",
        "vest", "sweatshirt", "sweatpant", "jogger", "legging", "jumpsuit", "romper", "bodysuit",
        "swimsuit", "swimwear", "bikini", "kaftan", "kimono", "overalls", "dungarees", "midi",
        "maxi", "mini dress", "mini skirt", "parka", "windbreaker", "anorak", "pullover",
        "sportswear", "activewear", "bra", "shorts", "chino", "denim",
        "cami", "bralette", "skort", "capri", "panty", "panties", "underwear", "shapewear",
        "robe", "bodysuit", "sock", "thong", "brief", "boxer", "lounge", "intimates",
        "sleepwear", "nightwear", "slip", "corset", "bustier", "teddy", "catsuit",
    ],
    "footwear": ["shoe", "sneaker", "heel", "sandal", "boot", "pump", "flat", "loafer", "mule", "stilleto", "slide", "clog", "espadrille", "trainer"],
    "accessories": ["bag", "handbag", "purse", "wallet", "belt", "watch", "jewelry", "jewellery", "backpack", "crossbody", "tote", "briefcase", "charm", "sunglasses", "scarf", "hat", "cap", "gloves", "mittens"]
}

DETERMINISTIC_GENDER = {
    "WOMEN": ["dress", "skirt", "heel", "pump", "stilleto", "purse", "clutch", "handbag", "lady", "ladies", "womenswear", "blouse", "flats", "mules", "crossbody", "tote", "shoulder bag", "satchel", "hobo", "bucket bag", "ergo"],
    "MEN": ["briefcase", "brief", "messenger bag", "suit", "menswear", "trousers", "shaving", "cufflink", "tie", "gentle", "gents", "backpack"]
}

GENDER_FORBIDDEN = {
    "MEN": ["women", "lady", "ladies", "female", "girl", "her", "she ", "she's", "womens", "dress", "skirt", "heel", "pump", "stilleto", "purse", "clutch", "handbag", "feminine"],
    "WOMEN": ["men", "male", "gents", "him", "his ", "mens", "masculine", "briefcase"]
}

# ── Mirage Store Tag Taxonomy ──────────────────────────────────────────────────
# Canonical 3-level hierarchy: gender → main category → sub category.
# Every product tag set must include exactly one main category and one sub category
# from this taxonomy. Sourced from the Mirage store tag spreadsheet.

STORE_TAG_TAXONOMY = {
    "WOMEN": {
        # ── Handbags — 3-level: women / womens-handbags / <sub> ──
        "shoulder-bag":         ("womens-handbags", "womens-shoulderbags"),
        "satchel":              ("womens-handbags", "womens-shoulderbags"),
        "hobo-bag":             ("womens-handbags", "womens-shoulderbags"),
        "bucket-bag":           ("womens-handbags", "womens-shoulderbags"),
        "handbag":              ("womens-handbags", "womens-shoulderbags"),
        "bag":                  ("womens-handbags", "womens-shoulderbags"),
        "tote":                 ("womens-handbags", "womens-totebags"),
        "mini-bag":             ("womens-handbags", "womens-minibag"),
        "crossbody-bag":        ("womens-handbags", "womens-crossbodybag"),
        "clutch":               ("womens-handbags", "womens-crossbodybag"),
        "wristlet":             ("womens-handbags", "womens-crossbodybag"),
        "belt-bag":             ("womens-handbags", "womens-crossbodybag"),
        "backpack":             ("womens-handbags", "womens-backpacks"),
        "pouch":                ("womens-handbags", "womens-crossbodybag"),
        "duffle-bag":           ("womens-handbags", "womens-totebags"),
        "messenger-bag":        ("womens-handbags", "womens-shoulderbags"),
        # ── Footwear ──
        "sneaker":              ("womens-footwear", "womens-sneakers"),
        "loafer":               ("womens-footwear", "womens-loafers"),
        "flat":                 ("womens-footwear", "womens-flats"),
        "heeled-sandal":        ("womens-footwear", "womens-heels"),
        "sandal":               ("womens-footwear", "womens-heels"),
        "slide":                ("womens-footwear", "womens-flats"),
        "pump":                 ("womens-footwear", "womens-heels"),
        "boot":                 ("womens-footwear", "womens-heels"),
        "shoe":                 ("womens-footwear", "womens-sneakers"),
        "mule":                 ("womens-footwear", "womens-heels"),
        "clog":                 ("womens-footwear", "womens-heels"),
        "mary-jane":            ("womens-footwear", "womens-heels"),
        "derby":                ("womens-footwear", "womens-heels"),
        "flip-flop":            ("womens-footwear", "womens-flats"),
        # ── Accessories — 3-level: women / womens-accessories / <sub> ──
        "belt":                 ("womens-accessories", "womens-belts"),
        "watch":                ("womens-accessories", "womens-watches"),
        "wallet":               ("womens-accessories", "womens-smallaccessories"),
        "card-case":            ("womens-accessories", "womens-smallaccessories"),
        "coin-purse":           ("womens-accessories", "womens-smallaccessories"),
        "small-leather-goods":  ("womens-accessories", "womens-smallaccessories"),
        "leather-goods":        ("womens-accessories", "womens-smallaccessories"),
        "bag-charm":            ("womens-accessories", "womens-smallaccessories"),
        "bag-strap":            ("womens-accessories", "womens-smallaccessories"),
        "gloves":               ("womens-accessories", "womens-smallaccessories"),
        "home-accessory":       ("womens-accessories", "womens-smallaccessories"),
        "accessories":          ("womens-accessories", "womens-smallaccessories"),
        "jewelry":              ("womens-accessories", "womens-jewellery"),
        "fragrance":            ("womens-accessories", "womens-smallaccessories"),
        "sunglasses":           ("womens-accessories", "womens-smallaccessories"),
        "scarf":                ("womens-accessories", "womens-smallaccessories"),
        "stole":                ("womens-accessories", "womens-smallaccessories"),
        "shawl":                ("womens-accessories", "womens-smallaccessories"),
        "hat":                  ("womens-accessories", "womens-smallaccessories"),
        "charm":                ("womens-accessories", "womens-smallaccessories"),
        "keyring":              ("womens-accessories", "womens-smallaccessories"),
        "keyfob":               ("womens-accessories", "womens-smallaccessories"),
        "strap":                ("womens-accessories", "womens-smallaccessories"),
        "lanyard":              ("womens-accessories", "womens-smallaccessories"),
        # ── Apparel ──
        "top":                  ("womens-apparel", "womens-topsandsportsbra"),
        "dress":                ("womens-apparel", "womens-dresses"),
        "leggings":             ("womens-apparel", "womens-bottomwear"),
        "jacket":               ("womens-apparel", "womens-winterwear"),
        "coat":                 ("womens-apparel", "womens-winterwear"),
        "pant":                 ("womens-apparel", "womens-bottomwear"),
        "shorts":               ("womens-apparel", "womens-bottomwear"),
        "polo":                 ("womens-apparel", "womens-topsandsportsbra"),
        "shirt":                ("womens-apparel", "womens-topsandsportsbra"),
        "apparel":              ("womens-apparel", "womens-topsandsportsbra"),
        "skirt":                ("womens-apparel", "womens-bottomwear"),
        "swimwear":             ("womens-apparel", "womens-topsandsportsbra"),
    },
    "MEN": {
        # ── Bags — 3-level: men / mens-accessories / mens-bags ──
        "bag":                  ("mens-accessories", "mens-bags"),
        "backpack":             ("mens-accessories", "mens-bags"),
        "shoulder-bag":         ("mens-accessories", "mens-bags"),
        "crossbody-bag":        ("mens-accessories", "mens-bags"),
        "tote":                 ("mens-accessories", "mens-bags"),
        "messenger-bag":        ("mens-accessories", "mens-bags"),
        # ── Accessories — 3-level: men / mens-accessories / <sub> ──
        "wallet":               ("mens-accessories", "mens-wallets"),
        "card-case":            ("mens-accessories", "mens-wallets"),
        "small-leather-goods":  ("mens-accessories", "mens-wallets"),
        "leather-goods":        ("mens-accessories", "mens-wallets"),
        "coin-purse":           ("mens-accessories", "mens-wallets"),
        "belt":                 ("mens-accessories", "mens-belts"),
        "jewelry":              ("mens-accessories", "mens-jewellery"),
        "fragrance":            ("mens-accessories", "mens-jewellery"),
        "watch":                ("mens-accessories", "mens-jewellery"),
        "sunglasses":           ("mens-accessories", "mens-jewellery"),
        "scarf":                ("mens-accessories", "mens-jewellery"),
        "stole":                ("mens-accessories", "mens-jewellery"),
        "shawl":                ("mens-accessories", "mens-jewellery"),
        "hat":                  ("mens-accessories", "mens-jewellery"),
        "charm":                ("mens-accessories", "mens-wallets"),
        "keyring":              ("mens-accessories", "mens-wallets"),
        "keyfob":               ("mens-accessories", "mens-wallets"),
        "strap":                ("mens-accessories", "mens-wallets"),
        "lanyard":              ("mens-accessories", "mens-wallets"),
        "bag-charm":            ("mens-accessories", "mens-wallets"),
        "gloves":               ("mens-accessories", "mens-wallets"),
        "accessories":          ("mens-accessories", "mens-wallets"),
        # ── Footwear ──
        "sneaker":              ("mens-footwear", "mens-sneakers"),
        "loafer":               ("mens-footwear", "mens-loafers"),
        "slide":                ("mens-footwear", "mens-slides"),
        "sandal":               ("mens-footwear", "mens-slides"),
        "boot":                 ("mens-footwear", "mens-sneakers"),
        "flat":                 ("mens-footwear", "mens-loafers"),
        "shoe":                 ("mens-footwear", "mens-sneakers"),
        "clog":                 ("mens-footwear", "mens-sneakers"),
        "flip-flop":            ("mens-footwear", "mens-slides"),
        # ── Apparel ──
        "top":                  ("mens-apparel", "mens-tshirts"),
        "shirt":                ("mens-apparel", "mens-shirt"),
        "polo":                 ("mens-apparel", "mens-polo"),
        "jacket":               ("mens-apparel", "mens-winterwear"),
        "coat":                 ("mens-apparel", "mens-winterwear"),
        "pant":                 ("mens-apparel", "mens-bottomwear"),
        "apparel":              ("mens-apparel", "mens-tshirts"),
        "skirt":                ("mens-apparel", "mens-bottomwear"),
    },
    "UNISEX": {
        # ── Bags ──
        "bag":              ("womens-handbags",    ""),
        "backpack":         ("womens-backpacks", ""),
        "shoulder-bag":     ("womens-shoulderbags", ""),
        "tote":             ("womens-totebags",     ""),
        "crossbody-bag":    ("womens-crossbodybag", ""),
        "mini-bag":         ("womens-minibag",      ""),
        "belt-bag":         ("womens-crossbodybag", ""),
        "clutch":           ("womens-crossbodybag", ""),
        "wristlet":         ("womens-crossbodybag", ""),
        "pouch":            ("womens-crossbodybag", ""),
        "duffle-bag":       ("womens-totebags", ""),
        "messenger-bag":    ("womens-shoulderbags", ""),
        "wallet":           ("womens-smallaccessories",  ""),
        "card-case":        ("womens-smallaccessories",  ""),
        "belt":             ("womens-belts",             ""),
        "watch":            ("womens-watches",           ""),
        "jewelry":          ("womens-jewellery",         ""),
        "hat":              ("womens-smallaccessories",  ""),
        "scarf":            ("womens-smallaccessories",  ""),
        "sunglasses":       ("womens-smallaccessories",  ""),
        "bag-charm":        ("womens-smallaccessories",  ""),
        "bag-strap":        ("womens-smallaccessories",  ""),
        "gloves":           ("womens-smallaccessories",  ""),
        "home-accessory":   ("womens-smallaccessories",  ""),
        "accessories":      ("womens-accessories",  "womens-smallaccessories"),
        "keyring":          ("womens-smallaccessories",  ""),
        "keyfob":           ("womens-smallaccessories",  ""),
        "charm":            ("womens-smallaccessories",  ""),
        "strap":            ("womens-smallaccessories",  ""),
        "lanyard":          ("womens-smallaccessories",  ""),
        # ── Footwear ──
        "sneaker":          ("womens-footwear",    "womens-sneakers"),
        "loafer":           ("womens-footwear",    "womens-loafers"),
        "boot":             ("womens-footwear",    "womens-heels"),
        "heeled-sandal":    ("womens-footwear",    "womens-heels"),
        "sandal":           ("womens-footwear",    "womens-heels"),
        "slide":            ("womens-footwear",    "womens-flats"),
        "shoe":             ("womens-footwear",    "womens-sneakers"),
        "mule":             ("womens-footwear",    "womens-heels"),
        "clog":             ("womens-footwear",    "womens-heels"),
        "mary-jane":        ("womens-footwear",    "womens-heels"),
        "derby":            ("womens-footwear",    "womens-heels"),
        "flip-flop":        ("womens-footwear",    "womens-flats"),
        # ── Apparel ──
        "top":              ("womens-apparel",     "womens-topsandsportsbra"),
        "shirt":            ("womens-apparel",     "womens-topsandsportsbra"),
        "jacket":           ("womens-apparel",     "womens-winterwear"),
        "coat":             ("womens-apparel",     "womens-winterwear"),
        "pant":             ("womens-apparel",     "womens-bottomwear"),
        "shorts":           ("womens-apparel",     "womens-bottomwear"),
        "dress":            ("womens-apparel",     "womens-dresses"),
        "apparel":          ("womens-apparel",     "womens-topsandsportsbra"),
        "skirt":            ("womens-apparel",     "womens-bottomwear"),
        "swimwear":         ("womens-apparel",     "womens-topsandsportsbra"),
    },
}

# Fallback when sub_cat_slug isn't in the taxonomy — keyed by broad category
_STORE_TAG_FALLBACK = {
    "WOMEN": {
        "accessories": ("womens-smallaccessories", ""),
        "footwear":    ("womens-footwear",    "womens-sneakers"),
        "apparel":     ("womens-apparel",     "womens-topsandsportsbra"),
    },
    "MEN": {
        "accessories": ("mens-accessories", ""),
        "footwear":    ("mens-footwear",    "mens-sneakers"),
        "apparel":     ("mens-apparel",     "mens-tshirts"),
    },
    "UNISEX": {
        "accessories": ("womens-smallaccessories", ""),
        "footwear":    ("womens-footwear",    "womens-sneakers"),
        "apparel":     ("womens-apparel",     "womens-topsandsportsbra"),
    },
}


def map_to_store_tags(gender, broad_cat, sub_cat_slug):
    """
    Map (gender, broad_cat, sub_cat_slug) to the Mirage store's canonical
    (main_category_tag, sub_category_tag) pair.
    """
    g = (gender or "UNISEX").upper()
    if g not in STORE_TAG_TAXONOMY:
        g = "UNISEX"

    taxonomy = STORE_TAG_TAXONOMY[g]

    if sub_cat_slug and sub_cat_slug in taxonomy:
        return taxonomy[sub_cat_slug]

    fallback = _STORE_TAG_FALLBACK.get(g, {})
    if broad_cat and broad_cat in fallback:
        return fallback[broad_cat]

    return ("womens-accessories", "womens-smallaccessories")


# ── UK Size Tag Tables ─────────────────────────────────────────────────────────
# Valid store size tags (normalised — trailing periods removed).
VALID_UK_SIZES = [
    "UK 2", "UK 2.5", "UK 3", "UK 3.5", "UK 4", "UK 4.5",
    "UK 5", "UK 5.5", "UK 6", "UK 6.5", "UK 7", "UK 7.5",
    "UK 8", "UK 8.5", "UK 9", "UK 9.5", "UK 10", "UK 10.5",
    "UK 11", "UK 11.5", "UK 12", "UK 12.5",
]

# US → UK conversion for women's footwear
_US_TO_UK_WOMEN = {
    "4": "UK 2", "4.5": "UK 2.5", "5": "UK 3", "5.5": "UK 3.5",
    "6": "UK 4", "6.5": "UK 4.5", "7": "UK 5", "7.5": "UK 5.5",
    "8": "UK 6", "8.5": "UK 6.5", "9": "UK 7", "9.5": "UK 7.5",
    "10": "UK 8", "10.5": "UK 8.5", "11": "UK 9", "11.5": "UK 9.5",
    "12": "UK 10",
}

# US → UK conversion for men's footwear
_US_TO_UK_MEN = {
    "6": "UK 5.5", "6.5": "UK 6", "7": "UK 6.5", "7.5": "UK 7",
    "8": "UK 7.5", "8.5": "UK 8", "9": "UK 8.5", "9.5": "UK 9",
    "10": "UK 9.5", "10.5": "UK 10", "11": "UK 10.5", "11.5": "UK 11",
    "12": "UK 11.5", "12.5": "UK 12", "13": "UK 12.5",
}

# EU → UK conversion (unisex)
_EU_TO_UK = {
    "35": "UK 2.5", "35.5": "UK 3", "36": "UK 3.5", "36.5": "UK 4",
    "37": "UK 4", "37.5": "UK 4.5", "38": "UK 5", "38.5": "UK 5.5",
    "39": "UK 6", "39.5": "UK 6.5", "40": "UK 6.5", "40.5": "UK 7",
    "41": "UK 7.5", "42": "UK 8", "42.5": "UK 8.5", "43": "UK 9",
    "44": "UK 9.5", "44.5": "UK 10", "45": "UK 10.5", "46": "UK 11",
    "47": "UK 11.5", "48": "UK 12",
}


def us_size_to_uk_tag(us_size_str, gender="WOMEN"):
    """
    Convert a US or EU shoe size string to a 'UK X' store tag.
    Returns the UK tag string if found, else None.
    """
    s = str(us_size_str).strip().upper()
    # Already a UK size?
    if s.startswith("UK "):
        normalised = s.rstrip(".")  # remove accidental trailing period
        return normalised if normalised in VALID_UK_SIZES else None
    # EU size?
    if s.startswith("EU ") or s.startswith("EUR"):
        num = re.sub(r"[^0-9.]", "", s)
        return _EU_TO_UK.get(num)
    # Plain numeric — treat as US size
    num = re.sub(r"[^0-9.]", "", s)
    if not num:
        return None
    table = _US_TO_UK_MEN if (gender or "").upper() == "MEN" else _US_TO_UK_WOMEN
    return table.get(num)


def get_uk_size_tags(variant_sizes, gender="WOMEN"):
    """
    Given a list of size strings from product variants, return a sorted list
    of valid 'UK X' store size tags.
    """
    tags = []
    for sz in (variant_sizes or []):
        tag = us_size_to_uk_tag(sz, gender)
        if tag and tag not in tags:
            tags.append(tag)
    # Sort by numeric value
    def _uk_sort_key(t):
        try:
            return float(t.replace("UK ", ""))
        except ValueError:
            return 99
    return sorted(tags, key=_uk_sort_key)


def call_gemini_intel(product_data):
    """
    Query Gemini for deep classification when local rules are ambiguous.
    """
    global _gemini_backoff_until
    if not gemini_model: return None
    if time.time() < _gemini_backoff_until:
        return None
    
    title = product_data.get('Title', '')
    desc = product_data.get('description', '')[:500] # Limit context
    vendor = product_data.get('Vendor', '')
    
    cache_key = f"{vendor}:{title}".lower()
    if cache_key in _gemini_cache:
        return _gemini_cache[cache_key]

    prompt = f"""
    Fashion Data Specialist Task:
    Product: {title}
    Vendor: {vendor}
    Description: {desc}

    Identify:
    1. Primary Gender (MEN, WOMEN, or UNISEX)
    2. Tags (5-10 descriptive shopify tags)

    Return ONLY a JSON object: 
    {{"gender": "...", "tags": ["tag1", "tag2"]}}
    """
    
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        # Attempt to parse json from response
        clean_text = re.search(r'\{.*\}', response.text.strip(), re.DOTALL)
        if clean_text:
            data = json.loads(clean_text.group(0))
            # Save to cache
            _gemini_cache[cache_key] = data
            os.makedirs("scraped_files", exist_ok=True)
            with open(GEMINI_CACHE_FILE, 'w') as f:
                json.dump(_gemini_cache, f)
            return data
    except Exception as e:
        err_msg = str(e)
        # Avoid hammering API and flooding logs when daily quota is exceeded or key is invalid.
        if "429" in err_msg or "quota" in err_msg.lower() or "INVALID_ARGUMENT" in err_msg or "API_KEY_INVALID" in err_msg:
            _gemini_backoff_until = time.time() + 86400  # 24 h circuit breaker for fatal key errors
            logger.warning("Gemini disabled for this run (quota exceeded or invalid API key); using local tagging rules.")
            return None
        logger.error(f"Gemini API Error: {e}")
    return None


def _normalize_gender_token(g: str) -> str:
    if not g:
        return "UNISEX"
    u = str(g).strip().upper()
    if "WOMEN" in u or "LADIES" in u or "LADY" in u or "FEMALE" in u:
        return "WOMEN"
    if "UNISEX" in u:
        return "UNISEX"
    # Avoid matching "MEN" inside "WOMEN"
    if re.search(r"\bMENS\b|\bMEN\b", u) or "GENTS" in u:
        return "MEN"
    return "UNISEX"


def _breadcrumb_gender_signal(blob: str) -> str | None:
    """Return MEN, WOMEN, or None if ambiguous from PDP breadcrumbs / URL."""
    if not blob:
        return None
    b = blob.lower()
    men_hit = bool(
        re.search(r"\bmen'?s\b", b)
        or re.search(r"\bmens\b", b)
        or "/men/" in b
        or "/mens/" in b
        or re.search(r"\bfor men\b", b)
    )
    women_hit = bool(
        re.search(r"\bwomen'?s\b", b)
        or re.search(r"\bwomens\b", b)
        or "/women/" in b
        or "/womens/" in b
        or re.search(r"\bfor women\b", b)
    )
    if men_hit and not women_hit:
        return "MEN"
    if women_hit and not men_hit:
        return "WOMEN"
    return None


def verify_gender_with_gemini(
    title: str,
    description: str = "",
    raw_tags: str = "",
    local_gender: str = "",
    url: str = "",
    url_crawl_hint: str = "",
    breadcrumbs: str = "",
) -> str | None:
    """
    Cross-check scraped tags + local classifier vs product truth using Gemini.
    Returns MEN, WOMEN, UNISEX or None if unavailable / error.
    """
    global _gemini_gender_verify_cache, _gemini_backoff_until
    if not gemini_model:
        return None
    if time.time() < _gemini_backoff_until:
        return None

    title = (title or "").strip()
    desc = (description or "")[:1200]
    tags = (raw_tags or "").strip()
    local = _normalize_gender_token(local_gender)
    key_src = f"{title}|{tags}|{local}|{url}|{breadcrumbs}"
    cache_key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:32]
    if cache_key in _gemini_gender_verify_cache:
        cached = _gemini_gender_verify_cache[cache_key]
        if isinstance(cached, str) and cached in ("MEN", "WOMEN", "UNISEX"):
            return cached

    prompt = f"""You are a luxury fashion merchandiser. Fix gender for Shopify.

Rules:
- Crawl/category hints and auto-tags are OFTEN WRONG (e.g. women's tags on a men's watch).
- Trust product title, description, and site breadcrumb text over raw tag strings when they conflict.
- Watches: large men's chronographs (e.g. 39–44mm, deployment closure, ionic plated bracelet) are usually MEN unless the copy says women's/ladies.
- If truly for any gender, answer UNISEX.

Product title: {title}
Description (excerpt): {desc}
Scraped tags (may be wrong): {tags}
URL: {url}
Crawl gender hint (often wrong): {url_crawl_hint}
Breadcrumb / category text: {breadcrumbs}
Local classifier guess: {local}

Return ONLY valid JSON: {{"gender":"MEN"|"WOMEN"|"UNISEX","reason":"one short phrase"}}
"""
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = (response.text or "").strip()
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        g = str(data.get("gender", "")).strip().upper()
        if g not in ("MEN", "WOMEN", "UNISEX"):
            return None
        _gemini_gender_verify_cache[cache_key] = g
        os.makedirs("scraped_files", exist_ok=True)
        with open(GEMINI_GENDER_VERIFY_CACHE, "w") as f:
            json.dump(_gemini_gender_verify_cache, f)
        return g
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "quota" in err_msg.lower() or "INVALID_ARGUMENT" in err_msg or "API_KEY_INVALID" in err_msg:
            _gemini_backoff_until = time.time() + 86400  # 24 h circuit breaker
            logger.warning("Gemini gender verify disabled for this run (quota/invalid key); using local rules.")
        else:
            logger.error(f"Gemini gender verify error: {e}")
    return None


def refine_gender_for_coach_export(
    title: str,
    description: str = "",
    raw_tags: str = "",
    local_gender: str = "",
    url: str = "",
    url_gender_hint: str | None = None,
    breadcrumbs: str = "",
) -> str:
    """
    Final gender for Coach → Shopify: PDP breadcrumbs first, then Gemini vs raw tags + local guess.
    Returns WOMEN / MEN / UNISEX (same as detect_gender).
    """
    local = _normalize_gender_token(local_gender)
    combined = f"{title} {description} {url} {breadcrumbs}"

    sig = _breadcrumb_gender_signal(combined)
    if sig:
        local = sig

    # Deterministic safeguard for watches/chronographs where crawl hints are noisy.
    t = f"{title} {description} {raw_tags} {breadcrumbs}".lower()
    if "watch" in t or "chronograph" in t:
        women_markers = bool(re.search(r"\b(women|woman|ladies|lady|female|womens)\b", t))
        men_markers = bool(re.search(r"\b(men|man|mens|male|gents)\b", t))
        mm_vals = [int(m) for m in re.findall(r"\b(\d{2})\s*mm\b", t)]
        max_mm = max(mm_vals) if mm_vals else 0
        # In practice, many Coach watches >=39mm are men's unless explicit women's cue exists.
        if not women_markers and (men_markers or max_mm >= 39):
            local = "MEN"

    gem = verify_gender_with_gemini(
        title, description, raw_tags, local, url, str(url_gender_hint or ""), breadcrumbs
    )
    if gem:
        return gem

    return local


def classify_gender_v11(product, website_url=None, gender_hint=None):
    """Hybrid Intelligence: Local Determinism -> LLM Inference"""
    # 1. Normalized inputs
    title = str(product.get('Title') or product.get('name') or '').lower()
    url = str(website_url or product.get('url', '')).lower()
    handle = str(product.get('Handle', '')).lower()
    vendor = str(product.get('Vendor', '')).lower()

    # Tags from source Shopify (Karl etc. often tag products "Men" / "Women")
    tags_raw = product.get('tags', '')
    if isinstance(tags_raw, list):
        tags_blob = " ".join(str(t).lower() for t in tags_raw)
    else:
        tags_blob = str(tags_raw).lower()

    category_data = " ".join([
        str(product.get('Product Category', '')),
        str(product.get('Type', '')),
        str(product.get('category', '')),
        str(product.get('subCategory', '')),
        " ".join(product.get('item_category', []) if isinstance(product.get('item_category'), list) else [])
    ]).lower()

    # --- PHASE 1: DIRECT HINTS ---
    h = str(gender_hint or product.get('gender') or '').upper()
    if "WOMEN" in h or "LADIES" in h: return "WOMEN"
    if "MEN" in h or "GENTS" in h: return "MEN"

    # --- PHASE 2: SOURCE TAGS (High-confidence for Shopify-sourced feeds) ---
    # Karl Lagerfeld and other Shopify stores tag products with "Men" / "Women"
    tag_words = set(re.findall(r'\b\w+\b', tags_blob))
    men_tagged = "men" in tag_words or "mens" in tag_words or "male" in tag_words
    women_tagged = "women" in tag_words or "womens" in tag_words or "female" in tag_words or "ladies" in tag_words or "lady" in tag_words
    if men_tagged and not women_tagged:
        return "MEN"
    if women_tagged and not men_tagged:
        return "WOMEN"

    # --- PHASE 3: STRUCTURAL RULES (URL / Handle / Category) ---
    path_blob = f"{url} {handle} {category_data}"
    if any(x in path_blob for x in ['/men/', '/mens/', '-men-', '/men-']): return "MEN"
    if any(x in path_blob for x in ['/women/', '/womens/', '-women-', 'women-']): return "WOMEN"

    # User Request: If title contains: women, female, lady -> women; men, male -> men; Else -> unisex
    title_norm = f" {title} ".replace("-", " ")
    if any(x in title_norm for x in [" women ", " female ", " lady ", " girl ", " shell ", " childrens "]):
        return "WOMEN"
    if any(x in title_norm for x in [" men ", " male ", " boy ", " mens ", " gents "]):
        if " women " in title_norm: return "WOMEN"
        return "MEN"

    # --- PHASE 5: DETERMINISTIC CATEGORY ---
    for gender, words in DETERMINISTIC_GENDER.items():
        if any(re.search(r'\b' + re.escape(w) + r'\b', title) for w in words):
            return gender

    # --- PHASE 6: GEMINI INTELLIGENCE ---
    if gemini_model:
        intel = call_gemini_intel(product)
        if intel and intel.get("gender"):
            return str(intel["gender"]).upper()

    return "UNISEX"

def apply_standardized_tags(product, website_url=None, gender_hint=None):
    """
    v12.0 Tactical Framework: [category] + [use-case] + [audience] + [trend]
    """
    gender = classify_gender_v11(product, website_url, gender_hint)
    title_raw = str(product.get('Title') or '').lower()
    
    # 1. CATEGORY
    category = "accessories"
    for cat_name, keywords in CATEGORIES.items():
        if any(re.search(r'\b' + re.escape(k), title_raw) for k in keywords):
            category = cat_name
            break
            
    # 2. USE-CASE (Mapping from Category)
    use_case = "lifestyle"
    if category == "accessories": use_case = "daily-essential"
    elif category == "footwear": use_case = "comfort-fit"
    elif category == "apparel":
        _sw_kw = ("hoodie", "hoody", "sweatshirt", "jogger", "tracksuit",
                  "graphic tee", "graphic-tee", "jersey", "sporty", "athletic",
                  "bomber", "windbreaker")
        if any(k in title_raw for k in _sw_kw):
            use_case = "streetwear"
        else:
            use_case = "smart-casual"
    
    # 3. AUDIENCE & TREND
    audience = gender.lower()
    trend = "premium"
    
    # Core Framework Tags
    final_tags = {category, use_case, audience, trend, "mirage-curated"}
    
    # Add vendor if present
    vendor = str(product.get('Vendor') or '').strip().lower()
    if vendor: final_tags.add(vendor)

    # 4. PRUNING (No Cross-Gender Noise)
    # Use word-boundary matching so "men" doesn't falsely match inside "women"
    # (plain substring check would make `"men" in "women"` → True and strip the
    #  audience tag "women" from every WOMEN product).
    forbidden = []
    if gender == "MEN":   forbidden = ["women", "womens", "female", "girl", "lady"]
    elif gender == "WOMEN": forbidden = ["mens", "male", "boy", "gent"]

    clean_set = set()
    for t in final_tags:
        if not any(re.search(r'\b' + re.escape(f) + r'\b', t, re.IGNORECASE) for f in forbidden):
            clean_set.add(t)

    return ", ".join(sorted(list(clean_set)))

# Compatibility shims
def detect_gender(product, website_url=None, gender_hint=None):
    return classify_gender_v11(product, website_url, gender_hint)

def classify_gender_v9(product, website_url=None, gender_hint=None):
    return classify_gender_v11(product, website_url, gender_hint)

def detect_gender_rule(text):
    return classify_gender_v11({"Title": text})

def generate_handle(title, p_id="", *args, style_code=None, product_key=None, **kwargs):
    slug = re.sub(r'[^a-z0-9]+', '-', str(title).lower()).strip('-')
    suffix_parts = [str(a) for a in args if a]
    if not suffix_parts:
        if style_code:
            suffix_parts.append(re.sub(r'[^a-z0-9]+', '-', str(style_code).lower()).strip('-'))
        elif product_key:
            suffix_parts.append(re.sub(r'[^a-z0-9]+', '-', str(product_key).lower()).strip('-'))
        elif p_id:
            suffix_parts.append(re.sub(r'[^a-z0-9]+', '-', str(p_id).lower()).strip('-'))
    suffix = "-".join(s for s in suffix_parts if s)
    return f"{slug}-{suffix}" if suffix else slug

_TITLE_KEEP_UPPER = {"UK","USA","US","UV","LED","XL","XXL","XS","2XL","3XL","RFID","II","III","IV","VI","DJ","AI"}
_TITLE_KEEP_LOWER_MID = {"a","an","the","and","or","but","in","on","at","to","of","for","with","by","from","via"}

def _normalize_caps(title: str) -> str:
    """Convert a fully-uppercase title to smart Title Case, preserving known abbreviations."""
    words = title.split()
    result = []
    for i, w in enumerate(words):
        core = re.sub(r'[^A-Za-z0-9]', '', w).upper()
        if core in _TITLE_KEEP_UPPER:
            result.append(w.upper())
        elif i == 0:
            result.append(w.capitalize())
        elif w.lower() in _TITLE_KEEP_LOWER_MID:
            result.append(w.lower())
        else:
            result.append(w.capitalize())
    return " ".join(result)

def clean_title(title):
    t = str(title).strip()
    # Normalize fully-uppercase titles (e.g. "THE DUAL CHAIN MINI BAG" → "The Dual Chain Mini Bag")
    if t.isupper() and len(t) > 3:
        t = _normalize_caps(t)
    # Remove gender possessives first (e.g. "Women's", "Men's") then standalone gender words
    cleaned = re.sub(r"\b(men|women|mens|womens|unisex|male|female)'s?\b\s*", ' ', t, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(men|women|mens|womens|unisex|male|female)\b\s*", ' ', cleaned, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', cleaned).strip()

def generate_shopify_tags_v4_5(gender, product, website_url=None):
    return apply_standardized_tags(product, website_url, gender_hint=gender)

def sanitize_html_description(html):
    """
    Convert any HTML or plain-text description into clean, Shopify-ready HTML.

    Output rules
    ───────────────────────────────────────────────────────────────────────────
    • Bullet lists (≥2 lines starting with • or -) → <ul><li>…</li></ul>
    • Multi-paragraph text (blank-line separated)  → multiple <p>…</p> blocks
    • Single-paragraph / plain text                → single <p>…</p> block
    • Empty / whitespace-only input                → "" (caller handles fallback)

    Never emits bare text, <br> spam, or dangling paragraph tags.
    """
    if not html:
        return ""
    text = str(html)

    # 1. Preserve semantic line-breaks BEFORE stripping tags
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?p[^>]*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '\n• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?ul[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?ol[^>]*>', '\n', text, flags=re.IGNORECASE)

    # 2. Strip all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # 2b. Split intra-line bullet sequences: "item1 • item2" → separate lines
    #     Only split on • that is preceded by a non-bullet character (not at line start)
    text = re.sub(r'(?<=[^\n]) •\s+', '\n• ', text)

    # 3. Normalize whitespace
    text = re.sub(r'\r\n|\r', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)         # collapse horizontal space
    text = re.sub(r' *\n *', '\n', text)        # trim spaces around newlines
    text = re.sub(r'\n{3,}', '\n\n', text)      # max 2 consecutive newlines
    text = text.strip()
    if not text:
        return ""

    # 4. Split into individual lines (for bullet-detection + paragraph building)
    lines = [l.strip() for l in text.split('\n')]

    # 5. Detect bullet-point lists (majority of non-empty lines start with • or -)
    non_empty = [l for l in lines if l]
    bullet_lines = [l for l in non_empty
                    if l.startswith('•') or (l.startswith('-') and len(l) > 2)]
    is_bullet_list = len(bullet_lines) >= max(2, len(non_empty) * 0.5)

    if is_bullet_list:
        items = []
        current = ""
        for line in non_empty:
            if line.startswith(('•', '-')):
                if current:
                    items.append(current)
                current = line.lstrip('•- ').strip()
            else:
                current = (current + ' ' + line).strip() if current else line
        if current:
            items.append(current)
        return '<ul>' + ''.join(f'<li>{it}</li>' for it in items if it) + '</ul>'

    # 6. Build <p> paragraphs (blank lines = paragraph boundaries)
    paragraphs: list[str] = []
    current_lines: list[str] = []
    for line in lines:
        if not line:
            if current_lines:
                paragraphs.append(' '.join(current_lines))
                current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        paragraphs.append(' '.join(current_lines))

    return ''.join(f'<p>{p}</p>' for p in paragraphs if p)

def append_brand_message(desc):
    if not desc or not str(desc).strip():
        return "<p>Exclusively curated for Mirage Retail Collective.</p>"
    return f"{desc}<p><em>Exclusively curated for Mirage Retail Collective.</em></p>"


# ── Sub-category slug detection ───────────────────────────────────────────────
# Ordered most-specific → least-specific so the first match wins.
_SUBCATSLUG_RULES = [
    # Bags (specific first)
    ("crossbody-bag",   ["crossbody", "cross body", "cross-body"]),
    ("mini-bag",        ["mini bag", "minibag", "micro bag", "nano bag"]),
    ("belt-bag",        ["belt bag", "belt-bag", "fanny pack", "waist bag", "bumbag"]),
    ("wristlet",        ["wristlet"]),
    ("clutch",          ["clutch", "evening bag", "envelope bag"]),
    ("tote",            ["tote"]),
    ("backpack",        ["backpack", "rucksack"]),
    ("shoulder-bag",    ["shoulder bag", "shoulder-bag", "hobo", "satchel",
                         "bucket bag", "bucket-bag", "handbag", "top handle"]),
    ("bag",             ["bag", "pouch", "purse"]),
    # Footwear
    ("sneaker",         ["sneaker", "trainer", "running shoe", "trail shoe",
                         "court shoe", "canvas shoe", "runner"]),
    ("loafer",          ["loafer", "moccasin"]),
    ("pump",            ["pump", "stiletto", "wedge", "kitten heel"]),
    ("boot",            ["boot", "bootie", "ankle boot"]),
    ("heeled-sandal",   ["heeled sandal", "sculptural sandal", "platform sandal",
                         "wedge sandal", "stiletto sandal", "strappy heel"]),
    ("sandal",          ["sandal", "espadrille"]),
    ("slide",           ["slide", "mule", "clog", "slipper", "flip flop",
                         "flip-flop", "recovery slide"]),
    ("flat",            ["flat", "ballerina", "ballet flat"]),
    ("shoe",            ["shoe", "footwear", "hiker", "hike"]),
    # Accessories
    ("watch",           ["watch", "timepiece"]),
    ("card-case",       ["card case", "card-case", "cardholder", "card holder"]),
    ("wallet",          ["wallet", "coin purse", "zip around"]),
    ("belt",            ["belt"]),
    ("sunglasses",      ["sunglass", "sunglasses", "glasses", "eyewear", "optical"]),
    ("jewelry",         ["jewelry", "jewellery", "necklace", "bracelet",
                         "earring", "ring", "bangle", "pendant", "charm"]),

    ("scarf",           ["scarf", "stole", "shawl", "muffler"]),
    ("hat",             ["hat", "beanie", "beret", "bucket hat"]),
    ("keyring",         ["keyring", "key ring", "keyfob", "key fob", "key chain"]),
    # Apparel
    ("dress",           ["dress", "gown", "skirt", "mini dress", "maxi dress"]),
    ("jacket",          ["jacket", "blazer", "coat", "puffer", "parka", "windbreaker"]),
    ("leggings",        ["legging", "tight"]),
    ("polo",            ["polo"]),
    ("top",             ["top", "blouse", "shirt", "t-shirt", "tee ", "tank",
                         "vest", "cami", "hoodie", "sweatshirt", "sweater"]),
    ("pant",            ["pant", "trouser", "jean", "chino", "short", "jogger"]),
]


def detect_sub_cat_slug(title: str, product_type: str = "") -> str:
    """
    Detect the sub-category slug (key in STORE_TAG_TAXONOMY) from a product
    title + product type string.  Returns "" if nothing matches.
    Uses word-boundary matching so "pump" doesn't match "pumping" and
    "string" doesn't match "String Thong".
    """
    haystack = f"{title} {product_type}".lower()
    for slug, keywords in _SUBCATSLUG_RULES:
        if any(re.search(r'\b' + re.escape(kw) + r'\b', haystack) for kw in keywords):
            return slug
    return ""


# ── Generic Mirage description builder ───────────────────────────────────────
_MIRAGE_USE_CASES = {
    # Bags
    "shoulder-bag":        ("elevating everyday wardrobes, weekend outings, or polished work ensembles",
                            "those who refuse to choose between function and iconic style"),
    "tote":                ("carrying essentials to the office, weekend getaways, or city errands",
                            "those who demand a bag that moves seamlessly from desk to dinner"),
    "crossbody-bag":       ("hands-free city exploring, travel, or casual outings",
                            "those who want effortless style on the move"),
    "backpack":            ("commuting, travel, or weekend adventures",
                            "those who pair practicality with premium craftsmanship"),
    "wristlet":            ("evenings out, travel, or pared-back everyday carry",
                            "those who embrace minimalist elegance"),
    "clutch":              ("evenings out, formal occasions, or statement styling",
                            "those who believe less is always more"),
    "bag":                 ("everyday carry, travel, or weekend styling",
                            "those who value timeless craftsmanship in every detail"),
    "belt-bag":            ("hands-free outings, travel, or casual styling",
                            "those who blend utility with premium design"),
    "mini-bag":            ("evening outings, travel, or statement accessorising",
                            "those who believe the boldest statements come in the smallest packages"),
    "bucket-bag":          ("city days, weekend outings, or elevated casual looks",
                            "those who appreciate relaxed silhouettes with premium craftsmanship"),
    "hobo-bag":            ("relaxed weekend styling or everyday carry",
                            "those who pair effortless form with artisanal quality"),
    "satchel":             ("the office, weekend outings, or structured everyday styling",
                            "those who value considered design and premium materials"),
    # Wallets & small leather goods
    "wallet":              ("organising everyday essentials with effortless elegance",
                            "those who invest in craftsmanship even in the smallest details"),
    "card-case":           ("streamlined everyday carry or gifting",
                            "those who appreciate refined minimalism"),
    "small-leather-goods": ("gifting, everyday carry, or accessorising a signature look",
                            "those who value iconic craftsmanship in every piece"),
    # Footwear
    "sneaker":             ("pairing with casual looks, weekend styling, or city errands",
                            "those who bring the same high standards to everyday footwear"),
    "heeled-sandal":       ("warm-weather occasion dressing, resort styling, or elevated evening looks",
                            "those who demand sculptural elegance and artisanal quality in every step"),
    "sandal":              ("warm-weather dressing, resort styling, or elevated casual looks",
                            "those who expect artisanal quality in every step"),
    "boot":                ("cooler-season styling, city looks, or statement footwear moments",
                            "those who invest in footwear that defines the season"),
    "loafer":              ("smart-casual dressing, office styling, or elevated weekend looks",
                            "those who understand that refined shoes complete any look"),
    "pump":                ("formal occasions, power dressing, or evening events",
                            "those who make a statement from the ground up"),
    "flat":                ("everyday elegance, office dressing, or relaxed weekend looks",
                            "those who refuse to sacrifice comfort for style"),
    "mule":                ("effortless styling, warm-weather dressing, or considered casual looks",
                            "those who appreciate understated elegance"),
    "slide":               ("relaxed off-duty moments, resort dressing, or casual outings",
                            "those who bring iconic quality to every step"),
    "shoe":                ("everyday styling, smart-casual dressing, or considered footwear moments",
                            "those who invest in footwear crafted with artisanal care"),
    # Accessories
    "watch":               ("completing a signature look, gifting, or everyday timekeeping",
                            "those who value precision craftsmanship and iconic design"),
    "jewelry":             ("elevating any outfit, gifting, or adding a signature finishing touch",
                            "those who celebrate considered accessorising"),
    "sunglasses":          ("sun-soaked days, travel, or completing a polished look",
                            "those who invest in eyewear that frames their signature style"),
    "belt":                ("completing tailored looks, smart-casual dressing, or structured styling",
                            "those who believe the right belt defines the whole outfit"),
    "scarf":               ("layering, travel, or adding a finishing touch to any look",
                            "those who understand that a great scarf elevates everything"),
    "hat":                 ("completing casual looks, sun protection, or statement styling",
                            "those who believe in considered accessories for every season"),
    "keyring":             ("gifting, bag accessorising, or everyday carry",
                            "those who appreciate artisanal craftsmanship in every detail"),
    # Apparel
    "jacket":              ("layering for cooler weather, smart-casual dressing, or statement outerwear moments",
                            "those who invest in outerwear that transcends seasons"),
    "dress":               ("evening occasions, weekend dressing, or elevated relaxed styling",
                            "those who appreciate considered design in every detail"),
    "leggings":            ("active styling, athleisure looks, or relaxed everyday dressing",
                            "those who refuse to compromise between comfort and style"),
    "top":                 ("everyday styling, casual layering, or relaxed weekend looks",
                            "those who bring considered standards to every piece they wear"),
    "pant":                ("smart-casual styling, the office, or weekend looks",
                            "those who value considered tailoring for everyday life"),
    "apparel":             ("everyday styling, casual dressing, or completing a signature look",
                            "those who bring considered aesthetics to every outfit"),
    "fragrance":           ("gifting, everyday wear, or completing a signature scent wardrobe",
                            "those who consider fragrance the ultimate finishing touch"),
}

_MIRAGE_TECH_SHOWCASE = {
    "accessories": "premium leather craftsmanship and iconic hardware",
    "bags":        "premium leather construction and considered interior organisation",
    "footwear":    "premium leather upper and artisanal construction",
    "apparel":     "premium fabric quality and considered tailoring",
    "watches":     "precision movement and premium case construction",
}

_MIRAGE_CATEGORY_INTRO = {
    "bags":        "this bag embodies the brand's signature design language and artisanal spirit",
    "footwear":    "this footwear embodies the brand's commitment to precision craftsmanship and considered design",
    "accessories": "this piece embodies the brand's iconic aesthetic and uncompromising attention to detail",
    "apparel":     "this piece embodies the brand's distinctive design philosophy and premium quality standards",
    "watches":     "this timepiece embodies the brand's precision craftsmanship and considered design heritage",
}

# ── Per-brand heritage copy ───────────────────────────────────────────────────
# Each entry provides:
#   "bags" / "footwear" / "accessories" / "apparel" / "watches" — category intro
#   "_heritage" — phrase used in closing ("Crafted with the precision expected from …")
#   "_default" — fallback intro when broad_cat is not found
_BRAND_HERITAGE = {
    # ── Scraper brands ────────────────────────────────────────────────────────
    "Coach": {
        "bags":        "this bag embodies the iconic New York heritage and artisanal spirit that define Coach's legacy",
        "footwear":    "this footwear embodies the iconic New York craftsmanship and considered design that define Coach's legacy",
        "accessories": "this piece embodies the iconic New York heritage and artisanal spirit that define Coach's legacy",
        "apparel":     "this piece embodies the relaxed New York elegance and considered design that define Coach's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision craftsmanship and considered design that define Coach's accessory legacy",
        "_heritage":   "Coach's New York heritage",
        "_default":    "this piece embodies the iconic New York heritage and artisanal spirit that define Coach's legacy",
    },
    "Michael Kors": {
        "bags":        "this bag embodies the jet-set glamour and considered design that define Michael Kors' modern American luxury",
        "footwear":    "this footwear embodies the jet-set sensibility and considered craftsmanship that define Michael Kors' luxury vision",
        "accessories": "this piece embodies the jet-set glamour and considered design that define Michael Kors' modern American luxury",
        "apparel":     "this piece embodies the confident American elegance and considered design that define Michael Kors' ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision craftsmanship and jet-set elegance that define Michael Kors' accessory legacy",
        "_heritage":   "Michael Kors' New York sensibility",
        "_default":    "this piece embodies the jet-set glamour and considered design that define Michael Kors' modern American luxury",
    },
    "Karl Lagerfeld": {
        "bags":        "this bag embodies the Parisian irreverence and artisanal spirit that define Karl Lagerfeld's iconic legacy",
        "footwear":    "this footwear embodies the Parisian elegance and considered craft that define Karl Lagerfeld's design vision",
        "accessories": "this piece embodies the Parisian irreverence and artisanal spirit that define Karl Lagerfeld's iconic legacy",
        "apparel":     "this piece embodies the Parisian chic and considered design that define Karl Lagerfeld's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision and Parisian elegance that define Karl Lagerfeld's design heritage",
        "_heritage":   "Karl Lagerfeld's Parisian design legacy",
        "_default":    "this piece embodies the Parisian irreverence and artisanal spirit that define Karl Lagerfeld's iconic legacy",
    },
    "Marc Jacobs": {
        "bags":        "this bag embodies the downtown New York irreverence and eclectic design that define Marc Jacobs' creative vision",
        "footwear":    "this footwear embodies the eclectic spirit and considered craft that define Marc Jacobs' creative vision",
        "accessories": "this piece embodies the downtown New York irreverence and considered design that define Marc Jacobs' creative legacy",
        "apparel":     "this piece embodies the eclectic American spirit and considered design that define Marc Jacobs' ready-to-wear legacy",
        "watches":     "this timepiece embodies the creative precision and considered design that define Marc Jacobs' accessory legacy",
        "_heritage":   "Marc Jacobs' creative New York heritage",
        "_default":    "this piece embodies the downtown New York irreverence and eclectic design that define Marc Jacobs' creative vision",
    },
    "Tory Burch": {
        "bags":        "this bag embodies the preppy American elegance and artisanal spirit that define Tory Burch's distinctive aesthetic",
        "footwear":    "this footwear embodies the American sportswear sensibility and considered craftsmanship that define Tory Burch's legacy",
        "accessories": "this piece embodies the preppy American elegance and considered design that define Tory Burch's distinctive aesthetic",
        "apparel":     "this piece embodies the American sportswear spirit and considered design that define Tory Burch's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision craftsmanship and American elegance that define Tory Burch's accessory legacy",
        "_heritage":   "Tory Burch's American heritage",
        "_default":    "this piece embodies the preppy American elegance and artisanal spirit that define Tory Burch's distinctive aesthetic",
    },
    "Cruise Fashion": {
        "bags":        "this bag embodies the curated British luxury and considered design that define Cruise Fashion's premium offering",
        "footwear":    "this footwear embodies the curated luxury sensibility and considered craftsmanship that define Cruise Fashion's premium selection",
        "accessories": "this piece embodies the curated British luxury and considered design that define Cruise Fashion's premium offering",
        "apparel":     "this piece embodies the curated British elegance and considered design that define Cruise Fashion's premium ready-to-wear",
        "watches":     "this timepiece embodies the precision and curated luxury that define Cruise Fashion's premium offering",
        "_heritage":   "Cruise Fashion's heritage of premium curation",
        "_default":    "this piece embodies the curated British luxury and considered design that define Cruise Fashion's premium offering",
    },
    "Mytheresa": {
        "bags":        "this bag embodies the European luxury curation and discerning design that define Mytheresa's premium selection",
        "footwear":    "this footwear embodies the European luxury sensibility and considered craftsmanship curated by Mytheresa",
        "accessories": "this piece embodies the European luxury curation and considered design that define Mytheresa's premium selection",
        "apparel":     "this piece embodies the European elegance and considered design that define Mytheresa's premium ready-to-wear curation",
        "watches":     "this timepiece embodies the precision craftsmanship and European elegance curated by Mytheresa",
        "_heritage":   "Mytheresa's heritage of luxury curation",
        "_default":    "this piece embodies the European luxury curation and considered design that define Mytheresa's premium selection",
    },
    "Polène": {
        "bags":        "this bag embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
        "footwear":    "this footwear embodies the Parisian minimalism and considered craftsmanship that define Polène's design ethos",
        "accessories": "this piece embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
        "apparel":     "this piece embodies the Parisian elegance and considered design that define Polène's design ethos",
        "watches":     "this timepiece embodies the precision and Parisian minimalism that define Polène's design philosophy",
        "_heritage":   "Polène's Parisian heritage",
        "_default":    "this piece embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
    },
    "Polene": {  # ASCII fallback
        "bags":        "this bag embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
        "footwear":    "this footwear embodies the Parisian minimalism and considered craftsmanship that define Polène's design ethos",
        "accessories": "this piece embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
        "apparel":     "this piece embodies the Parisian elegance and considered design that define Polène's design ethos",
        "watches":     "this timepiece embodies the precision and Parisian minimalism that define Polène's design philosophy",
        "_heritage":   "Polène's Parisian heritage",
        "_default":    "this piece embodies the Parisian minimalism and artisanal spirit that define Polène's considered design philosophy",
    },
    "The Designer Box UK": {
        "bags":        "this bag embodies the curated designer excellence and considered design that represent the finest in contemporary fashion",
        "footwear":    "this footwear embodies the curated designer excellence and considered craft that represent the finest in contemporary fashion",
        "accessories": "this piece embodies the curated designer excellence and considered design that represent the finest in contemporary fashion",
        "apparel":     "this piece embodies the curated designer elegance and considered design that represent the finest in contemporary ready-to-wear",
        "watches":     "this timepiece embodies the precision and curated designer excellence that represent the finest in contemporary fashion",
        "_heritage":   "the designer's creative heritage",
        "_default":    "this piece embodies the curated designer excellence and considered design that represent the finest in contemporary fashion",
    },
    "HOKA": {
        "bags":        "this piece embodies the performance innovation and considered design that define HOKA's athletic legacy",
        "footwear":    "this footwear embodies the performance innovation and precision engineering that define HOKA's athletic legacy",
        "accessories": "this piece embodies the performance innovation and considered design that define HOKA's athletic legacy",
        "apparel":     "this piece embodies the performance-focused design and considered engineering that define HOKA's athletic ready-to-wear",
        "watches":     "this timepiece embodies the precision and performance ethos that define HOKA's athletic legacy",
        "_heritage":   "HOKA's performance heritage",
        "_default":    "this piece embodies the performance innovation and precision engineering that define HOKA's athletic legacy",
    },
    # ── Common luxury designer brands (Mytheresa / The Designer Box UK) ───────
    "Valentino": {
        "bags":        "this bag embodies the Roman couture elegance and artisanal spirit that define Valentino's iconic legacy",
        "footwear":    "this footwear embodies the Roman couture craftsmanship and considered design that define Valentino's legacy",
        "accessories": "this piece embodies the Roman couture elegance and artisanal spirit that define Valentino's iconic legacy",
        "apparel":     "this piece embodies the Roman couture elegance and considered design that define Valentino's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Roman craftsmanship and considered design that define Valentino's legacy",
        "_heritage":   "Valentino's Roman couture heritage",
        "_default":    "this piece embodies the Roman couture elegance and artisanal spirit that define Valentino's iconic legacy",
    },
    "Gucci": {
        "bags":        "this bag embodies the Florentine craftsmanship and eclectic spirit that define Gucci's iconic legacy",
        "footwear":    "this footwear embodies the Florentine artisanal craft and considered design that define Gucci's legacy",
        "accessories": "this piece embodies the Florentine craftsmanship and eclectic spirit that define Gucci's iconic legacy",
        "apparel":     "this piece embodies the Italian eclectic elegance and considered design that define Gucci's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Florentine precision and considered design that define Gucci's legacy",
        "_heritage":   "Gucci's Florentine heritage",
        "_default":    "this piece embodies the Florentine craftsmanship and eclectic spirit that define Gucci's iconic legacy",
    },
    "Prada": {
        "bags":        "this bag embodies the Milanese intellectual rigour and artisanal spirit that define Prada's iconic legacy",
        "footwear":    "this footwear embodies the Milanese craftsmanship and considered design that define Prada's legacy",
        "accessories": "this piece embodies the Milanese intellectual rigour and artisanal spirit that define Prada's iconic legacy",
        "apparel":     "this piece embodies the Milanese intellectual elegance and considered design that define Prada's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Milanese precision and considered design that define Prada's legacy",
        "_heritage":   "Prada's Milanese heritage",
        "_default":    "this piece embodies the Milanese intellectual rigour and artisanal spirit that define Prada's iconic legacy",
    },
    "Saint Laurent": {
        "bags":        "this bag embodies the Parisian rock chic and artisanal spirit that define Saint Laurent's iconic legacy",
        "footwear":    "this footwear embodies the Parisian rock elegance and considered craft that define Saint Laurent's legacy",
        "accessories": "this piece embodies the Parisian rock chic and artisanal spirit that define Saint Laurent's iconic legacy",
        "apparel":     "this piece embodies the Parisian rock chic and considered design that define Saint Laurent's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Parisian precision and considered design that define Saint Laurent's legacy",
        "_heritage":   "Saint Laurent's Parisian heritage",
        "_default":    "this piece embodies the Parisian rock chic and artisanal spirit that define Saint Laurent's iconic legacy",
    },
    "Balenciaga": {
        "bags":        "this bag embodies the avant-garde design vision and artisanal spirit that define Balenciaga's iconic legacy",
        "footwear":    "this footwear embodies the avant-garde silhouette mastery and considered craft that define Balenciaga's legacy",
        "accessories": "this piece embodies the avant-garde design vision and artisanal spirit that define Balenciaga's iconic legacy",
        "apparel":     "this piece embodies the avant-garde design vision and considered craft that define Balenciaga's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision and avant-garde spirit that define Balenciaga's legacy",
        "_heritage":   "Balenciaga's couture heritage",
        "_default":    "this piece embodies the avant-garde design vision and artisanal spirit that define Balenciaga's iconic legacy",
    },
    "Bottega Veneta": {
        "bags":        "this bag embodies the Venetian intrecciato craft and considered design that define Bottega Veneta's iconic legacy",
        "footwear":    "this footwear embodies the Venetian artisanal mastery and considered design that define Bottega Veneta's legacy",
        "accessories": "this piece embodies the Venetian intrecciato craft and artisanal spirit that define Bottega Veneta's iconic legacy",
        "apparel":     "this piece embodies the Venetian artisanal elegance and considered design that define Bottega Veneta's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Venetian craftsmanship and considered design that define Bottega Veneta's legacy",
        "_heritage":   "Bottega Veneta's Venetian heritage",
        "_default":    "this piece embodies the Venetian intrecciato craft and considered design that define Bottega Veneta's iconic legacy",
    },
    "Burberry": {
        "bags":        "this bag embodies the quintessential British heritage and artisanal spirit that define Burberry's iconic legacy",
        "footwear":    "this footwear embodies the quintessential British craftsmanship and considered design that define Burberry's legacy",
        "accessories": "this piece embodies the quintessential British heritage and artisanal spirit that define Burberry's iconic legacy",
        "apparel":     "this piece embodies the quintessential British elegance and considered design that define Burberry's ready-to-wear legacy",
        "watches":     "this timepiece embodies the British precision and considered design that define Burberry's legacy",
        "_heritage":   "Burberry's British heritage",
        "_default":    "this piece embodies the quintessential British heritage and artisanal spirit that define Burberry's iconic legacy",
    },
    "Versace": {
        "bags":        "this bag embodies the bold Italian glamour and Medusa spirit that define Versace's iconic legacy",
        "footwear":    "this footwear embodies the bold Italian craftsmanship and considered design that define Versace's legacy",
        "accessories": "this piece embodies the bold Italian glamour and Medusa spirit that define Versace's iconic legacy",
        "apparel":     "this piece embodies the bold Italian glamour and considered design that define Versace's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Italian precision and bold spirit that define Versace's legacy",
        "_heritage":   "Versace's Italian heritage",
        "_default":    "this piece embodies the bold Italian glamour and Medusa spirit that define Versace's iconic legacy",
    },
    "Givenchy": {
        "bags":        "this bag embodies the Parisian couture elegance and artisanal spirit that define Givenchy's iconic legacy",
        "footwear":    "this footwear embodies the Parisian couture craftsmanship and considered design that define Givenchy's legacy",
        "accessories": "this piece embodies the Parisian couture elegance and artisanal spirit that define Givenchy's iconic legacy",
        "apparel":     "this piece embodies the Parisian couture elegance and considered design that define Givenchy's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Parisian precision and considered design that define Givenchy's legacy",
        "_heritage":   "Givenchy's Parisian couture heritage",
        "_default":    "this piece embodies the Parisian couture elegance and artisanal spirit that define Givenchy's iconic legacy",
    },
    "Alexander McQueen": {
        "bags":        "this bag embodies the darkly romantic craftsmanship and artisanal spirit that define Alexander McQueen's iconic legacy",
        "footwear":    "this footwear embodies the sculptural mastery and considered craft that define Alexander McQueen's legacy",
        "accessories": "this piece embodies the darkly romantic craftsmanship and artisanal spirit that define Alexander McQueen's iconic legacy",
        "apparel":     "this piece embodies the darkly romantic elegance and considered design that define Alexander McQueen's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision and considered design that define Alexander McQueen's legacy",
        "_heritage":   "Alexander McQueen's British heritage",
        "_default":    "this piece embodies the darkly romantic craftsmanship and artisanal spirit that define Alexander McQueen's iconic legacy",
    },
    "Moncler": {
        "bags":        "this piece embodies the Alpine luxury and artisanal spirit that define Moncler's iconic legacy",
        "footwear":    "this footwear embodies the Alpine performance craft and considered design that define Moncler's legacy",
        "accessories": "this piece embodies the Alpine luxury and artisanal spirit that define Moncler's iconic legacy",
        "apparel":     "this piece embodies the Alpine luxury and considered technical design that define Moncler's outerwear legacy",
        "watches":     "this timepiece embodies the Alpine precision and considered design that define Moncler's legacy",
        "_heritage":   "Moncler's Alpine heritage",
        "_default":    "this piece embodies the Alpine luxury and artisanal spirit that define Moncler's iconic legacy",
    },
    "Off-White": {
        "bags":        "this bag embodies the streetwear-meets-luxury spirit and considered design that define Off-White's iconic legacy",
        "footwear":    "this footwear embodies the streetwear-meets-luxury craft and considered design that define Off-White's legacy",
        "accessories": "this piece embodies the streetwear-meets-luxury spirit and considered design that define Off-White's iconic legacy",
        "apparel":     "this piece embodies the streetwear-meets-luxury spirit and considered design that define Off-White's ready-to-wear legacy",
        "watches":     "this timepiece embodies the considered design and luxury spirit that define Off-White's legacy",
        "_heritage":   "Off-White's design heritage",
        "_default":    "this piece embodies the streetwear-meets-luxury spirit and considered design that define Off-White's iconic legacy",
    },
    "Balmain": {
        "bags":        "this bag embodies the Parisian power dressing and artisanal spirit that define Balmain's iconic legacy",
        "footwear":    "this footwear embodies the Parisian power craft and considered design that define Balmain's legacy",
        "accessories": "this piece embodies the Parisian power dressing and artisanal spirit that define Balmain's iconic legacy",
        "apparel":     "this piece embodies the Parisian power dressing and considered design that define Balmain's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Parisian precision and power elegance that define Balmain's legacy",
        "_heritage":   "Balmain's Parisian heritage",
        "_default":    "this piece embodies the Parisian power dressing and artisanal spirit that define Balmain's iconic legacy",
    },
    "Dsquared2": {
        "bags":        "this bag embodies the irreverent Italian-Canadian spirit and artisanal design that define Dsquared2's iconic legacy",
        "footwear":    "this footwear embodies the irreverent craft and considered design that define Dsquared2's legacy",
        "accessories": "this piece embodies the irreverent Italian-Canadian spirit and artisanal design that define Dsquared2's iconic legacy",
        "apparel":     "this piece embodies the irreverent Italian-Canadian spirit and considered design that define Dsquared2's ready-to-wear legacy",
        "watches":     "this timepiece embodies the considered design and irreverent spirit that define Dsquared2's legacy",
        "_heritage":   "Dsquared2's design heritage",
        "_default":    "this piece embodies the irreverent Italian-Canadian spirit and artisanal design that define Dsquared2's iconic legacy",
    },
    "Love Moschino": {
        "bags":        "this bag embodies the playful Italian spirit and considered design that define Love Moschino's iconic aesthetic",
        "footwear":    "this footwear embodies the playful Italian craft and considered design that define Love Moschino's aesthetic",
        "accessories": "this piece embodies the playful Italian spirit and considered design that define Love Moschino's iconic aesthetic",
        "apparel":     "this piece embodies the playful Italian elegance and considered design that define Love Moschino's ready-to-wear aesthetic",
        "watches":     "this timepiece embodies the Italian spirit and considered design that define Love Moschino's aesthetic",
        "_heritage":   "Love Moschino's Italian heritage",
        "_default":    "this piece embodies the playful Italian spirit and considered design that define Love Moschino's iconic aesthetic",
    },
    "BOSS": {
        "bags":        "this bag embodies the sharp German precision and considered design that define BOSS's contemporary legacy",
        "footwear":    "this footwear embodies the German precision craft and considered design that define BOSS's legacy",
        "accessories": "this piece embodies the sharp German precision and considered design that define BOSS's contemporary legacy",
        "apparel":     "this piece embodies the sharp German tailoring and considered design that define BOSS's ready-to-wear legacy",
        "watches":     "this timepiece embodies the German precision and considered design that define BOSS's legacy",
        "_heritage":   "BOSS's German heritage",
        "_default":    "this piece embodies the sharp German precision and considered design that define BOSS's contemporary legacy",
    },
    "Vivienne Westwood": {
        "bags":        "this bag embodies the rebellious British spirit and artisanal design that define Vivienne Westwood's iconic legacy",
        "footwear":    "this footwear embodies the rebellious British craft and considered design that define Vivienne Westwood's legacy",
        "accessories": "this piece embodies the rebellious British spirit and artisanal design that define Vivienne Westwood's iconic legacy",
        "apparel":     "this piece embodies the rebellious British spirit and considered design that define Vivienne Westwood's ready-to-wear legacy",
        "watches":     "this timepiece embodies the British rebellious precision and considered design that define Vivienne Westwood's legacy",
        "_heritage":   "Vivienne Westwood's British heritage",
        "_default":    "this piece embodies the rebellious British spirit and artisanal design that define Vivienne Westwood's iconic legacy",
    },
    "Celine": {
        "bags":        "this bag embodies the Parisian minimalist rigour and artisanal spirit that define Celine's iconic legacy",
        "footwear":    "this footwear embodies the Parisian minimalist craft and considered design that define Celine's legacy",
        "accessories": "this piece embodies the Parisian minimalist rigour and artisanal spirit that define Celine's iconic legacy",
        "apparel":     "this piece embodies the Parisian minimalist elegance and considered design that define Celine's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Parisian precision and minimalist spirit that define Celine's legacy",
        "_heritage":   "Celine's Parisian heritage",
        "_default":    "this piece embodies the Parisian minimalist rigour and artisanal spirit that define Celine's iconic legacy",
    },
    "Fendi": {
        "bags":        "this bag embodies the Roman craftsmanship and artisanal spirit that define Fendi's iconic legacy",
        "footwear":    "this footwear embodies the Roman artisanal craft and considered design that define Fendi's legacy",
        "accessories": "this piece embodies the Roman craftsmanship and artisanal spirit that define Fendi's iconic legacy",
        "apparel":     "this piece embodies the Roman elegance and considered design that define Fendi's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Roman precision and considered design that define Fendi's legacy",
        "_heritage":   "Fendi's Roman heritage",
        "_default":    "this piece embodies the Roman craftsmanship and artisanal spirit that define Fendi's iconic legacy",
    },
    "Loewe": {
        "bags":        "this bag embodies the Spanish leather mastery and artisanal spirit that define Loewe's iconic legacy",
        "footwear":    "this footwear embodies the Spanish artisanal craft and considered design that define Loewe's legacy",
        "accessories": "this piece embodies the Spanish leather mastery and artisanal spirit that define Loewe's iconic legacy",
        "apparel":     "this piece embodies the Spanish artisanal elegance and considered design that define Loewe's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Spanish precision and considered design that define Loewe's legacy",
        "_heritage":   "Loewe's Spanish heritage",
        "_default":    "this piece embodies the Spanish leather mastery and artisanal spirit that define Loewe's iconic legacy",
    },
    "Toteme": {
        "bags":        "this bag embodies the Scandinavian minimalism and considered design that define Toteme's understated legacy",
        "footwear":    "this footwear embodies the Scandinavian minimalist craft and considered design that define Toteme's legacy",
        "accessories": "this piece embodies the Scandinavian minimalism and considered design that define Toteme's understated legacy",
        "apparel":     "this piece embodies the Scandinavian minimalism and considered design that define Toteme's ready-to-wear legacy",
        "watches":     "this timepiece embodies the Scandinavian precision and minimalist spirit that define Toteme's legacy",
        "_heritage":   "Toteme's Scandinavian heritage",
        "_default":    "this piece embodies the Scandinavian minimalism and considered design that define Toteme's understated legacy",
    },
    "The Row": {
        "bags":        "this bag embodies the quiet luxury and considered craftsmanship that define The Row's iconic legacy",
        "footwear":    "this footwear embodies the quiet luxury and considered craft that define The Row's legacy",
        "accessories": "this piece embodies the quiet luxury and considered craftsmanship that define The Row's iconic legacy",
        "apparel":     "this piece embodies the quiet luxury and considered design that define The Row's ready-to-wear legacy",
        "watches":     "this timepiece embodies the precision and quiet luxury that define The Row's legacy",
        "_heritage":   "The Row's design heritage",
        "_default":    "this piece embodies the quiet luxury and considered craftsmanship that define The Row's iconic legacy",
    },
}

_STYLE_NO_RE_MIRAGE = re.compile(r'\s*Style\s+No\.?\s+\S+', re.I)


def build_mirage_description(raw_desc: str, title: str, brand: str,
                              gender: str = "WOMEN",
                              broad_cat_hint: str = "") -> str:
    """
    Build a full Mirage-template HTML description for any brand product.

    Structure:
      1. Opening hook: "Discover the iconic [Brand] [Title]..."
      2. Key Features & Characteristics heading + <ul> bullets (from raw_desc)
      3. Closing heritage/use-case paragraph
      4. append_brand_message footer

    broad_cat_hint: optional override for brands whose product titles don't
    contain category keywords (e.g. HOKA model names like "Mach 7").
    Falls back to auto-detection from title when not supplied.
    """
    brand_clean = (brand or "").strip()
    gender_lc   = (gender or "").lower().strip()
    if gender_lc in ("unisex", ""):
        gender_lc = ""

    title_str  = str(title or "").strip()
    sub_slug   = detect_sub_cat_slug(title_str)
    broad_cat  = "accessories"
    for cat_name, keywords in CATEGORIES.items():
        if any(re.search(r'\b' + re.escape(k), title_str.lower()) for k in keywords):
            broad_cat = cat_name
            break
    # If title-based detection fell back to accessories, accept an external hint
    if broad_cat == "accessories" and broad_cat_hint in CATEGORIES:
        broad_cat = broad_cat_hint

    type_label  = (sub_slug or broad_cat or "product").replace("-", " ")
    title_clean = title_str
    if title_clean.lower().startswith(brand_clean.lower() + " "):
        title_clean = title_clean[len(brand_clean) + 1:].strip()

    # ── Brand-specific heritage look-up ──────────────────────────────────────
    brand_data      = _BRAND_HERITAGE.get(brand_clean, {})
    cat_intro       = (brand_data.get(broad_cat)
                       or brand_data.get("_default")
                       or _MIRAGE_CATEGORY_INTRO.get(broad_cat,
                           "this piece embodies the brand's iconic design language and uncompromising craftsmanship"))
    heritage_phrase = brand_data.get("_heritage") or f"{brand_clean}'s heritage"

    # ── Opening hook ─────────────────────────────────────────────────────────
    _STRIP_PUNCT       = str.maketrans('', '', '.,;:!?"\'-')
    title_words_lower  = [w.translate(_STRIP_PUNCT).rstrip('s') for w in title_clean.lower().split()]
    type_last_word     = type_label.split()[-1].lower().rstrip('s')
    title_has_type     = type_last_word in title_words_lower

    if title_has_type:
        gender_str  = ""
        type_suffix = ""
    else:
        gender_str  = f" {gender_lc}'s" if gender_lc else ""
        type_suffix = f" {type_label}"

    opening = (
        f"<p>Discover the iconic {brand_clean} {title_clean}{gender_str}{type_suffix}, exclusively "
        f"curated by The Mirage. A masterpiece of artisanal craftsmanship, {cat_intro}.</p>"
    )

    # ── Feature bullets ───────────────────────────────────────────────────────
    bullets_html = ""
    raw = str(raw_desc or "").strip()
    if raw:
        text  = re.sub(r'<[^>]+>', ' ', raw)
        text  = re.sub(r'\r\n|\r', '\n', text)
        text  = re.sub(r'[ \t]+', ' ', text)
        lines = [re.sub(r'^[*\-\u2022]\s*', '', ln).strip() for ln in text.split('\n') if ln.strip()]
        lines = [_STYLE_NO_RE_MIRAGE.sub('', ln).strip() for ln in lines]
        lines = [ln for ln in lines if ln and len(ln) > 3]
        if lines:
            bullets_html = (
                "<p><strong>Key Features &amp; Characteristics:</strong></p>"
                "<ul>" + "".join(f"<li>{ln}</li>" for ln in lines) + "</ul>"
            )

    if not bullets_html:
        tech       = _MIRAGE_TECH_SHOWCASE.get(broad_cat, "premium materials and artisanal craftsmanship")
        type_title = type_label.title()
        bullets_html = (
            "<p><strong>Key Features &amp; Characteristics:</strong></p>"
            f"<ul><li>Expertly crafted {type_title} from {brand_clean}'s premium material selection</li>"
            f"<li>Considered design with meticulous attention to detail</li>"
            f"<li>Signature {tech} throughout</li>"
            f"<li>Versatile styling that transitions seamlessly from day to evening</li></ul>"
        )

    # ── Closing paragraph ─────────────────────────────────────────────────────
    use_case, audience = _MIRAGE_USE_CASES.get(
        sub_slug,
        _MIRAGE_USE_CASES.get(
            broad_cat,
            ("elevating everyday looks or special occasions",
             "those who appreciate considered craftsmanship and iconic design")
        )
    )
    tech_phrase = _MIRAGE_TECH_SHOWCASE.get(broad_cat, "premium materials and artisanal craftsmanship")
    closing = (
        f"<p>Crafted with the precision expected from {heritage_phrase}, the {title_clean} "
        f"represents more than a {type_label} — it's a statement in considered style. "
        f"The {tech_phrase} showcase {brand_clean}'s commitment to artisanal excellence without compromise. "
        f"Whether {use_case}, this {type_label} delivers the quality and character that {audience} demand. "
        f"At The Mirage, we celebrate pieces that transcend trends. This {brand_clean} creation is a testament "
        f"to purposeful design and uncompromising quality, making it an essential addition to any "
        f"discerning wardrobe.</p>"
    )

    return append_brand_message(opening + bullets_html + closing)


def build_full_tags(
    title: str,
    vendor: str,
    gender: str,
    product_type: str = "",
    url: str = "",
    extra_tags: list | None = None,
) -> str:
    """
    Build a complete, de-duped Shopify tag string for any product by combining:
      1. apply_standardized_tags  — core framework tags (category, audience, vendor…)
      2. map_to_store_tags        — Mirage store taxonomy tags (womens-handbags etc.)
      3. extra_tags               — caller-supplied additions (e.g. trail, hiking)

    Returns a comma-separated, alphabetically sorted tag string.
    """
    g = _normalize_gender_token(gender)

    # 1. Core framework tags
    tag_meta = {"Title": title, "Vendor": vendor, "Type": product_type, "url": url}
    core = apply_standardized_tags(tag_meta, website_url=url, gender_hint=g)
    tag_set = set(t.strip() for t in core.split(",") if t.strip())

    # 2. Store taxonomy tags
    sub_slug  = detect_sub_cat_slug(title, product_type)
    broad_cat = "accessories"
    search_blob = f"{title} {product_type}".lower()
    for cat_name, keywords in CATEGORIES.items():
        if any(re.search(r'\b' + re.escape(k), search_blob) for k in keywords):
            broad_cat = cat_name
            break

    main_tag, sub_tag = map_to_store_tags(g, broad_cat, sub_slug)
    if main_tag:
        tag_set.add(main_tag)
    if sub_tag:
        tag_set.add(sub_tag)

    # 3. Extra caller tags
    for t in (extra_tags or []):
        if t:
            tag_set.add(t.strip().lower())

    # Remove empty strings
    tag_set.discard("")
    return ", ".join(sorted(tag_set))
