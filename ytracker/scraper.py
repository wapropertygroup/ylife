"""
ytracker.scraper
~~~~~~~~~~~~~~~~
Multi-store price scraper.
Supports: Amazon, Walmart, Uber Eats, Nike, Lululemon, Best Buy, Safeway,
          Costco, Temu, Home Depot, Target.

Strategy per page (layered fallback):
  1. HTTP fetch  →  JSON-LD / __NEXT_DATA__ / <meta> / CSS selectors
  2. If HTTP fails (bot detection, no price, JS-only rendering)
     → Playwright headless Chromium renders the page, then same extractors.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
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
    "target":    "Target",
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
    "target":    "#cc0000",
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
    ("target.com",    "target"),
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
    Bypasses system proxy entirely via trust_env=False session.
    Uses realistic browser headers to avoid bot detection.
    """
    _rate_limit()

    session = requests.Session()
    session.trust_env = False  # Ignore system/corporate proxy completely

    for attempt in range(2):
        try:
            headers = _get_headers()
            headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            })
            resp = session.get(url, headers=headers, timeout=timeout,
                               allow_redirects=True)
            log.info("Fetch %s → %d (%d bytes, attempt %d)",
                     url[:80], resp.status_code, len(resp.text), attempt + 1)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            log.warning("Fetch %s returned %d (attempt %d)", url, resp.status_code, attempt + 1)
        except Exception as exc:
            log.warning("Fetch %s failed (attempt %d): %s", url, attempt + 1, exc)
        if attempt == 0:
            time.sleep(1 + random.random())
    return None


# ---------------------------------------------------------------------------
# Playwright headless browser fallback
# ---------------------------------------------------------------------------
# Lazy-loaded single browser instance shared across all requests.
# Falls back gracefully if playwright is not installed.

_browser_lock = threading.Lock()
_browser = None          # playwright Browser instance
_playwright_obj = None   # playwright Playwright instance
_pw_available: Optional[bool] = None  # None = not checked yet


def _is_playwright_available() -> bool:
    """Check once if playwright + chromium are installed."""
    global _pw_available
    if _pw_available is not None:
        return _pw_available
    try:
        import playwright.sync_api  # noqa: F401
        _pw_available = True
        log.info("Playwright available — headless browser fallback enabled")
    except ImportError:
        _pw_available = False
        log.info("Playwright not installed — headless browser fallback disabled")
    return _pw_available


def _get_browser():
    """Lazy-init a shared Chromium browser instance (thread-safe)."""
    global _browser, _playwright_obj
    if _browser and _browser.is_connected():
        return _browser
    with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser
        try:
            from playwright.sync_api import sync_playwright
            _playwright_obj = sync_playwright().start()
            _browser = _playwright_obj.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            log.info("Playwright Chromium browser launched")
            return _browser
        except Exception as exc:
            log.warning("Failed to launch Playwright browser: %s", exc)
            return None


