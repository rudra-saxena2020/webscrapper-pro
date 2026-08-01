"""
Coach CSV Comprehensive Fixer
==============================
Fixes ALL structural issues in exports/coach_shopify.csv:

  1. Wrong broad-category tag (accessories for bags/footwear/apparel/watches)
  2. Missing plain gender tags (women/men/unisex) — sourced from Google Shopping/Gender
  3. Bad-casing tags (Men-apparel → men-apparel)
  4. "Coach Coach" double-prefix in Body (HTML)
  5. Adds men-* taxonomy tags for UNISEX products that only have womens-* tags
  6. Removes stray cross-gender taxonomy tags (women's taxonomy on men-only products)

Output: exports/coach_shopify.csv (in-place) + exports/coach_shopify_fixed.csv (backup)
"""

import csv
import re
import sys
import os
import shutil
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tag_engine import map_to_store_tags

# ── Type → broad-category ─────────────────────────────────────────────────────
BAGS_TYPES = {
    'shoulder bag','crossbody bag','tote','bag','backpack','wristlet','clutch',
    'bucket bag','satchel','hobo bag','mini bag','belt bag','messenger bag',
    'duffle bag','tote bag',
}
FOOTWEAR_TYPES = {
    'sneaker','boot','sandal','loafer','flat','pump','mule','slide','shoe',
    'clog','slingback','mary jane','flip flop','slipper','derby','bootie',
    'oxford','wedge','espadrille',
}
APPAREL_TYPES = {
    'top','jacket','coat','pant','dress','shorts','knitwear','apparel','shirt',
    'leggings','skirt','pajama','romper','jumpsuit','cardigan','sweater',
    'hoodie','sweatshirt','polo','blazer','overcoat','vest','bodysuit',
    'swimwear',
}
WATCH_TYPES = {'watch','watches'}

ALL_BROADS = {'accessories', 'bags', 'footwear', 'apparel', 'watches'}


def type_to_broad(ptype: str) -> str:
    t = ptype.strip().lower()
    if t in BAGS_TYPES:     return 'bags'
    if t in FOOTWEAR_TYPES: return 'footwear'
    if t in APPAREL_TYPES:  return 'apparel'
    if t in WATCH_TYPES:    return 'watches'
    return 'accessories'


# ── Type → sub_cat_slug (for taxonomy lookup) ─────────────────────────────────
TYPE_TO_SLUG = {
    'shoulder bag': 'shoulder-bag', 'crossbody bag': 'crossbody-bag',
    'tote': 'tote', 'bag': 'bag', 'backpack': 'backpack',
    'wristlet': 'wristlet', 'clutch': 'clutch', 'bucket bag': 'bucket-bag',
    'satchel': 'satchel', 'hobo bag': 'hobo-bag', 'mini bag': 'mini-bag',
    'belt bag': 'belt-bag', 'messenger bag': 'bag', 'duffle bag': 'bag',
    'sneaker': 'sneaker', 'boot': 'boot', 'sandal': 'sandal',
    'loafer': 'loafer', 'flat': 'flat', 'pump': 'pump', 'mule': 'mule',
    'slide': 'slide', 'shoe': 'shoe', 'clog': 'flat', 'slingback': 'pump',
    'mary jane': 'flat', 'flip flop': 'sandal', 'slipper': 'flat',
    'derby': 'shoe', 'bootie': 'boot', 'oxford': 'shoe', 'wedge': 'pump',
    'espadrille': 'sandal',
    'top': 'top', 'jacket': 'jacket', 'coat': 'coat', 'pant': 'pant',
    'dress': 'dress', 'shorts': 'shorts', 'knitwear': 'top',
    'apparel': 'apparel', 'shirt': 'top', 'leggings': 'leggings',
    'skirt': 'dress', 'pajama': 'apparel', 'romper': 'apparel',
    'jumpsuit': 'apparel', 'cardigan': 'top', 'sweater': 'top',
    'hoodie': 'top', 'sweatshirt': 'top', 'polo': 'top', 'blazer': 'jacket',
    'overcoat': 'coat', 'vest': 'apparel', 'bodysuit': 'top',
    'swimwear': 'apparel',
    'wallet': 'wallet', 'card case': 'card-case', 'belt': 'belt',
    'watch': 'watch', 'jewelry': 'jewelry', 'eyewear': 'sunglasses',
    'sunglasses': 'sunglasses', 'scarf': 'scarf', 'hat': 'hat',
    'gloves': 'gloves', 'bag charm': 'charm', 'bag strap': 'strap',
    'fragrance': 'fragrance', 'small leather goods': 'small-leather-goods',
    'luggage tag': 'keyring', 'passport case': 'small-leather-goods',
    'phone case': 'small-leather-goods', 'pouch': 'small-leather-goods',
    'travel kit': 'small-leather-goods', 'jewelry box': 'small-leather-goods',
    'home accessory': 'accessories', 'umbrella': 'accessories',
    'socks': 'accessories', 'passport case': 'small-leather-goods',
}


