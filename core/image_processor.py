"""
BiRefNet AI Background Removal — Image Processing Pipeline
===========================================================
Wraps BiRefNet (ZhengPeng7/BiRefNet) with an async worker queue.
Only processes product["images"][0] (the hero/featured image).
All other images are untouched.

Cache design (two-level):
  Level 1:  url  → phash_str          (avoids re-download for known URLs)
  Level 2:  phash_str → local_url     (avoids re-processing for identical images
                                        even when served from different CDN URLs)

Config precedence:
  1. config/image_processing.yaml  (if present)
  2. IMAGE_PROCESSING dict below    (always present)
"""

import os
import json
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Default configuration ──────────────────────────────────────────────────────
IMAGE_PROCESSING = {
    "enabled": True,                # Pipeline is on by default; degrades gracefully if torch absent
    "model": "ZhengPeng7/BiRefNet",
    "workers": 4,                   # ThreadPoolExecutor max_workers
    "canvas_size": 2000,            # Output canvas: 2000×2000 px
    "output_format": "JPEG",        # "JPEG" or "PNG" — controls save format and file extension
    "jpeg_quality": 95,
    "cache_enabled": True,
    "auto_detect_white_bg": True,   # Skip inference if already white/transparent
    "white_bg_threshold": 245,      # Border pixel brightness (0–255) to consider "white"
    "white_bg_border_pct": 0.85,    # Fraction of border pixels that must pass threshold
    "sync_timeout": 5,              # Seconds to wait for a job before falling back to original URL
    "cache_dir": "processed_images",
    "cache_file": "processed_images/cache.json",
    "hf_cache_dir": ".birefnet_cache",
}

# ── Load config/image_processing.yaml override ────────────────────────────────
def _load_yaml_config():
    yaml_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "image_processing.yaml"
    )
    if not os.path.exists(yaml_path):
        return
    try:
        import yaml
        with open(yaml_path, "r") as f:
            override = yaml.safe_load(f) or {}
        IMAGE_PROCESSING.update(override)
        logger.info(f"[ImageProcessor] Loaded config from {yaml_path}")
    except Exception as e:
        logger.warning(f"[ImageProcessor] Could not load config/image_processing.yaml: {e}")

_load_yaml_config()

# ── Thread-safe stats ──────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_stats = {"processed": 0, "errors": 0, "cache_hits": 0, "queue_depth": 0}

def _inc(key: str, n: int = 1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n

def get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


# ── BiRefNet model state ───────────────────────────────────────────────────────
_model_lock = threading.Lock()
_model = None
_transform = None
_model_load_error: str = None


def _get_base_url() -> str:
    """
    Construct the public base URL this Flask server is reachable at.

    Resolution order (first non-empty wins):
      1. PUBLIC_BASE_URL env var  — set this in production so Shopify can fetch
         the processed images (e.g. https://yourapp.replit.app)
      2. REPLIT_DEV_DOMAIN        — auto-set by the Replit dev proxy
      3. localhost fallback        — NOT reachable by Shopify; a warning is logged
    """
    pub = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if pub:
        return pub
    dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "").strip()
    if dev_domain:
        return f"https://{dev_domain}"
    port = int(os.environ.get("FLASK_PORT", 8000))
    logger.warning(
        "[ImageProcessor] PUBLIC_BASE_URL is not set and REPLIT_DEV_DOMAIN is "
        "absent — processed image URLs will use localhost and will NOT be "
        "reachable by Shopify. Set PUBLIC_BASE_URL to your deployment URL."
    )
    return f"http://localhost:{port}"


def _filename_to_url(filename: str) -> str:
    """Convert a bare filename (e.g. 'abc123.jpg') to its full public URL."""
    return f"{_get_base_url()}/processed/{filename}"


