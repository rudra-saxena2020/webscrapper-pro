"""
Pricing Engine — Universal INR Pricing for all scrapers
========================================================

## INR (no conversion — India retail price):
  price < ₹1,000  → ((price + 2000) × 1.25)
  price ≥ ₹1,000  → ((price + 2000) × 1.22)

## All other currencies (fetch live rate, convert to INR first):
  price < 1,000   → ((price × (live_rate + 12)) + 2000) × 1.25
  price ≥ 1,000   → ((price × (live_rate + 12)) + 2000) × 1.22

  GBP exception: always × 1.25 regardless of amount (flat rate).

Threshold is always 1,000 in the original currency unit for all currencies.

Exchange rates are fetched live from open.er-api.com every hour.
All 16 currencies are extracted from one single API call (USD base).
No currency may silently fall back to another currency's rate —
  an unsupported currency raises UnsupportedCurrencyError immediately.
Compare price = selling_price × 1.25 (fallback when no ticket price available).
"""

import re
import time
import logging

logger = logging.getLogger(__name__)


# ── Custom Exception ──────────────────────────────────────────────────────────

class UnsupportedCurrencyError(ValueError):
    """Raised when a currency code is not in the supported set."""
    pass


# ── Configuration — sourced from scraper_config (reads pricing_algo= env var)
# Falls back to hardcoded values if the env var is missing or unparseable.

try:
    from core.scraper_config import PRICING as _PC
    RATE_OFFSET      = _PC["RATE_OFFSET"]
    FIXED_FEE        = _PC["FIXED_FEE"]
    MARKUP_USD_BELOW = _PC["MARKUP_USD_BELOW"]
    MARKUP_USD_ABOVE = _PC["MARKUP_USD_ABOVE"]
    MARKUP_EUR_BELOW = _PC.get("MARKUP_EUR_BELOW", 1.25)
    MARKUP_EUR_ABOVE = _PC.get("MARKUP_EUR_ABOVE", 1.22)
    MARKUP_GBP_BELOW = _PC["MARKUP_GBP_BELOW"]
    MARKUP_GBP_ABOVE = _PC["MARKUP_GBP_ABOVE"]
    MARKUP_CHF_BELOW = _PC.get("MARKUP_CHF_BELOW", 1.25)
    MARKUP_CHF_ABOVE = _PC.get("MARKUP_CHF_ABOVE", 1.22)
    logger.info(
        f"[PricingEngine] Loaded from env (pricing_algo=): "
        f"offset=+{RATE_OFFSET} fee=+{FIXED_FEE} "
        f"USD<1k×{MARKUP_USD_BELOW} USD≥1k×{MARKUP_USD_ABOVE} "
        f"EUR<1k×{MARKUP_EUR_BELOW} EUR≥1k×{MARKUP_EUR_ABOVE} "
        f"GBP<1k×{MARKUP_GBP_BELOW} GBP≥1k×{MARKUP_GBP_ABOVE} "
        f"CHF<1k×{MARKUP_CHF_BELOW} CHF≥1k×{MARKUP_CHF_ABOVE}"
    )
except Exception as _pricing_err:
    logger.warning(f"[PricingEngine] env var unavailable — hardcoded fallback: {_pricing_err}")
    RATE_OFFSET      = 12
    FIXED_FEE        = 2000
    MARKUP_USD_BELOW = 1.25
    MARKUP_USD_ABOVE = 1.22
    MARKUP_EUR_BELOW = 1.25
    MARKUP_EUR_ABOVE = 1.22
    MARKUP_GBP_BELOW = 1.25
    MARKUP_GBP_ABOVE = 1.25
    MARKUP_CHF_BELOW = 1.25
    MARKUP_CHF_ABOVE = 1.22


RATES_TTL = 3600      # Refresh live rates every 1 hour