def get_sub_slug(ptype: str) -> str:
    return TYPE_TO_SLUG.get(ptype.strip().lower(), 'accessories')


# ── Gender helpers ─────────────────────────────────────────────────────────────
def _parse_gender(gs_gender: str) -> str:
    """Map Google Shopping / Gender → canonical WOMEN/MEN/UNISEX."""
    g = (gs_gender or '').strip().lower()
    if g in ('women', 'female', 'girl', 'girls'):    return 'WOMEN'
    if g in ('men', 'male', 'boy', 'boys'):          return 'MEN'
    return 'UNISEX'


# Tags that are allowed to stay lowercase even if they have hyphens
_PRESERVE_TAGS = {
    'coach', 'coachtopia', 'mirage-curated', 'premium', 'daily-essential',
    'streetwear', 'new-arrival', 'sale', 'men', 'women', 'unisex',
}

WOMENS_TAX_PREFIXES = ('womens-', 'women-')
MENS_TAX_PREFIXES   = ('mens-', 'men-')


def _has_womens_tax(tags):
    return any(t.lower().startswith(WOMENS_TAX_PREFIXES) for t in tags)

def _has_mens_tax(tags):
    return any(t.lower().startswith(MENS_TAX_PREFIXES) for t in tags)


def fix_tags(existing_tags_str: str, ptype: str, gs_gender: str) -> str:
    """
    Full tag correction pipeline:
      1. Parse existing tags
      2. Lowercase everything (fixes Men-apparel etc.)
      3. Remove wrong broad-category tag(s); add correct one
      4. Add/fix gender plain tags
      5. Add missing taxonomy tags for the detected gender
      6. Return sorted, comma-separated tag string
    """
    tags = {t.strip().lower() for t in existing_tags_str.split(',') if t.strip()}

    gender = _parse_gender(gs_gender)
    broad  = type_to_broad(ptype)
    slug   = get_sub_slug(ptype)

    # ── 1. Fix broad category ────────────────────────────────────────────────
    tags -= ALL_BROADS
    tags.add(broad)

    # ── 2. Fix gender plain tags ─────────────────────────────────────────────
    tags -= {'men', 'women', 'unisex'}
    if gender == 'WOMEN':
        tags.add('women')
    elif gender == 'MEN':
        tags.add('men')
    else:  # UNISEX
        tags.update(['women', 'men', 'unisex'])

    # ── 3. Fix cross-gender taxonomy leaks ──────────────────────────────────
    # Women-only: strip mens-* taxonomy
    if gender == 'WOMEN':
        tags = {t for t in tags if not t.lower().startswith(MENS_TAX_PREFIXES)}
    # Men-only: strip womens-* taxonomy
    elif gender == 'MEN':
        tags = {t for t in tags if not t.lower().startswith(WOMENS_TAX_PREFIXES)}

    # ── 4. Add missing taxonomy tags ─────────────────────────────────────────
    if gender in ('WOMEN', 'UNISEX') and not _has_womens_tax(tags):
        m, s = map_to_store_tags('WOMEN', broad.replace('bags','accessories'), slug)
        if m: tags.add(m.lower())
        if s: tags.add(s.lower())

    if gender in ('MEN', 'UNISEX') and not _has_mens_tax(tags):
        m, s = map_to_store_tags('MEN', broad.replace('bags','accessories'), slug)
        if m: tags.add(m.lower())
        if s: tags.add(s.lower())

    # For UNISEX: also add WOMEN taxonomy if only men's was added
    if gender == 'UNISEX':
        if not _has_womens_tax(tags):
            m, s = map_to_store_tags('WOMEN', broad.replace('bags','accessories'), slug)
            if m: tags.add(m.lower())
            if s: tags.add(s.lower())
        if not _has_mens_tax(tags):
            m, s = map_to_store_tags('MEN', broad.replace('bags','accessories'), slug)
            if m: tags.add(m.lower())
            if s: tags.add(s.lower())

    # ── 5. Always ensure the RudraScrapper safety tag is present ─────────────
    tags.add('RudraScrapper-coach')

    # ── 6. Final cleanup ─────────────────────────────────────────────────────
    tags.discard('')
    tags.discard(None)

    return ', '.join(sorted(tags))


