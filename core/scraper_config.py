"""
Scraper Config — Format Ruling Values from Replit Env Vars
===========================================================
Reads and parses the 4 canonical format-ruling env vars set in Replit:

  all_sizes=          → VALID_UK_SIZES  (footwear size whitelist)
  all_Tags=           → TAG_ROWS / tag_lookup()  (Mirage store taxonomy)
  description_example= → DESC_TEMPLATE  (HTML description structure)
  pricing_algo=       → PRICING  (markup constants for INR formula)

All values have hardcoded fallbacks so scrapers run even if an env var
is temporarily unset. Pricing constants are used directly by pricing_engine.py.

Env var key names include a trailing '=' (that is literally part of the
key name as entered in Replit). We try both forms for robustness.
"""

import os
import re
import logging

logger = logging.getLogger(__name__)


# ── Env var reader (handles trailing = in key name) ───────────────────────────

def _env(key: str) -> str:
    """Try the key with and without trailing '=' — whichever is set."""
    bare = key.rstrip("=")
    return (
        os.environ.get(key)
        or os.environ.get(bare)
        or os.environ.get(key + "=")
        or ""
    )


_RAW_SIZES   = _env("all_sizes=")
_RAW_TAGS    = _env("all_Tags=")
_RAW_DESC    = _env("description_example=")
_RAW_PRICING = _env("pricing_algo=")


# ═══════════════════════════════════════════════════════════════════════════════
# FORMAT RULE 1 — SIZES
# "Always Show UK size UK 2 UK 2.5 … UK 12.5"
# Scrapers must only emit these values for footwear variants.
# Apparel scrapers (XS/S/M/L/XL) are exempt — letter sizes are kept as-is.
# ═══════════════════════════════════════════════════════════════════════════════

VALID_UK_SIZES: list[str] = re.findall(r"UK \d+(?:\.\d+)?", _RAW_SIZES) or [
    "UK 2",  "UK 2.5",  "UK 3",  "UK 3.5",
    "UK 4",  "UK 4.5",  "UK 5",  "UK 5.5",
    "UK 6",  "UK 6.5",  "UK 7",  "UK 7.5",
    "UK 8",  "UK 8.5",  "UK 9",  "UK 9.5",
    "UK 10", "UK 10.5", "UK 11", "UK 11.5",
    "UK 12", "UK 12.5",
]
VALID_UK_SIZES_SET: set[str] = set(VALID_UK_SIZES)

_APPAREL_SIZE_RE = re.compile(
    r"^(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|One Size|OS|\d{1,2}[WR]?|US \d+|EU \d+)$",
    re.IGNORECASE,
)

def is_footwear_size(size: str) -> bool:
    """Return True if the size string looks like a footwear (numeric) size."""
    s = size.strip()
    return bool(re.match(r"^UK \d|^\d+(\.\d+)?$|^US \d+(\.\d+)?$|^EU \d+$", s, re.IGNORECASE))


def validate_size(size: str, is_footwear: bool = False) -> str | None:
    """
    Validate a size value against the format ruling:
      - Footwear: must be in VALID_UK_SIZES (or convertible).
        Returns None if outside the valid range → skip that variant.
      - Apparel: always keep as-is (letter sizes are exempt from UK-size rule).
    """
    s = (size or "").strip()
    if not s:
        return "One Size"
    if not is_footwear:
        return s   # apparel: pass through unchanged
    # Footwear: check whitelist
    if s in VALID_UK_SIZES_SET:
        return s
    # Accept "UK X" pattern even if not exactly in list (rounding guard)
    if re.match(r"^UK \d+(?:\.\d+)?$", s):
        return s if s in VALID_UK_SIZES_SET else None
    return None   # non-UK footwear size → caller should skip variant


# ═══════════════════════════════════════════════════════════════════════════════
# FORMAT RULE 2 — TAGS
# Tab-separated taxonomy:  gender \t main_category \t sub_category
# tag_lookup(gender, p_type, title) → (main_tag, sub_tag)
# ═══════════════════════════════════════════════════════════════════════════════

# Parse the raw table into a list of (gender, main_cat, sub_cat) triples.
# The env var value has actual TAB characters between columns but rows may be
# space-separated rather than newline-separated (Replit env var strips newlines).
# Strategy: regex-match every occurrence of  gender<TAB>main_cat<TAB>sub_cat.
# sub_cat is a single hyphenated slug (no spaces), so [^\t ]+ terminates it.
TAG_ROWS: list[tuple[str, str, str]] = re.findall(
    r"\b(men|women|unisex)\t([^\t]+?)\t([^\t ]+)",
    _RAW_TAGS,
    re.IGNORECASE,
)

