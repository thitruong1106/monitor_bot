"""Shopify adapter — uses public .json endpoints, no HTML scraping."""
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

PREORDER_PATTERNS = re.compile(
    r"\b(pre-?order|coming\s+soon|preorder)\b", re.IGNORECASE
)

TRACKING_PARAMS = {"srsltid", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid"}


def clean_url(url: str) -> str:
    """Strip tracking query params and fragments from a URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    cleaned_qs = {k: v for k, v in qs.items() if k not in TRACKING_PARAMS}
    # Rebuild query string preserving order isn't critical here
    new_query = "&".join(
        f"{k}={v[0]}" for k, v in cleaned_qs.items()
    )
    return urlunparse(parsed._replace(query=new_query, fragment=""))


def _collection_json_url(collection_url: str) -> str:
    """Convert a Shopify collection URL to its .json endpoint."""
    parsed = urlparse(collection_url)
    # Keep only the path part (drop query/fragment for the API call)
    path = parsed.path.rstrip("/")
    base = f"{parsed.scheme}://{parsed.netloc}{path}/products.json"
    return base


def _is_preorder(product: dict) -> bool:
    title = product.get("title", "")
    tags = product.get("tags", [])
    handle = product.get("handle", "")
    text = " ".join([title, handle] + (tags if isinstance(tags, list) else []))
    return bool(PREORDER_PATTERNS.search(text))


def _normalize_price(product: dict) -> str | None:
    variants = product.get("variants", [])
    if not variants:
        return None
    prices = [v.get("price") for v in variants if v.get("price")]
    if not prices:
        return None
    try:
        min_price = min(float(p) for p in prices)
        return f"${min_price:.2f}"
    except (ValueError, TypeError):
        return str(prices[0])


def _product_available(product: dict) -> bool:
    variants = product.get("variants", [])
    return any(v.get("available", False) for v in variants)


def _apply_client_filters(products: list[dict], query_string: str) -> list[dict]:
    """
    Apply Shopify storefront filter params client-side.
    Supported:
      pf_st_availability=in-stock  → keep only available products
      pf_pt_product_type=Booster+Box (repeatable) → keep matching product_type
    """
    if not query_string:
        return products

    qs = parse_qs(query_string)

    # Availability filter
    avail_values = [v.lower() for v in qs.get("pf_st_availability", [])]
    if "in-stock" in avail_values:
        products = [p for p in products if _product_available(p)]

    # Product type filter (OR across repeated values)
    type_values = [v.strip().lower() for v in qs.get("pf_pt_product_type", [])]
    if type_values:
        products = [
            p for p in products
            if (p.get("product_type") or "").strip().lower() in type_values
        ]

    return products


def fetch_collection(url: str, session: requests.Session) -> list[dict]:
    """
    Fetch all products from a Shopify collection and return normalized dicts.
    Handles pagination (page param) and client-side filter params.
    """
    parsed = urlparse(url)
    query_string = parsed.query

    api_base = _collection_json_url(url)
    products_raw: list[dict] = []
    page = 1

    while True:
        api_url = f"{api_base}?limit=250&page={page}"
        logger.debug("Fetching %s", api_url)
        resp = session.get(api_url)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("products", [])
        if not batch:
            break
        products_raw.extend(batch)
        if len(batch) < 250:
            break
        page += 1

    products_raw = _apply_client_filters(products_raw, query_string)

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    results = []
    for p in products_raw:
        product_url = urljoin(base_url, f"/products/{p['handle']}")
        results.append({
            "product_url": product_url,
            "title": p.get("title", ""),
            "available": _product_available(p),
            "is_preorder": _is_preorder(p),
            "price": _normalize_price(p),
        })
    return results


def fetch_product(url: str, session: requests.Session) -> list[dict]:
    """Fetch a single Shopify product URL."""
    clean = clean_url(url)
    parsed = urlparse(clean)
    path = parsed.path.rstrip("/")
    api_url = f"{parsed.scheme}://{parsed.netloc}{path}.json"
    logger.debug("Fetching product %s", api_url)
    resp = session.get(api_url)
    resp.raise_for_status()
    data = resp.json()
    p = data.get("product", {})
    if not p:
        return []
    product_url = f"{parsed.scheme}://{parsed.netloc}/products/{p['handle']}"
    return [{
        "product_url": product_url,
        "title": p.get("title", ""),
        "available": _product_available(p),
        "is_preorder": _is_preorder(p),
        "price": _normalize_price(p),
    }]


def is_shopify(url: str, session: requests.Session) -> bool:
    """
    Detect whether a URL belongs to a Shopify store.
    Tries hitting /products.json on the root — a 200 response is a strong signal.
    Falls back to checking page HTML for 'Shopify' string.
    """
    parsed = urlparse(url)
    probe_url = f"{parsed.scheme}://{parsed.netloc}/products.json?limit=1"
    try:
        r = session.get(probe_url, timeout=8)
        if r.status_code == 200:
            try:
                data = r.json()
                if "products" in data:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    # Fallback: check HTML
    try:
        r = session.get(url, timeout=10)
        if "Shopify" in r.text or "shopify" in r.text:
            return True
    except Exception:
        pass
    return False
