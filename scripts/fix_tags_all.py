"""
Comprehensive tag fix for all scraper CSVs.

Fixes applied to every row:
  1.  Men-apparel        → mens-apparel
  2.  men-tshirts        → mens-tshirts
  3.  men-shirt          → mens-shirt
  4.  men-polo           → mens-polo
  5.  men-winterwear     → mens-winterwear
  6.  men-apparel        → mens-apparel  (lowercase duplicate of #1)
  7.  women-apparel      → womens-apparel
  8.  Add standalone "women" tag if missing but "womens-XXX" store tag exists
  9.  Add standalone "men"   tag if missing but "mens-XXX"  store tag exists
  10. Add broad store taxonomy tag (e.g. mens-apparel, womens-footwear) for
      products that have gender + broad-cat but no store-taxonomy tag
      (mainly Cruise Fashion)
  11. Fix sandal misclassified as womens-flats → womens-heels

Patches both scraped_files/<id>_latest.csv and exports/<id>_shopify.csv.
Safe to re-run (idempotent).
"""

import csv
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

SCRAPERS = [
    "coach", "cruise_fashion", "karl", "michael_kors", "marcjacobs",
    "tory", "mytheresa", "uk_polene", "thedesignerboxuk", "hoka",
]

# Exact tag replacements (case-sensitive key → correct lowercase value)
TAG_RENAMES = {
    "Men-apparel":  "mens-apparel",
    "men-apparel":  "mens-apparel",
    "men-tshirts":  "mens-tshirts",
    "men-shirt":    "mens-shirt",
    "men-polo":     "mens-polo",
    "men-winterwear": "mens-winterwear",
    "women-apparel": "womens-apparel",
}

SANDAL_RE = re.compile(r"\bsandal\b", re.I)

# Broad-cat derivation for products with no store-taxonomy tag
# gender ("women"/"men") + broad_cat tag → store tag
_BROAD_TO_STORE = {
    ("women",  "bags"):        "womens-handbags",
    ("women",  "footwear"):    "womens-footwear",
    ("women",  "accessories"): "womens-accessories",
    ("women",  "apparel"):     "womens-apparel",
    ("women",  "watches"):     "womens-accessories",
    ("men",    "bags"):        "mens-accessories",
    ("men",    "footwear"):    "mens-footwear",
    ("men",    "accessories"): "mens-accessories",
    ("men",    "apparel"):     "mens-apparel",
    ("men",    "watches"):     "mens-accessories",
    # unisex → use womens- store taxonomy (same as Cruise Fashion convention)
    ("unisex", "bags"):        "womens-handbags",
    ("unisex", "footwear"):    "womens-footwear",
    ("unisex", "accessories"): "womens-accessories",
    ("unisex", "apparel"):     "womens-apparel",
    ("unisex", "watches"):     "womens-accessories",
}


def fix_tags(tags_str: str, title: str) -> tuple[str, int]:
    """Return (fixed_tags_csv, change_count)."""
    if not tags_str:
        return tags_str, 0

    tag_list = [t.strip() for t in tags_str.split(",") if t.strip()]
    changes  = 0

    # ── Step 1: Apply exact renames ───────────────────────────────────────
    new_tags = []
    for t in tag_list:
        renamed = TAG_RENAMES.get(t)
        if renamed and renamed not in [x.lower() for x in tag_list]:
            new_tags.append(renamed)
            changes += 1
        elif renamed:
            # Duplicate after rename — skip the old one
            changes += 1
        else:
            new_tags.append(t)
    tag_list = new_tags
    tl = [t.lower() for t in tag_list]

    # ── Step 2: Add standalone gender tag if missing ──────────────────────
    if "women" not in tl and any(t.startswith("womens-") for t in tl):
        tag_list.insert(0, "women")
        tl.insert(0, "women")
        changes += 1
    elif "men" not in tl and any(t.startswith("mens-") for t in tl):
        tag_list.insert(0, "men")
        tl.insert(0, "men")
        changes += 1

    # ── Step 3: Add broad store taxonomy if totally missing ───────────────
    has_store = any(t.startswith(("womens-", "mens-")) for t in tl)
    if not has_store:
        if "women" in tl:
            gender = "women"
        elif "men" in tl:
            gender = "men"
        elif "unisex" in tl:
            gender = "unisex"
        else:
            gender = ""
        broad_cat = next((b for b in ("bags", "footwear", "apparel", "accessories", "watches")
                          if b in tl), "")
        store_tag = _BROAD_TO_STORE.get((gender, broad_cat), "")
        if store_tag:
            tag_list.append(store_tag)
            tl.append(store_tag)
            changes += 1

    # ── Step 4: Fix sandal wrongly tagged as womens-flats ────────────────
    if title and SANDAL_RE.search(title) and "womens-flats" in tl and "womens-heels" not in tl:
        tag_list = ["womens-heels" if t.lower() == "womens-flats" else t for t in tag_list]
        tl = [t.lower() for t in tag_list]
        changes += 1

    # ── Step 5: Fix wrong broad-cat and lifestyle for bag products ────────
    # Bags/handbags must use "accessories" + "daily-essential" — never
    # "apparel" (wrong broad-cat) or "streetwear" (apparel lifestyle).
    _BAG_STORE = {
        "womens-handbags", "womens-crossbodybag", "womens-shoulderbags",
        "womens-totebags", "womens-minibag", "mens-bags",
    }
    is_bag = any(t in _BAG_STORE for t in tl)
    if is_bag:
        if "apparel" in tl and "accessories" not in tl:
            tag_list = ["accessories" if t == "apparel" else t for t in tag_list]
            tl = [t.lower() for t in tag_list]
            changes += 1
        if "streetwear" in tl and "daily-essential" not in tl:
            tag_list = ["daily-essential" if t == "streetwear" else t for t in tag_list]
            tl = [t.lower() for t in tag_list]
            changes += 1

    return ", ".join(tag_list), changes


def patch_csv(path: str) -> int:
    """Patch a CSV in-place. Returns number of changed rows."""
    if not os.path.exists(path):
        return -1

    with open(path, newline="", encoding="utf-8") as f:
        raw = f.read()

    reader    = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return 0

    rows       = list(reader)
    fieldnames = list(reader.fieldnames)
    changed    = 0
    prev_title = ""

    for row in rows:
        title = (row.get("Title") or "").strip()
        if not title:
            title = prev_title
        else:
            prev_title = title

        old_tags = row.get("Tags") or ""
        if not old_tags:
            continue

        new_tags, n = fix_tags(old_tags, title)
        if n:
            row["Tags"] = new_tags
            changed += n

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return changed


def main():
    grand_total = 0
    print(f"{'Scraper':<22}  {'latest':>10}  {'shopify':>10}  {'total':>8}")
    print("-" * 55)

    for sid in SCRAPERS:
        latest  = os.path.join(ROOT, "scraped_files", f"{sid}_latest.csv")
        shopify = os.path.join(ROOT, "exports",       f"{sid}_shopify.csv")

        c_latest  = patch_csv(latest)
        c_shopify = patch_csv(shopify)
        total     = (max(c_latest, 0) + max(c_shopify, 0))
        grand_total += total

        l_str = str(c_latest)  if c_latest  >= 0 else "NOT FOUND"
        s_str = str(c_shopify) if c_shopify >= 0 else "NOT FOUND"
        print(f"{sid:<22}  {l_str:>10}  {s_str:>10}  {total:>8}")

    print("-" * 55)
    print(f"{'TOTAL':<22}  {'':>10}  {'':>10}  {grand_total:>8}")
    print("\nDone. Re-run audit to verify.")


if __name__ == "__main__":
    main()
