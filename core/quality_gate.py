"""
Quality Gate — comprehensive pre-upload product validation for Shopify CSVs.

Checks every product across 7 categories:
  • images      — count (3+ ideal), no duplicate URLs, HTTPS only
  • variants    — at least 1 variant, no duplicate SKUs, colour-variant image parity
  • description — body_html structure (length, required markers from DESC_TEMPLATE)
  • tags        — gender tag, store-taxonomy tag (from TAG_ROWS), RudraScrapper-{id} tag
  • sizes       — size values against VALID_UK_SIZES (config) or standard letter sizes
  • pricing     — INR range, compare_at > price, vendor/brand non-empty
  • category    — broad category tag (bags/accessories/apparel/footwear/watches)

Title checks (non-empty, no illegal prefixes) are folded into the "description" category
since both concern content quality.

Public API:
  validate_csv(scraper_id, csv_path)           → full result dict (from CSV file)
  run_quality_gate(products, scraper_id)        → spec contract — validates in-memory dicts
  run_quality_gate(scraper_id, csv_path)        → operational form — alias for validate_csv
"""
import csv
import os
import re
import logging
from typing import Union

logger = logging.getLogger(__name__)

# ── Load config from scraper_config.py (with safe fallbacks) ─────────────────

try:
    from core.scraper_config import (
        VALID_UK_SIZES_SET,
        TAG_ROWS,
        DESC_HAS_OPENING,
        DESC_HAS_FEATURES,
        DESC_HAS_FOOTER,
        PRICING as _PRICING_CFG,
    )
    _VALID_UK_SIZES_SET = VALID_UK_SIZES_SET
    _TAG_ROWS = TAG_ROWS
    _DESC_HAS_OPENING  = DESC_HAS_OPENING
    _DESC_HAS_FEATURES = DESC_HAS_FEATURES
    _DESC_HAS_FOOTER   = DESC_HAS_FOOTER
    _PRICING = _PRICING_CFG
    logger.debug("[QG] Loaded config from scraper_config.py")
except Exception as _cfg_err:
    logger.warning(f"[QG] scraper_config import failed, using fallbacks: {_cfg_err}")
    _VALID_UK_SIZES_SET = set()
    _TAG_ROWS = []
    _DESC_HAS_OPENING = _DESC_HAS_FEATURES = _DESC_HAS_FOOTER = False
    _PRICING = {
        "RATE_OFFSET": 12, "FIXED_FEE": 2000,
        "MARKUP_USD_BELOW": 1.25, "MARKUP_USD_ABOVE": 1.22,
        "MARKUP_GBP_BELOW": 1.25, "MARKUP_GBP_ABOVE": 1.25,
    }

# ── Constants ────────────────────────────────────────────────────────────────

_GENDER_EXACT    = {"women", "womens", "men", "mens", "unisex"}
_GENDER_PREFIXES = ("womens-", "mens-", "men-", "women-")

# Build taxonomy tag set from TAG_ROWS when available; otherwise use hardcoded fallback
if _TAG_ROWS:
    CATEGORY_TAG_PREFIXES: set[str] = {sub.lower() for _, _, sub in _TAG_ROWS}
    # Add common main-category slugs too
    CATEGORY_TAG_PREFIXES |= {main.lower() for _, main, _ in _TAG_ROWS}
else:
    CATEGORY_TAG_PREFIXES = {
        "womens-handbags", "womens-shoulderbags", "womens-totebags",
        "womens-minibag", "womens-crossbodybag",
        "womens-footwear", "womens-sneakers", "womens-loafers", "womens-flats",
        "womens-heels", "womens-boots", "womens-mules", "womens-sandals",
        "womens-accessories", "womens-belts", "womens-watches", "womens-sunglasses",
        "womens-smallaccessories", "womens-jewellery", "womens-scarves",
        "womens-apparel", "womens-leggings", "womens-winterwear",
        "womens-co-ordsets", "womens-topsandsportsbra", "womens-dresses",
        "womens-jeans", "womens-shirts", "womens-knitwear",
        "mens-accessories", "mens-bags", "mens-wallets", "mens-belts",
        "mens-jewellery", "mens-sunglasses", "mens-scarves",
        "mens-footwear", "mens-sneakers", "mens-loafers", "mens-slides",
        "mens-boots", "mens-oxfords", "mens-sandals",
        "mens-apparel", "mens-tshirts", "mens-shirt", "mens-polo",
        "mens-winterwear", "mens-jeans", "mens-knitwear",
        "men-apparel", "men-tshirts", "men-shirt", "men-polo", "men-winterwear",
        "women-apparel",
    }