# ── Description fixer ─────────────────────────────────────────────────────────
_COACH_COACH_RE = re.compile(r'\bCoach\s+Coach\b', re.I)

def fix_description(body: str) -> str:
    """Replace 'Coach Coach X' → 'Coach X'."""
    return _COACH_COACH_RE.sub('Coach', body)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    src  = 'exports/coach_shopify.csv'
    bak  = 'exports/coach_shopify_prefixed.csv'   # previous backup kept
    out  = 'exports/coach_shopify.csv'
    out2 = 'scraped_files/coach_latest.csv'

    print(f"Reading {src}…")
    with open(src, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    fixed = 0
    tag_fixed = 0
    desc_fixed = 0
    gender_fixed = 0
    broad_fixed = 0

    current_tags = None  # carry from header row to continuation rows

    for i, row in enumerate(rows):
        title = (row.get('Title') or '').strip()

        if title:
            # ── Header row — apply all fixes ──────────────────────────────
            ptype     = (row.get('Type') or '').strip()
            gs_gender = (row.get('Google Shopping / Gender') or '').strip()
            old_tags  = (row.get('Tags') or '').strip()
            old_body  = (row.get('Body (HTML)') or '').strip()

            # Tags
            new_tags = fix_tags(old_tags, ptype, gs_gender)
            if new_tags != old_tags:
                row['Tags'] = new_tags
                tag_fixed += 1

                # Count gender vs broad separately for reporting
                old_t = {t.strip().lower() for t in old_tags.split(',')}
                new_t = {t.strip() for t in new_tags.split(',')}
                if not any(g in old_t for g in ('men','women','unisex')):
                    gender_fixed += 1
                old_broad = old_t & ALL_BROADS
                new_broad = new_t & ALL_BROADS
                if old_broad != new_broad:
                    broad_fixed += 1

            current_tags = new_tags

            # Description
            new_body = fix_description(old_body)
            if new_body != old_body:
                row['Body (HTML)'] = new_body
                desc_fixed += 1

            fixed += 1

        # Continuation rows don't carry tags or descriptions — leave as-is

    # ── Write output ─────────────────────────────────────────────────────────
    for path in [out, out2]:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        print(f"✅ Written {len(rows)} rows → {path}")

    print(f"\n── Fix Summary ──")
    print(f"  Products processed : {fixed}")
    print(f"  Tags fixed         : {tag_fixed}")
    print(f"  Gender tags added  : {gender_fixed}")
    print(f"  Broad cat fixed    : {broad_fixed}")
    print(f"  Descriptions fixed : {desc_fixed}")
    print(f"  Total rows         : {len(rows)}")

    # ── Post-fix audit ───────────────────────────────────────────────────────
    print("\n── Post-fix validation ──")
    header_rows = [r for r in rows if (r.get('Title') or '').strip()]
    no_gender = [r for r in header_rows if not any(
        g in [t.strip() for t in (r.get('Tags') or '').split(',')]
        for g in ('men', 'women', 'unisex')
    )]
    wrong_broad = []
    for r in header_rows:
        ptype = (r.get('Type') or '').strip()
        expected = type_to_broad(ptype)
        tags = [t.strip() for t in (r.get('Tags') or '').split(',')]
        active_broad = {t for t in tags if t in ALL_BROADS}
        if active_broad != {expected}:
            wrong_broad.append((r.get('Title','')[:50], ptype, expected, active_broad))

    double_coach = [r for r in header_rows if re.search(r'Coach\s+Coach', r.get('Body (HTML)', ''), re.I)]
    bad_case = [t for r in header_rows for t in (r.get('Tags') or '').split(',') if t.strip() and t.strip()[0].isupper()]

    print(f"  Missing gender tags    : {len(no_gender)}")
    print(f"  Wrong broad category   : {len(wrong_broad)}")
    print(f"  'Coach Coach' descs    : {len(double_coach)}")
    print(f"  Uppercase tag starts   : {len(bad_case)}")
    if wrong_broad:
        for title, pt, exp, act in wrong_broad[:5]:
            print(f"    [{title}]  type={pt}  expected={exp}  got={act}")
    if no_gender:
        for r in no_gender[:3]:
            print(f"    {r.get('Title','')[:55]}  tags={r.get('Tags','')[:80]}")


if __name__ == '__main__':
    main()
