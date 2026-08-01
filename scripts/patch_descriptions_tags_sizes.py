"""
Patch all non-Coach Shopify CSVs in-place:
  1. Rebuild Body (HTML) with brand-specific heritage phrases.
     - If the row already has a Mirage template, extract the <li> bullets and
       rebuild with the new brand-specific opening / closing (preserving details).
     - If the row has only raw/old content, build from scratch.
  2. Fix sandal tag: womens-flats → womens-heels for products with "sandal" slug.
  3. Strip EU suffix from UK size Option values: "UK 6 (EU 39)" → "UK 6".

Patches both scraped_files/<id>_latest.csv and exports/<id>_shopify.csv.
Safe to re-run (idempotent after this version).
"""

import sys
import os
import re
import csv
import io

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from core.tag_engine import build_mirage_description, detect_sub_cat_slug

SCRAPER_BRANDS = {
    "cruise_fashion":   "Cruise Fashion",
    "karl":             "Karl Lagerfeld",
    "mytheresa":        "Mytheresa",
    "michael_kors":     "Michael Kors",
    "marcjacobs":       "Marc Jacobs",
    "tory":             "Tory Burch",
    "uk_polene":        "Polène",
    "thedesignerboxuk": "The Designer Box UK",
    "hoka":             "HOKA",
}

_EU_RE     = re.compile(r'^(UK\s+[\d\.]+)\s*\(EU\s+[\d\.]+\)$', re.I)
_LI_RE     = re.compile(r'<li>(.*?)</li>', re.DOTALL)
_MIRAGE_MARKER = "Discover the iconic"


def _strip_eu(val: str) -> str:
    m = _EU_RE.match(val.strip())
    return m.group(1).strip() if m else val


def _extract_bullets_as_text(body: str) -> str:
    """Return bullet text from existing <li> items as newline-separated lines."""
    items = _LI_RE.findall(body)
    if items:
        # Strip any inner HTML tags from each bullet
        clean = [re.sub(r'<[^>]+>', '', li).strip() for li in items]
        return "\n".join(li for li in clean if li)
    return ""


_TAG_TO_BROAD_CAT = {
    "footwear": "footwear", "womens-footwear": "footwear", "mens-footwear": "footwear",
    "bags": "bags", "womens-handbags": "bags", "mens-bags": "bags",
    "accessories": "accessories", "womens-accessories": "accessories", "mens-accessories": "accessories",
    "apparel": "apparel", "womens-apparel": "apparel", "mens-apparel": "apparel",
    "watches": "watches", "womens-watches": "watches", "mens-watches": "watches",
}


def _gender_from_tags(tags_str: str) -> str:
    tags = [t.strip().lower() for t in (tags_str or "").split(",")]
    if "women" in tags:
        return "women"
    if "men" in tags:
        return "men"
    return "women"


def _broad_cat_from_tags(tags_str: str) -> str:
    """Derive broad_cat hint from Shopify tags for brands with model-name titles."""
    for tag in (t.strip().lower() for t in (tags_str or "").split(",")):
        cat = _TAG_TO_BROAD_CAT.get(tag)
        if cat:
            return cat
    return ""


def _vendor_from_row(row: dict, scraper_id: str) -> str:
    v = (row.get("Vendor") or "").strip()
    return v if v else SCRAPER_BRANDS.get(scraper_id, "")


def patch_csv(path: str, scraper_id: str) -> tuple[int, int, int]:
    """Returns (desc_fixed, tag_fixed, size_fixed) counts."""
    if not os.path.exists(path):
        print(f"  SKIP (not found): {path}")
        return 0, 0, 0

    with open(path, newline="", encoding="utf-8") as f:
        raw = f.read()

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        print(f"  SKIP (empty): {path}")
        return 0, 0, 0

    rows        = list(reader)
    fieldnames  = list(reader.fieldnames)

    desc_fixed = tag_fixed = size_fixed = 0
    prev_handle = prev_title = None

    for row in rows:
        handle = row.get("Handle", "")
        title  = (row.get("Title") or "").strip()
        if not title:
            title = prev_title or handle
        else:
            prev_title = title
        if not handle:
            handle = prev_handle or ""
        else:
            prev_handle = handle

        brand    = _vendor_from_row(row, scraper_id)
        tags_str = row.get("Tags", "")
        gender   = _gender_from_tags(tags_str)
        cat_hint = _broad_cat_from_tags(tags_str)
        body     = row.get("Body (HTML)") or ""

        # ── 1. Description ─────────────────────────────────────────────────
        if body and _MIRAGE_MARKER in body:
            # Already a Mirage template — extract bullets, rebuild with brand heritage
            raw_for_rebuild = _extract_bullets_as_text(body)
            new_body = build_mirage_description(raw_for_rebuild, title, brand, gender,
                                                broad_cat_hint=cat_hint)
            if new_body != body:
                row["Body (HTML)"] = new_body
                desc_fixed += 1
        else:
            # No Mirage template yet — build from scratch using body as raw desc
            row["Body (HTML)"] = build_mirage_description(body, title, brand, gender,
                                                          broad_cat_hint=cat_hint)
            desc_fixed += 1

        # ── 2. Tags: sandal womens-flats → womens-heels ────────────────────
        tags_str = row.get("Tags") or ""
        if "womens-flats" in tags_str:
            slug = detect_sub_cat_slug(title)
            if slug in ("sandal", "heeled-sandal"):
                row["Tags"] = re.sub(r'\bwomens-flats\b', "womens-heels", tags_str)
                tag_fixed += 1

        # ── 3. Sizes: strip EU suffix from Option columns ──────────────────
        for col in ("Option1 Value", "Option2 Value", "Option3 Value"):
            val = row.get(col) or ""
            stripped = _strip_eu(val)
            if stripped != val:
                row[col] = stripped
                size_fixed += 1

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return desc_fixed, tag_fixed, size_fixed


def main():
    total_d = total_t = total_s = 0

    for scraper_id in SCRAPER_BRANDS:
        latest  = os.path.join(ROOT, "scraped_files", f"{scraper_id}_latest.csv")
        exports = os.path.join(ROOT, "exports",       f"{scraper_id}_shopify.csv")

        print(f"\n{'─'*60}")
        print(f"Scraper: {scraper_id}")

        for path in [latest, exports]:
            d, t, s = patch_csv(path, scraper_id)
            total_d += d; total_t += t; total_s += s
            print(f"  {os.path.basename(path)}: desc={d}  tags={t}  sizes={s}")

    print(f"\n{'='*60}")
    print(f"TOTAL: descriptions={total_d}  tags={total_t}  sizes={total_s}")
    print("Done.")


if __name__ == "__main__":
    main()
