"""Shopify adapter — uses public .json endpoints, no HTML scraping."""
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests

from ratelimit import RateLimited

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


def _image_url(product: dict) -> str | None:
    """First product image from a products.json item (or a .js payload)."""
    images = product.get("images") or []
    first = images[0] if images else product.get("featured_image") or product.get("image")
    if isinstance(first, dict):
        first = first.get("src")
    if not first or not isinstance(first, str):
        return None
    return "https:" + first if first.startswith("//") else first


def _buy_variant(product: dict) -> dict | None:
    """The variant a quick-buy link should use: first available, else first."""
    variants = product.get("variants") or []
    for v in variants:
        if v.get("available") and v.get("id"):
            return v
    return variants[0] if variants and variants[0].get("id") else None


def _quick_buy_links(base_url: str, product: dict) -> dict:
    """
    Shopify links that skip the product page:
      cart_url     /cart/add?id=VARIANT&quantity=1  (adds to cart, opens cart)
      checkout_url /cart/VARIANT:1                   (cart permalink, opens checkout)
    """
    variant = _buy_variant(product)
    if not variant:
        return {}
    vid = variant["id"]
    return {
        "cart_url": f"{base_url}/cart/add?id={vid}&quantity=1",
        "checkout_url": f"{base_url}/cart/{vid}:1",
    }


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
    type_values = [v.lower() for v in qs.get("pf_pt_product_type", [])]
    if type_values:
        products = [
            p for p in products
            if p.get("product_type", "").lower() in type_values
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
        item = {
            "product_url": product_url,
            "title": p.get("title", ""),
            "available": _product_available(p),
            "is_preorder": _is_preorder(p),
            "price": _normalize_price(p),
        }
        image = _image_url(p)
        if image:
            item["image_url"] = image
        item.update(_quick_buy_links(base_url, p))
        results.append(item)
    return results


def _product_path(url: str) -> str:
    """'/collections/x/products/handle/' or '/products/handle' -> '/products/handle'."""
    path = urlparse(url).path.rstrip("/")
    if "/products/" in path:
        return "/products/" + path.split("/products/", 1)[1].split("/")[0]
    return path


def _parse_js_product(data: dict, base_url: str) -> dict:
    """
    Parse Shopify's AJAX product payload (/products/<handle>.js).

    This endpoint carries `available` on the product and on each variant, and
    prices in cents. The /products/<handle>.json endpoint does NOT carry
    variant availability, so a product-page watch read from it would always
    look sold out and could never alert.
    """
    variants = data.get("variants") or []
    available = bool(data.get("available")) or any(v.get("available") for v in variants)
    cents = [v.get("price") for v in variants if v.get("available") and v.get("price") is not None]
    if not cents:
        cents = [v.get("price") for v in variants if v.get("price") is not None]
    if not cents and data.get("price") is not None:
        cents = [data["price"]]
    price = None
    if cents:
        try:
            price = f"${min(float(c) for c in cents) / 100:.2f}"
        except (TypeError, ValueError):
            price = None
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    item = {
        "product_url": f"{base_url}/products/{data['handle']}",
        "title": data.get("title", ""),
        "available": available,
        "is_preorder": _is_preorder({"title": data.get("title", ""), "handle": data.get("handle", ""), "tags": tags}),
        "price": price,
    }
    image = _image_url(data)
    if image:
        item["image_url"] = image
    item.update(_quick_buy_links(base_url, data))
    return item


def fetch_product(url: str, session: requests.Session) -> list[dict]:
    """
    Fetch a single Shopify product page watch.

    Reads /products/<handle>.js (has availability). Falls back to .json only if
    .js is not JSON; if .json has no availability either, this raises so the
    watch reports a failure instead of silently reading "sold out" forever.
    """
    parsed = urlparse(clean_url(url))
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = _product_path(url)

    js_url = f"{base_url}{path}.js"
    logger.debug("Fetching product %s", js_url)
    resp = session.get(js_url)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("handle"):
        return [_parse_js_product(data, base_url)]

    json_url = f"{base_url}{path}.json"
    logger.debug("Falling back to %s", json_url)
    resp = session.get(json_url)
    resp.raise_for_status()
    p = resp.json().get("product", {})
    if not p:
        return []
    variants = p.get("variants") or []
    if not any("available" in v for v in variants):
        raise ValueError(f"{json_url} has no variant availability; cannot tell if it is in stock")
    item = {
        "product_url": f"{base_url}/products/{p['handle']}",
        "title": p.get("title", ""),
        "available": _product_available(p),
        "is_preorder": _is_preorder(p),
        "price": _normalize_price(p),
    }
    image = _image_url(p)
    if image:
        item["image_url"] = image
    item.update(_quick_buy_links(base_url, p))
    return [item]


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
    except RateLimited:
        raise   # don't cache "not Shopify" just because the site said slow down
    except Exception:
        pass
    # Fallback: check HTML
    try:
        r = session.get(url, timeout=10)
        if "Shopify" in r.text or "shopify" in r.text:
            return True
    except RateLimited:
        raise
    except Exception:
        pass
    return False