# Keyword → sub_category matching hints (title/type keywords → sub_cat substring)
_KEYWORD_HINTS: list[tuple[list[str], str]] = [
    (["sneaker", "trainer", "running shoe"],    "sneaker"),
    (["loafer", "moccasin"],                    "loafer"),
    (["slide", "slipper", "mule"],              "slide"),
    (["heel", "pump", "stiletto", "wedge"],     "heel"),
    (["flat", "ballet"],                        "flat"),
    (["legging", "tight"],                      "legging"),
    (["top", "tee", "t-shirt", "bra", "bralette", "bodysuit", "sport"],
                                                "topsandsportsbra"),
    (["co-ord", "set", "twin"],                 "co-ordset"),
    (["winter", "coat", "jacket", "puffer"],    "winterwear"),
    (["bag", "tote", "shopper"],                "totebag"),
    (["shoulder"],                              "shoulderbag"),
    (["crossbody", "cross body"],               "crossbodybag"),
    (["mini bag", "micro"],                     "minibag"),
    (["wallet", "purse", "cardholder"],         "wallet"),
    (["belt"],                                  "belt"),
    (["watch"],                                 "watch"),
    (["jewel", "earring", "ring", "necklace",
      "bracelet", "pendant"],                   "jewellery"),
    (["underwear", "brief", "thong", "pant",
      "knicker", "boxer"],                      "smallaccessories"),
    (["shapewear", "shaper"],                   "topsandsportsbra"),
    (["intimates", "lingerie"],                 "topsandsportsbra"),
]

def _broad_category(p_type: str, title: str) -> str:
    """Infer broad category from product type + title keywords."""
    blob = f"{p_type} {title}".lower()
    if any(k in blob for k in ("shoe", "sneaker", "boot", "sandal", "slide",
                               "loafer", "pump", "heel", "flat", "footwear",
                               "trainer", "mule")):
        return "footwear"
    if any(k in blob for k in ("bag", "tote", "wallet", "crossbody", "purse",
                               "clutch", "wristlet", "pouch", "backpack")):
        return "handbags" if "women" in blob.split() else "accessories"
    if any(k in blob for k in ("watch",)):
        return "accessories"
    if any(k in blob for k in ("apparel", "top", "tee", "shirt", "dress",
                               "pant", "legging", "bra", "short", "hoodie",
                               "sweat", "jacket", "coat", "clothing",
                               "underwear", "shapewear", "intimates",
                               "bralette", "bodysuit")):
        return "apparel"
    return "accessories"


def _norm_taxonomy_tag(tag: str) -> str:
    """Normalise a store-taxonomy tag from the env var to canonical format.

    Fixes historical mismatches in the all_Tags= secret:
      'Men-apparel'   → 'mens-apparel'
      'women-apparel' → 'womens-apparel'
      'men-xxx'       → 'mens-xxx'
    """
    import re as _re
    t = (tag or "").strip().lower()
    if _re.match(r'^women-', t):
        t = 'womens-' + t[6:]
    elif _re.match(r'^men-', t) and not t.startswith('mens-'):
        t = 'mens-' + t[4:]
    return t


def tag_lookup(gender: str, p_type: str, title: str = "") -> tuple[str, str]:
    """
    Return (main_tag, sub_tag) from the env-var taxonomy.

    Matching strategy:
      1. Determine broad category (footwear / apparel / handbags / accessories)
      2. Find keyword hints in title/type to pick the best sub-category row
      3. Fall back to the first row that matches broad category + gender
      4. Ultimate fallback if taxonomy is empty

    Returns empty strings if the env var was not set and TAG_ROWS is empty.
    """
    g     = (gender or "women").lower().rstrip("s")   # "women" or "men"
    blob  = f"{p_type} {title}".lower()
    broad = _broad_category(p_type, title)

    # Filter rows to this gender
    gender_rows = [(main, sub) for (gen, main, sub) in TAG_ROWS if gen == g]
    if not gender_rows:
        # Fallback if env var not loaded
        if g == "men":
            return "mens-apparel", "mens-tshirts"
        return "womens-apparel", "womens-topsandsportsbra"

    # 1. Keyword-hint match: find the best sub_cat via hint keywords
    for keywords, sub_hint in _KEYWORD_HINTS:
        if any(kw in blob for kw in keywords):
            for main, sub in gender_rows:
                if sub_hint in sub.lower():
                    return _norm_taxonomy_tag(main), sub

    # 2. Broad-category match: pick first row whose main_cat matches broad
    broad_map = {
        "footwear":    "footwear",
        "apparel":     "apparel",
        "handbags":    "handbag",
        "accessories": "accessories",
    }
    broad_key = broad_map.get(broad, "")
    for main, sub in gender_rows:
        if broad_key and broad_key in main.lower():
            return _norm_taxonomy_tag(main), sub

    # 3. First row for this gender (normalise before returning)
    main0, sub0 = gender_rows[0]
    return _norm_taxonomy_tag(main0), sub0