_BROAD_CATEGORIES = {"bags", "accessories", "apparel", "footwear", "watches"}

_ONE_SIZE_VALUES = {
    "one size", "onesize", "o/s", "os", "default title", "default", "title", "n/a", "free size",
}

# Pricing thresholds from config (or defaults)
PRICE_MIN_INR   = 500
PRICE_MAX_INR   = 2_500_000   # ₹25 lakh ceiling — above this is likely a formula error
PRICE_WARN_LOW  = 1_500       # suspiciously cheap for luxury
PRICE_WARN_HIGH = 1_500_000   # very high — verify formula

# Structural markers for description validation when DESC_TEMPLATE is loaded
_OPENING_RE  = re.compile(r"<p>\s*Discover the iconic", re.IGNORECASE)
_FEATURES_RE = re.compile(r"<(?:p|strong)[^>]*>Key Features", re.IGNORECASE)
_FOOTER_RE   = re.compile(r"Exclusively curated", re.IGNORECASE)

_COLOR_OPTION_KEYS = {"color", "colour", "shade", "finish"}

_CAT_NAMES = ["images", "variants", "description", "tags", "sizes", "pricing", "category", "configuration"]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _load_csv(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _group_products(rows: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Group CSV rows into one product dict per handle.
    Returns (products_list, rows_by_handle).
    """
    by_handle: dict[str, dict]       = {}
    rows_by:   dict[str, list[dict]] = {}
    order:     list[str]             = []

    for row in rows:
        handle = (row.get("Handle") or "").strip()
        title  = (row.get("Title")  or "").strip()
        if not handle:
            continue

        if handle not in by_handle:
            by_handle[handle] = {
                "handle":     handle,
                "title":      title or handle,
                "body_html":  (row.get("Body (HTML)") or "").strip(),
                "tags":       (row.get("Tags")        or "").strip(),
                "sku":        (row.get("Variant SKU") or "").strip(),
                "price":      (row.get("Variant Price") or "0").strip(),
                "compare_at": (row.get("Variant Compare At Price") or "").strip(),
                "vendor":     (row.get("Vendor") or "").strip(),
                "images":     [],
            }
            rows_by[handle] = []
            order.append(handle)

        p = by_handle[handle]
        rows_by[handle].append(row)

        sku = (row.get("Variant SKU") or "").strip()
        if not p["sku"] and sku:
            p["sku"] = sku

        price = (row.get("Variant Price") or "").strip()
        if price and price not in ("0", "0.0") and (not p["price"] or p["price"] in ("0", "0.0")):
            p["price"] = price

        vendor = (row.get("Vendor") or "").strip()
        if vendor and not p["vendor"]:
            p["vendor"] = vendor

        if title and not p["title"]:
            p["title"] = title
        if title and not p["body_html"]:
            p["body_html"] = (row.get("Body (HTML)") or "").strip()
        if title and not p["tags"]:
            p["tags"] = (row.get("Tags") or "").strip()

        # Collect de-duplicated image URLs from gallery + variant image columns
        img_srcs = []
        if row.get("Image Src"):
            img_srcs.append(row["Image Src"].strip())
        vai = (row.get("Variant All Images") or "").strip()
        if vai:
            img_srcs.extend(u.strip() for u in vai.split("|") if u.strip())
        elif row.get("Variant Image"):
            img_srcs.append(row["Variant Image"].strip())
        for img in img_srcs:
            if img and img not in p["images"]:
                p["images"].append(img)

    return [by_handle[h] for h in order], rows_by


# ── Per-category check functions ──────────────────────────────────────────────

def _check_title_and_description(title: str, body_html: str) -> dict:
    """
    Combined content quality check: title integrity + description structure.
    Title checks (non-empty, no illegal prefixes) are folded here so they
    appear in the 'description' category breakdown.
    """
    issues, warnings = [], []

    # ── Title checks ─────────────────────────────────────────────────────────
    title = (title or "").strip()
    if not title:
        issues.append("Empty title — product will show blank in Shopify")
    else:
        words = title.split()
        # Repeated-brand prefix: first two words identical (e.g. "Coach Coach Bag")
        # Exclude legitimate product/brand names that naturally repeat a word:
        #   "Bon Bon" (Jimmy Choo), "Pom Pom" (hat style), "Zsa Zsa" (Aquazzura),
        #   "Michael Michael Kors" (MICHAEL by Michael Kors diffusion line),
        #   "Lowmel Lowmel" (brand + model), "Palm Palm" (brand), "Foulard Foulard" (textile brand)
        _REPEATED_OK = frozenset({
            'bon', 'pom', 'zsa', 'cha', 'can', 'woo',   # common style-name repeats
            'michael', 'lowmel', 'palm', 'foulard',       # brand/product-name repeats
        })
        if (len(words) >= 2
                and words[0].lower() == words[1].lower()
                and words[0].lower() not in _REPEATED_OK):
            issues.append(
                f"Repeated brand prefix in title: {words[0]!r} appears twice"
            )
        if len(title) < 5:
            warnings.append(f"Title very short ({len(title)} chars): {title!r}")

    # ── Description / body_html checks ───────────────────────────────────────
    raw_text = re.sub(r"<[^>]+>", "", body_html or "").strip()
    length   = len(raw_text)

    if not raw_text:
        issues.append("Empty description — body_html has no visible text")
    elif length < 30:
        issues.append(f"Description too short ({length} chars — need 30+ meaningful characters)")
    elif length < 80:
        warnings.append(f"Description short ({length} chars — 80+ recommended)")

    # Config-driven structure check: if DESC_TEMPLATE defines required sections,
    # verify each is present in body_html.
    if body_html and (_DESC_HAS_OPENING or _DESC_HAS_FEATURES or _DESC_HAS_FOOTER):
        if _DESC_HAS_OPENING and not _OPENING_RE.search(body_html):
            warnings.append(
                "Missing opening paragraph (expected 'Discover the iconic…') "
                "— description may not match Mirage template"
            )
        if _DESC_HAS_FEATURES and not _FEATURES_RE.search(body_html):
            warnings.append("Missing 'Key Features' section from description template")
        if _DESC_HAS_FOOTER and not _FOOTER_RE.search(body_html):
            warnings.append("Missing Mirage footer ('Exclusively curated…') from description template")

    ok = not issues and length >= 80
    return {"ok": ok, "issues": issues, "warnings": warnings, "length": length}


def _check_images(images: list[str]) -> dict:
    issues, warnings = [], []
    count = len(images)

    if count == 0:
        issues.append("No images found — product will be invisible in store")
    elif count == 1:
        warnings.append("Only 1 image — 3+ recommended for conversion")
    elif count == 2:
        warnings.append("Only 2 images — 3+ recommended; CDN probe may not have run")

    # Duplicate URL detection
    seen: set[str] = set()
    for url in images:
        if url:
            if url in seen:
                warnings.append(f"Duplicate image URL (may waste variant slots): {url[:70]}")
                break
            seen.add(url)
        if url and not url.startswith("https://"):
            issues.append(f"Non-HTTPS image URL: {url[:70]}")

    ok = not issues and count >= 3
    return {"ok": ok, "issues": issues, "warnings": warnings, "count": count}


def _check_variants(variant_rows: list[dict], product_image_count: int = 0) -> dict:
    """
    Validate variant integrity:
    - At least 1 variant row
    - No duplicate SKUs
    - No all-zero inventory (every variant OOS = product should be excluded)
    - Colour-variant image parity: colour variants should have images
    """
    issues, warnings = [], []

    if not variant_rows:
        issues.append("No variant rows found — product has no purchasable variants")
        return {"ok": False, "issues": issues, "warnings": warnings, "variant_count": 0}

    # All-OOS check: every variant qty == 0 means the scraper-level OOS filter
    # failed to drop this product. The transformer should have caught it, but
    # this safety-net flags it explicitly at the quality gate.
    qty_values = []
    for row in variant_rows:
        raw_qty = (row.get("Variant Inventory Qty") or "").strip()
        if raw_qty:
            try:
                qty_values.append(int(raw_qty))
            except ValueError:
                pass
    if qty_values and all(q == 0 for q in qty_values):
        issues.append(
            "All variants have Inventory Qty=0 — product is fully OOS and should not be uploaded"
        )

    # Duplicate SKU check
    skus = [
        (row.get("Variant SKU") or "").strip()
        for row in variant_rows
        if (row.get("Variant SKU") or "").strip()
    ]
    seen_skus: set[str] = set()
    dups: set[str] = set()
    for sku in skus:
        if sku in seen_skus:
            dups.add(sku)
        seen_skus.add(sku)
    if dups:
        issues.append(f"Duplicate SKU(s) in variants: {', '.join(sorted(dups)[:3])}")

    # OOS variant check: flag rows explicitly marked as zero inventory
    oos_rows = [
        r for r in variant_rows
        if (r.get("Variant Inventory Qty") or "").strip() == "0"
    ]
    if oos_rows:
        n_oos = len(oos_rows)
        n_total = len(variant_rows)
        if n_oos == n_total:
            issues.append(
                f"All {n_total} variant rows have Inventory Qty = 0 — product is fully OOS"
            )
        else:
            issues.append(
                f"{n_oos}/{n_total} variant rows have Inventory Qty = 0 — partial OOS; "
                f"remove OOS variants before upload"
            )

    # Colour-variant image parity
    # Detect if Option1 is a colour axis
    opt1_is_color = False
    for row in variant_rows:
        opt1_name = (row.get("Option1 Name") or "").strip().lower()
        if opt1_name:
            opt1_is_color = any(k in opt1_name for k in _COLOR_OPTION_KEYS)
            break

    if opt1_is_color:
        # Colour-variant image parity — check per unique colour, NOT per row.
        #
        # Shopify CSV format stores the image on the FIRST size row for each
        # colour; subsequent size rows for the same colour intentionally have
        # no Variant Image.  Checking every row would always flag multi-size
        # colour products as broken (e.g. 44 colours × 2 sizes → 41/85 "missing").
        # Instead we check: does each unique colour have at least one row with
        # a Variant Image (or Variant All Images)?
        colours_with_img: set[str] = set()
        colours_all: set[str] = set()
        for row in variant_rows:
            col = (row.get("Option1 Value") or "").strip()
            if col:
                colours_all.add(col)
                if row.get("Variant Image") or row.get("Variant All Images") or row.get("Image Src"):
                    colours_with_img.add(col)
        colours_no_img = colours_all - colours_with_img
        if colours_no_img and colours_all:
            pct_missing = len(colours_no_img) / len(colours_all)
            if pct_missing > 0.3:
                issues.append(
                    f"{len(colours_no_img)}/{len(colours_all)} colour variants have no images "
                    f"— {round(pct_missing * 100)}% missing variant images"
                )
            elif colours_no_img:
                warnings.append(
                    f"{len(colours_no_img)}/{len(colours_all)} colour variants missing images"
                )

        # Image count parity: unique variant images vs product-level source images.
        # Accept Variant All Images, Variant Image, or Image Src — scrapers differ
        # in which column they populate.
        variant_img_set: set[str] = set()
        for row in variant_rows:
            vai = (row.get("Variant All Images") or "").strip()
            if vai:
                variant_img_set.update(u.strip() for u in vai.split("|") if u.strip())
            elif row.get("Variant Image"):
                variant_img_set.add(row["Variant Image"].strip())
            elif row.get("Image Src"):
                variant_img_set.add(row["Image Src"].strip())
        if product_image_count > 0 and len(variant_img_set) < product_image_count * 0.5:
            warnings.append(
                f"Variant images ({len(variant_img_set)}) significantly fewer than "
                f"source images ({product_image_count}) — some colours may show wrong photo"
            )

    ok = not issues
    return {"ok": ok, "issues": issues, "warnings": warnings, "variant_count": len(variant_rows)}


def _check_tags(tags: str, scraper_id: str) -> dict:
    """
    Tag compliance check:
    - Gender tag present (women/mens/unisex)
    - Store-taxonomy category tag (from TAG_ROWS config or fallback set)
    - RudraScrapper-{scraper_id} safety tag present
    """
    issues, warnings = [], []
    tag_list = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    tag_set  = set(tag_list)

    # Gender tag
    gender_found = any(
        t in _GENDER_EXACT or any(t.startswith(p) for p in _GENDER_PREFIXES)
        for t in tag_set
    )
    if not gender_found:
        issues.append("Missing gender tag — need one of: women, mens, unisex")

    # Store-taxonomy category tag (from config TAG_ROWS or fallback)
    cat_found = any(t in CATEGORY_TAG_PREFIXES for t in tag_set)
    if not cat_found:
        warnings.append("No store-taxonomy category tag found (e.g. womens-handbags, mens-footwear)")

    # RudraScrapper safety tag (lowercase).
    # Scrapers write the tag with the scraper_id verbatim (underscores preserved),
    # e.g. "RudraScrapper-cruise_fashion" → lowercased "rudrascrapper-cruise_fashion".
    # Do NOT convert underscores to hyphens here — that mismatches what's in the CSV.
    expected_tag = f"rudrascrapper-{scraper_id.lower().replace(' ', '_')}"
    if expected_tag not in tag_set:
        issues.append(
            f"Missing RudraScrapper tag: {expected_tag} — Shopify bulk-delete safety will not work"
        )

    ok = gender_found and cat_found and (expected_tag in tag_set)
    return {"ok": ok, "issues": issues, "warnings": warnings}


def _check_sizes(variant_rows: list[dict]) -> dict:
    """
    Size value validation. Uses VALID_UK_SIZES_SET from scraper_config for footwear,
    letter-size regex for apparel. Falls back to heuristics when config is unavailable.
    """
    issues, warnings = [], []

    # Detect colour axis
    opt1_is_color = False
    for row in variant_rows:
        opt1_name = (row.get("Option1 Name") or "").strip().lower()
        if opt1_name:
            opt1_is_color = any(k in opt1_name for k in _COLOR_OPTION_KEYS)
            break

    # Detect footwear sizes
    _footwear_re = re.compile(r"^UK\s?\d|^\d+(\.\d+)?$|^US\s?\d+|^EU\s?\d+$", re.IGNORECASE)

    size_values: set[str] = set()
    for row in variant_rows:
        for opt_col in ("Option1 Value", "Option2 Value"):
            if opt_col == "Option1 Value" and opt1_is_color:
                continue
            val = (row.get(opt_col) or "").strip()
            if val:
                size_values.add(val)

    normalized = {v.lower() for v in size_values}

    if not size_values:
        if opt1_is_color:
            return {"ok": True, "issues": issues, "warnings": warnings, "size_count": 0}
        issues.append("No size/variant values found — all Option columns are empty")
    elif normalized <= _ONE_SIZE_VALUES:
        warnings.append(
            "Only 'One Size' / 'Default Title' variant — acceptable for bags & accessories"
        )
    else:
        # Check each size value for format compliance
        is_footwear = any(_footwear_re.match(v) for v in size_values
                          if v.lower() not in _ONE_SIZE_VALUES)
        for v in size_values:
            lower = v.lower()
            if lower in _ONE_SIZE_VALUES:
                continue

            if is_footwear and _VALID_UK_SIZES_SET:
                # Use config whitelist for footwear
                if not _footwear_re.match(v):
                    warnings.append(f"Unusual footwear size format: {v!r} — expected 'UK X'")
                    break
            else:
                is_letter_size = bool(re.match(
                    r'^(XXS|XS|S|M|L|XL|XXL|[0-5]X[LP]?|[2-5]XL)$', v, re.IGNORECASE,
                ))
                is_band_cup    = bool(re.match(r'^\d{2}\s*/\s*[A-J]{1,2}$', v))
                is_single_cup  = bool(re.match(r'^[A-J]{1,3}$', v))
                is_combined    = bool(re.match(r'^[0-5]X/[0-5]X$', v, re.IGNORECASE))
                is_numeric_ext = bool(re.match(r'^\d+([./]\d+)?[+]?(\s*\(.*\))?$', v))
                is_uk_size     = bool(re.match(r'^UK\s?\d', v, re.IGNORECASE))
                is_numeric     = bool(re.match(r'^\d+(\.\d+)?$', v))
                is_eu_size     = bool(re.match(r'^\d{2,3}(EU)?$', v, re.IGNORECASE))
                if not any([is_letter_size, is_band_cup, is_single_cup, is_combined,
                            is_numeric_ext, is_uk_size, is_numeric, is_eu_size]):
                    warnings.append(f"Unusual size format: {v!r} — expected UK X, S/M/L or numeric")
                    break

    ok = not issues and not (normalized <= _ONE_SIZE_VALUES and not normalized)
    return {"ok": ok, "issues": issues, "warnings": warnings, "size_count": len(size_values)}


def _check_pricing(price_str: str, compare_at_str: str) -> dict:
    issues, warnings = [], []

    try:
        price = float(price_str or "0")
    except ValueError:
        issues.append(f"Non-numeric price value: {price_str!r}")
        return {"ok": False, "issues": issues, "warnings": warnings}

    if price <= 0:
        issues.append(f"Price ≤ 0 (got {price_str!r}) — check scraper pricing engine")
    elif price < PRICE_MIN_INR:
        issues.append(
            f"Price ₹{price:,.0f} below minimum ₹{PRICE_MIN_INR:,} — possible conversion error"
        )
    elif price > PRICE_MAX_INR:
        issues.append(f"Price ₹{price:,.0f} exceeds ₹{PRICE_MAX_INR:,} ceiling — likely formula error")
    elif price < PRICE_WARN_LOW:
        warnings.append(f"Price ₹{price:,.0f} unusually low for luxury goods")
    elif price > PRICE_WARN_HIGH:
        warnings.append(f"Price ₹{price:,.0f} very high — verify formula")

    if compare_at_str:
        try:
            cap = float(compare_at_str)
            if 0 < cap <= price:
                warnings.append(
                    f"Compare-at ₹{cap:,.0f} ≤ sale price ₹{price:,.0f} — no discount shown"
                )
        except ValueError:
            warnings.append(f"Non-numeric compare-at value: {compare_at_str!r}")
    elif price > 0:
        warnings.append("Compare-at price not set — no strikethrough price on Shopify")

    ok = not issues and price > PRICE_WARN_LOW
    return {"ok": ok, "issues": issues, "warnings": warnings, "price": price}


def _check_category(tags: str) -> dict:
    """Check that a recognised broad category tag is present."""
    issues, warnings = [], []
    tag_set = {t.strip().lower() for t in (tags or "").split(",") if t.strip()}
    found   = tag_set & _BROAD_CATEGORIES
    if not found:
        issues.append(
            f"No broad category tag — expected one of: {', '.join(sorted(_BROAD_CATEGORIES))}"
        )
    ok = bool(found)
    return {"ok": ok, "issues": issues, "warnings": warnings, "categories": list(found)}


def _check_configuration(scraper_id: str) -> dict:
    """
    Global configuration gate — checks environment, scraper config, and data availability.
    Issues (critical): block pass_rate to 0 in _build_summary.
    Warnings: noted but do not fail the gate.
    """
    issues, warnings = [], []

    # 1. Shopify credentials
    has_test = bool(
        (os.getenv("TEST_SHOPIFY_STORE_URL") or os.getenv("SHOPIFY_STORE_URL", "")).strip()
        and (os.getenv("TEST_SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_ACCESS_TOKEN", "")).strip()
    )
    has_main = bool(
        os.getenv("MAIN_SHOPIFY_STORE_URL", "").strip()
        and os.getenv("MAIN_SHOPIFY_ACCESS_TOKEN", "").strip()
    )
    if not has_test and not has_main:
        issues.append("No Shopify credentials configured — SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set")
    elif not has_main:
        warnings.append("MAIN store credentials not set — promote-to-MAIN will be unavailable")

    # 2. Scraper-specific CSV exists
    csv_candidates = [
        os.path.join("scraped_files", f"{scraper_id}_latest.csv"),
        os.path.join("exports", f"{scraper_id}_shopify.csv"),
    ]
    if scraper_id and not any(os.path.exists(p) for p in csv_candidates):
        warnings.append(f"No CSV found for '{scraper_id}' — run the scraper first to generate output")

    # 3. Tag taxonomy loaded
    if not _TAG_ROWS:
        issues.append("Tag taxonomy (TAG_ROWS) not loaded — scraper_config.py may be missing or broken")

    # 4. Size list loaded
    if not _VALID_UK_SIZES_SET:
        warnings.append("UK size whitelist (VALID_UK_SIZES_SET) not loaded — size validation uses regex fallback")

    # 5. Pricing rules present
    required_pricing_keys = ("RATE_OFFSET", "FIXED_FEE", "MARKUP_USD_BELOW", "MARKUP_USD_ABOVE")
    missing_keys = [k for k in required_pricing_keys if k not in _PRICING]
    if missing_keys:
        issues.append(f"Pricing config incomplete — missing keys: {', '.join(missing_keys)}")
    else:
        rate = _PRICING.get("RATE_OFFSET", 0)
        fee  = _PRICING.get("FIXED_FEE", 0)
        if rate <= 0 or fee <= 0:
            warnings.append(f"Pricing values look suspicious: RATE_OFFSET={rate}, FIXED_FEE={fee}")

    ok = not issues
    return {"ok": ok, "issues": issues, "warnings": warnings}


# ── Master product checker ────────────────────────────────────────────────────

def _check_product(p: dict, variant_rows: list[dict], scraper_id: str = "") -> dict:
    cats: dict[str, dict] = {
        "images":        _check_images(p["images"]),
        "variants":      _check_variants(variant_rows, product_image_count=len(p["images"])),
        "description":   _check_title_and_description(p["title"], p["body_html"]),
        "tags":          _check_tags(p["tags"], scraper_id),
        "sizes":         _check_sizes(variant_rows),
        "pricing":       _check_pricing(p["price"], p["compare_at"]),
        "category":      _check_category(p["tags"]),
        "configuration": {"ok": True, "issues": [], "warnings": []},
    }

    # Vendor / brand non-empty — folded into pricing category
    if not (p.get("vendor") or "").strip():
        cats["pricing"].setdefault("warnings", []).append(
            "Empty Vendor/Brand field — product will have no brand in Shopify"
        )
        cats["pricing"]["ok"] = False

    # Missing SKU — folded into variants category
    if not (p.get("sku") or "").strip():
        cats["variants"].setdefault("issues", []).append(
            "Missing SKU — Shopify update/dedup will not work"
        )
        cats["variants"]["ok"] = False

    all_issues   = [f"[{cat}] {m}" for cat, c in cats.items() for m in c.get("issues", [])]
    all_warnings = [f"[{cat}] {m}" for cat, c in cats.items() for m in c.get("warnings", [])]

    severity = "error" if all_issues else ("warning" if all_warnings else "ok")

    return {
        "title":    p["title"][:100],
        "handle":   p["handle"][:60],
        "sku":      p.get("sku") or "(no SKU)",
        "price":    p.get("price", ""),
        "vendor":   p.get("vendor", ""),
        "severity": severity,
        "issues":   all_issues,
        "warnings": all_warnings,
        "checks": {
            k: {
                "ok":       v.get("ok", True),
                "issues":   v.get("issues", []),
                "warnings": v.get("warnings", []),
            }
            for k, v in cats.items()
        },
    }


# ── Per-category aggregate helper ─────────────────────────────────────────────

def _aggregate(results: list[dict]) -> dict:
    per_cat: dict[str, dict] = {}
    for cat in _CAT_NAMES:
        cat_ok = cat_warn = cat_err = 0
        for r in results:
            c = r["checks"].get(cat, {})
            if c.get("issues"):
                cat_err += 1
            elif c.get("warnings"):
                cat_warn += 1
            else:
                cat_ok += 1
        per_cat[cat] = {"ok": cat_ok, "warnings": cat_warn, "errors": cat_err}
    return per_cat


def _build_summary(scraper_id: str, results: list[dict], source_hint: str = "") -> dict:
    n_errors   = sum(1 for r in results if r["severity"] == "error")
    n_warnings = sum(1 for r in results if r["severity"] == "warning")
    n_ok       = sum(1 for r in results if r["severity"] == "ok")
    total      = len(results)
    # data_pass_rate: reflects ACTUAL product data quality (ignores config/credential issues)
    data_pass_rate = round((n_ok + n_warnings) / total * 100, 1) if total else 0
    pass_rate      = data_pass_rate
    ready          = n_errors == 0

    config_check = _check_configuration(scraper_id)
    per_cat = _aggregate(results)

    config_blocked = False
    # Inject configuration as a summary-level category entry so UI can display it
    if config_check["issues"]:
        # Critical config failure — upload is blocked, but DATA quality pass_rate is preserved
        per_cat["configuration"] = {"ok": 0, "warnings": 0, "errors": total or 1}
        # pass_rate = 0.0 only for MAIN-store gate compat; data_pass_rate keeps real value
        pass_rate = 0.0
        ready = False
        config_blocked = True
    elif config_check["warnings"]:
        per_cat["configuration"] = {"ok": 0, "warnings": total or 1, "errors": 0}
    else:
        per_cat["configuration"] = {"ok": total or 1, "warnings": 0, "errors": 0}

    return {
        "scraper_id":           scraper_id,
        "source":               source_hint,
        "total":                total,
        "ok":                   n_ok,
        "warnings":             n_warnings,
        "errors":               n_errors,
        "pass_rate":            pass_rate,
        "data_pass_rate":       data_pass_rate,
        "config_blocked":       config_blocked,
        "ready_to_upload":      ready,
        "products":             results,
        "per_category_summary": per_cat,
        "config_check":         config_check,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def validate_csv(scraper_id: str, csv_path: str) -> dict:
    """
    Validate all products in a Shopify-format CSV.
    Returns a dict with summary counts, per-category aggregates, and per-product results.
    """
    _empty = {
        "scraper_id": scraper_id, "source": csv_path,
        "total": 0, "ok": 0, "warnings": 0, "errors": 0,
        "pass_rate": 0, "ready_to_upload": False,
        "products": [], "per_category_summary": {},
    }
    if not os.path.exists(csv_path):
        return {**_empty, "error": f"CSV not found: {csv_path}"}

    rows = _load_csv(csv_path)

    # Duplicate handle detection must happen on raw rows BEFORE _group_products
    # deduplicates them.  A duplicate handle means two unrelated products share the
    # same handle — Shopify will silently overwrite one with the other.
    _handle_first_title: dict[str, str] = {}
    _dup_handles: list[str] = []
    for _r in rows:
        _h = (_r.get("Handle") or "").strip()
        _t = (_r.get("Title") or "").strip()
        if not _h:
            continue
        if _h in _handle_first_title:
            # Only flag when the Title on the duplicate row differs (i.e. it's a
            # genuinely different product, not just extra variant rows for the same
            # product which legitimately repeat the handle).
            if _t and _t != _handle_first_title[_h] and _h not in _dup_handles:
                _dup_handles.append(_h)
        else:
            _handle_first_title[_h] = _t

    products, rows_by = _group_products(rows)

    if not products:
        return {**_empty, "error": "CSV has no products (no rows with a Handle)"}

    # Duplicate handle detection on raw rows — must run BEFORE _group_products
    # deduplicates them.  Only flag when the Title differs (variant rows for the
    # same product legitimately repeat the Handle with the same Title).
    _handle_first_title: dict[str, str] = {}
    _dup_handles: list[str] = []
    for _r in rows:
        _h = (_r.get("Handle") or "").strip()
        _t = (_r.get("Title") or "").strip()
        if not _h:
            continue
        if _h in _handle_first_title:
            if _t and _t != _handle_first_title[_h] and _h not in _dup_handles:
                _dup_handles.append(_h)
        else:
            _handle_first_title[_h] = _t

    results = [_check_product(p, rows_by.get(p["handle"], []), scraper_id) for p in products]
    summary = _build_summary(scraper_id, results, source_hint=csv_path)

    if _dup_handles:
        dup_msg = (
            f"Duplicate handles in CSV ({len(_dup_handles)}): "
            + ", ".join(_dup_handles[:5])
            + (" …" if len(_dup_handles) > 5 else "")
            + " — Shopify will overwrite one product with another"
        )
        summary["errors"] = (summary.get("errors") or 0) + 1
        summary["ready_to_upload"] = False
        summary.setdefault("csv_errors", []).append(dup_msg)
        summary["duplicate_handles"] = _dup_handles

    return summary


def _validate_product_dicts(products: list[dict], scraper_id: str) -> dict:
    """
    Validate a list of in-memory product dicts (spec contract form).
    Each dict should have: title, body_html, tags, sku, price, compare_at,
    vendor, images (list[str]), variants (list[dict] with Variant SKU, Option1 Value, etc.)
    """
    results = []
    for p in products:
        # Normalise field names — accept both Shopify-CSV style and pythonic style
        normalised = {
            "handle":     (p.get("handle") or p.get("Handle") or "").strip(),
            "title":      (p.get("title")  or p.get("Title")  or "").strip(),
            "body_html":  (p.get("body_html") or p.get("Body (HTML)") or "").strip(),
            "tags":       (p.get("tags")   or p.get("Tags")   or "").strip(),
            "sku":        (p.get("sku")    or p.get("Variant SKU") or "").strip(),
            "price":      str(p.get("price") or p.get("Variant Price") or "0").strip(),
            "compare_at": str(p.get("compare_at") or p.get("Variant Compare At Price") or "").strip(),
            "vendor":     (p.get("vendor") or p.get("Vendor") or "").strip(),
            "images":     p.get("images") or [],
        }
        variant_rows = p.get("variants") or [p]  # single-variant fallback
        results.append(_check_product(normalised, variant_rows, scraper_id))
    return _build_summary(scraper_id, results, source_hint="in-memory")


def run_quality_gate(
    first_arg: Union[list, str],
    second_arg: str = "",
) -> dict:
    """
    Dual-signature public API matching the task spec contract.

    Spec contract form (in-memory product dicts):
        run_quality_gate(products: list[dict], scraper_id: str) -> dict

    Operational / CSV form:
        run_quality_gate(scraper_id: str, csv_path: str) -> dict

    Both forms return the same summary structure.
    """
    if isinstance(first_arg, list):
        # Spec contract: run_quality_gate(products, scraper_id)
        return _validate_product_dicts(first_arg, second_arg)
    else:
        # Operational: run_quality_gate(scraper_id, csv_path)
        return validate_csv(first_arg, second_arg)
