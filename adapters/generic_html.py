"""Generic HTML adapter for non-Shopify stores."""
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Patterns that indicate a product is available to buy
IN_STOCK_PATTERNS = re.compile(
    r"\b(add\s+to\s+cart|buy\s+now|add\s+to\s+bag|in\s+stock|pre-?order|preorder)\b",
    re.IGNORECASE,
)

# Patterns that indicate out of stock
OOS_PATTERNS = re.compile(
    r"\b(sold\s+out|out\s+of\s+stock|unavailable|notify\s+me|coming\s+soon|not\s+available)\b",
    re.IGNORECASE,
)

PREORDER_PATTERNS = re.compile(
    r"\b(pre-?order|preorder|coming\s+soon)\b",
    re.IGNORECASE,
)

PRICE_PATTERNS = re.compile(r"\$\s*[\d,]+\.?\d*")


def _extract_price(text: str) -> str | None:
    m = PRICE_PATTERNS.search(text)
    return m.group(0).replace(" ", "") if m else None


def _parse_product_card(card, base_url: str) -> dict | None:
    """Try to extract product info from a generic card/article/li element."""
    text = card.get_text(" ", strip=True)

    # Try to find a link for the product URL
    link = card.find("a", href=True)
    if not link:
        return None
    product_url = urljoin(base_url, link["href"])

    # Title: prefer heading tags, fallback to link text
    heading = card.find(re.compile(r"h[1-6]"))
    title = (heading.get_text(strip=True) if heading else link.get_text(strip=True)) or "Unknown"

    # Availability
    available = bool(IN_STOCK_PATTERNS.search(text))
    if OOS_PATTERNS.search(text) and not re.search(r"\badd\s+to\s+cart\b", text, re.I):
        available = False

    is_preorder = bool(PREORDER_PATTERNS.search(text)) and available

    price = _extract_price(text)

    return {
        "product_url": product_url,
        "title": title,
        "available": available,
        "is_preorder": is_preorder,
        "price": price,
    }


def _parse_page(html: str, page_url: str) -> list[dict]:
    """Parse an HTML page and return a list of normalized product dicts."""
    soup = BeautifulSoup(html, "html.parser")
    base_url = "{0.scheme}://{0.netloc}".format(urlparse(page_url))

    products: list[dict] = []
    seen_urls: set[str] = set()

    # Common card selectors (in priority order)
    card_selectors = [
        "[class*='product-card']",
        "[class*='product-item']",
        "[class*='product_card']",
        "[class*='product_item']",
        "article.product",
        "li.product",
        "[class*='ProductCard']",
        "[class*='ProductItem']",
        "[class*='item-product']",
        "[data-product-id]",
        "[data-product-handle]",
    ]

    cards = []
    for selector in card_selectors:
        cards = soup.select(selector)
        if len(cards) >= 2:
            break

    if len(cards) < 2:
        # Fallback: any <article> or grid <li> that contains a price
        for tag in soup.find_all(["article", "li"]):
            text = tag.get_text()
            if PRICE_PATTERNS.search(text) and tag.find("a"):
                cards.append(tag)

    for card in cards:
        result = _parse_product_card(card, base_url)
        if result and result["product_url"] not in seen_urls:
            seen_urls.add(result["product_url"])
            products.append(result)

    return products


def fetch_page(url: str, session: requests.Session) -> list[dict]:
    """Fetch an HTML page and extract products. Returns normalized list."""
    logger.debug("HTML fetch: %s", url)
    resp = session.get(url)
    resp.raise_for_status()
    products = _parse_page(resp.text, url)
    logger.info("Generic HTML adapter found %d products at %s", len(products), url)
    return products


# ── Per-domain adapters ──────────────────────────────────────────────────────

def _fetch_kaika(url: str, session: requests.Session) -> list[dict]:
    """kaika.com.au — standard HTML, products in article cards."""
    return fetch_page(url, session)


def _fetch_gamersroom(base_url: str, session: requests.Session) -> list[dict]:
    """
    gamersroom.com.au — Havealook custom platform.

    Product links match /shop/{slug}/{id}.
    Out-of-stock items have "(Out of stock)" in the link text before the price.
    Pages are paginated; append ?page=N to iterate (stops when a page returns
    fewer links than the previous page, or no product links at all).
    """
    from urllib.parse import urlparse, urlunparse, parse_qs

    PRODUCT_URL_RE = re.compile(
        r"^https?://(?:www\.)?gamersroom\.com\.au/shop/[^/]+/\d+",
        re.IGNORECASE,
    )
    OOS_RE = re.compile(r"\(Out\s+of\s+[Ss]tock\)", re.IGNORECASE)
    PREORDER_RE = re.compile(r"\b(pre-?order|preorder|coming\s+soon)\b", re.IGNORECASE)

    # Strip any existing page/filter params from the base URL so we control pagination
    parsed = urlparse(base_url)
    clean_base = urlunparse(parsed._replace(query="", fragment=""))

    all_products: list[dict] = []
    seen_urls: set[str] = set()

    page = 1
    while True:
        page_url = f"{clean_base}?page={page}&items=20&sortBy="
        logger.debug("Gamers Room fetch: %s", page_url)
        resp = session.get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found_on_page = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Ensure absolute URL
            if href.startswith("/"):
                href = "https://www.gamersroom.com.au" + href
            if not PRODUCT_URL_RE.match(href):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            link_text = a.get_text(" ", strip=True)
            is_oos = bool(OOS_RE.search(link_text))

            # Strip the OOS marker and price from the title
            title = OOS_RE.sub("", link_text)
            # Remove trailing price: "$XX.XX incl. GST" or similar
            title = re.sub(r"\s*\$[\d,]+\.?\d*.*$", "", title).strip()
            title = title or "Unknown"

            available = not is_oos
            is_preorder = bool(PREORDER_RE.search(link_text)) and available
            price = _extract_price(link_text)

            all_products.append({
                "product_url": href,
                "title": title,
                "available": available,
                "is_preorder": is_preorder,
                "price": price,
            })
            found_on_page += 1

        if found_on_page == 0:
            break  # No product links on this page — we're past the last page
        page += 1

    logger.info("Gamers Room adapter found %d products across %d page(s)", len(all_products), page - 1)
    return all_products


def _fetch_mg(url: str, session: requests.Session) -> list[dict]:
    """m-g.com.au pre-orders page."""
    return fetch_page(url, session)


def _fetch_nj(url: str, session: requests.Session) -> list[dict]:
    """njcollectables.com.au."""
    return fetch_page(url, session)


DOMAIN_ADAPTERS: dict[str, callable] = {
    "kaika.com.au": _fetch_kaika,
    "www.kaika.com.au": _fetch_kaika,
    "gamersroom.com.au": _fetch_gamersroom,
    "www.gamersroom.com.au": _fetch_gamersroom,
    "m-g.com.au": _fetch_mg,
    "www.m-g.com.au": _fetch_mg,
    "njcollectables.com.au": _fetch_nj,
    "www.njcollectables.com.au": _fetch_nj,
}


def get_domain_adapter(url: str):
    """Return domain-specific adapter fn, or None if no specific adapter exists."""
    host = urlparse(url).netloc.lower()
    return DOMAIN_ADAPTERS.get(host)


def fetch(url: str, session: requests.Session) -> list[dict]:
    """Entry point for generic HTML adapter — dispatches to domain adapter if available."""
    adapter_fn = get_domain_adapter(url)
    if adapter_fn:
        return adapter_fn(url, session)
    return fetch_page(url, session)
