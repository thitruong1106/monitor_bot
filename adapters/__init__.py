"""Adapter registry — maps URLs to the right scraping adapter."""
import logging
from urllib.parse import urlparse

import requests

from adapters import generic_html, shopify

logger = logging.getLogger(__name__)

# Domains known to be Shopify (avoid probing on every run)
_KNOWN_SHOPIFY_DOMAINS: set[str] = set()
# Domains confirmed NOT Shopify
_KNOWN_NON_SHOPIFY_DOMAINS: set[str] = set()


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def get_adapter(url: str, session: requests.Session | None = None):
    """
    Return (adapter_module, fetch_fn) for the given URL.

    Returns a callable: fetch_fn(url, session) -> list[dict]
    """
    host = _domain(url)

    # Check known caches first
    if host in _KNOWN_SHOPIFY_DOMAINS:
        return _shopify_fn(url)
    if host in _KNOWN_NON_SHOPIFY_DOMAINS:
        return generic_html.fetch

    # Try generic HTML domain registry first (always non-Shopify)
    if generic_html.get_domain_adapter(url):
        _KNOWN_NON_SHOPIFY_DOMAINS.add(host)
        return generic_html.fetch

    # Probe (requires a live session — only done in watcher.py)
    if session is not None:
        if shopify.is_shopify(url, session):
            _KNOWN_SHOPIFY_DOMAINS.add(host)
            return _shopify_fn(url)
        else:
            _KNOWN_NON_SHOPIFY_DOMAINS.add(host)
            return generic_html.fetch

    # Default: assume Shopify for /collections/ or /products/ URLs, else generic
    if "/collections/" in url or "/products/" in url or url.endswith(".json"):
        return _shopify_fn(url)
    return generic_html.fetch


def _shopify_fn(url: str):
    """Return the right Shopify fetch fn based on URL path."""
    if "/collections/" in url:
        return shopify.fetch_collection
    return shopify.fetch_product


def is_shopify_url(url: str) -> bool:
    """Heuristic — true for known Shopify URL patterns."""
    host = _domain(url)
    if host in _KNOWN_SHOPIFY_DOMAINS:
        return True
    if host in _KNOWN_NON_SHOPIFY_DOMAINS:
        return False
    # Check generic HTML registry
    if generic_html.get_domain_adapter(url):
        return False
    # Path heuristic
    return "/collections/" in url or "/products/" in url