def _fetch_page_browser(url: str, store: str, timeout: int = 30) -> Optional[BeautifulSoup]:
    """Fetch a page using headless Chromium (Playwright).

    Renders JavaScript, waits for network idle, then returns the fully
    rendered DOM as BeautifulSoup.
    """
    if not _is_playwright_available():
        return None

    browser = _get_browser()
    if not browser:
        return None

    context = None
    page = None
    try:
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            # Hide automation signals
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        # Remove webdriver flag that sites detect
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        page = context.new_page()
        log.info("Browser fetch: %s", url[:80])

        resp = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        status = resp.status if resp else 0
        log.info("Browser navigation: %s → %d", url[:60], status)

        # Grab content
        html = page.content()
        log.info("Browser fetch complete: %d bytes", len(html))
        return BeautifulSoup(html, "html.parser")

    except Exception as exc:
        # Try to grab whatever content we have even on error
        try:
            if page:
                html = page.content()
                if html and len(html) > 1000:
                    log.info("Browser partial content salvaged: %d bytes", len(html))
                    return BeautifulSoup(html, "html.parser")
        except Exception:
            pass
        log.warning("Browser fetch failed for %s: %s", url[:60], exc)
        return None
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _is_bot_page(soup: BeautifulSoup) -> bool:
    """Detect bot-detection / CAPTCHA pages."""
    if not soup:
        return False
    page_text = soup.get_text(separator=" ", strip=True)[:500].lower()
    return any(phrase in page_text for phrase in (
        "robot or human", "are you a robot", "captcha",
        "press and hold", "verify you are human",
        "access denied", "please verify",
    ))


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
    "target": [
        ("[data-test='product-price']", None),
        ("span[data-test='product-price']", None),
        ("div[data-test='product-price'] span", None),
        ("[data-test='product-regular-price']", None),
        ("[data-test='product-sale-price']", None),
        ("span[class*='CurrentPrice']", None),
        ("div[class*='price-wrapper'] span", None),
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
    "target":    ["h1[data-test='product-title']", "span[data-test='product-title']", "h1"],
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
    "target":    [("img[data-test='product-hero']", "src"), ("div[data-test='image-gallery'] img", "src"), ("picture img", "src")],
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
_TARGET_ID_RE = re.compile(r"/A-(\d{8,9})")


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

    if store == "target":
        # Target URLs: /p/product-name/-/A-12345678
        m = _TARGET_ID_RE.search(url)
        if m:
            return m.group(1)
        # Fallback: look for TCIN in query string
        qs = parse_qs(urlparse(url).query)
        tcin = qs.get("tcin", qs.get("TCIN", [None]))[0]
        if tcin:
            return tcin

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

    session = requests.Session()
    session.trust_env = False  # Bypass corporate proxy

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
    api_url = f"https://www.walmart.com/orchestra/home/auto/product/{item_id}"
    try:
        log.info("Walmart strategy 1 (orchestra API): %s", api_url)
        resp = session.get(api_url, headers=headers, timeout=15, allow_redirects=True)
        log.info("  → %d (%d bytes)", resp.status_code, len(resp.text))
        if resp.status_code == 200:
            data = resp.json()
            result = _parse_walmart_api(data, item_id)
            if result and result.get("price"):
                log.info("  → SUCCESS: $%s — %s", result["price"], result.get("title", "")[:60])
                return result
            log.info("  → JSON parsed but no price found")
    except Exception as exc:
        log.info("  → FAILED: %s", exc)

    # ── Strategy 2: Walmart search API with item ID ───────────────────
    search_api = "https://www.walmart.com/orchestra/snb/graphql"
    try:
        log.info("Walmart strategy 2 (GraphQL search): %s", item_id)
        gql_headers = {**headers, "Content-Type": "application/json"}
        payload = {
            "query": "query($query:String!){search(query:$query,count:1){searchResult{itemStacks{items{id name price imageInfo{thumbnailUrl}}}}}}",
            "variables": {"query": item_id},
        }
        resp = session.post(search_api, json=payload, headers=gql_headers, timeout=15)
        log.info("  → %d (%d bytes)", resp.status_code, len(resp.text))
        if resp.status_code == 200:
            data = resp.json()
            items = (data.get("data", {}).get("search", {})
                     .get("searchResult", {}).get("itemStacks", [{}])[0]
                     .get("items", []))
            if items:
                item = items[0]
                price = item.get("price")
                if isinstance(price, (int, float)) and price > 0:
                    log.info("  → SUCCESS: $%s — %s", price, item.get("name", "")[:60])
                    return {
                        "item_id": item_id,
                        "store": "walmart",
                        "title": (item.get("name") or "Walmart Product")[:300],
                        "price": float(price),
                        "currency": "USD",
                        "image_url": item.get("imageInfo", {}).get("thumbnailUrl", ""),
                        "item_url": f"https://www.walmart.com/ip/{item_id}",
                    }
            log.info("  → No matching items found")
    except Exception as exc:
        log.info("  → FAILED: %s", exc)

    # ── Strategy 3: Mobile endpoint (lighter page, sometimes less bot detection)
    mobile_url = f"https://www.walmart.com/ip/seort/{item_id}"
    try:
        log.info("Walmart strategy 3 (mobile page): %s", mobile_url)
        mobile_headers = {
            **headers,
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = session.get(mobile_url, headers=mobile_headers, timeout=15, allow_redirects=True)
        log.info("  → %d (%d bytes)", resp.status_code, len(resp.text))
        if resp.status_code == 200 and "robot or human" not in resp.text.lower():
            soup = BeautifulSoup(resp.text, "html.parser")
            # Try __NEXT_DATA__ from mobile page
            nextdata = _extract_nextdata(soup, "walmart")
            if nextdata.get("price"):
                log.info("  → SUCCESS via __NEXT_DATA__: $%s", nextdata["price"])
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
                log.info("  → SUCCESS via JSON-LD: $%s", ld["price"])
                return {
                    "item_id": item_id,
                    "store": "walmart",
                    "title": (ld.get("title") or "Walmart Product")[:300],
                    "price": ld["price"],
                    "currency": ld.get("currency", "USD"),
                    "image_url": ld.get("image", ""),
                    "item_url": f"https://www.walmart.com/ip/{item_id}",
                }
            log.info("  → Page loaded but no price extracted (bot page? %s)",
                     "yes" if "robot" in resp.text.lower() else "no")
        elif resp.status_code == 200:
            log.info("  → Bot detection page returned")
    except Exception as exc:
        log.info("  → FAILED: %s", exc)

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
# Target API (redsky.target.com — public product API, no auth needed)
# ---------------------------------------------------------------------------

_TARGET_REDSKY_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"  # public browser key


def _target_api_fetch(item_id: str) -> Optional[dict]:
    """Fetch Target product data via their public Redsky API."""
    _rate_limit()
    session = requests.Session()
    session.trust_env = False

    api_url = (
        f"https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
        f"?key={_TARGET_REDSKY_KEY}&tcin={item_id}"
        f"&pricing_store_id=3991&has_pricing_store_id=true"
    )
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Origin": "https://www.target.com",
        "Referer": "https://www.target.com/",
    }
    try:
        log.info("Target API (redsky): tcin=%s", item_id)
        resp = session.get(api_url, headers=headers, timeout=15)
        log.info("  → %d (%d bytes)", resp.status_code, len(resp.text))
        if resp.status_code == 200:
            data = resp.json()
            product = data.get("data", {}).get("product", {})
            if not product:
                log.info("  → No product in response")
                return None

            title = product.get("item", {}).get("product_description", {}).get("title", "Target Product")
            price_data = product.get("price", {})
            price = price_data.get("formatted_current_price") or price_data.get("current_retail") or price_data.get("reg_retail")
            if isinstance(price, str):
                price = _clean_price(price)
            elif isinstance(price, (int, float)):
                price = float(price)
            else:
                price = None

            images = product.get("item", {}).get("enrichment", {}).get("images", {})
            image_url = images.get("primary_image_url", "")

            if price and price > 0:
                log.info("  → SUCCESS: $%s — %s", price, str(title)[:60])
                return {
                    "item_id": item_id, "store": "target",
                    "title": str(title)[:300], "price": float(price),
                    "currency": "USD", "image_url": image_url,
                    "item_url": f"https://www.target.com/p/-/A-{item_id}",
                }
            log.info("  → Product found but no price extracted")
    except Exception as exc:
        log.info("  → FAILED: %s", exc)
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
        "target":    f"https://www.target.com/p/-/A-{item_id}",
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

    # ── Target: try Redsky API first (reliable, no bot detection) ─────
    if store == "target":
        result = _target_api_fetch(item_id)
        if result and result.get("price"):
            result["item_url"] = url
            return result

    soup = _fetch_page(url)
    used_browser = False

    if _is_bot_page(soup):
        log.warning("Bot detection via HTTP for %s/%s — will try browser", store, item_id)
        soup = None

    # ── Phase 2: Extract from HTTP response ─────────────────────────────
    result = _extract_product_from_soup(soup, store) if soup else None

    # ── Phase 3: Browser fallback if HTTP failed or got no price ────────
    if not result or not result.get("price"):
        reason = "no page" if not soup else "no price"
        log.info("HTTP insufficient for %s/%s (%s) — trying browser", store, item_id, reason)
        browser_soup = _fetch_page_browser(url, store)
        if browser_soup and not _is_bot_page(browser_soup):
            used_browser = True
            browser_result = _extract_product_from_soup(browser_soup, store)
            if browser_result and browser_result.get("price"):
                result = browser_result
        elif browser_soup:
            log.warning("Browser also got bot page for %s/%s", store, item_id)

    if not result:
        return None

    method = "browser" if used_browser else "http"
    log.info("Scraped %s/%s [%s]: title=%s, price=%s",
             store, item_id, method, result.get("title", "?")[:50], result.get("price"))
    result["item_id"] = item_id
    result["store"] = store
    result["item_url"] = url
    return result


def _extract_product_from_soup(soup: BeautifulSoup, store: str) -> Optional[dict]:
    """Run all extraction layers on a BeautifulSoup page."""
    if not soup:
        return None
    ld = _extract_jsonld(soup)
    nextdata = _extract_nextdata(soup, store)
    meta = _extract_meta(soup)
    css_price = _css_extract_price(soup, store)
    css_title = _css_extract_title(soup, store)
    css_image = _css_extract_image(soup, store)
    title = ld.get("title") or nextdata.get("title") or meta.get("title") or css_title or f"{STORE_NAMES.get(store, store)} Product"
    price = ld.get("price") or nextdata.get("price") or meta.get("price") or css_price
    image = ld.get("image") or nextdata.get("image") or meta.get("image") or css_image or ""
    currency = ld.get("currency") or nextdata.get("currency") or "USD"
    return {"title": title[:300], "price": price, "currency": currency, "image_url": image}
