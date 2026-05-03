"""
ytracker.scraper
~~~~~~~~~~~~~~~~
Multi-store price scraper.
Supports: Amazon, Walmart, Uber Eats, Nike, Lululemon, Best Buy, Safeway,
          Costco, Temu, Home Depot.

Strategy per page:
  1. JSON-LD structured data  (most reliable)
  2. <meta> tags              (og:price:amount, product:price:amount)
  3. Store-specific CSS selectors (fallback)
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Store registry — add new stores here
# ---------------------------------------------------------------------------

STORE_NAMES = {
    "amazon":    "Amazon",
    "walmart":   "Walmart",
    "ubereats":  "Uber Eats",
    "nike":      "Nike",
    "lululemon": "Lululemon",
    "bestbuy":   "Best Buy",
    "safeway":   "Safeway",
    "costco":    "Costco",
    "temu":      "Temu",
    "homedepot": "Home Depot",
}

STORE_COLORS = {
    "amazon":    "#ff9900",
    "walmart":   "#0071dc",
    "ubereats":  "#06c167",
    "nike":      "#111111",
    "lululemon": "#d31334",
    "bestbuy":   "#0046be",
    "safeway":   "#e21a2c",
    "costco":    "#e31837",
    "temu":      "#fb7701",
    "homedepot": "#f96302",
}

# hostname fragments → store key
_STORE_DOMAINS: list[tuple[str, str]] = [
    ("amazon.com",    "amazon"),
    ("amzn.to",       "amazon"),
    ("amzn.com",      "amazon"),
    ("a.co",          "amazon"),
    ("walmart.com",   "walmart"),
    ("ubereats.com",  "ubereats"),
    ("nike.com",      "nike"),
    ("lululemon.com", "lululemon"),
    ("bestbuy.com",   "bestbuy"),
    ("safeway.com",   "safeway"),
    ("costco.com",    "costco"),
    ("temu.com",      "temu"),
    ("homedepot.com", "homedepot"),
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
]

_last_request_time: float = 0.0


def _rate_limit() -> None:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)
    _last_request_time = time.time()


def _get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def _fetch_page(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    """Fetch URL → BeautifulSoup, with one retry.
    Bypasses HTTP_PROXY / HTTPS_PROXY to avoid corporate proxy blocking stores.
    Uses realistic browser headers to avoid bot detection.
    """
    _rate_limit()
    # Bypass proxy — store sites must be fetched directly
    no_proxy = {"http": None, "https": None}

    for attempt in range(2):
        try:
            headers = _get_headers()
            # Safari-style headers (Safari does NOT send Sec-CH-UA)
            headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            })
            resp = requests.get(url, headers=headers, timeout=timeout,
                                allow_redirects=True, proxies=no_proxy)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            log.warning("Fetch %s returned %d (attempt %d)", url, resp.status_code, attempt + 1)
        except Exception as exc:
            log.warning("Fetch %s failed (attempt %d): %s", url, attempt + 1, exc)
        if attempt == 0:
            time.sleep(1 + random.random())
    return None


# ---------------------------------------------------------------------------
# Price / text helpers
# ---------------------------------------------------------------------------

def _clean_price(text: str) -> Optional[float]:
    """Extract a numeric price from text like '$29.99' or '29.99'."""
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    if m:
        try:
            val = float(m.group())
            if val > 0:
                return val
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Generic extraction: JSON-LD → <meta> → fallback
# ---------------------------------------------------------------------------

def _extract_jsonld(soup: BeautifulSoup) -> dict:
    """Extract product data from JSON-LD <script> tags (most reliable)."""
    result: dict = {"title": None, "price": None, "image": None, "currency": None}
    try:
        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                _try_jsonld_item(item, result)
                if result["price"]:
                    return result
                # Check @graph array (common in Best Buy, Nike)
                for sub in item.get("@graph", []):
                    _try_jsonld_item(sub, result)
                    if result["price"]:
                        return result
    except Exception as exc:
        log.debug("JSON-LD parse failed: %s", exc)
    return result


def _try_jsonld_item(item: dict, result: dict) -> None:
    """Try to populate result from a single JSON-LD object."""
    item_type = item.get("@type", "")
    if isinstance(item_type, list):
        item_type = " ".join(item_type)
    if not any(t in item_type for t in ("Product", "IndividualProduct", "MenuItem", "FoodEstablishment", "Offer")):
        # Also handle mainEntity
        main = item.get("mainEntity")
        if isinstance(main, dict):
            _try_jsonld_item(main, result)
        return

    result["title"] = result["title"] or item.get("name")

    # Image
    img = item.get("image")
    if img and not result["image"]:
        if isinstance(img, list):
            result["image"] = img[0] if img else None
        elif isinstance(img, dict):
            result["image"] = img.get("url") or img.get("contentUrl")
        else:
            result["image"] = str(img)

    # Price from offers
    offers = item.get("offers") or item.get("Offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        # Might be an AggregateOffer
        p = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
        if p and not result["price"]:
            result["price"] = _clean_price(str(p))
            result["currency"] = offers.get("priceCurrency", "USD")
        # Check nested offer list
        for sub_offer in offers.get("offers", []):
            if isinstance(sub_offer, dict) and not result["price"]:
                sp = sub_offer.get("price") or sub_offer.get("lowPrice")
                if sp:
                    result["price"] = _clean_price(str(sp))
                    result["currency"] = sub_offer.get("priceCurrency", "USD")


def _extract_meta(soup: BeautifulSoup) -> dict:
    """Extract product data from <meta> tags."""
    result: dict = {"title": None, "price": None, "image": None}

    # Price
    for attr in ["product:price:amount", "og:price:amount", "twitter:data1"]:
        el = soup.select_one(f'meta[property="{attr}"]') or soup.select_one(f'meta[name="{attr}"]')
        if el and el.get("content"):
            p = _clean_price(el["content"])
            if p:
                result["price"] = p
                break

    # Title
    for attr in ["og:title", "twitter:title", "product:name"]:
        el = soup.select_one(f'meta[property="{attr}"]') or soup.select_one(f'meta[name="{attr}"]')
        if el and el.get("content"):
            result["title"] = el["content"].strip()
            break

    # Image
    for attr in ["og:image", "twitter:image", "product:image"]:
        el = soup.select_one(f'meta[property="{attr}"]') or soup.select_one(f'meta[name="{attr}"]')
        if el and el.get("content"):
            result["image"] = el["content"]
            break

    return result


# ---------------------------------------------------------------------------
# Store-specific CSS fallback selectors
# ---------------------------------------------------------------------------

# Each entry: list of (selector, attribute_or_None)
# If attribute is None → get text content; else get that attribute.
_PRICE_SELECTORS: dict[str, list[tuple[str, Optional[str]]]] = {
    "amazon": [
        ("span.a-price span.a-offscreen", None),
        ("#corePrice_desktop span.a-offscreen", None),
        ("#apex_offerDisplay_desktop span.a-offscreen", None),
        ("#priceblock_ourprice", None),
        ("#priceblock_dealprice", None),
        ("#tp_price_block_total_price_ww span.a-offscreen", None),
    ],
    "walmart": [
        # itemprop="price" with content attribute is most reliable
        ("span[itemprop='price']", "content"),
        ("[itemprop='price']", "content"),
        # aria-label on price container often has full "Now $24.98"
        ("[data-testid='price-wrap']", "aria-label"),
        ("[data-automation-id='product-price']", "aria-label"),
        # Full text of the price wrap (may include "Now $24.98")
        ("span[itemprop='price']", None),
        # Hero price display
        ("[data-seo-id='hero-price']", None),
        ("[data-testid='hero-price']", None),
        # Current (2025) Walmart price format with data-testid
        ("[data-testid='product-price'] span", None),
        # Walmart Plus price
        ("[data-testid='price-wrap'] [aria-hidden='true']", None),
    ],
    "bestbuy": [
        ("div.priceView-hero-price span[aria-hidden='true']", None),
        ("[data-testid='customer-price'] span", None),
    ],
    "nike": [
        ("div[data-test='product-price']", None),
        ("div.product-price", None),
        ("#price-summary span", None),
    ],
    "lululemon": [
        ("span.price-1jnQj", None),
        ("span[data-lulu-price]", None),
        (".price-module span", None),
    ],
    "safeway": [
        (".product-price .price-value", None),
        ("[data-testid='product-price']", None),
        (".price-card-amount", None),
    ],
    "ubereats": [
        ("[data-testid='rich-text'] span", None),
        ("span[data-testid*='price']", None),
    ],
    "costco": [
        ("#pull-right-price span.value", None),
        ("div.price span", None),
        (".your-price span", None),
    ],
    "temu": [
        ("span.goods-price", None),
        ("[data-testid='price'] span", None),
        ("div.price span", None),
    ],
    "homedepot": [
        ("span.sui-font-bold.sui-text-primary-regular", None),
        ("[data-testid='price-format'] span", None),
        ("div.price-format__main-price span", None),
        ("span.price-format__large", None),
        ("#standard-price span", None),
    ],
}

_TITLE_SELECTORS: dict[str, list[str]] = {
    "amazon":    ["#productTitle", "#title", "h1 span#productTitle"],
    "walmart":   ["h1[itemprop='name']", "[data-testid='product-title']", "h1"],
    "bestbuy":   ["h1.heading-5", "h1"],
    "nike":      ["h1#pdp_product_title", "h1"],
    "lululemon": ["h1.pdp-title", "h1"],
    "safeway":   ["h1.product-title", "h1"],
    "ubereats":  ["h1", "[data-testid='store-title']"],
    "costco":    ["h1.product-title", "h1", "meta[name='title']"],
    "temu":      ["h1.goods-name", "h1", "title"],
    "homedepot": ["h1.sui-font-regular", "h1[class*='product-title']", "h1"],
}

_IMAGE_SELECTORS: dict[str, list[tuple[str, Optional[str]]]] = {
    "amazon":    [("#landingImage", "data-old-hires"), ("#landingImage", "src"), ("#imgBlkFront", "src")],
    "walmart":   [("img[data-testid='hero-image']", "src"), ("[data-testid='media-thumbnail'] img", "src"), ("img.db", "src")],
    "bestbuy":   [("img.primary-image", "src"), ("img[data-testid='image-media']", "src")],
    "nike":      [("img[data-testid='HeroImg']", "src"), ("picture img", "src")],
    "lululemon": [("img.carousel-image", "src"), (".pdp-image img", "src")],
    "safeway":   [("img.product-image", "src")],
    "ubereats":  [("img[role='presentation']", "src"), ("img", "src")],
    "costco":    [("img.product-image", "src"), ("img[data-testid='product-image']", "src")],
    "temu":      [("img.goods-img", "src"), ("img[data-testid='product-image']", "src")],
    "homedepot": [("img[data-testid='hero-image']", "src"), ("img.mediabrowser-image", "src"), (".media-viewer img", "src")],
}


# ---------------------------------------------------------------------------
# __NEXT_DATA__ extraction (Next.js apps: Walmart, etc.)
# ---------------------------------------------------------------------------

def _extract_nextdata(soup: BeautifulSoup, store: str) -> dict:
    """Extract product data from Next.js __NEXT_DATA__ hydration JSON.

    Many modern retail sites (Walmart, etc.) use Next.js and embed product
    data in <script id="__NEXT_DATA__">.  The price is often NOT in the
    rendered HTML, only in this JSON blob.
    """
    result: dict = {"title": None, "price": None, "image": None, "currency": None}
    try:
        script = soup.select_one('script#__NEXT_DATA__')
        if not script or not script.string:
            return result
        data = json.loads(script.string)
    except Exception:
        return result

    if store == "walmart":
        _nextdata_walmart(data, result)
    else:
        # Generic: walk the JSON looking for price-like structures
        _nextdata_generic(data, result)

    if result["price"]:
        log.info("__NEXT_DATA__ extracted %s price: %s", store, result["price"])
    return result


def _nextdata_walmart(data: dict, result: dict) -> None:
    """Extract Walmart product info from __NEXT_DATA__ JSON."""
    try:
        # Navigate Walmart's Next.js data structure
        # Path: props.pageProps.initialData.data.product
        page_props = data.get("props", {}).get("pageProps", {})
        init_data = page_props.get("initialData", {}).get("data", {})

        # Try multiple paths Walmart has used
        product = (
            init_data.get("product")
            or init_data.get("contentLayout", {}).get("modules", [{}])[0].get("data", {}).get("product")
            or {}
        )

        # Title
        result["title"] = result["title"] or product.get("name")

        # Price — multiple possible locations
        price_info = product.get("priceInfo", {})
        current = price_info.get("currentPrice", {})
        result["price"] = result["price"] or current.get("price")

        if not result["price"]:
            # Try unitPrice or was price
            result["price"] = price_info.get("unitPrice", {}).get("price")
        if not result["price"]:
            # Try priceRange
            pr = price_info.get("priceRange", {})
            result["price"] = pr.get("minPrice") or pr.get("maxPrice")
        if not result["price"]:
            # Try offers inside product
            offers = product.get("offers", [])
            if offers and isinstance(offers, list):
                result["price"] = _clean_price(str(offers[0].get("price", "")))

        # Currency
        result["currency"] = current.get("currencyCode") or "USD"

        # Image
        images = product.get("imageInfo", {}).get("thumbnailUrl") or product.get("imageInfo", {}).get("allImages", [])
        if isinstance(images, str):
            result["image"] = images
        elif isinstance(images, list) and images:
            img = images[0]
            result["image"] = img.get("url") if isinstance(img, dict) else str(img)

        # Also try the idml path
        if not result["price"]:
            idml = init_data.get("idml", {})
            for key in idml:
                mod = idml[key]
                if isinstance(mod, dict):
                    p = mod.get("price") or mod.get("currentPrice")
                    if p:
                        result["price"] = _clean_price(str(p)) if isinstance(p, str) else p
                        break

    except (KeyError, IndexError, TypeError) as exc:
        log.debug("Walmart __NEXT_DATA__ parse failed: %s", exc)


def _nextdata_generic(data: dict, result: dict) -> None:
    """Generic extraction: walk JSON looking for price/name/image keys."""
    try:
        text = json.dumps(data)
        # Quick check: does it even contain price-like content?
        if '"price"' not in text.lower():
            return

        def _walk(obj: dict | list, depth: int = 0) -> None:
            if depth > 8 or result["price"]:
                return
            if isinstance(obj, dict):
                # Check for price-like keys
                for key in ("price", "currentPrice", "salePrice", "finalPrice"):
                    val = obj.get(key)
                    if val and not result["price"]:
                        p = _clean_price(str(val)) if isinstance(val, str) else val
                        if isinstance(p, (int, float)) and 0.5 < p < 100000:
                            result["price"] = float(p)
                # Title
                if not result["title"]:
                    result["title"] = obj.get("productName") or obj.get("name") or obj.get("title")
                # Recurse
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        _walk(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj[:10]:  # limit list traversal
                    if isinstance(item, (dict, list)):
                        _walk(item, depth + 1)

        _walk(data)
    except Exception:
        pass


def _css_extract_price(soup: BeautifulSoup, store: str) -> Optional[float]:
    """Fallback: extract price via store-specific CSS selectors."""
    for selector, attr in _PRICE_SELECTORS.get(store, []):
        el = soup.select_one(selector)
        if el:
            text = el.get(attr) if attr else el.get_text(strip=True)
            p = _clean_price(text)
            if p:
                return p

    # ── Walmart split-price handler ─────────────────────────────────────
    # Walmart renders price as separate spans: "$" + "24" + "98" for $24.98
    # The characteristic (dollars) has a large font class, mantissa (cents) smaller.
    if store == "walmart":
        price = _walmart_split_price(soup)
        if price:
            return price

    # Last resort: scan all elements for a dollar-amount pattern.
    # Require price > $2 to avoid picking up "$1" shipping/membership badges.
    for el in soup.select("span, div"):
        text = el.get_text(strip=True)
        if text.startswith("$") and 3 < len(text) < 12:
            p = _clean_price(text)
            if p and p > 2.0 and p < 100000:
                return p
    return None


def _walmart_split_price(soup: BeautifulSoup) -> Optional[float]:
    """Handle Walmart's split-price spans: characteristic (dollars) + mantissa (cents).

    Walmart renders prices like:
      <span data-testid="price-wrap">
        <span aria-hidden="true">
          <span>Now</span> <span>$</span>
          <span class="...f1...">24</span>      ← dollars (large font)
          <span class="...f6...">98</span>      ← cents  (small font)
        </span>
      </span>
    """
    # Try price-wrap containers
    for selector in ["[data-testid='price-wrap']", "[data-automation-id='product-price']"]:
        wrap = soup.select_one(selector)
        if not wrap:
            continue

        # First check aria-label (most reliable: "Now $24.98" or "current price $24.98")
        label = wrap.get("aria-label", "")
        p = _clean_price(label)
        if p and p > 1:
            return p

        # Get all text content and try to find a price pattern
        full_text = wrap.get_text(separator=" ", strip=True)
        # Look for "$XX.XX" or "$XX XX" (space between dollars and cents)
        m = re.search(r'\$\s*(\d[\d,]*)\s*[.\s]\s*(\d{2})\b', full_text)
        if m:
            try:
                dollars = m.group(1).replace(",", "")
                cents = m.group(2)
                return float(f"{dollars}.{cents}")
            except ValueError:
                pass

        # Fallback: just try to extract any dollar amount from full text
        p = _clean_price(full_text)
        if p and p > 1:
            return p

    return None


def _css_extract_title(soup: BeautifulSoup, store: str) -> Optional[str]:
    for selector in _TITLE_SELECTORS.get(store, ["h1"]):
        el = soup.select_one(selector)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t[:300]
    return None


def _css_extract_image(soup: BeautifulSoup, store: str) -> Optional[str]:
    for selector, attr in _IMAGE_SELECTORS.get(store, []):
        el = soup.select_one(selector)
        if el:
            val = el.get(attr, "")
            if val:
                return val
    return None


# ---------------------------------------------------------------------------
# Store detection + item ID extraction
# ---------------------------------------------------------------------------

def detect_store(url: str) -> Optional[str]:
    """Detect which store a URL belongs to."""
    url_lower = url.lower()
    host = urlparse(url_lower).hostname or url_lower
    for domain_frag, store_key in _STORE_DOMAINS:
        if domain_frag in host:
            return store_key
    return None


_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_RAW_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_WALMART_ID_RE = re.compile(r"/ip/[^/]*/(\d+)|/ip/(\d+)")
_UBEREATS_ID_RE = re.compile(r"/store/[^/]+/([a-f0-9-]+)")
_HOMEDEPOT_ID_RE = re.compile(r"/p/[^/]+/(\d{9})|/p/(\d{9})")


def extract_item_id(url: str, store: str) -> Optional[str]:
    """Extract a store-specific item ID from a URL."""
    url = url.strip()

    if store == "amazon":
        if _RAW_ASIN_RE.match(url):
            return url
        m = _ASIN_RE.search(url)
        return m.group(1) if m else None

    if store == "walmart":
        m = _WALMART_ID_RE.search(url)
        if m:
            return m.group(1) or m.group(2)
        qs = parse_qs(urlparse(url).query)
        return qs.get("product_id", [None])[0]

    if store == "ubereats":
        m = _UBEREATS_ID_RE.search(url)
        if m:
            return m.group(1)

    if store == "homedepot":
        # Home Depot URLs: /p/Product-Name/123456789
        m = _HOMEDEPOT_ID_RE.search(url)
        if m:
            return m.group(1) or m.group(2)
        # Fallback: look for a 9-digit numeric segment in the path
        m = re.search(r"/(\d{9})(?:[/?#]|$)", url)
        if m:
            return m.group(1)

    # Generic: use the last meaningful path segment as item ID
    # Works for Nike (/t/PRODUCT_NAME/SKU), Lululemon (/prod123), Best Buy (/site/.../SKU.p),
    # Safeway (/shop/product-detail.960.html), etc.
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    if parts:
        # Best Buy: SKU is like "6505727.p" → strip .p
        last = parts[-1]
        last = re.sub(r"\.p$", "", last)
        # Safeway: "960.html" → strip .html
        last = re.sub(r"\.html$", "", last)
        return last
    return None


# ---------------------------------------------------------------------------
# Walmart API fallback (bypasses bot detection)
# ---------------------------------------------------------------------------

def _walmart_api_fetch(item_id: str) -> Optional[dict]:
    """Fetch Walmart product data via their internal BE API.

    Walmart's HTML pages are behind PerimeterX bot detection, but their
    backend API endpoints are more permissive.  We try multiple endpoints
    in order of reliability.
    """
    _rate_limit()
    no_proxy = {"http": None, "https": None}

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.walmart.com/",
        "Origin": "https://www.walmart.com",
    }

    # ── Strategy 1: Walmart's product page BE API ─────────────────────
    # This returns the same data as __NEXT_DATA__ but as a direct JSON call
    api_url = f"https://www.walmart.com/orchestra/home/auto/product/{item_id}"
    try:
        resp = requests.get(api_url, headers=headers, timeout=15,
                            proxies=no_proxy, allow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            result = _parse_walmart_api(data, item_id)
            if result and result.get("price"):
                log.info("Walmart API (orchestra): %s price=$%s", item_id, result["price"])
                return result
    except Exception as exc:
        log.debug("Walmart orchestra API failed for %s: %s", item_id, exc)

    # ── Strategy 2: Walmart search API with item ID ───────────────────
    search_url = f"https://www.walmart.com/search?q={item_id}"
    search_api = f"https://www.walmart.com/orchestra/snb/graphql"
    try:
        gql_headers = {**headers, "Content-Type": "application/json"}
        payload = {
            "query": "query($query:String!){search(query:$query,count:1){searchResult{itemStacks{items{id name price imageInfo{thumbnailUrl}}}}}}",
            "variables": {"query": item_id},
        }
        resp = requests.post(search_api, json=payload, headers=gql_headers,
                             timeout=15, proxies=no_proxy)
        if resp.status_code == 200:
            data = resp.json()
            items = (data.get("data", {}).get("search", {})
                     .get("searchResult", {}).get("itemStacks", [{}])[0]
                     .get("items", []))
            if items:
                item = items[0]
                price = item.get("price")
                if isinstance(price, (int, float)) and price > 0:
                    log.info("Walmart GraphQL search: %s price=$%s", item_id, price)
                    return {
                        "item_id": item_id,
                        "store": "walmart",
                        "title": (item.get("name") or "Walmart Product")[:300],
                        "price": float(price),
                        "currency": "USD",
                        "image_url": item.get("imageInfo", {}).get("thumbnailUrl", ""),
                        "item_url": f"https://www.walmart.com/ip/{item_id}",
                    }
    except Exception as exc:
        log.debug("Walmart GraphQL search failed for %s: %s", item_id, exc)

    # ── Strategy 3: Mobile endpoint (lighter page, sometimes less bot detection)
    mobile_url = f"https://www.walmart.com/ip/seort/{item_id}"
    try:
        mobile_headers = {
            **headers,
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(mobile_url, headers=mobile_headers, timeout=15,
                            proxies=no_proxy, allow_redirects=True)
        if resp.status_code == 200 and "robot or human" not in resp.text.lower():
            soup = BeautifulSoup(resp.text, "html.parser")
            # Try __NEXT_DATA__ from mobile page
            nextdata = _extract_nextdata(soup, "walmart")
            if nextdata.get("price"):
                log.info("Walmart mobile page: %s price=$%s", item_id, nextdata["price"])
                return {
                    "item_id": item_id,
                    "store": "walmart",
                    "title": (nextdata.get("title") or "Walmart Product")[:300],
                    "price": nextdata["price"],
                    "currency": nextdata.get("currency", "USD"),
                    "image_url": nextdata.get("image", ""),
                    "item_url": f"https://www.walmart.com/ip/{item_id}",
                }
            # Try JSON-LD and meta from mobile page
            ld = _extract_jsonld(soup)
            if ld.get("price"):
                return {
                    "item_id": item_id,
                    "store": "walmart",
                    "title": (ld.get("title") or "Walmart Product")[:300],
                    "price": ld["price"],
                    "currency": ld.get("currency", "USD"),
                    "image_url": ld.get("image", ""),
                    "item_url": f"https://www.walmart.com/ip/{item_id}",
                }
    except Exception as exc:
        log.debug("Walmart mobile fetch failed for %s: %s", item_id, exc)

    log.warning("All Walmart API strategies failed for %s", item_id)
    return None


def _parse_walmart_api(data: dict, item_id: str) -> Optional[dict]:
    """Parse Walmart orchestra API response into a product dict."""
    try:
        # Navigate the response — structure varies
        product = data.get("product", data)

        # Sometimes wrapped in initialData
        if "initialData" in data:
            product = data["initialData"].get("data", {}).get("product", {})

        title = product.get("name", "Walmart Product")
        price_info = product.get("priceInfo", {})
        current = price_info.get("currentPrice", {})
        price = current.get("price")

        if not price:
            price = price_info.get("unitPrice", {}).get("price")
        if not price:
            pr = price_info.get("priceRange", {})
            price = pr.get("minPrice") or pr.get("maxPrice")

        image = ""
        img_info = product.get("imageInfo", {})
        if img_info.get("thumbnailUrl"):
            image = img_info["thumbnailUrl"]
        elif img_info.get("allImages"):
            imgs = img_info["allImages"]
            if imgs:
                image = imgs[0].get("url", "") if isinstance(imgs[0], dict) else str(imgs[0])

        if price and float(price) > 0:
            return {
                "item_id": item_id,
                "store": "walmart",
                "title": str(title)[:300],
                "price": float(price),
                "currency": current.get("currencyCode", "USD"),
                "image_url": image,
                "item_url": f"https://www.walmart.com/ip/{item_id}",
            }
    except Exception as exc:
        log.debug("Walmart API parse failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# URL builders for re-checking
# ---------------------------------------------------------------------------

def _build_url(store: str, item_id: str) -> str:
    """Build a product URL from store + item_id."""
    urls = {
        "amazon":    f"https://www.amazon.com/dp/{item_id}",
        "walmart":   f"https://www.walmart.com/ip/{item_id}",
        "ubereats":  f"https://www.ubereats.com/store/{item_id}",
        "bestbuy":   f"https://www.bestbuy.com/site/{item_id}.p",
        "nike":      f"https://www.nike.com/t/{item_id}",
        "lululemon": f"https://shop.lululemon.com/{item_id}",
        "safeway":   f"https://www.safeway.com/shop/product-detail.{item_id}.html",
        "costco":    f"https://www.costco.com/{item_id}.product.html",
        "temu":      f"https://www.temu.com/{item_id}.html",
        "homedepot": f"https://www.homedepot.com/p/{item_id}",
    }
    return urls.get(store, "")


# ---------------------------------------------------------------------------
# Unified fetch — the one public function
# ---------------------------------------------------------------------------

def fetch_product(url_or_id: str, store: Optional[str] = None) -> Optional[dict]:
    """
    Fetch product info from any supported store.

    Returns dict: item_id, store, title, price, currency, image_url, item_url
    """
    url_or_id = url_or_id.strip()

    if not store:
        store = detect_store(url_or_id)
    if not store:
        if _RAW_ASIN_RE.match(url_or_id):
            store = "amazon"
        else:
            return None

    item_id = extract_item_id(url_or_id, store)
    if not item_id:
        return None

    # Determine which URL to fetch
    is_full_url = url_or_id.startswith("http")
    url = url_or_id if is_full_url else _build_url(store, item_id)
    if not url:
        return None

    # ── Walmart: try API first (HTML scraping hits bot detection) ────────
    if store == "walmart":
        result = _walmart_api_fetch(item_id)
        if result and result.get("price"):
            result["item_url"] = url
            return result
        # API failed — fall through to HTML scraping as backup

    soup = _fetch_page(url)

    # Detect bot-detection pages and bail early
    if soup:
        page_text = soup.get_text(separator=" ", strip=True)[:500].lower()
        if any(phrase in page_text for phrase in ("robot or human", "are you a robot", "captcha", "press and hold", "verify you are human")):
            log.warning("Bot detection page for %s/%s — scraping blocked", store, item_id)
            # For Walmart, we already tried the API above. For others, return None.
            if store == "walmart":
                return None
            soup = None

    if not soup:
        return None

    # ── Layer 1: JSON-LD ──
    ld = _extract_jsonld(soup)

    # ── Layer 1b: __NEXT_DATA__ (Next.js hydration data — Walmart, etc.) ──
    nextdata = _extract_nextdata(soup, store)

    # ── Layer 2: <meta> tags ──
    meta = _extract_meta(soup)

    # ── Layer 3: CSS selectors ──
    css_price = _css_extract_price(soup, store)
    css_title = _css_extract_title(soup, store)
    css_image = _css_extract_image(soup, store)

    # ── Merge (prefer JSON-LD > __NEXT_DATA__ > meta > CSS) ──
    title = ld.get("title") or nextdata.get("title") or meta.get("title") or css_title or f"{STORE_NAMES.get(store, store)} Product"
    price = ld.get("price") or nextdata.get("price") or meta.get("price") or css_price
    image = ld.get("image") or nextdata.get("image") or meta.get("image") or css_image or ""
    currency = ld.get("currency") or nextdata.get("currency") or "USD"

    log.info("Scraped %s/%s: title=%s, price=%s, image=%s",
             store, item_id, title[:50], price, bool(image))

    return {
        "item_id": item_id,
        "store": store,
        "title": title[:300],
        "price": price,
        "currency": currency,
        "image_url": image,
        "item_url": url,
    }