# ═══════════════════════════════════════════════════════════════════════════════
# FORMAT RULE 3 — DESCRIPTION TEMPLATE
# HTML structure from description_example= env var.
# Structure (matches build_mirage_description output):
#   1. Opening hook — "Discover the iconic [Brand] [Product]..."
#   2. Key Features & Characteristics — <ul> bullets from product specs
#   3. Heritage/use-case closing paragraph
#   4. Mirage footer — <em>Exclusively curated for Mirage Retail Collective.</em>
# ═══════════════════════════════════════════════════════════════════════════════

DESC_TEMPLATE: str = _RAW_DESC.strip().strip('"')

# Extract structural markers so scrapers can reference them programmatically
_OPENING_RE  = re.compile(r"<p>Discover the iconic (.+?)</p>",    re.S)
_FEATURES_RE = re.compile(r"<p><strong>Key Features.*?</ul>",     re.S)
_FOOTER_RE   = re.compile(r"<p><em>Exclusively curated.*?</em></p>", re.S)

DESC_HAS_OPENING  = bool(_OPENING_RE.search(DESC_TEMPLATE))
DESC_HAS_FEATURES = bool(_FEATURES_RE.search(DESC_TEMPLATE))
DESC_HAS_FOOTER   = bool(_FOOTER_RE.search(DESC_TEMPLATE))


# ═══════════════════════════════════════════════════════════════════════════════
# FORMAT RULE 4 — PRICING
# Parse the human-readable formula into numeric constants consumed by
# core/pricing_engine.py. Formula: ((price × (rate + OFFSET)) + FEE) × MARKUP
#
# Env var:
#   "Below 1000 usd ... x 1.25  Above 1000 usd ... x 1.22
#    Below 1000 £  ... x 1.25  Above 1000 £   ... x 1.25"
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_pricing(raw: str) -> dict:
    markups      = [float(v) for v in re.findall(r"\bx\s*([\d.]+)", raw, re.IGNORECASE)]
    offset_match = re.search(r"RATE\+(\d+)",  raw, re.IGNORECASE)
    fee_match    = re.search(r"\+(\d{3,6})\)", raw)
    return {
        "RATE_OFFSET":      int(offset_match.group(1)) if offset_match else 12,
        "FIXED_FEE":        int(fee_match.group(1))    if fee_match    else 2000,
        "MARKUP_USD_BELOW": markups[0] if len(markups) > 0 else 1.25,
        "MARKUP_USD_ABOVE": markups[1] if len(markups) > 1 else 1.22,
        # EUR/CHF markups use same defaults; pricing_engine.py applies
        # .get("MARKUP_EUR_BELOW/ABOVE", default) so these are optional.
        "MARKUP_GBP_BELOW": markups[2] if len(markups) > 2 else 1.25,
        "MARKUP_GBP_ABOVE": markups[3] if len(markups) > 3 else 1.25,
        "MARKUP_CHF_BELOW": markups[4] if len(markups) > 4 else 1.25,
        "MARKUP_CHF_ABOVE": markups[5] if len(markups) > 5 else 1.22,
    }

PRICING: dict = _parse_pricing(_RAW_PRICING)


# ── Startup log ───────────────────────────────────────────────────────────────

logger.info(
    f"[ScraperConfig] Loaded — "
    f"sizes={len(VALID_UK_SIZES)} | "
    f"tag_rows={len(TAG_ROWS)} | "
    f"pricing={PRICING} | "
    f"desc_template={'OK' if DESC_TEMPLATE else 'FALLBACK'}"
)