# ── Supported currencies and their markup thresholds ─────────────────────────
# (below_markup, above_markup, threshold_in_original_currency)
# Threshold is 1,000 in the original currency for every supported currency.
_CURRENCY_MARKUP = {
    # (below_markup, above_markup, threshold_in_original_currency)
    # Threshold = 1,000 for every currency. GBP is flat 1.25 both tiers.
    "USD": (1.25, 1.22, 1000),
    "EUR": (1.25, 1.22, 1000),
    "GBP": (1.25, 1.25, 1000),   # flat — 1.25 regardless of amount
    "CHF": (1.25, 1.22, 1000),
    "AUD": (1.25, 1.22, 1000),
    "CAD": (1.25, 1.22, 1000),
    "SGD": (1.25, 1.22, 1000),
    "NZD": (1.25, 1.22, 1000),
    "AED": (1.25, 1.22, 1000),
    "SAR": (1.25, 1.22, 1000),
    "JPY": (1.25, 1.22, 1000),
    "CNY": (1.25, 1.22, 1000),
    "HKD": (1.25, 1.22, 1000),
    "SEK": (1.25, 1.22, 1000),
    "NOK": (1.25, 1.22, 1000),
    "DKK": (1.25, 1.22, 1000),
}

# ── Fallback rates if live fetch fails (updated Jun 2025) ────────────────────
LAST_KNOWN_RATES = {
    "USD": 95.23,
    "GBP": 128.94,
    "EUR": 107.41,
    "CHF": 96.50,
    "AUD": 67.12,
    "CAD": 68.17,
    "JPY": 0.5948,
    "CNY": 14.06,
    "HKD": 12.16,
    "SGD": 74.23,
    "AED": 25.95,
    "SAR": 25.42,
    "SEK": 10.10,
    "NOK": 10.02,
    "DKK": 14.77,
    "NZD": 55.57,
}

# ── State ────────────────────────────────────────────────────────────────────

_cached_rates:      dict  = {}
_rates_fetched_at:  float = 0.0
_rates_source:      str   = "uninitialised"

# ── Exchange Rate Fetching ────────────────────────────────────────────────────

def get_exchange_rates() -> dict:
    """
    Returns INR rates for all 16 supported currencies.
    All rates are derived from a single open.er-api.com call (USD base).
    Refreshes at most once per RATES_TTL seconds.
    Falls back to LAST_KNOWN_RATES on any network failure — never silently
    falls back to a different currency's rate.
    """
    global _cached_rates, _rates_fetched_at, _rates_source

    now = time.time()
    if _cached_rates and (now - _rates_fetched_at) < RATES_TTL:
        return _cached_rates

    try:
        try:
            from curl_cffi import requests as cffi_requests
            res = cffi_requests.get(
                "https://open.er-api.com/v6/latest/USD",
                timeout=8,
                impersonate="chrome131"
            )
        except ImportError:
            import requests as std_requests
            res = std_requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)

        if res.ok:
            data       = res.json()
            api_rates  = data.get("rates", {})
            inr        = api_rates.get("INR")

            if inr and inr > 0:
                new_rates = {"USD": round(inr, 4)}

                # Derive all supported currencies from the same payload
                for code in _CURRENCY_MARKUP:
                    if code == "USD":
                        continue
                    foreign = api_rates.get(code)
                    if foreign and foreign > 0:
                        new_rates[code] = round(inr / foreign, 4)
                    else:
                        # Currency missing from API response — keep last known
                        new_rates[code] = LAST_KNOWN_RATES.get(code)
                        logger.warning(
                            "⚠️  %s not in API response — using last known rate %.4f",
                            code, new_rates[code] or 0,
                        )

                _cached_rates = new_rates
                LAST_KNOWN_RATES.update({k: v for k, v in new_rates.items() if v})
                _rates_fetched_at = now
                _rates_source = "live"

                logger.info(
                    "💱 Live rates fetched (16 currencies): "
                    "USD=%.4f GBP=%.4f EUR=%.4f CHF=%.4f "
                    "AUD=%.4f CAD=%.4f SGD=%.4f NZD=%.4f "
                    "JPY=%.4f AED=%.4f SAR=%.4f "
                    "SEK=%.4f NOK=%.4f DKK=%.4f HKD=%.4f CNY=%.4f",
                    new_rates.get("USD", 0), new_rates.get("GBP", 0),
                    new_rates.get("EUR", 0), new_rates.get("CHF", 0),
                    new_rates.get("AUD", 0), new_rates.get("CAD", 0),
                    new_rates.get("SGD", 0), new_rates.get("NZD", 0),
                    new_rates.get("JPY", 0), new_rates.get("AED", 0),
                    new_rates.get("SAR", 0), new_rates.get("SEK", 0),
                    new_rates.get("NOK", 0), new_rates.get("DKK", 0),
                    new_rates.get("HKD", 0), new_rates.get("CNY", 0),
                )
                return _cached_rates

    except Exception as e:
        logger.warning("⚠️ Rate fetch failed (%s), using fallback rates.", e)

    _cached_rates = LAST_KNOWN_RATES.copy()
    _rates_fetched_at = now
    _rates_source = "fallback"
    logger.warning(
        "⚠️  Using fallback rates for all 16 currencies (live fetch failed)."
    )
    return _cached_rates


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_price(price) -> float | None:
    """Safely parse price from float, int, or string. Returns None on failure."""
    if price is None:
        return None
    try:
        if isinstance(price, (int, float)):
            return float(price)
        cleaned = re.sub(r"[^\d.]", "", str(price))
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


