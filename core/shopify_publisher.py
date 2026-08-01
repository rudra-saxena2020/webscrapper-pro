"""
Shopify Admin API Publisher
============================
Handles all Shopify API operations for the Mirage Scraper Engine.

Safety contract:
  Every write/delete action first verifies the product carries the correct
  RudraScrapper-{scraper_id} tag. If not, it aborts and logs a warning.
  This system physically cannot delete or modify a product it did not create.

Rate limits:
  Shopify Basic = 40 req / 20 s leaky-bucket (≈ 2 req/s average).
  ShopifyRateLimiter enforces 2 req/s globally across all worker threads,
  preventing 429s and maximising sustained throughput.
"""

import csv
import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ── Global rate limiter (2 req/s — Shopify Basic safe ceiling) ────────────────

class ShopifyRateLimiter:
    """
    Simple sequential token-bucket: 1 token per min_interval seconds.
    Serialises all outbound Shopify calls so we never exceed 2 req/s
    regardless of how many worker threads are active.
    """
    def __init__(self, calls_per_second: float = 2.0):
        self._interval = 1.0 / calls_per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            gap = self._last + self._interval - now
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_rate_limiter = ShopifyRateLimiter(calls_per_second=2.0)

# Tracks when we're sleeping for the daily variant limit reset (set by _shopify_request)
# Keyed by store_key ('test' or 'main') so each store's quota is tracked independently.
_daily_limit_resume_at: dict = {}       # {store_key: str}  e.g. {"test": "00:02 UTC"}
_daily_limit_resume_ts_unix: dict = {}  # {store_key: float} Unix timestamp of resume time
_session_variants_created: dict = {}    # {store_key: int} — variants created this session


# ── Per-thread store context ───────────────────────────────────────────────────
# Flask routes call _set_store_key(store_key) inside each daemon thread before
# invoking any publisher function. All internal calls read _cur_store_key()
# transparently — no signature changes needed across 20+ internal helpers.

_thread_local = threading.local()


def _set_store_key(key: str) -> None:
    """Set the active Shopify store for the current thread ('test' or 'main')."""
    _thread_local.store_key = key


def _cur_store_key() -> str:
    """Return the active store key for this thread. Defaults to 'test'."""
    return getattr(_thread_local, 'store_key', 'test')


# ── DB audit logging (lazy import — never crashes if DB is down) ──────────────

def _try_log(scraper_id, action_type, status="success", result=None, error=None, notes=None):
    try:
        from .db import log_shopify_action
        log_shopify_action(scraper_id, action_type, status, result, error, notes, store=_cur_store_key())
    except Exception as _e:
        logger.warning(f"[AuditLog] Could not write log: {_e}")

def _try_register(scraper_id, shopify_product_id, sku=None, handle=None, title=None):
    try:
        from .db import register_shopify_product
        store = _cur_store_key()
        register_shopify_product(scraper_id, shopify_product_id, sku, handle, title, store=store)
    except Exception as _e:
        logger.warning(f"[Registry] Could not register product: {_e}")

def _try_remove(shopify_product_id):
    try:
        from .db import remove_shopify_product
        remove_shopify_product(shopify_product_id)
    except Exception as _e:
        logger.warning(f"[Registry] Could not remove product: {_e}")

def _try_verify_ownership(shopify_product_id, scraper_id) -> bool:
    try:
        from .db import verify_product_ownership
        return verify_product_ownership(shopify_product_id, scraper_id)
    except Exception as _e:
        logger.warning(f"[Registry] Ownership check failed: {_e} — defaulting to True")
        return True


# ── Credentials ───────────────────────────────────────────────────────────────

def _get_credentials() -> tuple[str, str]:
    key = _cur_store_key()
    if key == 'main':
        store_url = os.getenv("MAIN_SHOPIFY_STORE_URL", "").strip().rstrip("/")
        token = os.getenv("MAIN_SHOPIFY_ACCESS_TOKEN", "").strip()
        if not store_url or not token:
            raise EnvironmentError(
                "MAIN_SHOPIFY_STORE_URL and MAIN_SHOPIFY_ACCESS_TOKEN must be set as secrets "
                "before performing MAIN STORE operations."
            )
    else:
        # TEST store: prefer TEST_* vars, fall back to legacy SHOPIFY_* vars
        store_url = (
            os.getenv("TEST_SHOPIFY_STORE_URL") or os.getenv("SHOPIFY_STORE_URL") or ""
        ).strip().rstrip("/")
        token = (
            os.getenv("TEST_SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_ACCESS_TOKEN") or ""
        ).strip()
        if not store_url or not token:
            raise EnvironmentError(
                "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set as secrets."
            )
    if store_url.startswith("http"):
        store_url = store_url.split("://", 1)[1]
    return store_url, token


def _base_url() -> str:
    store_url, _ = _get_credentials()
    return f"https://{store_url}/admin/api/2025-01"


# ── Core HTTP (rate-limited, retry-aware) ─────────────────────────────────────

def _shopify_request(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    retries: int = 5,
) -> dict:
    """Authenticated Shopify Admin API request with rate-limiting and 429 back-off."""
    store_url, token = _get_credentials()
    base = f"https://{store_url}/admin/api/2025-01"
    url = path if path.startswith("http") else f"{base}{path}"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

    _rate_limiter.acquire()

    for attempt in range(retries):
        try:
            resp = requests.request(
                method, url, headers=headers,
                json=payload, params=params, timeout=90
            )
            if resp.status_code == 429:
                body_text = ""
                try:
                    body_text = resp.text
                except Exception:
                    pass
                if "Daily variant creation limit" in body_text:
                    # Hard Shopify daily variant quota — sleep until midnight UTC + 2-min buffer
                    # then resume automatically. Each store tracked independently.
                    _sk = _cur_store_key()
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    next_reset = (now_utc + datetime.timedelta(days=1)).replace(
                        hour=0, minute=2, second=0, microsecond=0
                    )
                    secs = max(60.0, (next_reset - now_utc).total_seconds())
                    hrs = secs / 3600
                    _daily_limit_resume_at[_sk] = next_reset.strftime("%H:%M UTC")
                    _daily_limit_resume_ts_unix[_sk] = next_reset.timestamp()
                    logger.warning(
                        f"[Shopify:{_sk}] Daily variant creation limit reached. "
                        f"Sleeping {hrs:.1f}h until {next_reset.strftime('%H:%M UTC')} "
                        f"(auto-resumes — Shopify resets quota at midnight UTC)."
                    )
                    sleep_end = time.monotonic() + secs
                    while time.monotonic() < sleep_end:
                        remaining = sleep_end - time.monotonic()
                        time.sleep(min(30.0, max(0, remaining)))
                    _daily_limit_resume_at.pop(_sk, None)
                    _daily_limit_resume_ts_unix.pop(_sk, None)
                else:
                    wait = float(resp.headers.get("Retry-After", 4.0))
                    logger.warning(f"[Shopify] Rate-limited. Waiting {wait}s…")
                    time.sleep(wait)
                _rate_limiter.acquire()
                continue
            if resp.status_code in (500, 502, 503, 504):
                wait = 2 ** attempt
                logger.warning(f"[Shopify] Server error {resp.status_code}, retry {attempt+1}/{retries} (wait {wait}s)")
                time.sleep(wait)
                _rate_limiter.acquire()
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            # Don't retry client errors (4xx) — they won't change on retry
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                if 400 <= e.response.status_code < 500:
                    raise
            logger.warning(f"[Shopify] Request error: {e}, retry {attempt+1}/{retries}")
            time.sleep(2 ** attempt)
            _rate_limiter.acquire()

    raise RuntimeError(f"Shopify request failed after {retries} retries: {method} {path}")


def _shopify_request_with_link(
    method: str,
    path: str,
    params: Optional[dict] = None,
) -> tuple[dict, str]:
    """
    Like _shopify_request but also returns the raw Link response header.
    Used by get_scraper_products for cursor-based pagination.
    """
    store_url, token = _get_credentials()
    base = f"https://{store_url}/admin/api/2025-01"
    url = path if path.startswith("http") else f"{base}{path}"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

    _rate_limiter.acquire()

    for attempt in range(5):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 4.0))
                logger.warning(f"[Shopify] Rate-limited (paginate). Waiting {wait}s…")
                time.sleep(wait)
                _rate_limiter.acquire()
                continue
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                _rate_limiter.acquire()
                continue
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            return data, resp.headers.get("Link", "")
        except requests.exceptions.RequestException as e:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
            _rate_limiter.acquire()

    raise RuntimeError(f"Shopify paginate request failed after 5 retries: {path}")


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the rel="next" URL from a Shopify Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            m = re.search(r'<([^>]+)>', part)
            if m:
                return m.group(1)
    return None


def _base_handle(handle: str) -> str:
    """
    Normalise a Shopify handle for deduplication matching.
    Strips:
      - CSV split suffix        …-part-2, -part-3 …
      - Shopify collision suffix …-1, -2, -3 …  (added when handle already taken)
    Order matters: strip -part-N first so '-part-2' doesn't become '-part'.
    """
    h = handle.strip()
    # Strip CSV split suffix, optionally followed by a Shopify collision suffix:
    #   blue-bag-part-2   → blue-bag
    #   blue-bag-part-2-1 → blue-bag
    h = re.sub(r'-part-\d+(-[12])?$', '', h)
    # Strip bare Shopify collision suffix (-1 or -2 only).
    # Numbers ≥ 3 are legitimate model names (bondi-9, clifton-9) — do not strip.
    h = re.sub(r'-[12]$', '', h)
    return h


# ── Tag utilities ──────────────────────────────────────────────────────────────

def _scrapper_tag(scraper_id: str) -> str:
    return f"RudraScrapper-{scraper_id}"

def _has_scrapper_tag(product: dict, scraper_id: str) -> bool:
    """
    Case-insensitive check — Shopify normalises tags to lowercase on the wire.
    Handles both REST (comma-separated string) and GraphQL (list of strings).
    """
    tags_raw = product.get("tags") or ""
    if isinstance(tags_raw, list):
        tags_lower = {str(t).strip().lower() for t in tags_raw}
    else:
        tags_lower = {t.strip().lower() for t in str(tags_raw).split(",")}
    return _scrapper_tag(scraper_id).lower() in tags_lower

