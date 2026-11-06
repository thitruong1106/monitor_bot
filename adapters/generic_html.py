"""Generic HTML adapter for non-Shopify stores."""
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

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


# Query-string keys that only ever appear on listing/refine links, never on a
# product page (Neto/Maropost: rf=pr for price buckets, pgnum for pagination).
FACET_QUERY_KEYS = {"rf", "pr", "pgnum"}


def _extract_price(text: str) -> str | None:
    m = PRICE_PATTERNS.search(text)
    return m.group(0).replace(" ", "") if m else None


def _is_facet_link(product_url: str, page_url: str) -> bool:
    """
    True when a link is a filter/sort/pagination link back to the listing page
    rather than a product. Those links carry a price in their text
    ("$0 - $100") so the price-based fallback used to mistake them for products.
    """
    pu = urlparse(product_url)
    pg = urlparse(page_url)
    same_page = (
        pu.netloc.lower() == pg.netloc.lower()
        and pu.path.rstrip("/") == pg.path.rstrip("/")
    )
    if same_page:
        return True
    keys = {k.lower() for k in parse_qs(pu.query, keep_blank_values=True)}
    return bool(keys & FACET_QUERY_KEYS)


def _img_src(node, base_url: str) -> str | None:
    """Best image URL inside a card: lazy-load attributes first, then src."""
    img = node.find("img")
    if img is None:
        return None
    for attr in ("data-src", "data-original", "data-lazy", "src"):
        val = img.get(attr)
        if val and not str(val).startswith("data:"):
            return urljoin(base_url, str(val).strip())
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        first = str(srcset).split(",")[0].strip().split(" ")[0]
        if first:
            return urljoin(base_url, first)
    return None


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

    result = {
        "product_url": product_url,
        "title": title,
        "available": available,
        "is_preorder": is_preorder,
        "price": price,
    }
    image = _img_src(card, base_url)
    if image:
        result["image_url"] = image
    return result


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
        if not result or result["product_url"] in seen_urls:
            continue
        if _is_facet_link(result["product_url"], page_url):
            logger.debug("Ignoring facet/filter link %s", result["product_url"])
            continue
        seen_urls.add(result["product_url"])
        products.append(result)

    return products


# ── schema.org microdata (Neto / Maropost and many other themes) ─────────────

_AVAILABLE_SCHEMA = ("instock", "preorder", "presale", "limitedavailability", "onlineonly", "instoreonly", "backorder")
_UNAVAILABLE_SCHEMA = ("outofstock", "soldout", "discontinued")


def _itemprop_text(node, prop: str) -> str | None:
    el = node.find(attrs={"itemprop": prop})
    if el is None:
        return None
    for attr in ("content", "href", "src", "data-src"):
        if el.has_attr(attr) and el[attr]:
            return str(el[attr]).strip()
    return el.get_text(" ", strip=True) or None


def _parse_microdata_products(soup, page_url: str) -> list[dict]:
    """
    Parse product cards marked up with schema.org/Product microdata.
    Returns [] when the page has none, so callers can fall back.
    """
    base_url = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    nodes = [
        n for n in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product$", re.I)})
        # skip Products nested inside another Product (e.g. variants)
        if n.find_parent(attrs={"itemtype": re.compile(r"schema\.org/Product$", re.I)}) is None
    ]
    products: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        href = _itemprop_text(node, "url")
        if not href:
            link = node.find("a", href=True)
            href = link["href"] if link else None
        if not href:
            continue
        product_url = urljoin(base_url, href)
        if product_url in seen or _is_facet_link(product_url, page_url):
            continue

        title = _itemprop_text(node, "name")
        if not title:
            heading = node.find(re.compile(r"h[1-6]"))
            link = node.find("a", href=True)
            title = (heading.get_text(strip=True) if heading else (link.get_text(strip=True) if link else "")) or "Unknown"

        text = node.get_text(" ", strip=True)
        availability = (_itemprop_text(node, "availability") or "").lower().replace(" ", "")
        if any(k in availability for k in _UNAVAILABLE_SCHEMA):
            available = False
        elif any(k in availability for k in _AVAILABLE_SCHEMA):
            available = True
        else:
            available = bool(IN_STOCK_PATTERNS.search(text))
            if OOS_PATTERNS.search(text) and not re.search(r"\badd\s+to\s+cart\b", text, re.I):
                available = False

        is_preorder = available and (
            "preorder" in availability or "presale" in availability or bool(PREORDER_PATTERNS.search(text))
        )

        price = None
        raw_price = _itemprop_text(node, "price") or _itemprop_text(node, "lowPrice")
        if raw_price:
            try:
                price = f"${float(raw_price.replace('$', '').replace(',', '')):.2f}"
            except ValueError:
                price = _extract_price(raw_price)
        if not price:
            price = _extract_price(text)

        seen.add(product_url)
        item = {
            "product_url": product_url,
            "title": title,
            "available": available,
            "is_preorder": is_preorder,
            "price": price,
        }
        image = _itemprop_text(node, "image")
        image = urljoin(base_url, image) if image else _img_src(node, base_url)
        if image:
            item["image_url"] = image
        products.append(item)
    return products


def _next_page_url(soup, page_url: str) -> str | None:
    """Return the rel=next link if the listing is paginated, else None."""
    for tag in soup.find_all(["a", "link"], href=True):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if "next" in [r.lower() for r in rel]:
            return urljoin(page_url, tag["href"])
    return None


def fetch_page(url: str, session: requests.Session) -> list[dict]:
    """Fetch an HTML page and extract products. Returns normalized list."""
    logger.debug("HTML fetch: %s", url)
    resp = session.get(url)
    resp.raise_for_status()
    products = _parse_page(resp.text, url)
    logger.info("Generic HTML adapter found %d products at %s", len(products), url)
    return products


# ── Per-domain adapters ──────────────────────────────────────────────────────

KAIKA_MAX_PAGES = 10


def _fetch_kaika(url: str, session: requests.Session) -> list[dict]:
    """
    kaika.com.au — Neto/Maropost storefront (the "?pr=0-100&rf=pr" links are
    Neto's price-refine sidebar).

    Why the old approach never alerted: the generic parser found no
    "product-card"-style elements, fell back to "any <li> with a price and a
    link", and matched the nine price-range filter links in the sidebar
    ($0-$100, $100-$200, ...). Those nine "products" had no add-to-cart text,
    so every one read as unavailable forever.

    Now: parse schema.org/Product microdata (what Neto themes emit per
    product thumbnail), follow rel=next pagination, and ignore facet links.
    If no microdata is present we fall back to the generic card parser with
    facet links excluded; if that finds nothing the watch reports 0 products
    and the failure alert fires once, instead of silently reporting junk.
    """
    products: list[dict] = []
    seen: set[str] = set()
    page_url: str | None = url
    pages = 0
    used_microdata = False
    while page_url and pages < KAIKA_MAX_PAGES:
        pages += 1
        resp = session.get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        batch = _parse_microdata_products(soup, page_url)
        if batch:
            used_microdata = True
        elif pages == 1:
            batch = _parse_page(resp.text, page_url)
        for p in batch:
            if p["product_url"] not in seen:
                seen.add(p["product_url"])
                products.append(p)
        page_url = _next_page_url(soup, page_url) if used_microdata else None
    logger.info(
        "Kaika adapter found %d products across %d page(s) via %s",
        len(products), pages, "microdata" if used_microdata else "generic cards",
    )
    return products


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