# Currency symbol / alias → canonical 3-letter code
_CURRENCY_ALIASES = {
    # USD
    "USD": "USD", "$": "USD", "US": "USD",
    # GBP
    "GBP": "GBP", "£": "GBP", "UKP": "GBP", "UK": "GBP",
    # EUR
    "EUR": "EUR", "€": "EUR", "EURO": "EUR",
    # CHF
    "CHF": "CHF", "FR": "CHF", "SFR": "CHF",
    # AUD
    "AUD": "AUD", "A$": "AUD", "AU$": "AUD",
    # CAD
    "CAD": "CAD", "C$": "CAD", "CA$": "CAD",
    # JPY
    "JPY": "JPY", "¥": "JPY", "YEN": "JPY",
    # CNY
    "CNY": "CNY", "RMB": "CNY", "人民币": "CNY",
    # HKD
    "HKD": "HKD", "HK$": "HKD",
    # SGD
    "SGD": "SGD", "S$": "SGD",
    # AED
    "AED": "AED", "DH": "AED", "DHS": "AED", "AED": "AED",
    # SAR
    "SAR": "SAR", "SR": "SAR", "﷼": "SAR",
    # SEK
    "SEK": "SEK", "KR": "SEK",
    # NOK
    "NOK": "NOK",
    # DKK
    "DKK": "DKK",
    # NZD
    "NZD": "NZD", "NZ$": "NZD",
    # INR (pass-through)
    "INR": "INR", "₹": "INR", "RS": "INR", "RS.": "INR",
}

def _normalize_currency(currency) -> str:
    """
    Normalize any currency symbol or alias to canonical 3-letter code.
    Returns the canonical code, or the uppercased input if unrecognised
    (caller is responsible for detecting unsupported codes via _CURRENCY_MARKUP).
    """
    if not currency:
        return "USD"
    c = str(currency).upper().strip()
    return _CURRENCY_ALIASES.get(c, c)


# ── Core Functions ────────────────────────────────────────────────────────────

def _resolve_rate(curr: str, rates: dict) -> float:
    """
    Resolve the INR rate for a canonical currency code.
    Priority: live cache → LAST_KNOWN_RATES.
    Raises UnsupportedCurrencyError if the currency is not in _CURRENCY_MARKUP
    and has no known rate at all.
    """
    # Live cache hit
    rate = rates.get(curr)
    if rate:
        return rate

    # Fallback to last-known rate for supported currencies
    rate = LAST_KNOWN_RATES.get(curr)
    if rate:
        logger.warning(
            "⚠️  %s not in live cache — using last-known rate %.4f", curr, rate
        )
        return rate

    # Truly unknown currency — hard error
    raise UnsupportedCurrencyError(
        f"Currency '{curr}' is not supported and has no known INR rate. "
        f"Add it to LAST_KNOWN_RATES and _CURRENCY_MARKUP before processing."
    )


def calculate_cost_inr(price, currency, rates: dict) -> float | None:
    """
    Pre-markup cost used for 'Cost per item' and compare-price baseline.

    INR:     cost = price + FIXED_FEE  (no rate conversion, no markup)
    Non-INR: cost = (price × (rate + RATE_OFFSET)) + FIXED_FEE

    Returns None on invalid input. Raises UnsupportedCurrencyError for unknown currencies.
    """
    p = _parse_price(price)
    if p is None or p <= 0:
        return None

    curr = _normalize_currency(currency)

    if curr == "INR":
        return round(p + FIXED_FEE, 2)  # India price + ₹2,000 fee, no markup

    rate = _resolve_rate(curr, rates)
    cost = (p * (rate + RATE_OFFSET)) + FIXED_FEE
    return round(cost, 2)