def _load_model():
    """Lazy-load BiRefNet once; subsequent calls return cached model/None."""
    global _model, _transform, _model_load_error
    with _model_lock:
        if _model is not None or _model_load_error:
            return _model
        try:
            import torch
            from transformers import AutoModelForImageSegmentation
            from torchvision import transforms as T

            hf_cache = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                IMAGE_PROCESSING["hf_cache_dir"],
            )
            os.makedirs(hf_cache, exist_ok=True)
            os.environ.setdefault("HF_HOME", hf_cache)
            os.environ.setdefault("TRANSFORMERS_CACHE", hf_cache)

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"[ImageProcessor] Loading BiRefNet on {device} …")
            model = AutoModelForImageSegmentation.from_pretrained(
                IMAGE_PROCESSING["model"],
                trust_remote_code=True,
                cache_dir=hf_cache,
                torch_dtype=torch.float32,
            )
            model.to(device)
            model.eval()

            transform = T.Compose([
                T.Resize((1024, 1024)),
                T.ToTensor(),
                T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
            _model = model
            _transform = transform
            logger.info("[ImageProcessor] BiRefNet loaded successfully.")
            return _model
        except Exception as e:
            _model_load_error = str(e)
            logger.error(
                f"[ImageProcessor] BiRefNet load failed (original URLs will be used): {e}"
            )
            return None


# ── Two-level cache ────────────────────────────────────────────────────────────
# Stored in a single JSON file:
#   { "url_to_phash": { url → phash }, "phash_to_filename": { phash → "sha.ext" } }
#
# Filenames (not full URLs) are stored so the cache remains valid across
# server restarts and domain changes (Replit proxy, PUBLIC_BASE_URL edits).
# _filename_to_url() reconstructs the full URL fresh from env vars each call.
_cache_lock = threading.Lock()
_url_to_phash: dict = {}
_phash_to_filename: dict = {}          # phash → bare filename, e.g. "abc.jpg"


def _load_cache():
    global _url_to_phash, _phash_to_filename
    if not IMAGE_PROCESSING.get("cache_enabled", True):
        return
    cache_file = IMAGE_PROCESSING["cache_file"]
    if not os.path.exists(cache_file):
        return
    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
        u2p = data.get("url_to_phash", {})
        # New format: phash_to_filename
        p2f = data.get("phash_to_filename", {})
        # Backward compat: old format stored full URLs under "phash_to_local"
        if not p2f and "phash_to_local" in data:
            for ph, full_url in data["phash_to_local"].items():
                # Extract just the filename from the stored URL
                p2f[ph] = full_url.rsplit("/", 1)[-1]
        with _cache_lock:
            _url_to_phash = u2p
            _phash_to_filename = p2f
        logger.info(
            f"[ImageProcessor] Cache loaded: {len(_url_to_phash)} URLs, "
            f"{len(_phash_to_filename)} unique images from {cache_file}"
        )
    except Exception as e:
        logger.warning(f"[ImageProcessor] Cache load error: {e}")


def _save_cache():
    if not IMAGE_PROCESSING.get("cache_enabled", True):
        return
    cache_file = IMAGE_PROCESSING["cache_file"]
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with _cache_lock:
            snapshot = {
                "url_to_phash": dict(_url_to_phash),
                "phash_to_filename": dict(_phash_to_filename),
            }
        with open(cache_file, "w") as f:
            json.dump(snapshot, f)
    except Exception as e:
        logger.warning(f"[ImageProcessor] Cache save error: {e}")


def _cache_lookup_url(url: str):
    """Fast URL-based lookup. Returns bare filename or None."""
    with _cache_lock:
        phash = _url_to_phash.get(url)
        if phash:
            return _phash_to_filename.get(phash)
    return None


def _cache_lookup_phash(phash: str):
    """Phash-based lookup — dedup across different CDN URLs for same image."""
    with _cache_lock:
        return _phash_to_filename.get(phash)


def _cache_store(url: str, phash: str, filename: str):
    """Store url→phash and phash→filename (bare, not full URL)."""
    with _cache_lock:
        _url_to_phash[url] = phash
        _phash_to_filename[phash] = filename
    _save_cache()


def cache_size() -> int:
    with _cache_lock:
        return len(_phash_to_filename)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _download_image(url: str):
    """Download and return (PIL.Image, raw_bytes)."""
    import requests
    from PIL import Image
    import io
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MirageBot/1.0)"}
    r = requests.get(url, headers=headers, timeout=30, stream=False)
    r.raise_for_status()
    raw = r.content
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    return img, raw


def _compute_phash(img) -> str:
    """Compute perceptual hash of image; returns hex string."""
    import imagehash
    return str(imagehash.phash(img.convert("RGB")))


# ── White-background detector ──────────────────────────────────────────────────
def is_white_background(img) -> bool:
    """
    Returns True if the image already has a clean white or transparent background.
    Samples a 10-pixel wide border around the image.
    """
    if not IMAGE_PROCESSING.get("auto_detect_white_bg", True):
        return False
    try:
        import numpy as np
        threshold = IMAGE_PROCESSING.get("white_bg_threshold", 245)
        border_pct = IMAGE_PROCESSING.get("white_bg_border_pct", 0.85)

        if img.mode == "RGBA":
            arr = np.array(img)
            border_px = []
            border_px.extend(arr[:10, :, :].reshape(-1, 4).tolist())
            border_px.extend(arr[-10:, :, :].reshape(-1, 4).tolist())
            border_px.extend(arr[:, :10, :].reshape(-1, 4).tolist())
            border_px.extend(arr[:, -10:, :].reshape(-1, 4).tolist())
            total = len(border_px)
            if total == 0:
                return False
            transparent = sum(1 for px in border_px if px[3] < 10)
            if transparent / total >= border_pct:
                return True
            white = sum(
                1 for px in border_px
                if px[3] > 200 and px[0] > threshold and px[1] > threshold and px[2] > threshold
            )
            return (white / total) >= border_pct

        arr = np.array(img.convert("RGB"))
        border_px = []
        border_px.extend(arr[:10, :, :].reshape(-1, 3).tolist())
        border_px.extend(arr[-10:, :, :].reshape(-1, 3).tolist())
        border_px.extend(arr[:, :10, :].reshape(-1, 3).tolist())
        border_px.extend(arr[:, -10:, :].reshape(-1, 3).tolist())
        total = len(border_px)
        if total == 0:
            return False
        white = sum(
            1 for px in border_px
            if px[0] > threshold and px[1] > threshold and px[2] > threshold
        )
        return (white / total) >= border_pct
    except Exception as e:
        logger.debug(f"[ImageProcessor] White-bg check failed: {e}")
        return False


# ── Core processing function ───────────────────────────────────────────────────
def process_image(url: str, product_id: str = "", title: str = "") -> str:
    """
    Downloads image at `url`, runs BiRefNet background removal if needed,
    composites onto 2000×2000 white canvas, saves as JPEG Q95, returns local URL.

    Returns the original `url` unchanged on any failure.

    Emits one structured log line per invocation containing:
      product_id | title | original_url | start | end | duration_ms |
      dimensions | bg_removed | cache_hit | success/failure
    """
    t_start = datetime.now(timezone.utc)
    bg_removed = False
    cache_hit = False
    success = False
    dimensions = None
    error_msg = None
    local_url = url  # default: original URL on failure

    try:
        from PIL import Image

        cache_dir = IMAGE_PROCESSING["cache_dir"]
        os.makedirs(cache_dir, exist_ok=True)

        # ── Level-1 cache: URL lookup (no download needed) ─────────────────
        cached_fn = _cache_lookup_url(url)
        if cached_fn:
            cache_hit = True
            success = True
            local_url = _filename_to_url(cached_fn)   # domain resolved fresh
            _inc("cache_hits")
            _emit_log(product_id, title, url, t_start, None, cache_hit, bg_removed, success, None)
            return local_url

        # ── Download image ─────────────────────────────────────────────────
        img, raw_bytes = _download_image(url)
        dimensions = f"{img.size[0]}x{img.size[1]}"

        # ── Level-2 cache: phash lookup (dedup across CDN URLs) ────────────
        phash = _compute_phash(img)
        cached_fn2 = _cache_lookup_phash(phash)
        if cached_fn2:
            cache_hit = True
            success = True
            local_url = _filename_to_url(cached_fn2)  # domain resolved fresh
            _inc("cache_hits")
            # Record URL→phash so future calls skip the download entirely
            with _cache_lock:
                _url_to_phash[url] = phash
            _save_cache()
            _emit_log(product_id, title, url, t_start, dimensions, cache_hit, bg_removed, success, None)
            return local_url

        # ── Stable output filename (sha256 of raw image bytes) ────────────
        sha = hashlib.sha256(raw_bytes).hexdigest()
        fmt = (IMAGE_PROCESSING.get("output_format") or "JPEG").upper()
        ext = "png" if fmt == "PNG" else "jpg"
        out_filename = f"{sha}.{ext}"
        out_path = os.path.join(cache_dir, out_filename)

        # ── White-background detection ─────────────────────────────────────
        if is_white_background(img):
            result = _composite_on_white(img, IMAGE_PROCESSING["canvas_size"])
            _save_result(result, out_path, fmt)
            _cache_store(url, phash, out_filename)     # store filename only
            local_url = _filename_to_url(out_filename)
            _inc("processed")
            success = True
            bg_removed = False   # background was already clean; no removal done
        else:
            # ── BiRefNet inference ─────────────────────────────────────────
            model = _load_model()
            if model is None:
                error_msg = f"BiRefNet unavailable: {_model_load_error}"
                _inc("errors")
                _emit_log(product_id, title, url, t_start, dimensions, cache_hit, bg_removed, False, error_msg)
                return url

            mask = _run_birefnet(img, model)
            result = _apply_mask_and_composite(img, mask, IMAGE_PROCESSING["canvas_size"])
            _save_result(result, out_path, fmt)
            _cache_store(url, phash, out_filename)     # store filename only
            local_url = _filename_to_url(out_filename)
            _inc("processed")
            success = True
            bg_removed = True

    except Exception as e:
        error_msg = str(e)
        _inc("errors")
        logger.error(
            f"[ImageProcessor] FAILED pid={product_id} url={url[:80]} err={e} — keeping original URL"
        )
        local_url = url

    _emit_log(product_id, title, url, t_start, dimensions, cache_hit, bg_removed, success, error_msg)
    return local_url


def _emit_log(
    product_id: str, title: str, url: str,
    t_start: datetime, dimensions, cache_hit: bool,
    bg_removed: bool, success: bool, error_msg
):
    """Emit one complete structured log line per image."""
    t_end = datetime.now(timezone.utc)
    duration_ms = int((t_end - t_start).total_seconds() * 1000)
    logger.info(
        "[ImageProc] "
        f"product_id={product_id!r} "
        f"title={str(title)[:40]!r} "
        f"url={url[:80]!r} "
        f"start={t_start.isoformat()} "
        f"end={t_end.isoformat()} "
        f"duration_ms={duration_ms} "
        f"dimensions={dimensions} "
        f"bg_removed={bg_removed} "
        f"cache_hit={cache_hit} "
        f"success={success}"
        + (f" error={error_msg!r}" if error_msg else "")
    )


def _run_birefnet(img, model):
    """Run BiRefNet inference; returns a single-channel PIL mask matching img.size."""
    import torch
    from PIL import Image as _PILImage

    device = next(model.parameters()).device
    rgb = img.convert("RGB")
    inp = _transform(rgb).unsqueeze(0).to(device)

    with torch.no_grad():
        preds = model(inp)

    pred = preds[-1].squeeze().cpu()
    pred = (pred - pred.min()) / (pred.max() - pred.min() + 1e-8)
    mask_arr = (pred.numpy() * 255).astype("uint8")
    mask = _PILImage.fromarray(mask_arr).resize(img.size, resample=_PILImage.LANCZOS)
    return mask


def _crop_to_foreground_bounds(rgba_img):
    """
    Trim transparent margins around the foreground subject.
    Returns a tightly-cropped RGBA image.  Falls back to the original if
    the foreground bounding box cannot be determined.
    """
    try:
        import numpy as np
        arr = np.array(rgba_img)
        alpha = arr[:, :, 3]
        rows = np.any(alpha > 10, axis=1)
        cols = np.any(alpha > 10, axis=0)
        if not rows.any() or not cols.any():
            return rgba_img          # fully transparent — nothing to crop
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        return rgba_img.crop((int(cmin), int(rmin), int(cmax) + 1, int(rmax) + 1))
    except Exception as e:
        logger.debug(f"[ImageProcessor] Crop-to-bounds failed: {e}")
        return rgba_img


def _apply_mask_and_composite(img, mask, canvas_size: int):
    """
    Apply BiRefNet mask as alpha, crop tight to foreground bounds,
    then centre on a 2000×2000 white canvas.
    """
    from PIL import Image
    rgba = img.convert("RGBA")
    r, g, b, _ = rgba.split()
    masked = Image.merge("RGBA", (r, g, b, mask))
    cropped = _crop_to_foreground_bounds(masked)
    return _composite_on_white(cropped, canvas_size)


def _composite_on_white(img, canvas_size: int):
    """Paste img (centred, aspect-ratio preserved) onto a white canvas."""
    from PIL import Image
    canvas = Image.new("RGB", (canvas_size, canvas_size), (255, 255, 255))
    src = img.convert("RGBA") if img.mode != "RGBA" else img
    src_w, src_h = src.size
    scale = min(canvas_size / src_w, canvas_size / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    src_resized = src.resize((new_w, new_h), Image.LANCZOS)
    offset_x = (canvas_size - new_w) // 2
    offset_y = (canvas_size - new_h) // 2
    canvas.paste(src_resized, (offset_x, offset_y), mask=src_resized)
    return canvas


def _save_result(img, out_path: str, fmt: str):
    """Save composited PIL image honoring the output_format config."""
    fmt = fmt.upper()
    if fmt == "PNG":
        img.save(out_path, "PNG")
    else:
        quality = IMAGE_PROCESSING.get("jpeg_quality", 95)
        # Convert to RGB before JPEG save (JPEG doesn't support alpha)
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        rgb.save(out_path, "JPEG", quality=quality)


# ── ProcessingQueue ────────────────────────────────────────────────────────────
class ProcessingQueue:
    """
    Thread pool that accepts (product_dict, unique_images_list) jobs.
    Workers call process_image and update unique_images[0] in-place on success.
    """

    def __init__(self, workers: int = None):
        n = workers or IMAGE_PROCESSING.get("workers", 4)
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="img_proc")
        logger.info(f"[ProcessingQueue] Initialised with {n} workers.")

    def submit(self, product: dict, unique_images: list) -> "Future | None":
        if not unique_images:
            return None
        url = unique_images[0]
        if not url or not isinstance(url, str) or not url.startswith("http"):
            return None
        product_id = str(
            product.get("id") or product.get("styleCode") or product.get("Handle") or ""
        )
        title = str(product.get("Title") or product.get("name") or "")
        _inc("queue_depth")

        def _job():
            try:
                new_url = process_image(url, product_id, title)
                if new_url and new_url != url:
                    unique_images[0] = new_url
            finally:
                _inc("queue_depth", -1)

        return self._pool.submit(_job)

    def shutdown(self, wait: bool = True):
        self._pool.shutdown(wait=wait)


# ── Singleton ImageProcessor ───────────────────────────────────────────────────
class ImageProcessor:
    """
    Singleton facade used by shopify_transformer and app.py.
    On enqueue, checks URL-level cache first and updates unique_images[0]
    synchronously (no download needed) — guarantees deterministic Image Src
    for all previously-seen images regardless of row-write timing.
    Unknowns are submitted to the thread pool.
    """

    def __init__(self):
        self._queue: ProcessingQueue = None
        self._queue_lock = threading.Lock()
        _load_cache()

    def _get_queue(self) -> ProcessingQueue:
        if self._queue is None:
            with self._queue_lock:
                if self._queue is None:
                    self._queue = ProcessingQueue(workers=IMAGE_PROCESSING.get("workers", 4))
        return self._queue

    @property
    def enabled(self) -> bool:
        return bool(IMAGE_PROCESSING.get("enabled", False))

    def enqueue_if_enabled(self, product: dict, unique_images: list):
        """
        Called by shopify_transformer after unique_images list is built.
        Returns a Future (for uncached images) or None (disabled / cache hit).

        Fast-path (synchronous, no thread):
          If the URL is already in the two-level cache, unique_images[0] is
          updated in-place right now — before any row is written — so Image Src
          carries the processed URL deterministically. Returns None.

        Slow-path (background thread):
          Unknown URLs are submitted to the thread pool. Returns the Future so
          the caller can wait with a bounded timeout (sync_timeout config, default
          5 s) before writing rows — giving white-bg and fast-inference images
          a chance to complete and land in Image Src. On timeout, the original
          URL is used for this run; the processed URL is in cache for next run.
        """
        if not self.enabled or not unique_images:
            return None
        url = unique_images[0]
        if not url or not isinstance(url, str) or not url.startswith("http"):
            return None
        try:
            # Fast-path: URL already processed — reconstruct URL from filename
            # so the domain is always current (never a stale cached host)
            cached_fn = _cache_lookup_url(url)
            if cached_fn:
                unique_images[0] = _filename_to_url(cached_fn)
                _inc("cache_hits")
                return None
            # Slow-path: submit to background thread; caller uses the returned
            # Future for a bounded global drain before row generation
            return self._get_queue().submit(product, unique_images)
        except Exception as e:
            logger.error(
                f"[ImageProcessor] Enqueue error for "
                f"{product.get('Handle', '?')}: {e}"
            )
            return None

    def status(self) -> dict:
        s = get_stats()
        total_ops = s.get("cache_hits", 0) + s.get("processed", 0)
        hit_rate = (
            round(s["cache_hits"] / total_ops, 4) if total_ops > 0 else 0.0
        )
        return {
            "enabled": self.enabled,
            "workers": IMAGE_PROCESSING.get("workers", 4),
            "queue_depth": s.get("queue_depth", 0),
            "processed": s.get("processed", 0),
            "errors": s.get("errors", 0),
            "cache_hits": s.get("cache_hits", 0),
            "cache_hit_rate": hit_rate,
            "cache_size": cache_size(),
            "model": IMAGE_PROCESSING.get("model"),
        }


# ── Module-level singleton ─────────────────────────────────────────────────────
_processor_instance: ImageProcessor = None
_processor_lock = threading.Lock()


def get_image_processor() -> ImageProcessor:
    global _processor_instance
    if _processor_instance is None:
        with _processor_lock:
            if _processor_instance is None:
                _processor_instance = ImageProcessor()
    return _processor_instance
