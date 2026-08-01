"""
Upload Verification Report
===========================
Generates a human-readable audit report comparing the current CSV with live
Shopify products for one or more scrapers.

Checks per scraper:
  - CSV products / SKUs vs Shopify products / SKUs
  - Missing SKUs (in CSV but not yet in Shopify)
  - Extra SKUs (in Shopify but not in CSV — OOS candidates)
  - Shopify duplicate handles (same product uploaded twice)
  - Per-product variant parity (CSV variant count vs Shopify variant count)
  - Tag completeness (RudraScrapper-{id} owner tag + gender tag)
  - Broken image probe (HEAD sample of first images)
  - Pricing spot check (Shopify price vs CSV price for matched SKUs)
  - Activity log failure summary (last 10 failed actions)
  - Quality gate (errors / warnings / readiness)

Public API:
  build_verification_report(scraper_ids, store_key='test') → dict
  write_verification_report(scraper_ids, store_key='test', path=None) → str
"""

import csv as _csv
import logging
import os
import random
import re
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_REPORT_PATH = "exports/upload_verification_report.txt"
_IMAGE_PROBE_SAMPLE  = 10   # number of images to HEAD-probe per scraper
_VARIANT_PARITY_CAP  = 30   # max products to compare variant counts for


def _shopify_csv_path(scraper_id: str) -> str | None:
    candidates = [
        os.path.join("scraped_files", f"{scraper_id}_latest.csv"),
        os.path.join("exports", f"{scraper_id}_shopify.csv"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _read_csv_products(csv_path: str) -> tuple[int, int, set, dict, dict]:
    """
    Returns:
      (product_count, variant_count, sku_set, sku_to_price, handle_to_variants)
    where:
      sku_to_price      : {sku: (compare_at_price, price)}
      handle_to_variants: {handle: variant_row_count}
    """
    products: set = set()
    skus: set     = set()
    sku_to_price: dict = {}
    handle_variants: dict = {}
    rows = 0
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                h   = (row.get("Handle") or "").strip()
                sku = (row.get("Variant SKU") or "").strip()
                if h:
                    products.add(h)
                    rows += 1
                    handle_variants[h] = handle_variants.get(h, 0) + 1
                if sku:
                    skus.add(sku)
                    comp  = row.get("Variant Compare At Price") or ""
                    price = row.get("Variant Price") or ""
                    sku_to_price[sku] = (comp.strip(), price.strip())
    except Exception as e:
        logger.warning(f"[VerifReport] Could not read CSV {csv_path}: {e}")
    return len(products), rows, skus, sku_to_price, handle_variants


def _probe_images(image_urls: list[str], sample: int = _IMAGE_PROBE_SAMPLE) -> dict:
    """
    HEAD-probe a random sample of image URLs.
    Returns {"ok": n, "broken": [url, ...], "timeout": n}.
    """
    if not image_urls:
        return {"ok": 0, "broken": [], "timeout": 0}
    targets = random.sample(image_urls, min(sample, len(image_urls)))
    ok = 0
    broken = []
    timeout_count = 0
    for url in targets:
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "MirageVerifier/1.0")
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status < 400:
                    ok += 1
                else:
                    broken.append(url)
        except TimeoutError:
            timeout_count += 1
        except Exception:
            broken.append(url)
    return {"ok": ok, "broken": broken, "timeout": timeout_count}


def _check_tag_completeness(
    shopify_products: list[dict],
    scraper_id: str,
) -> dict:
    """
    Checks every Shopify product for:
      1. Owner tag  (RudraScrapper-{scraper_id})
      2. Gender tag (womens / mens / unisex / kids)
    Returns {"ok": n, "missing_owner_tag": n, "missing_gender_tag": n, "examples": [...]}.
    """
    owner_tag     = f"RudraScrapper-{scraper_id}".lower()
    gender_tags   = {"womens", "mens", "unisex", "kids"}
    missing_owner  = []
    missing_gender = []
    for p in shopify_products:
        raw_tags  = (p.get("tags") or "").lower()
        tag_set   = {t.strip() for t in raw_tags.split(",")}
        title     = p.get("title", "")
        if owner_tag not in tag_set:
            missing_owner.append(title)
        gender_present = bool(tag_set & gender_tags)
        if not gender_present:
            missing_gender.append(title)
    return {
        "total":               len(shopify_products),
        "ok":                  len(shopify_products) - len(missing_owner) - len(missing_gender),
        "missing_owner_tag":   len(missing_owner),
        "missing_gender_tag":  len(missing_gender),
        "examples_owner":      missing_owner[:5],
        "examples_gender":     missing_gender[:5],
    }


def _price_spot_check(
    shopify_products: list[dict],
    sku_to_price: dict,
    sample: int = 20,
) -> dict:
    """
    For a random sample of SKUs present in both CSV and Shopify, compare
    Shopify variant price against CSV Variant Price.
    Returns {"checked": n, "mismatches": [{sku, csv_price, shopify_price}, ...]}.
    """
    candidates = []
    for p in shopify_products:
        for v in p.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku and sku in sku_to_price:
                shopify_price = str(v.get("price") or "").strip()
                _, csv_price  = sku_to_price[sku]
                candidates.append((sku, csv_price, shopify_price))
    if not candidates:
        return {"checked": 0, "mismatches": []}
    targets = random.sample(candidates, min(sample, len(candidates)))
    mismatches = []
    for sku, csv_price, shopify_price in targets:
        try:
            if abs(float(csv_price) - float(shopify_price)) > 1.0:
                mismatches.append({
                    "sku":           sku,
                    "csv_price":     csv_price,
                    "shopify_price": shopify_price,
                })
        except (ValueError, TypeError):
            pass
    return {"checked": len(targets), "mismatches": mismatches}


def _shopify_dup_handles(shopify_products: list[dict]) -> list[str]:
    """
    Detect duplicate handles in live Shopify data.
    Shopify auto-collision handles end in -1/-2; strip those to find true duplicates.
    """
    def _base(h: str) -> str:
        h = re.sub(r'-part-\d+$', '', h)
        h = re.sub(r'-[12]$', '', h)
        return h

    groups: dict[str, list[str]] = {}
    for p in shopify_products:
        h = (p.get("handle") or "").strip()
        if h:
            b = _base(h)
            groups.setdefault(b, []).append(h)
    return [
        f"{b!r}: " + ", ".join(hs)
        for b, hs in groups.items()
        if len(hs) > 1
    ]


def _variant_parity(
    shopify_products: list[dict],
    handle_to_csv_variants: dict,
    cap: int = _VARIANT_PARITY_CAP,
) -> list[dict]:
    """
    Compare variant count per product (CSV rows vs Shopify variants).
    Returns list of mismatches [{handle, csv_variants, shopify_variants}].
    """
    mismatches = []
    sample = random.sample(shopify_products, min(cap, len(shopify_products)))
    for p in sample:
        h = (p.get("handle") or "").strip()
        csv_n  = handle_to_csv_variants.get(h)
        if csv_n is None:
            continue
        shop_n = len(p.get("variants") or [])
        if csv_n != shop_n:
            mismatches.append({
                "handle":           h,
                "title":            p.get("title", ""),
                "csv_variants":     csv_n,
                "shopify_variants": shop_n,
            })
    return mismatches


def _activity_failures(scraper_id: str, store_key: str, limit: int = 10) -> list[dict]:
    """Fetch the most recent failed activity log entries for this scraper."""
    try:
        from core.db import get_shopify_logs
        return get_shopify_logs(
            scraper_id=scraper_id,
            status="error",
            store=store_key,
            limit=limit,
        )
    except Exception as e:
        logger.warning(f"[VerifReport] Could not fetch activity logs for {scraper_id}: {e}")
        return []


def build_verification_report(
    scraper_ids: list,
    store_key: str = "test",
) -> dict:
    """
    Build a full verification report dict for the given scrapers.
    """
    from core.shopify_publisher import get_scraper_products, _set_store_key
    from core.quality_gate import validate_csv

    _set_store_key(store_key)

    sections: list[dict] = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for sid in scraper_ids:
        entry: dict = {"scraper_id": sid, "store_key": store_key}

        csv_path = _shopify_csv_path(sid)
        if not csv_path:
            entry["error"] = "No CSV found — scraper has not been run"
            sections.append(entry)
            continue

        csv_products, csv_rows, csv_skus, sku_to_price, handle_to_variants = \
            _read_csv_products(csv_path)
        entry["csv_path"]     = csv_path
        entry["csv_products"] = csv_products
        entry["csv_rows"]     = csv_rows
        entry["csv_skus"]     = len(csv_skus)

        shopify_products: list[dict] = []
        try:
            shopify_products = get_scraper_products(sid)
            shopify_skus: set = set()
            image_urls: list  = []

            for p in shopify_products:
                for v in p.get("variants", []):
                    s = (v.get("sku") or "").strip()
                    if s:
                        shopify_skus.add(s)
                # collect first image per product for probe
                imgs = p.get("images") or []
                if imgs:
                    src = (imgs[0].get("src") or "").strip()
                    if src:
                        image_urls.append(src)

            entry["shopify_products"] = len(shopify_products)
            entry["shopify_skus"]     = len(shopify_skus)
            entry["missing_count"]    = len(csv_skus - shopify_skus)
            entry["extra_count"]      = len(shopify_skus - csv_skus)
            entry["missing_skus"]     = sorted(csv_skus - shopify_skus)[:50]
            entry["extra_skus"]       = sorted(shopify_skus - csv_skus)[:50]

            # Shopify duplicate handle check
            dup_handles = _shopify_dup_handles(shopify_products)
            entry["shopify_duplicate_handles"] = dup_handles

            # Tag completeness
            entry["tag_completeness"] = _check_tag_completeness(shopify_products, sid)

            # Image probe
            entry["image_probe"] = _probe_images(image_urls)

            # Pricing spot check
            entry["price_spot_check"] = _price_spot_check(shopify_products, sku_to_price)

            # Variant parity
            entry["variant_parity_mismatches"] = _variant_parity(
                shopify_products, handle_to_variants
            )

        except Exception as e:
            entry["shopify_error"] = str(e)

        # Activity log failures
        entry["activity_failures"] = _activity_failures(sid, store_key)

        # Quality gate
        try:
            qg = validate_csv(sid, csv_path)
            entry["quality_gate"] = {
                "pass_rate":         qg.get("pass_rate"),
                "errors":            qg.get("errors"),
                "warnings":          qg.get("warnings"),
                "total":             qg.get("total"),
                "ready":             qg.get("ready_to_upload"),
                "duplicate_handles": qg.get("duplicate_handles"),
                "csv_errors":        qg.get("csv_errors"),
            }
        except Exception as e:
            entry["quality_gate_error"] = str(e)

        sections.append(entry)

    return {"generated_at": generated_at, "store_key": store_key, "scrapers": sections}


def write_verification_report(
    scraper_ids: list,
    store_key: str = "test",
    path: str | None = None,
) -> str:
    """
    Build and write the verification report to disk. Returns the file path.
    """
    report  = build_verification_report(scraper_ids, store_key=store_key)
    out_path = path or _DEFAULT_REPORT_PATH
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    L: list[str] = []

    def _h(text: str = "") -> None:
        L.append(text)

    _h("=" * 72)
    _h("  MIRAGE SCRAPER — UPLOAD VERIFICATION REPORT")
    _h(f"  Store     : {store_key.upper()}")
    _h(f"  Generated : {report['generated_at']}")
    _h("=" * 72)
    _h()

    for entry in report["scrapers"]:
        sid = entry["scraper_id"]
        _h(f"── {sid.upper()} " + "─" * max(0, 60 - len(sid)))

        if "error" in entry:
            _h(f"  ✗ {entry['error']}")
            _h()
            continue

        _h(f"  CSV      : {entry['csv_path']}")
        _h(f"  Products : CSV {entry['csv_products']} | Shopify {entry.get('shopify_products', '?')}")
        _h(f"  SKUs     : CSV {entry['csv_skus']} | Shopify {entry.get('shopify_skus', '?')}")

        # SKU diff
        missing = entry.get("missing_count", 0)
        extra   = entry.get("extra_count", 0)
        if missing == 0 and extra == 0:
            _h("  ✓ SKU sets match exactly")
        else:
            if missing:
                _h(f"  ⚠ {missing} SKUs in CSV but NOT in Shopify (upload incomplete?)")
                for s in entry.get("missing_skus", [])[:10]:
                    _h(f"      {s}")
                if missing > 10:
                    _h(f"      … and {missing - 10} more")
            if extra:
                _h(f"  ⚠ {extra} SKUs in Shopify but NOT in CSV (OOS candidates)")

        # Shopify duplicate handles
        shop_dups = entry.get("shopify_duplicate_handles", [])
        if shop_dups:
            _h(f"  ✗ {len(shop_dups)} duplicate handle group(s) live in Shopify:")
            for d in shop_dups[:5]:
                _h(f"      {d}")
            if len(shop_dups) > 5:
                _h(f"      … and {len(shop_dups) - 5} more")
        else:
            _h("  ✓ No duplicate handles in Shopify")

        # Variant parity
        vp_mismatches = entry.get("variant_parity_mismatches", [])
        if vp_mismatches:
            _h(f"  ⚠ {len(vp_mismatches)} products with variant count mismatch (CSV vs Shopify):")
            for m in vp_mismatches[:5]:
                _h(f"      {m['handle']} — CSV {m['csv_variants']} vs Shopify {m['shopify_variants']}")
            if len(vp_mismatches) > 5:
                _h(f"      … and {len(vp_mismatches) - 5} more")
        else:
            _h("  ✓ Variant counts match (sampled)")

        # Tag completeness
        tc = entry.get("tag_completeness", {})
        if tc:
            mo = tc.get("missing_owner_tag", 0)
            mg = tc.get("missing_gender_tag", 0)
            if mo == 0 and mg == 0:
                _h(f"  ✓ Tags OK — all {tc.get('total', 0)} products have owner + gender tags")
            else:
                if mo:
                    _h(f"  ✗ {mo} products missing owner tag (RudraScrapper-{sid})")
                    for ex in tc.get("examples_owner", [])[:3]:
                        _h(f"      {ex}")
                if mg:
                    _h(f"  ✗ {mg} products missing gender tag")
                    for ex in tc.get("examples_gender", [])[:3]:
                        _h(f"      {ex}")

        # Image probe
        ip = entry.get("image_probe", {})
        if ip:
            broken = ip.get("broken", [])
            if not broken and ip.get("ok", 0) > 0:
                _h(f"  ✓ Images OK — {ip['ok']}/{ip['ok']} probed URLs responded 200")
            else:
                _h(f"  ⚠ Image probe: {ip.get('ok', 0)} OK, "
                   f"{len(broken)} broken, {ip.get('timeout', 0)} timeout")
                for b in broken[:3]:
                    _h(f"      {b}")

        # Pricing spot check
        psc = entry.get("price_spot_check", {})
        if psc:
            mm = psc.get("mismatches", [])
            if not mm:
                _h(f"  ✓ Prices OK — {psc.get('checked', 0)} spot-checked, 0 mismatches")
            else:
                _h(f"  ⚠ Price mismatches: {len(mm)} of {psc.get('checked', 0)} spot-checked:")
                for m in mm[:5]:
                    _h(f"      SKU {m['sku']}: CSV ₹{m['csv_price']} vs Shopify ₹{m['shopify_price']}")

        # Activity log failures
        failures = entry.get("activity_failures", [])
        if failures:
            _h(f"  ⚠ {len(failures)} recent failed activity log entries:")
            for f in failures[:5]:
                ts = (f.get("created_at") or "")[:19]
                _h(f"      [{ts}] {f.get('action_type','')} — {f.get('error_message','')[:80]}")
        else:
            _h("  ✓ No recent activity log failures")

        if "shopify_error" in entry:
            _h(f"  ✗ Shopify fetch error: {entry['shopify_error']}")

        # Quality gate summary
        qg = entry.get("quality_gate")
        if qg:
            ok_sym = "✓" if qg["ready"] else "✗"
            _h(
                f"  {ok_sym} Quality gate: {qg['pass_rate']}% pass | "
                f"{qg['errors']} errors | {qg['warnings']} warnings | "
                f"{qg['total']} products"
            )
            dup_h = qg.get("duplicate_handles") or []
            if dup_h:
                _h(f"      ✗ Duplicate handles in CSV: {', '.join(dup_h[:5])}")
            csv_errs = qg.get("csv_errors") or []
            for ce in csv_errs[:3]:
                _h(f"      ✗ {ce}")
        elif "quality_gate_error" in entry:
            _h(f"  ✗ Quality gate error: {entry['quality_gate_error']}")

        _h()

    _h("=" * 72)
    _h("END OF REPORT")
    _h("=" * 72)

    text = "\n".join(L)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    logger.info(f"[VerifReport] Written to {out_path} ({len(L)} lines)")
    return out_path