def calculate_price_inr(price, currency, rates: dict, _log: bool = False) -> float | None:
    """
    Calculate final INR selling price.

    Formula: ((price × (rate + RATE_OFFSET)) + FIXED_FEE) × markup

    Markup threshold and tiers are defined per currency in _CURRENCY_MARKUP.
    Pass _log=True to emit a step-by-step trace at INFO level.

    Returns None on invalid input.
    Raises UnsupportedCurrencyError for unrecognised currencies.
    """
    p = _parse_price(price)
    if p is None or p <= 0:
        return None

    curr = _normalize_currency(currency)

    if curr == "INR":
        # INR formula: ((price + 2000) × 1.25) if < ₹1,000 else ((price + 2000) × 1.22)
        markup = 1.25 if p < 1000 else 1.22
        result = round((p + FIXED_FEE) * markup, 2)
        if _log:
            logger.info(
                "[Pricing] INR ₹%.2f → ((%.2f + %d) × %.2f) = ₹%.2f",
                p, p, FIXED_FEE, markup, result,
            )
        return result

    # ── Rate lookup (errors on truly unknown currencies) ─────────────────────
    rate        = _resolve_rate(curr, rates)
    rate_source = "live" if curr in rates else "fallback"

    # ── Markup tier ──────────────────────────────────────────────────────────
    markup_entry = _CURRENCY_MARKUP.get(curr)
    if markup_entry:
        below_m, above_m, threshold = markup_entry
        markup = above_m if p >= threshold else below_m
    else:
        # Should not reach here after _resolve_rate — but be explicit
        raise UnsupportedCurrencyError(
            f"Currency '{curr}' has no markup configuration. "
            f"Add it to _CURRENCY_MARKUP before processing."
        )

    # ── Calculation steps ────────────────────────────────────────────────────
    adjusted_rate = rate + RATE_OFFSET          # rate + 12
    converted_inr = p * adjusted_rate           # price × adjusted_rate
    after_fee     = converted_inr + FIXED_FEE   # + ₹2,000
    selling       = round(after_fee * markup, 2)

    if _log:
        logger.info(
            "[Pricing] %s %.2f → ₹%.2f\n"
            "  1. Supplier price      : %s %.2f\n"
            "  2. Live %s→INR rate    : %.4f  (source: %s)\n"
            "  3. Adjusted rate (+%d) : %.4f\n"
            "  4. Converted INR       : %.2f × %.4f = ₹%.2f\n"
            "  5. After ₹%d fee       : ₹%.2f\n"
            "  6. Markup ×%.2f (thr≥%g): ₹%.2f",
            curr, p, selling,
            curr, p,
            curr, rate, rate_source,
            RATE_OFFSET, adjusted_rate,
            p, adjusted_rate, converted_inr,
            FIXED_FEE, after_fee,
            markup, markup_entry[2], selling,
        )

    return selling


def force_refresh_rates() -> dict:
    """
    Bypass the TTL cache and immediately fetch fresh live rates.
    Call this before every CSV rebuild to ensure prices use the latest rate.
    Returns the newly fetched rates dict.
    """
    global _cached_rates, _rates_fetched_at
    _cached_rates      = {}
    _rates_fetched_at  = 0.0
    return get_exchange_rates()


def log_price_calculation(price, currency, rates: dict) -> float | None:
    """
    Convenience wrapper: always emits the step-by-step log and returns the price.
    """
    return calculate_price_inr(price, currency, rates, _log=True)


def calculate_compare_price(final_price) -> float | None:
    """
    Fallback compare price: 25% above selling price.
    Rounded to 2 decimal places.
    """
    p = _parse_price(final_price)
    if p is None or p <= 0:
        return None
    return round(p * 1.25, 2)


def calculate_compare_price_from_ticket(ticket_price, currency, rates: dict) -> float | None:
    """
    Convert the real ticket/RRP price to INR using the same formula.
    """
    return calculate_price_inr(ticket_price, currency, rates)