def _add_scrapper_tag(existing_tags_str: str, scraper_id: str) -> str:
    tag = _scrapper_tag(scraper_id)
    tags = [t.strip() for t in (existing_tags_str or "").split(",") if t.strip()]
    # Case-insensitive dedup — don't add if already present in any casing
    if tag.lower() not in {t.lower() for t in tags}:
        tags.append(tag)
    return ", ".join(tags)


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _load_csv(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _extract_variant(row: dict, fallback_price: str = "0.00") -> Optional[dict]:
    """Extract a single variant dict from one CSV row. Returns None for blank/OOS rows."""
    opt1 = (row.get("Option1 Value") or "").strip()
    sku  = (row.get("Variant SKU")   or "").strip()
    if not opt1 and not sku:
        return None          # image-only / blank continuation row

    price = (row.get("Variant Price") or fallback_price or "0.00").strip()
    # OOS safety net: skip variants with no price or an explicitly-zero price
    if not price or price in ("0", "0.0", "0.00"):
        return None
    # Skip variants explicitly marked as zero inventory (scrapers that emit real stock counts)
    qty_raw = (row.get("Variant Inventory Qty") or "").strip()
    if qty_raw == "0":
        return None
    return {
        "sku":              sku,
        "price":            price,
        "compare_at_price": (row.get("Variant Compare At Price") or "").strip(),
        "option1_value":    opt1 or "Default Title",
        "option2_value":    (row.get("Option2 Value") or "").strip(),
        "inventory_qty":    int(float(qty_raw)) if qty_raw else 100,
        "variant_image":    (row.get("Variant Image") or "").strip(),
        "variant_all_images": (row.get("Variant All Images") or "").strip(),
    }


def _group_csv_rows(rows: list[dict]) -> list[dict]:
    """
    Group flat Shopify CSV rows into product dicts, keyed by Handle.

    Each product dict now carries a `variants` list containing ALL size/option
    variants found across every row for that handle.  Previous behaviour only
    stored the FIRST variant row and silently dropped every subsequent one,
    which caused Shopify products to be uploaded with a single variant regardless
    of how many sizes the scraper collected.

    Image accumulation now includes Image Src, Variant Image and Variant All Images
    so scrapers that put gallery shots in Variant All Images (e.g. Marc Jacobs)
    still have every image available for the creation payload.
    """
    products: list[dict] = []
    seen_handles: dict[str, dict] = {}

    for row in rows:
        handle = (row.get("Handle") or "").strip()
        title  = (row.get("Title")  or "").strip()

        # Gather every image URL this row carries
        row_imgs: list[str] = []
        for col in ("Image Src", "Variant Image", "Variant All Images"):
            raw = (row.get(col) or "").strip()
            if raw:
                if col == "Variant All Images":
                    row_imgs.extend(u.strip() for u in raw.split("|") if u.strip())
                else:
                    row_imgs.append(raw)

        if handle and handle in seen_handles:
            existing = seen_handles[handle]
            # Accumulate images from every row (dedup)
            for img in row_imgs:
                if img not in existing["images"]:
                    existing["images"].append(img)
            # Accumulate variant if this row carries actual variant data.
            # Deduplicate by (option1_value, option2_value) pair so that products
            # with the same color but different sizes all get through — only an
            # exact color+size duplicate is skipped.
            v = _extract_variant(row, existing["price"])
            if v:
                seen_opts = {
                    (x["option1_value"], x.get("option2_value", ""))
                    for x in existing["variants"]
                }
                key = (v["option1_value"], v.get("option2_value", ""))
                if key not in seen_opts:
                    existing["variants"].append(v)
            continue

        if not handle:
            continue

        # First row for this handle
        base_price = (row.get("Variant Price") or "0.00").strip()
        first_v    = _extract_variant(row, base_price)
        current: dict = {
            "handle":           handle,
            "title":            title or handle,
            "body_html":        (row.get("Body (HTML)") or "").strip(),
            "vendor":           (row.get("Vendor") or "").strip(),
            "product_type":     (row.get("Type") or "").strip(),
            "tags":             (row.get("Tags") or "").strip(),
            "price":            base_price,
            "compare_at_price": (row.get("Variant Compare At Price") or "").strip(),
            "gender":           (row.get("Google Shopping / Gender") or "").strip(),
            "images":           list(dict.fromkeys(row_imgs)),  # dedup while preserving order
            "option1_name":     (row.get("Option1 Name") or "Size").strip() or "Size",
            "option2_name":     (row.get("Option2 Name") or "").strip(),
            # Legacy single-variant fields kept for update_products compat
            "sku":              (row.get("Variant SKU") or "").strip(),
            "option1_value":    (row.get("Option1 Value") or "Default Title").strip(),
            "option2_value":    (row.get("Option2 Value") or "").strip(),
            "inventory_qty":    int(float(row.get("Variant Inventory Qty") or 100)),
            # Full variant list — populated here and in subsequent rows
            "variants":         [first_v] if first_v else [],
            # color → image URL map: used after product creation to link variant
            # images so Shopify shows the right photo per colour swatch
            "color_image_map":  {},
        }
        # Seed map from first variant — only when Option1 is a Color axis.
        # For size-only products (Dr. Martens etc.), color_image_map stays empty
        # so images are included in the initial product creation payload instead
        # of generating N_sizes × N_images redundant per-variant image API calls.
        _opt1_name = current.get("option1_name", "").lower()
        if first_v and first_v.get("option1_value") and any(
            k in _opt1_name for k in ("color", "colour", "shade", "finish")
        ):
            _seed_color_map(current["color_image_map"], first_v)
        products.append(current)
        seen_handles[handle] = current

    # Second pass: fill color_image_map from all accumulated variants.
    # Values are lists of image URLs so _link_color_images can POST every
    # image for a colour linked to that colour's Shopify variant_ids.
    # Only applies when Option1 is a Color axis — not Size (Dr. Martens etc.).
    for product in products:
        opt1_lower = product.get("option1_name", "").lower()
        if not any(k in opt1_lower for k in ("color", "colour", "shade", "finish")):
            # Size-only product: keep color_image_map empty so images are
            # sent in the initial product payload (no per-variant image linking).
            product["color_image_map"] = {}
            continue
        cmap = product.setdefault("color_image_map", {})
        for v in product.get("variants", []):
            color = v.get("option1_value", "")
            if color and color not in cmap:
                _seed_color_map(cmap, v)

    return products


# ── Shopify product payload builder ───────────────────────────────────────────

def _is_shopify_cdn(url: str) -> bool:
    """True when url is a cross-store Shopify CDN asset.
    Shopify's async src-based importer silently drops all but the first
    cross-store CDN image, so these must be uploaded via base64 attachment.
    """
    return bool(url and "cdn.shopify.com" in url)


def _post_image_b64(product_id: int, url: str, position: int) -> bool:
    """Download *url* and POST it to Shopify as a base64 attachment.
    Falls back to src-upload if the download fails.
    Returns True on success.
    """
    clean = _clean_shopify_image_url(url)
    if not clean:
        return False
    b64 = _fetch_image_as_base64(clean)
    if b64:
        img_payload: dict = {"attachment": b64, "position": position}
    else:
        img_payload = {"src": clean, "position": position}
    try:
        _shopify_request(
            "POST",
            f"/products/{product_id}/images.json",
            payload={"image": img_payload},
        )
        return True
    except Exception as e:
        logger.warning(f"[ImageLink] b64 post failed pos={position} {clean[:60]}: {e}")
        return False


def _clean_shopify_image_url(url: str) -> str:
    """
    Strip query parameters that cause Shopify's image importer to fail silently,
    and normalise CDN transform strings to formats Shopify can import.

    Known cases:
    - ?preset=...  (Bynder/Marc Jacobs CDN) forces AVIF which Shopify cannot import.
    - ?v=...       (Shopify CDN version tokens on cross-store URLs, e.g. Karl Lagerfeld)
                   Shopify's import service may reject or fail on versioned CDN URLs
                   from other stores. Stripping ?v= keeps the base JPEG URL intact.
    - ?_s=...      (Cloudinary signed-URL token, e.g. media.thereformation.com)
                   Signature tokens expire; Shopify's importer fetches the image
                   asynchronously and may hit an expired token. Strip for stable URLs.
    - f_auto       (Cloudinary auto-format transform on media.thereformation.com)
                   Delivers AVIF to Shopify's importer which cannot handle it.
                   Replace with f_jpg to force JPEG delivery.
    - Replit / sisko.replit.dev processed URLs are local-only endpoints that
                   Shopify cannot reach (SSL errors + ephemeral). Skip them entirely
                   so downstream upload logic uses the CDN alternative instead.
    """
    if not url:
        return url
    # Replit processed images are local-only — Shopify can't fetch them
    if 'sisko.replit' in url or 'replit.dev' in url:
        return ""
    import re as _re
    url = _re.sub(r'[?&]preset=[^&]*', '', url).rstrip('?&')
    url = _re.sub(r'[?&]v=[^&]*', '', url).rstrip('?&')
    url = _re.sub(r'[?&]_s=[^&]*', '', url).rstrip('?&')
    if 'media.thereformation.com' in url:
        url = url.replace('f_auto', 'f_jpg')
    return url


def _build_shopify_payload(product: dict, scraper_id: str) -> dict:
    tags_with_owner = _add_scrapper_tag(product["tags"], scraper_id)

    # Use the full variants list collected during CSV grouping.
    # Fall back to legacy single-variant format for any product dict that
    # predates the multi-variant fix.
    raw_variants: list[dict] = product.get("variants") or [{
        "sku":              product.get("sku", ""),
        "price":            product.get("price", "0.00"),
        "compare_at_price": product.get("compare_at_price", ""),
        "option1_value":    product.get("option1_value", "Default Title"),
        "option2_value":    product.get("option2_value", ""),
        "inventory_qty":    product.get("inventory_qty", 100),
    }]

    variants = []
    for v in raw_variants:
        variants.append({
            "sku":                  v["sku"],
            "price":                v["price"] or product.get("price", "0.00"),
            "compare_at_price":     v["compare_at_price"] or product.get("compare_at_price") or None,
            "option1":              v["option1_value"],
            "option2":              v["option2_value"] or None,
            "inventory_management": "shopify",
            "inventory_policy":     "deny",
            "fulfillment_service":  "manual",
            "requires_shipping":    True,
            "taxable":              True,
            "inventory_quantity":   v["inventory_qty"],
        })

    # Build option value lists preserving CSV order, deduped
    opt1_values = list(dict.fromkeys(v["option1_value"] for v in raw_variants))
    opt2_values = list(dict.fromkeys(v["option2_value"] for v in raw_variants if v.get("option2_value")))

    options = [{"name": product["option1_name"], "values": opt1_values}]
    if product.get("option2_name") and opt2_values:
        options.append({"name": product["option2_name"], "values": opt2_values})

    # For cross-store Shopify CDN images Shopify's async importer silently drops
    # all but the first image.  Include ONLY the hero in the creation payload;
    # the remaining images are POSTed individually as base64 attachments after
    # the product is created (see the extra-images loop in process_product).
    raw_imgs = [img for img in product["images"] if img]
    # Clean each URL and drop processed / broken ones (e.g. sisko.replit.dev)
    cleaned_imgs = [u for u in (_clean_shopify_image_url(i) for i in raw_imgs) if u]
    all_cdn = bool(cleaned_imgs) and all(_is_shopify_cdn(i) for i in cleaned_imgs)
    if all_cdn:
        # Hero only in payload — rest uploaded reliably via POST /images after creation
        images = [{"src": cleaned_imgs[0]}] if cleaned_imgs else []
    else:
        images = [{"src": u} for u in cleaned_imgs]

    return {
        "product": {
            "title":        product["title"],
            "body_html":    product["body_html"],
            "vendor":       product["vendor"],
            "product_type": product["product_type"],
            "tags":         tags_with_owner,
            "status":       "active",
            "variants":     variants,
            "options":      options,
            "images":       images,
        }
    }


def _seed_color_map(cmap: dict, variant: dict) -> None:
    """
    Populate cmap[color] with the PRIMARY image URL for this colour.
    Only the first image is stored — it will be POSTed to Shopify with the
    colour's variant_ids so the swatch shows the right hero image.
    Additional per-colour shots (2nd, 3rd…) are not stored here; they fall
    through to product["images"] and are included in the creation payload as
    gallery images, avoiding N extra API calls per colour.
    """
    color = variant.get("option1_value", "")
    if not color or color in cmap:
        return
    raw = variant.get("variant_all_images", "") or variant.get("variant_image", "")
    imgs = [u.strip() for u in raw.split("|") if u.strip()]
    if imgs:
        cmap[color] = imgs[:1]  # Primary swatch image only; extras become gallery


def _fetch_image_as_base64(url: str) -> Optional[str]:
    """
    Download an image from url and return it base64-encoded.

    Used for cross-store Shopify CDN URLs (cdn.shopify.com) where Shopify's
    async src-based import silently fails after the first few images — the API
    returns 201 but the background CDN-to-CDN fetch is throttled/blocked.
    Uploading as base64 via 'attachment' bypasses the async import entirely.

    Returns None on any download/encoding error (caller falls back to src).
    """
    try:
        import base64 as _base64
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return _base64.b64encode(resp.content).decode("ascii")
    except Exception as e:
        logger.warning(f"[ImageLink] base64 download failed for {url[:80]}: {e}")
        return None


def _img_basename(url: str) -> str:
    """
    Normalised filename extracted from a URL for dedup matching.

    Shopify re-hosts uploaded images on cdn.shopify.com but preserves the
    original filename in the path (e.g. P01130525.jpg or uuid.jpg).  By
    comparing basenames we can match the original hero URL against the
    Shopify-rehosted version returned in the creation response, without
    needing to resolve the full CDN URL.
    """
    if not url:
        return ""
    clean = url.split("?")[0].split("#")[0]
    # Trailing-slash URLs (e.g. Marc Jacobs Bynder CDN paths like .../Sequence/MAIN/)
    # have no filename segment. Fall back to the full path (minus domain) as the
    # identifier so colour-to-image matching still works for path-based assets.
    last_segment = clean.rstrip("/").rsplit("/", 1)[-1].lower()
    if last_segment:
        return last_segment
    # No filename → use the path tail (e.g. "sequence/main" from /.../Sequence/MAIN/)
    path_part = clean.split("//", 1)[-1]  # drop https://
    path_part = path_part.split("/", 1)[-1]  # drop domain
    return path_part.rstrip("/").lower().replace("/", "-")


def _link_color_images(product_id: int, shopify_product: dict, color_image_map: dict) -> int:
    """
    Link each colour's hero image to its variant IDs so Shopify shows the
    correct image when a colour swatch is selected.

    Strategy (upload path):
      Since upload_products now includes ALL images (hero + gallery) in the
      creation payload, the hero image is already in Shopify at position 1.
      We match it by filename basename against shopify_product["images"] and
      use PUT to attach variant_ids — no duplicate image is ever created.
      If the image is not found in the product (edge case: async import delay),
      we fall back to POST so the hero is never silently lost.

    Strategy (update path):
      Called after all existing images are deleted.  No match will be found,
      so every image is POSTed fresh — same behaviour as before.

    color_image_map: {option1_value → list[image_src_url]}
    (legacy single-string values are also accepted for backwards compatibility)

    For cross-store Shopify CDN URLs (cdn.shopify.com) the fallback POST uses
    a base64 attachment to bypass Shopify's async CDN import throttle.

    Returns the number of image link failures.
    """
    if not color_image_map:
        return 0

    # option1 value → list of Shopify variant IDs (from creation response)
    variant_ids_by_color: dict[str, list] = {}
    for v in shopify_product.get("variants", []):
        color = (v.get("option1") or "").strip()
        if color:
            variant_ids_by_color.setdefault(color, []).append(v["id"])

    # Build a case-insensitive lookup so "OBSIDIAN" matches "Obsidian" etc.
    variant_ids_by_color_lower: dict[str, list] = {
        k.lower(): v for k, v in variant_ids_by_color.items()
    }

    # Build basename → image_id map from images already in the Shopify product.
    # The hero was included in the creation payload, so it should be here.
    existing_by_basename: dict[str, int] = {}
    for img in shopify_product.get("images", []):
        bn = _img_basename(img.get("src", ""))
        if bn:
            existing_by_basename[bn] = img["id"]

    # ── Pre-fetch all CDN images in parallel (for POST fallback only) ─────
    # Sequential downloads (1-2s each) are the main bottleneck for cross-store
    # CDN images. Pre-download all unique URLs concurrently so the subsequent
    # POST loop only waits on Shopify rate limits, not download latency.
    all_urls: list[str] = []
    for img_data in color_image_map.values():
        img_list = img_data if isinstance(img_data, list) else [img_data]
        for img_url in img_list:
            clean = _clean_shopify_image_url(img_url)
            if clean and "cdn.shopify.com" in clean and clean not in all_urls:
                all_urls.append(clean)

    b64_cache: dict[str, Optional[str]] = {}
    if all_urls:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
        with _TPE(max_workers=min(10, len(all_urls))) as dl_pool:
            future_to_url = {dl_pool.submit(_fetch_image_as_base64, u): u for u in all_urls}
            for fut in _asc(future_to_url):
                u = future_to_url[fut]
                try:
                    b64_cache[u] = fut.result()
                except Exception:
                    b64_cache[u] = None
    # ──────────────────────────────────────────────────────────────────────

    position = 1
    failures = 0

    for color, img_data in color_image_map.items():
        # Exact match first; fall back to case-insensitive match
        v_ids = variant_ids_by_color.get(color) or variant_ids_by_color_lower.get(color.lower(), [])
        if not v_ids:
            logger.warning(
                f"[ImageLink] No Shopify variant_ids found for color={color!r}. "
                f"Available colors: {list(variant_ids_by_color.keys())[:10]}. "
                f"Image will be added to gallery only (not variant-linked)."
            )
        # Accept both list (new) and single-string (legacy) formats
        img_list = img_data if isinstance(img_data, list) else [img_data]
        for img_url in img_list:
            clean_url = _clean_shopify_image_url(img_url)
            if not clean_url:
                continue
            try:
                # ── Try PUT first: image is already in product from payload ──
                # Match by normalised basename (Shopify preserves filename when
                # re-hosting on its CDN, e.g. P01130525.jpg → .../P01130525.jpg)
                bn = _img_basename(clean_url)
                existing_img_id = existing_by_basename.get(bn)

                if existing_img_id:
                    # Image already exists — just attach variant_ids (no duplicate)
                    _shopify_request(
                        "PUT",
                        f"/products/{product_id}/images/{existing_img_id}.json",
                        payload={"image": {"variant_ids": v_ids}},
                    )
                    logger.debug(
                        f"[ImageLink] PUT id={existing_img_id} → color={color!r} "
                        f"variants={v_ids} url={clean_url[:60]}"
                    )
                else:
                    # ── Fallback POST: image not yet in product ────────────
                    # Cross-store Shopify CDN URLs: use base64 to bypass the
                    # async CDN-to-CDN import throttle.
                    if "cdn.shopify.com" in clean_url:
                        b64 = b64_cache.get(clean_url) or _fetch_image_as_base64(clean_url)
                        if b64:
                            img_payload = {
                                "attachment":  b64,
                                "variant_ids": v_ids,
                                "position":    position,
                            }
                        else:
                            img_payload = {
                                "src":         clean_url,
                                "variant_ids": v_ids,
                                "position":    position,
                            }
                    else:
                        img_payload = {
                            "src":         clean_url,
                            "variant_ids": v_ids,
                            "position":    position,
                        }
                    _shopify_request(
                        "POST",
                        f"/products/{product_id}/images.json",
                        payload={"image": img_payload},
                    )
                    logger.debug(
                        f"[ImageLink] POST pos={position} → color={color!r} "
                        f"variants={v_ids} url={clean_url[:60]}"
                    )
                position += 1
            except Exception as e:
                failures += 1
                logger.warning(f"[ImageLink] Failed for color={color!r}: {e}")
    return failures


# ── Core operations ───────────────────────────────────────────────────────────

def get_scraper_products(scraper_id: str) -> list[dict]:
    """
    Fetch ALL Shopify products tagged RudraScrapper-{scraper_id}.
    Uses GraphQL with tag-filtered query (fast, ~1-5 s for any size store).
    Falls back to REST Link-header pagination only if GraphQL fails.
    """
    tag = _scrapper_tag(scraper_id)
    store_url, token = _get_credentials()
    gql_url = f"https://{store_url}/admin/api/2025-01/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    products: list[dict] = []
    cursor: Optional[str] = None

    while True:
        _rate_limiter.acquire()
        after = f', after: "{cursor}"' if cursor else ""
        query = f"""{{ products(first: 250, query: \"tag:{tag}\"{after}) {{ nodes {{ id title handle tags variants(first: 100) {{ nodes {{ id sku title price compareAtPrice }} }} }} pageInfo {{ hasNextPage endCursor }} }} }}"""
        try:
            resp = requests.post(
                gql_url, headers=headers,
                json={"query": query}, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            nodes = data["data"]["products"]["nodes"]
            for n in nodes:
                if not _has_scrapper_tag(n, scraper_id):
                    continue
                variants = []
                for v in n.get("variants", {}).get("nodes", []):
                    variants.append({
                        "id": v.get("id", "").split("/")[-1],
                        "sku": v.get("sku", ""),
                        "title": v.get("title", ""),
                        "price": v.get("price", ""),
                        "compare_at_price": v.get("compareAtPrice", ""),
                    })
                products.append({
                    "id": n.get("id", "").split("/")[-1],
                    "title": n.get("title", ""),
                    "handle": n.get("handle", ""),
                    "tags": n.get("tags", []),
                    "variants": variants,
                })
            page_info = data["data"]["products"]["pageInfo"]
            logger.info(
                f"[Shopify] GraphQL page: {len(nodes)} nodes, "
                f"total tagged {len(products)}"
            )
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        except Exception as e:
            logger.warning(
                f"[Shopify] GraphQL pre-scan failed ({e}), falling back to REST"
            )
            return _get_scraper_products_rest(scraper_id)

    return products


def _get_scraper_products_rest(scraper_id: str) -> list[dict]:
    """Legacy REST fallback (slow but reliable)."""
    _MAX_ZERO_STREAK = 50
    tag = _scrapper_tag(scraper_id)
    products = []
    path = "/products.json"
    params: Optional[dict] = {
        "limit": 250,
        "fields": "id,title,tags,variants,handle,created_at",
        "tag": tag,
    }
    next_url: Optional[str] = None
    zero_match_streak = 0

    while True:
        if next_url:
            data, link_header = _shopify_request_with_link("GET", next_url, params=None)
        else:
            data, link_header = _shopify_request_with_link("GET", path, params=params)

        raw_batch = data.get("products", [])
        if not raw_batch:
            break

        batch = [p for p in raw_batch if _has_scrapper_tag(p, scraper_id)]
        products.extend(batch)
        logger.info(f"[Shopify] REST page: {len(batch)} tagged (total {len(products)})")

        if batch:
            zero_match_streak = 0
        else:
            zero_match_streak += 1
            if zero_match_streak >= _MAX_ZERO_STREAK:
                break

        next_url = _parse_next_link(link_header)
        if not next_url:
            break

    return products


def get_scraper_products_summary(scraper_id: str) -> dict:
    """
    Fetch ALL Shopify products tagged RudraScrapper-{scraper_id} and return
    a dict keyed by handle with lightweight summary fields for store comparison.

    Returns: {handle: {title, handle, image_count, variant_count, price, tags, skus}}
    - tags: raw comma-separated tag string from Shopify (excluding the RudraScrapper- owner tag)
    - skus: deduplicated list of non-empty variant SKUs (used for SKU-aware cross-handle matching)
    """
    _MAX_ZERO_STREAK = 100
    tag = _scrapper_tag(scraper_id)
    result: dict[str, dict] = {}
    path = "/products.json"
    params: Optional[dict] = {
        "limit": 250,
        "fields": "id,title,handle,tags,variants,images",
        "tag": tag,
    }
    next_url: Optional[str] = None
    zero_match_streak = 0

    while True:
        if next_url:
            data, link_header = _shopify_request_with_link("GET", next_url, params=None)
        else:
            data, link_header = _shopify_request_with_link("GET", path, params=params)

        raw_batch = data.get("products", [])
        if not raw_batch:
            break

        batch = [p for p in raw_batch if _has_scrapper_tag(p, scraper_id)]

        if batch:
            zero_match_streak = 0
        else:
            zero_match_streak += 1
            if zero_match_streak >= _MAX_ZERO_STREAK:
                logger.info(
                    f"[Shopify] {zero_match_streak} consecutive pages with 0 matches for "
                    f"'{scraper_id}' summary — ?tag= filter ignored, scan complete."
                )
                break

        for p in batch:
            handle = (p.get("handle") or "").strip()
            if not handle:
                continue
            variants = p.get("variants", [])
            images = p.get("images", [])
            price = (variants[0].get("price") or "0.00") if variants else "0.00"

            # Collect variant SKUs (deduplicated, non-empty)
            skus: list[str] = []
            seen_skus: set[str] = set()
            for v in variants:
                sku = (v.get("sku") or "").strip()
                if sku and sku not in seen_skus:
                    skus.append(sku)
                    seen_skus.add(sku)

            # Strip the owner tag so tags comparison is not polluted by RudraScrapper-*
            raw_tags = p.get("tags", "") or ""
            display_tags = ", ".join(
                t.strip() for t in raw_tags.split(",")
                if t.strip() and not t.strip().lower().startswith("rudrascrapper-")
            )

            result[handle] = {
                "title": p.get("title", handle),
                "handle": handle,
                "image_count": len(images),
                "variant_count": len(variants),
                "price": price,
                "tags": display_tags,
                "skus": skus,
            }

        next_url = _parse_next_link(link_header)
        if not next_url:
            break

    return result


def upload_products(
    scraper_id: str,
    csv_path: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """
    Create new Shopify products from CSV.
    - Tags each with RudraScrapper-{scraper_id}
    - Skips products whose SKU or handle already exists in Shopify
    - Respects stop_event for clean cancellation
    Returns: {created, skipped, failed, errors}
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)
    total = len(products)

    # Pre-estimate total variants for quota observability
    estimated_variants = sum(len(p.get("variants", [])) for p in products)

    # Pre-scan existing Shopify products tagged for this scraper so we can
    # skip true duplicates without relying solely on 422 responses (which
    # only fire after the API call, too late when 2 workers run in parallel).
    # This prevents duplicates when an upload is triggered more than once.
    if progress_callback:
        progress_callback(8, f"Pre-scanning existing products…", 0,
                          {"created": 0, "skipped": 0, "failed": 0, "total": total,
                           "processed": 0, "estimated_variants": estimated_variants})
    existing_products = get_scraper_products(scraper_id)
    existing_skus: set[str] = set()
    existing_handles: set[str] = set()
    _part_re = re.compile(r"-part-\d+$")
    for ep in existing_products:
        for v in ep.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                existing_skus.add(sku)
        h = (ep.get("handle") or "").strip()
        if h:
            existing_handles.add(h)
            # Also index the normalised base handle so that Shopify auto-collision
            # suffixes (…-1, …-2) and CSV split suffixes (…-part-2) are matched.
            base = _base_handle(h)
            if base != h:
                existing_handles.add(base)
    logger.info(
        f"[Upload] Pre-scan complete | {len(existing_products)} existing products "
        f"({len(existing_skus)} SKUs, {len(existing_handles)} handles) | "
        f"{total} in CSV | ~{estimated_variants} variants"
    )

    # ── Dedup pre-flight: remove duplicate products already in Shopify ───────
    # Group by base-handle; if any group has >1 product, delete all but newest.
    # Uses the already-fetched existing_products list — no extra API call.
    _dupe_groups: dict[str, list] = {}
    for _ep in existing_products:
        _bh = _base_handle((_ep.get("handle") or "").strip())
        _dupe_groups.setdefault(_bh, []).append(_ep)

    _deleted_ids: set = set()
    _dupe_count = 0
    for _bh, _group in _dupe_groups.items():
        if len(_group) <= 1:
            continue
        _best = (
            max(_group, key=lambda p: p.get("created_at") or "")
            if any(p.get("created_at") for p in _group)
            else max(_group, key=lambda p: len(p.get("variants", [])))
        )
        for _dup in _group:
            if _dup["id"] != _best["id"]:
                try:
                    _shopify_request("DELETE", f"/products/{_dup['id']}.json")
                    _deleted_ids.add(_dup["id"])
                    _dupe_count += 1
                    logger.info(f"[Upload/Dedup] Deleted dup id={_dup['id']} handle={_dup.get('handle')}")
                except Exception as _de:
                    logger.warning(f"[Upload/Dedup] Failed to delete dup id={_dup['id']}: {_de}")

    if _dupe_count:
        logger.info(f"[Upload] Dedup pre-flight removed {_dupe_count} duplicate products before upload")
        # Refresh existing_skus / existing_handles — exclude the deleted IDs
        existing_skus = set()
        existing_handles = set()
        for _ep in existing_products:
            if _ep["id"] in _deleted_ids:
                continue
            for v in _ep.get("variants", []):
                _s = (v.get("sku") or "").strip()
                if _s:
                    existing_skus.add(_s)
            _h2 = (_ep.get("handle") or "").strip()
            if _h2:
                existing_handles.add(_h2)
                _b2 = _base_handle(_h2)
                if _b2 != _h2:
                    existing_handles.add(_b2)

    # Emit early estimate so frontend can display it before the first product finishes
    if progress_callback:
        progress_callback(
            8,
            f"Preparing upload — {total} products · ~{estimated_variants:,} variants",
            0,
            {"created": 0, "skipped": 0, "failed": 0, "total": total, "processed": 0, "estimated_variants": estimated_variants},
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    created = skipped = failed = 0
    image_link_failures = 0
    errors = []
    lock = threading.Lock()
    start_time = time.monotonic()
    _store_key = _cur_store_key()

    def process_product(product):
        nonlocal created, skipped, failed, image_link_failures
        _set_store_key(_store_key)

        if stop_event and stop_event.is_set():
            with lock:
                skipped += 1
            return

        if not product["sku"]:
            with lock:
                skipped += 1
            return

        with lock:
            sku_exists = product["sku"] in existing_skus
            _h = product.get("handle") or ""
            handle_exists = bool(_h) and (
                _h in existing_handles or _base_handle(_h) in existing_handles
            )

        if sku_exists or handle_exists:
            with lock:
                skipped += 1
            return

        try:
            payload = _build_shopify_payload(product, scraper_id)
            color_image_map = product.get("color_image_map") or {}
            # All images (including colour-hero) stay in the creation payload.
            # _link_color_images matches them by filename and uses PUT to attach
            # variant_ids — no duplicate image is ever created.  Keeping the hero
            # in the payload guarantees it is at position 1 from creation and is
            # never lost even if the variant-linking step times out.
            payload_gallery_urls: set[str] = set()
            for _img in payload["product"].get("images", []):
                _u2 = _clean_shopify_image_url(
                    _img.get("src", "") if isinstance(_img, dict) else _img
                )
                if _u2:
                    payload_gallery_urls.add(_u2)
            try:
                resp = _shopify_request("POST", "/products.json", payload=payload)
            except requests.exceptions.HTTPError as http_err:
                # 422 means the handle/SKU already exists — treat as a skip,
                # not a failure, so re-runs are fully idempotent.
                if http_err.response is not None and http_err.response.status_code == 422:
                    with lock:
                        skipped += 1
                        existing_skus.add(product["sku"])
                        if product.get("handle"):
                            existing_handles.add(product["handle"])
                    return
                raise
            shopify_prod = resp.get("product", {})
            new_pid = shopify_prod.get("id")
            variant_count = len(shopify_prod.get("variants", product.get("variants", [])))
            with lock:
                created += 1
                existing_skus.add(product["sku"])
                if product.get("handle"):
                    existing_handles.add(product["handle"])
                # Track session variant creation per store for quota observability
                sk = _store_key or "default"
                _session_variants_created[sk] = _session_variants_created.get(sk, 0) + variant_count
                if new_pid:
                    _try_register(
                        scraper_id, new_pid,
                        sku=product["sku"],
                        handle=product.get("handle"),
                        title=product["title"],
                    )
            # POST each colour's primary image linked to its variant IDs.
            # Capture the failure count so it surfaces in the upload summary.
            if new_pid and color_image_map:
                link_failures = _link_color_images(new_pid, shopify_prod, color_image_map)
                if link_failures:
                    with lock:
                        image_link_failures += link_failures
                    logger.warning(
                        f"[Upload] {link_failures} image-link failure(s) for "
                        f"{product['title']!r} (product_id={new_pid})"
                    )
            # POST any remaining images not covered by colour-linking or the
            # creation payload.  In practice this loop is nearly always a no-op
            # for colour products (gallery images were included in the payload),
            # but kept as a safety net for edge cases.
            #
            # IMPORTANT: apply _clean_shopify_image_url to every URL before
            # adding to linked_urls.  color_image_map values come from the CSV's
            # "Variant All Images" column (may still carry ?v= tokens), while
            # product["images"] / clean come from Image Src (already stripped).
            # Without normalisation the check `clean in linked_urls` is always
            # False and every image is re-posted as an unlinked duplicate.
            if new_pid:
                linked_urls: set = set()
                for _img_data in color_image_map.values():
                    if isinstance(_img_data, list):
                        linked_urls.update(
                            _clean_shopify_image_url(u) for u in _img_data
                        )
                    else:
                        linked_urls.add(_clean_shopify_image_url(_img_data))
                # Also skip images already embedded in the creation payload
                linked_urls.update(payload_gallery_urls)
                # Count all linked images to set the next gallery position
                pos = sum(
                    len(v) if isinstance(v, list) else 1
                    for v in color_image_map.values()
                ) + 1

                # Build the ordered list of gallery images to upload
                gallery_queue: list[str] = []
                for _extra_img in product.get("images", []):
                    _clean_g = _clean_shopify_image_url(_extra_img)
                    if _clean_g and _clean_g not in linked_urls:
                        gallery_queue.append(_clean_g)

                extra_count = 0
                if gallery_queue:
                    # For CDN images: pre-download all in parallel to avoid
                    # serial blocking — downloads don't consume Shopify API quota.
                    cdn_gallery = [u for u in gallery_queue if _is_shopify_cdn(u)]
                    non_cdn_gallery = [u for u in gallery_queue if not _is_shopify_cdn(u)]

                    # Parallel pre-fetch base64 for CDN images
                    import base64 as _b64mod
                    b64_cache: dict[str, Optional[str]] = {}
                    if cdn_gallery:
                        with ThreadPoolExecutor(max_workers=4) as _dl_pool:
                            _dl_futures = {
                                _dl_pool.submit(_fetch_image_as_base64, u): u
                                for u in cdn_gallery
                            }
                            for _dlf in as_completed(_dl_futures):
                                _url = _dl_futures[_dlf]
                                try:
                                    b64_cache[_url] = _dlf.result()
                                except Exception:
                                    b64_cache[_url] = None

                    for _clean_g in gallery_queue:
                        try:
                            if _is_shopify_cdn(_clean_g):
                                _b64 = b64_cache.get(_clean_g)
                                if _b64:
                                    _img_pl = {"attachment": _b64, "position": pos}
                                else:
                                    _img_pl = {"src": _clean_g, "position": pos}
                            else:
                                _img_pl = {"src": _clean_g, "position": pos}
                            _shopify_request(
                                "POST",
                                f"/products/{new_pid}/images.json",
                                payload={"image": _img_pl},
                            )
                            linked_urls.add(_clean_g)
                            pos += 1
                            extra_count += 1
                        except Exception as _img_err:
                            logger.warning(f"[ImageLink] Gallery image failed: {_img_err}")

                # For size-only products with >1 variant: link the hero image
                # to every variant so each size row shows a thumbnail.
                # Skip when there is only 1 variant — no benefit and saves API calls.
                if not color_image_map:
                    _raw_variants = product.get("variants") or []
                    if len(_raw_variants) > 1:
                        try:
                            _fp = _shopify_request(
                                "GET", f"/products/{new_pid}.json",
                                params={"fields": "id,images,variants"},
                            ).get("product", {})
                            _prod_imgs = _fp.get("images", [])
                            if _prod_imgs:
                                _hero_id = _prod_imgs[0]["id"]
                                for _var in _fp.get("variants", []):
                                    if not _var.get("image_id"):
                                        try:
                                            _shopify_request(
                                                "PUT",
                                                f"/variants/{_var['id']}.json",
                                                payload={"variant": {"id": _var["id"], "image_id": _hero_id}},
                                            )
                                        except Exception:
                                            pass
                        except Exception as _vlink_err:
                            logger.debug(f"[ImageLink] Variant hero-link failed for {new_pid}: {_vlink_err}")
        except Exception as e:
            with lock:
                failed += 1
                errors.append({"title": product["title"], "error": str(e)})
            logger.warning(f"[Upload] Failed to create {product['title']!r}: {e}")

    # 2 workers at 2 req/s: rate limiter serialises actual Shopify calls
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(process_product, p): p for p in products}
        for i, future in enumerate(as_completed(futures)):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                elapsed = time.monotonic() - start_time
                speed = round((created + skipped + failed) / max(elapsed, 1) * 60, 0)
                with lock:
                    c, sk, f, ilf = created, skipped, failed, image_link_failures
                pct = int(10 + ((i + 1) / total) * 85)
                resume_at = _daily_limit_resume_at.get(_cur_store_key())
                if resume_at:
                    msg = f"⏸ Daily variant limit — auto-resumes at {resume_at} | {c} new, {sk} skipped, {f} failed"
                else:
                    msg = f"Uploading {i+1}/{total} — {c} new, {sk} skipped, {f} failed ({int(speed)}/min)"
                progress_callback(
                    pct,
                    msg,
                    c,
                    {"created": c, "skipped": sk, "failed": f, "image_link_failures": ilf,
                     "total": total, "processed": i + 1, "estimated_variants": estimated_variants},
                )

    if progress_callback:
        progress_callback(
            100,
            f"Upload complete — {created} created, {skipped} skipped, {failed} failed"
            + (f", {image_link_failures} image-link failure(s)" if image_link_failures else ""),
            created,
            {"created": created, "skipped": skipped, "failed": failed, "image_link_failures": image_link_failures, "total": total, "processed": total},
        )

    result = {"created": created, "skipped": skipped, "failed": failed, "image_link_failures": image_link_failures, "errors": errors[:20]}
    _try_log(
        scraper_id, "upload",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"CSV: {csv_path} | {total} products in CSV | stopped={stop_event.is_set() if stop_event else False}",
    )
    return result


def update_products(
    scraper_id: str,
    csv_path: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
    skip_images: bool = False,
    add_tag_if_missing: bool = False,
) -> dict:
    """
    Update title/body/price/images for existing Shopify products matched by SKU.
    Safety: by default only touches products carrying the RudraScrapper-{scraper_id} tag.
    Returns: {updated, skipped, failed, errors}
    
    skip_images: when True, only title/body/vendor/type/tags/prices are updated;
    image gallery and variant linking are left unchanged. This makes bulk updates
    much faster; use the update-images endpoint when images need refreshing.
    
    add_tag_if_missing: when True, a matched product that is missing the scraper
    tag will still be updated (the PUT payload includes the tag, so it is added)
    instead of being skipped for safety.
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)
    total = len(products)

    logger.info(f"[Update] Fetching existing products for {scraper_id}…")
    existing = get_scraper_products(scraper_id)

    sku_map: dict[str, dict] = {}
    for p in existing:
        if not _has_scrapper_tag(p, scraper_id):
            continue
        for v in p.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                sku_map[sku] = {"product_id": p["id"], "variant_id": v["id"]}

    updated = skipped = failed = 0
    image_link_failures = 0
    errors = []
    lock = threading.Lock()
    _store_key = _cur_store_key()

    def process_update(product):
        nonlocal updated, skipped, failed, image_link_failures
        _set_store_key(_store_key)

        if stop_event and stop_event.is_set():
            with lock:
                skipped += 1
            return

        sku = product["sku"]
        if not sku or sku not in sku_map:
            with lock:
                skipped += 1
            return

        pid = sku_map[sku]["product_id"]
        vid = sku_map[sku]["variant_id"]
        color_image_map = product.get("color_image_map") or {}

        try:
            # Fetch full product — needed for the tag safety check, the current
            # image IDs (so we can delete them before re-linking), and the
            # variant IDs that _link_color_images needs.
            full_resp = _shopify_request(
                "GET", f"/products/{pid}.json",
                params={"fields": "id,tags,images,variants"},
            )
            shopify_prod = full_resp.get("product", {})
            if not _has_scrapper_tag(shopify_prod, scraper_id) and not add_tag_if_missing:
                logger.warning(f"[Update] SAFETY: {pid} missing tag, skip.")
                with lock:
                    skipped += 1
                return

            # Build PUT payload. For colour-variant products (SKIMS etc.) the
            # images key is intentionally omitted here — we will delete all
            # existing gallery images and then POST each colour's images with
            # variant_ids after the PUT, so Shopify knows which photo to show
            # when a colour swatch is selected.
            product_payload: dict = {
                "id": pid,
                "title": product["title"],
                "body_html": product["body_html"],
                "vendor": product.get("vendor", ""),
                "product_type": product["product_type"],
                "tags": product.get("tags", ""),
            }
            if not color_image_map:
                raw_imgs_u = [img for img in product["images"] if img]
                all_cdn_u = bool(raw_imgs_u) and all(_is_shopify_cdn(i) for i in raw_imgs_u)
                if all_cdn_u:
                    # Hero only in PUT — remaining CDN images posted as base64 below
                    product_payload["images"] = (
                        [{"src": _clean_shopify_image_url(raw_imgs_u[0])}] if raw_imgs_u else []
                    )
                else:
                    product_payload["images"] = [
                        {"src": _clean_shopify_image_url(img)}
                        for img in raw_imgs_u
                    ]
            _shopify_request("PUT", f"/products/{pid}.json", payload={"product": product_payload})

            # For CDN-image products: POST the gallery images that weren't in
            # the PUT payload (i.e. everything after the hero) as base64
            # attachments, and link the hero image to every size variant.
            if not skip_images and not color_image_map:
                raw_imgs_u2 = [img for img in product["images"] if img]
                all_cdn_u2 = bool(raw_imgs_u2) and all(_is_shopify_cdn(i) for i in raw_imgs_u2)
                if all_cdn_u2 and len(raw_imgs_u2) > 1:
                    # Fetch current state to know which images already exist
                    try:
                        _cur = _shopify_request(
                            "GET", f"/products/{pid}.json",
                            params={"fields": "id,images,variants"},
                        ).get("product", {})
                        _existing_names = {
                            _img_basename(im.get("src", ""))
                            for im in _cur.get("images", [])
                        }
                        _pos = len(_cur.get("images", [])) + 1
                        for _extra_url in raw_imgs_u2[1:]:
                            _clean_u = _clean_shopify_image_url(_extra_url)
                            if not _clean_u or _img_basename(_clean_u) in _existing_names:
                                continue
                            if _post_image_b64(pid, _clean_u, _pos):
                                _existing_names.add(_img_basename(_clean_u))
                                _pos += 1
                        # Re-fetch to get final image list for variant linking
                        _cur2 = _shopify_request(
                            "GET", f"/products/{pid}.json",
                            params={"fields": "id,images,variants"},
                        ).get("product", {})
                        _prod_imgs2 = _cur2.get("images", [])
                        if _prod_imgs2:
                            _hero_id2 = _prod_imgs2[0]["id"]
                            for _var2 in _cur2.get("variants", []):
                                if not _var2.get("image_id"):
                                    try:
                                        _shopify_request(
                                            "PUT",
                                            f"/variants/{_var2['id']}.json",
                                            payload={"variant": {"id": _var2["id"], "image_id": _hero_id2}},
                                        )
                                    except Exception:
                                        pass
                    except Exception as _cdn_err:
                        logger.warning(f"[Update] CDN image repair failed for {pid}: {_cdn_err}")

            var_payload = {
                "variant": {
                    "id": vid,
                    "price": product["price"],
                    "compare_at_price": product["compare_at_price"] or None,
                }
            }
            _shopify_request("PUT", f"/variants/{vid}.json", payload=var_payload)

            # For colour-variant products: delete the existing flat gallery so
            # we start clean, then POST each colour's images with variant_ids
            # set — exactly the same path the Upload operation takes.
            if not skip_images and color_image_map:
                for img in shopify_prod.get("images", []):
                    img_id = img.get("id")
                    if img_id:
                        try:
                            _shopify_request("DELETE", f"/products/{pid}/images/{img_id}.json")
                        except Exception as del_err:
                            logger.warning(f"[Update] Could not delete image {img_id} on product {pid}: {del_err}")
                # Pass product with images cleared: all images were just deleted,
                # so existing_by_basename in _link_color_images must be empty to
                # force POST (not PUT against stale/deleted image IDs).
                link_failures = _link_color_images(pid, {**shopify_prod, "images": []}, color_image_map)
                if link_failures:
                    with lock:
                        image_link_failures += link_failures
                    logger.warning(
                        f"[Update] {link_failures} image-link failure(s) for "
                        f"{product['title']!r} (product_id={pid})"
                    )

            with lock:
                updated += 1
        except Exception as e:
            with lock:
                failed += 1
                errors.append({"title": product["title"], "error": str(e)})
            logger.warning(f"[Update] Failed to update {product['title']!r}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_update, p): p for p in products}
        for i, future in enumerate(as_completed(futures)):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                with lock:
                    u, sk, f = updated, skipped, failed
                pct = int(10 + ((i + 1) / total) * 85)
                progress_callback(
                    pct,
                    f"Updating {i+1}/{total} — {u} updated, {sk} skipped, {f} failed",
                    u,
                    {"updated": u, "skipped": sk, "failed": f, "total": total, "processed": i + 1},
                )

    if progress_callback:
        progress_callback(
            100,
            f"Update complete — {updated} updated, {skipped} skipped, {failed} failed"
            + (f", {image_link_failures} image-link failure(s)" if image_link_failures else ""),
            updated,
            {
                "updated": updated, "skipped": skipped, "failed": failed,
                "image_link_failures": image_link_failures,
                "total": total, "processed": total,
            },
        )

    result = {
        "updated": updated, "skipped": skipped, "failed": failed,
        "image_link_failures": image_link_failures,
        "errors": errors[:20],
    }
    _try_log(
        scraper_id, "update",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"CSV: {csv_path} | {total} products in CSV | stopped={stop_event.is_set() if stop_event else False}",
    )
    return result


def reimage_products(
    scraper_id: str,
    csv_path: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """
    Re-link all colour variant images for existing Shopify products without
    re-uploading them.

    For every product tagged RudraScrapper-{scraper_id}:
      1. Match to a CSV product by SKU (primary) or handle (fallback)
      2. Skip products with no color_image_map (size-only, no per-colour images)
      3. Delete all existing gallery images from the Shopify product
      4. Re-POST every colour image with variant_ids set, using base64 attachment
         for cross-store Shopify CDN URLs (bypasses async import failures)

    Use this to fix products that were uploaded before the base64 CDN fix, or
    that have a broken/missing image gallery due to the linked_urls URL-mismatch bug.

    Returns: {reimaged, skipped, failed, image_link_failures, errors}
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)

    logger.info(f"[Reimage] Loaded {len(products)} products from CSV for {scraper_id}…")

    # Build lookup maps from CSV: SKU → product, handle → product
    sku_to_product: dict[str, dict] = {}
    handle_to_product: dict[str, dict] = {}
    for p in products:
        if p.get("sku"):
            sku_to_product[p["sku"]] = p
        if p.get("handle"):
            handle_to_product[p["handle"]] = p

    existing = get_scraper_products(scraper_id)
    total = len(existing)
    logger.info(f"[Reimage] Found {total} tagged products in Shopify for {scraper_id}")

    reimaged = skipped = failed = 0
    image_link_failures = 0
    errors = []
    lock = threading.Lock()
    start_time = time.monotonic()
    _store_key = _cur_store_key()

    def process_reimage(shopify_prod):
        nonlocal reimaged, skipped, failed, image_link_failures
        _set_store_key(_store_key)

        if stop_event and stop_event.is_set():
            with lock:
                skipped += 1
            return

        if not _has_scrapper_tag(shopify_prod, scraper_id):
            with lock:
                skipped += 1
            return

        pid = shopify_prod["id"]
        shopify_handle = (shopify_prod.get("handle") or "").strip()

        # Match CSV product by SKU (any variant) then by handle
        csv_product = None
        for v in shopify_prod.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku and sku in sku_to_product:
                csv_product = sku_to_product[sku]
                break
        if csv_product is None and shopify_handle in handle_to_product:
            csv_product = handle_to_product[shopify_handle]

        if csv_product is None:
            logger.warning(
                f"[Reimage] No CSV match for {shopify_prod.get('title')!r} (id={pid}), skipping."
            )
            with lock:
                skipped += 1
            return

        color_image_map = csv_product.get("color_image_map") or {}
        if not color_image_map:
            # Size-only product — images were sent in the initial payload; nothing to re-link
            with lock:
                skipped += 1
            return

        try:
            # Fetch full product for current image IDs and exact variant data
            full_resp = _shopify_request(
                "GET", f"/products/{pid}.json",
                params={"fields": "id,tags,images,variants"},
            )
            full_prod = full_resp.get("product", {})

            # Safety re-check
            if not _has_scrapper_tag(full_prod, scraper_id):
                logger.warning(f"[Reimage] SAFETY: {pid} missing tag, skip.")
                with lock:
                    skipped += 1
                return

            # Delete all existing images so we start clean
            for img in full_prod.get("images", []):
                img_id = img.get("id")
                if img_id:
                    try:
                        _shopify_request("DELETE", f"/products/{pid}/images/{img_id}.json")
                    except Exception as del_err:
                        logger.warning(
                            f"[Reimage] Could not delete image {img_id} on product {pid}: {del_err}"
                        )

            # Re-POST all colour images with variant_ids (base64 for cdn.shopify.com).
            # Pass empty images list: all were just deleted, so existing_by_basename
            # must be empty to force POST (not PUT against deleted image IDs).
            link_failures = _link_color_images(pid, {**full_prod, "images": []}, color_image_map)
            if link_failures:
                with lock:
                    image_link_failures += link_failures
                logger.warning(
                    f"[Reimage] {link_failures} image-link failure(s) for "
                    f"{csv_product['title']!r} (product_id={pid})"
                )

            with lock:
                reimaged += 1
        except Exception as e:
            with lock:
                failed += 1
                errors.append({"title": shopify_prod.get("title", str(pid)), "error": str(e)})
            logger.warning(f"[Reimage] Failed for {shopify_prod.get('title')!r}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_reimage, p): p for p in existing}
        done = 0
        for future in as_completed(futures):
            done += 1
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                pct = int(5 + (done / max(total, 1)) * 90)
                elapsed = time.monotonic() - start_time
                rate = int(reimaged / max(elapsed / 60, 0.01))
                with lock:
                    _r, _s, _f = reimaged, skipped, failed
                counts = {
                    "reimaged": _r, "skipped": _s, "failed": _f,
                    "total": total, "processed": done,
                }
                progress_callback(
                    pct,
                    f"Re-linking images {done}/{total} — {_r} reimaged, {_s} skipped, {_f} failed ({rate}/min)",
                    _r,
                    counts,
                )

    result = {
        "reimaged":             reimaged,
        "skipped":              skipped,
        "failed":               failed,
        "image_link_failures":  image_link_failures,
        "errors":               errors[:20],
    }
    _try_log(
        scraper_id, "reimage",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"CSV: {csv_path} | {total} Shopify products | {reimaged} reimaged",
    )
    return result


def tag_repair_products(scraper_id: str, csv_path: str, progress_callback=None) -> dict:
    """
    SKU-based tag repair: scans every product on the Shopify store, matches by variant
    SKU against our CSV, and adds RudraScrapper-{scraper_id} to any that are missing it.

    Why SKU-based and not handle-based: handle formats can differ between scraper versions
    (e.g. old scraper used 'clifton-10', current uses 'hoka-clifton-10-men').  SKUs are
    stable product identifiers set by the brand and never change.

    Strategy:
      1. Build a lookup set of all variant SKUs from our CSV.
      2. Page through ALL Shopify products (no tag filter — we need to find untagged ones).
      3. For each Shopify product whose variant SKU appears in our CSV set:
           - if already tagged → count it
           - if not tagged    → PUT to add the tag
      4. Stop when pages are exhausted.

    Returns: {tagged, already_tagged, not_found, failed, total_csv, pages_scanned}
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)
    total = len(products)

    # Build CSV SKU set (all variant SKUs across all product groups)
    csv_skus: set[str] = set()
    for p in products:
        sku = (p.get("sku") or "").strip()
        if sku:
            csv_skus.add(sku)
        for v in p.get("variants", []):
            vsku = (v.get("sku") or "").strip()
            if vsku:
                csv_skus.add(vsku)

    logger.info(
        f"[TagRepair] Starting SKU scan for {scraper_id} — "
        f"{total} CSV products, {len(csv_skus)} unique SKUs"
    )
    if progress_callback:
        progress_callback(0, f"Tag repair: scanning Shopify for {len(csv_skus)} SKUs…", 0)

    tagged = 0
    already_tagged = 0
    not_found_skus = set(csv_skus)   # SKUs not yet found on Shopify
    failed = 0
    pages_scanned = 0
    _store_key = _cur_store_key()

    path = "/products.json"
    params: dict = {
        "limit": 250,
        "fields": "id,title,handle,tags,variants",
    }
    next_url: Optional[str] = None

    while True:
        _set_store_key(_store_key)
        if next_url:
            data, link_header = _shopify_request_with_link("GET", next_url, params=None)
        else:
            data, link_header = _shopify_request_with_link("GET", path, params=params)

        raw_batch = data.get("products", [])
        if not raw_batch:
            break  # exhausted all pages

        pages_scanned += 1

        for shopify_prod in raw_batch:
            pid = shopify_prod["id"]
            shopify_skus = {
                (v.get("sku") or "").strip()
                for v in shopify_prod.get("variants", [])
                if (v.get("sku") or "").strip()
            }
            matched_skus = shopify_skus & csv_skus
            if not matched_skus:
                continue

            # This Shopify product belongs to our scraper
            not_found_skus -= matched_skus
            try:
                if _has_scrapper_tag(shopify_prod, scraper_id):
                    already_tagged += 1
                    logger.debug(
                        f"[TagRepair] Already tagged: {shopify_prod.get('handle')} "
                        f"(SKU match: {next(iter(matched_skus))})"
                    )
                else:
                    new_tags = _add_scrapper_tag(shopify_prod.get("tags", "") or "", scraper_id)
                    _set_store_key(_store_key)
                    _shopify_request(
                        "PUT", f"/products/{pid}.json",
                        payload={"product": {"id": pid, "tags": new_tags}},
                    )
                    tagged += 1
                    logger.info(
                        f"[TagRepair] Tagged '{shopify_prod.get('handle')}' ({pid}) "
                        f"via SKU {next(iter(matched_skus))}"
                    )
            except Exception as e:
                failed += 1
                logger.error(f"[TagRepair] Failed to tag {pid}: {e}")

        logger.info(
            f"[TagRepair] Page {pages_scanned}: tagged={tagged}, "
            f"already_tagged={already_tagged}, remaining_skus={len(not_found_skus)}"
        )
        if progress_callback:
            progress_callback(
                min(95, pages_scanned * 5),
                f"Tag repair page {pages_scanned}: {tagged} tagged, {already_tagged} OK, "
                f"{len(not_found_skus)} SKUs still not found…",
                tagged,
            )

        # Stop early if we've accounted for all our SKUs
        if not not_found_skus:
            logger.info("[TagRepair] All CSV SKUs found — stopping early.")
            break

        next_url = _parse_next_link(link_header)
        if not next_url:
            break

    not_found_count = len(not_found_skus)
    logger.info(
        f"[TagRepair] Done — tagged={tagged}, already_tagged={already_tagged}, "
        f"not_found_skus={not_found_count}, failed={failed}, pages={pages_scanned}"
    )
    if progress_callback:
        progress_callback(
            100,
            f"Tag repair complete: {tagged} newly tagged, {already_tagged} already OK, "
            f"{not_found_count} SKUs not on Shopify, {pages_scanned} pages scanned",
            tagged,
        )
    return {
        "tagged": tagged,
        "already_tagged": already_tagged,
        "not_found": not_found_count,
        "failed": failed,
        "total_csv": total,
        "pages_scanned": pages_scanned,
    }


def check_oos_products(scraper_id: str, csv_path: str) -> dict:
    """
    Returns Shopify products (tagged RudraScrapper-{scraper_id}) absent from the current CSV.
    Returns: {oos: [{id, title, handle}], total_shopify, total_csv}
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)
    csv_skus = {p["sku"] for p in products if p["sku"]}
    csv_handles = {p["handle"] for p in products if p["handle"]}

    logger.info(f"[CheckOOS] CSV has {len(csv_skus)} SKUs, {len(csv_handles)} handles")

    existing = get_scraper_products(scraper_id)
    oos = []

    for p in existing:
        if not _has_scrapper_tag(p, scraper_id):
            continue
        shopify_skus = {(v.get("sku") or "").strip() for v in p.get("variants", [])}
        shopify_handle = (p.get("handle") or "").strip()

        in_csv_by_sku = bool(shopify_skus & csv_skus)
        in_csv_by_handle = shopify_handle and shopify_handle in csv_handles

        if not in_csv_by_sku and not in_csv_by_handle:
            oos.append({
                "id":     p["id"],
                "title":  p.get("title", ""),
                "handle": shopify_handle,
            })

    return {
        "oos":           oos,
        "total_shopify": len(existing),
        "total_csv":     len(products),
    }


def delete_oos_products(
    scraper_id: str,
    csv_path: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """
    Delete only OOS products (those absent from the current CSV).
    Dual-safety guard: tag check + local DB registry before every delete.
    Returns: {deleted, skipped, failed, errors}
    """
    oos_result = check_oos_products(scraper_id, csv_path)
    oos_list = oos_result["oos"]
    total = len(oos_list)

    if total == 0:
        return {"deleted": 0, "skipped": 0, "failed": 0, "errors": [], "oos_count": 0}

    deleted = skipped = failed = 0
    errors = []
    lock = threading.Lock()
    _store_key = _cur_store_key()

    def process_delete(item):
        nonlocal deleted, skipped, failed
        _set_store_key(_store_key)

        if stop_event and stop_event.is_set():
            with lock:
                skipped += 1
            return

        pid = item["id"]
        try:
            current = _shopify_request("GET", f"/products/{pid}.json", params={"fields": "id,tags,title"})
            current_product = current.get("product", {})
            if not _has_scrapper_tag(current_product, scraper_id):
                logger.warning(f"[Delete] SAFETY ABORT: {pid} ({item['title']!r}) missing tag. Skipping.")
                with lock:
                    skipped += 1
                return

            with lock:
                owns = _try_verify_ownership(str(pid), scraper_id)
            if not owns:
                logger.warning(f"[Delete] SAFETY ABORT (DB): {pid} ({item['title']!r}) not in local registry. Skipping.")
                with lock:
                    skipped += 1
                return

            _shopify_request("DELETE", f"/products/{pid}.json")
            with lock:
                _try_remove(str(pid))
                deleted += 1
            logger.info(f"[Delete] Deleted {pid} ({item['title']!r})")
        except Exception as e:
            with lock:
                failed += 1
                errors.append({"id": pid, "title": item["title"], "error": str(e)})
            logger.warning(f"[Delete] Failed to delete {pid}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_delete, item): item for item in oos_list}
        for i, future in enumerate(as_completed(futures)):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                with lock:
                    d, sk, f = deleted, skipped, failed
                pct = int(10 + ((i + 1) / total) * 85)
                progress_callback(
                    pct,
                    f"Deleting {i+1}/{total} — {d} deleted, {sk} skipped, {f} failed",
                    d,
                    {"deleted": d, "skipped": sk, "failed": f, "total": total, "processed": i + 1},
                )

    if progress_callback:
        progress_callback(
            100,
            f"Delete OOS complete — {deleted} deleted, {skipped} skipped, {failed} failed",
            deleted,
            {"deleted": deleted, "skipped": skipped, "failed": failed, "total": total, "processed": total},
        )

    result = {
        "deleted":   deleted,
        "skipped":   skipped,
        "failed":    failed,
        "errors":    errors[:20],
        "oos_count": total,
    }
    _try_log(
        scraper_id, "delete_oos",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"{total} OOS candidates | CSV: {csv_path} | stopped={stop_event.is_set() if stop_event else False}",
    )
    return result


def delete_all_shopify_products(
    scraper_id: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """
    Delete ALL products carrying RudraScrapper-{scraper_id} tag from Shopify.
    Safety guard: tag check before every delete.
    Returns: {deleted, skipped, failed, errors}
    """
    logger.info(f"[Nuke] Fetching all Shopify products for {scraper_id}…")
    existing = get_scraper_products(scraper_id)
    total = len(existing)

    if total == 0:
        return {"deleted": 0, "skipped": 0, "failed": 0, "errors": [], "total": 0}

    deleted = skipped = failed = 0
    errors = []
    lock = threading.Lock()
    _store_key = _cur_store_key()

    def process_nuke(item):
        nonlocal deleted, skipped, failed
        _set_store_key(_store_key)

        if stop_event and stop_event.is_set():
            with lock:
                skipped += 1
            return

        pid = item["id"]
        title = item.get("title", f"Product {pid}")
        try:
            if not _has_scrapper_tag(item, scraper_id):
                with lock:
                    skipped += 1
                return

            _shopify_request("DELETE", f"/products/{pid}.json")
            with lock:
                _try_remove(str(pid))
                deleted += 1
            logger.info(f"[Nuke] Deleted {pid} ({title})")
        except Exception as e:
            err_str = str(e)
            # 404 = product already gone from a prior partial nuke — treat as success
            if "404" in err_str or "Not Found" in err_str:
                with lock:
                    _try_remove(str(pid))
                    deleted += 1
                logger.info(f"[Nuke] {pid} already gone (404) — counted as deleted")
            else:
                with lock:
                    failed += 1
                    errors.append({"id": pid, "title": title, "error": err_str})
                logger.warning(f"[Nuke] Failed to delete {pid}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_nuke, item): item for item in existing}
        for i, future in enumerate(as_completed(futures)):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                with lock:
                    d, sk, f = deleted, skipped, failed
                pct = int(10 + ((i + 1) / total) * 85)
                progress_callback(
                    pct,
                    f"Deleting {i+1}/{total} — {d} deleted, {sk} skipped, {f} failed",
                    d,
                    {"deleted": d, "skipped": sk, "failed": f, "total": total, "processed": i + 1},
                )

    if progress_callback:
        progress_callback(
            100,
            f"Delete All complete — {deleted} deleted, {skipped} skipped, {failed} failed",
            deleted,
            {"deleted": deleted, "skipped": skipped, "failed": failed, "total": total, "processed": total},
        )

    result = {"deleted": deleted, "skipped": skipped, "failed": failed, "errors": errors[:20], "total": total}
    _try_log(
        scraper_id, "nuke_store",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"stopped={stop_event.is_set() if stop_event else False}",
    )
    return result


def deduplicate_products(
    scraper_id: str,
    progress_callback=None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """
    Sweep Shopify for duplicate products caused by repeated uploads or Shopify
    auto-collision suffixes (handle-1, handle-2 …).

    Algorithm:
      1. Fetch all products tagged RudraScrapper-{scraper_id}.
      2. Group by _base_handle() — normalises Shopify auto-suffixes and -part-N.
      3. For groups with >1 product, keep the one with the most variants
         (most complete); delete the rest.

    Returns: {deleted, kept, failed, groups_with_dupes, errors}
    """
    if progress_callback:
        progress_callback(5, "Dedup: Fetching existing products…", 0,
                          {"deleted": 0, "kept": 0, "failed": 0, "total": 0, "processed": 0})

    logger.info(f"[Dedup] Scanning Shopify for duplicate products (scraper={scraper_id})…")
    existing = get_scraper_products(scraper_id)
    total_fetched = len(existing)

    if total_fetched == 0:
        if progress_callback:
            progress_callback(100, "Dedup: No products found.", 0,
                              {"deleted": 0, "kept": 0, "failed": 0, "total": 0, "processed": 0})
        return {"deleted": 0, "kept": 0, "failed": 0, "groups_with_dupes": 0, "errors": []}

    # Group by normalised base handle
    groups: dict[str, list[dict]] = {}
    for p in existing:
        h = (p.get("handle") or "").strip()
        key = _base_handle(h) if h else str(p.get("id", ""))
        groups.setdefault(key, []).append(p)

    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    logger.info(
        f"[Dedup] {total_fetched} products → {len(groups)} base handles → "
        f"{len(dupe_groups)} groups with duplicates"
    )

    if not dupe_groups:
        if progress_callback:
            progress_callback(100, f"Dedup: No duplicates found among {total_fetched} products.", 0,
                              {"deleted": 0, "kept": 0, "failed": 0,
                               "total": total_fetched, "processed": total_fetched})
        _try_log(scraper_id, "dedup", status="success",
                 result={"deleted": 0, "kept": total_fetched, "failed": 0, "groups_with_dupes": 0},
                 notes=f"{total_fetched} products scanned, no duplicates")
        return {"deleted": 0, "kept": total_fetched, "failed": 0, "groups_with_dupes": 0, "errors": []}

    # Build list of product IDs to delete (all but best-per-group)
    to_delete: list[dict] = []
    kept = 0
    for key, products_in_group in dupe_groups.items():
        # Keep the NEWEST product (latest created_at) — it is the authoritative copy.
        # Fall back to most-variants if created_at is absent (very old API responses).
        def _sort_key(p: dict):
            ts = p.get("created_at") or ""
            return ts  # ISO-8601 strings sort correctly lexicographically

        best = max(products_in_group, key=_sort_key) if any(
            p.get("created_at") for p in products_in_group
        ) else max(products_in_group, key=lambda p: len(p.get("variants", [])))
        kept += 1
        for p in products_in_group:
            if p["id"] != best["id"]:
                to_delete.append(p)

    # Products in non-duplicate groups are also kept
    kept += sum(1 for k, v in groups.items() if len(v) == 1)

    total_to_delete = len(to_delete)
    logger.info(
        f"[Dedup] Will delete {total_to_delete} duplicate products across "
        f"{len(dupe_groups)} groups, keeping {kept} unique products."
    )

    if progress_callback:
        progress_callback(15, f"Dedup: Found {total_to_delete} duplicates in {len(dupe_groups)} groups — deleting…",
                          0, {"deleted": 0, "kept": kept, "failed": 0,
                              "total": total_to_delete, "processed": 0})

    deleted = failed = 0
    errors: list[dict] = []
    lock = threading.Lock()
    _store_key = _cur_store_key()

    def _delete_dupe(item: dict):
        nonlocal deleted, failed
        _set_store_key(_store_key)
        if stop_event and stop_event.is_set():
            return
        pid = item["id"]
        title = item.get("title", f"Product {pid}")
        handle = item.get("handle", "")
        try:
            if not _has_scrapper_tag(item, scraper_id):
                logger.warning(f"[Dedup] SAFETY: product {pid} ({handle!r}) missing tag — skip delete")
                with lock:
                    failed += 1
                    errors.append({"id": pid, "title": title, "error": "Missing RudraScrapper tag — safety skip"})
                return
            _shopify_request("DELETE", f"/products/{pid}.json")
            _try_remove(str(pid))
            with lock:
                deleted += 1
            logger.info(f"[Dedup] Deleted duplicate pid={pid} handle={handle!r} title={title!r}")
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "Not Found" in err_str:
                _try_remove(str(pid))
                with lock:
                    deleted += 1
                logger.info(f"[Dedup] {pid} already gone (404) — counted as deleted")
            else:
                with lock:
                    failed += 1
                    errors.append({"id": pid, "title": title, "error": err_str})
                logger.warning(f"[Dedup] Failed to delete duplicate {pid}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(_delete_dupe, item): item for item in to_delete}
        for i, future in enumerate(as_completed(futures)):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if progress_callback:
                with lock:
                    d, f_ = deleted, failed
                pct = int(15 + ((i + 1) / max(total_to_delete, 1)) * 80)
                progress_callback(
                    pct,
                    f"Dedup: Deleting {i+1}/{total_to_delete} — {d} removed, {f_} failed",
                    d,
                    {"deleted": d, "kept": kept, "failed": f_,
                     "total": total_to_delete, "processed": i + 1},
                )

    if progress_callback:
        progress_callback(
            100,
            f"Dedup complete — {deleted} duplicates removed, {kept} unique products kept, {failed} failed",
            deleted,
            {"deleted": deleted, "kept": kept, "failed": failed,
             "total": total_to_delete, "processed": total_to_delete},
        )

    result = {
        "deleted": deleted,
        "kept": kept,
        "failed": failed,
        "groups_with_dupes": len(dupe_groups),
        "errors": errors[:20],
    }
    _try_log(
        scraper_id, "dedup",
        status="success" if failed == 0 else "partial",
        result=result,
        notes=f"{total_fetched} scanned | {len(dupe_groups)} dupe groups | {total_to_delete} to delete | stopped={stop_event.is_set() if stop_event else False}",
    )
    return result


def fix_single_product_images(scraper_id: str, shopify_product_id: int, csv_path: str) -> dict:
    """
    Re-link colour variant images for ONE specific Shopify product.
    Fetches the product, deletes its current images, re-uploads from CSV color_image_map.
    Returns: {ok, reimaged, image_link_failures, product_id, title}
    """
    rows = _load_csv(csv_path)
    products = _group_csv_rows(rows)

    sku_to_product: dict = {}
    handle_to_product: dict = {}
    for prod in products:
        handle = prod.get("handle", "")
        if handle:
            handle_to_product[handle] = prod
        for v in prod.get("variants", []):
            sku = (v.get("variant_sku") or v.get("Variant SKU") or "").strip()
            if sku:
                sku_to_product[sku] = prod

    full_resp = _shopify_request(
        "GET", f"/products/{shopify_product_id}.json",
        params={"fields": "id,title,handle,tags,images,variants"},
    )
    full_prod = full_resp.get("product", {})
    if not full_prod:
        raise ValueError(f"Product {shopify_product_id} not found in Shopify")

    if not _has_scrapper_tag(full_prod, scraper_id):
        raise ValueError(f"Product {shopify_product_id} is not tagged for scraper '{scraper_id}' — safety check failed")

    pid = full_prod["id"]
    shopify_handle = (full_prod.get("handle") or "").strip()
    title = full_prod.get("title", "")

    csv_product = None
    for v in full_prod.get("variants", []):
        sku = (v.get("sku") or "").strip()
        if sku and sku in sku_to_product:
            csv_product = sku_to_product[sku]
            break
    if csv_product is None and shopify_handle in handle_to_product:
        csv_product = handle_to_product[shopify_handle]

    if csv_product is None:
        raise ValueError(f"No CSV match for Shopify product {shopify_product_id} (handle={shopify_handle!r})")

    color_image_map = csv_product.get("color_image_map") or {}
    if not color_image_map:
        return {
            "ok": True, "reimaged": False, "image_link_failures": 0,
            "product_id": pid, "title": title,
            "message": "Size-only product — no colour images to re-link",
        }

    for img in full_prod.get("images", []):
        img_id = img.get("id")
        if img_id:
            try:
                _shopify_request("DELETE", f"/products/{pid}/images/{img_id}.json")
            except Exception as del_err:
                logger.warning(f"[FixProduct] Could not delete image {img_id} on {pid}: {del_err}")

    # Pass empty images list: all were just deleted, so existing_by_basename
    # must be empty to force POST (not PUT against deleted image IDs).
    link_failures = _link_color_images(pid, {**full_prod, "images": []}, color_image_map)
    logger.info(f"[FixProduct] {title!r} (id={pid}): reimaged, {link_failures} link failure(s)")

    return {
        "ok": link_failures == 0,
        "reimaged": True,
        "image_link_failures": link_failures,
        "product_id": pid,
        "title": title,
    }


def check_variant_images(scraper_id: str) -> dict:
    """
    Audit Shopify products for broken variant image links.
    Fetches every product tagged RudraScrapper-{scraper_id} and checks:
      - product has at least one image
      - colour variants (Option1 Name contains color/colour/shade) have image_id set

    Returns a summary dict:
      {total, no_images_count, missing_variant_img_count, products_with_issues: [...]}
    """
    tag = _scrapper_tag(scraper_id)
    products_raw = []
    path = "/products.json"
    params: Optional[dict] = {
        "limit": 250,
        "fields": "id,title,handle,tags,images,variants",
        "tag": tag,
    }
    next_url: Optional[str] = None

    zero_match_streak = 0
    _MAX_ZERO_STREAK = 100
    while True:
        if next_url:
            data, link_header = _shopify_request_with_link("GET", next_url, params=None)
        else:
            data, link_header = _shopify_request_with_link("GET", path, params=params)
        raw_batch = data.get("products", [])
        if not raw_batch:
            break
        batch = [p for p in raw_batch if _has_scrapper_tag(p, scraper_id)]
        products_raw.extend(batch)
        if batch:
            zero_match_streak = 0
        else:
            zero_match_streak += 1
            if zero_match_streak >= _MAX_ZERO_STREAK:
                logger.info(
                    f"[Shopify] {zero_match_streak} consecutive pages with 0 matches for "
                    f"'{scraper_id}' variant-image check — scan complete."
                )
                break
        next_url = _parse_next_link(link_header)
        if not next_url:
            break

    total = len(products_raw)
    no_images_count = 0
    missing_variant_img_count = 0
    issues: list[dict] = []

    _color_keys = {"color", "colour", "shade", "finish"}

    for p in products_raw:
        pid = p.get("id")
        title = p.get("title", "")
        handle = p.get("handle", "")
        images = p.get("images") or []
        variants = p.get("variants") or []

        prod_issues: list[str] = []

        if not images:
            no_images_count += 1
            prod_issues.append("no_images")

        # Detect colour axis from first variant option
        is_color_axis = False
        if variants:
            v0 = variants[0]
            for opt_title_key in ("option1", "option2", "option3"):
                pass  # option name is in the product, not per-variant in REST API
            # Fall back: check if variant values look like colours (non-numeric, non-size)
            _size_re = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|[0-9]+|UK\s?\d|One\s?Size|OS|N/A|Default Title)$", re.I)
            all_opt1 = [str(v.get("option1", "")) for v in variants if v.get("option1")]
            if all_opt1 and not all(_size_re.match(v) for v in all_opt1):
                is_color_axis = True

        if is_color_axis and variants:
            missing = [v for v in variants if not v.get("image_id")]
            if missing:
                missing_variant_img_count += 1
                pct = round(len(missing) / len(variants) * 100)
                prod_issues.append(f"{len(missing)}/{len(variants)} variants missing image_id ({pct}%)")

        if prod_issues:
            issues.append({
                "id": pid,
                "title": title[:80],
                "handle": handle,
                "image_count": len(images),
                "variant_count": len(variants),
                "issues": prod_issues,
            })

    ok_count = total - len(issues)
    return {
        "scraper_id": scraper_id,
        "total": total,
        "ok": ok_count,
        "no_images_count": no_images_count,
        "missing_variant_img_count": missing_variant_img_count,
        "issues_count": len(issues),
        "pass_rate": round(ok_count / total * 100, 1) if total else 0,
        "products_with_issues": issues[:100],
    }
