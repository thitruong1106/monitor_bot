"""Tests for adapters: Shopify URL building, filter logic, dispatch, HTML parsing."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from adapters import shopify, generic_html
from adapters import get_adapter, is_shopify_url
import adapters


# ── URL / endpoint construction ──────────────────────────────────────────────

def test_collection_json_url_basic():
    url = "https://example.myshopify.com/collections/one-piece"
    result = shopify._collection_json_url(url)
    assert result == "https://example.myshopify.com/collections/one-piece/products.json"


def test_collection_json_url_strips_query_and_fragment():
    url = "https://shop.com/collections/tcg?pf_st_availability=in-stock#filter"
    result = shopify._collection_json_url(url)
    assert result == "https://shop.com/collections/tcg/products.json"
    assert "?" not in result
    assert "#" not in result


def test_clean_url_strips_srsltid():
    url = "https://shop.com/collections/op?srsltid=abc123&pf_st_availability=in-stock"
    result = shopify.clean_url(url)
    assert "srsltid" not in result
    assert "pf_st_availability=in-stock" in result


def test_clean_url_strips_utm():
    url = "https://shop.com/products/booster?utm_source=google&utm_medium=cpc"
    result = shopify.clean_url(url)
    assert "utm_source" not in result
    assert "utm_medium" not in result


# ── Client-side filter: in-stock ─────────────────────────────────────────────

def _make_shopify_product(handle: str, available: bool, product_type: str = "Booster Box") -> dict:
    return {
        "title": handle.replace("-", " ").title(),
        "handle": handle,
        "product_type": product_type,
        "tags": [],
        "variants": [{"price": "50.00", "available": available}],
    }


def test_filter_in_stock_removes_unavailable():
    products = [
        _make_shopify_product("a", available=True),
        _make_shopify_product("b", available=False),
    ]
    filtered = shopify._apply_client_filters(products, "pf_st_availability=in-stock")
    assert len(filtered) == 1
    assert filtered[0]["handle"] == "a"


def test_filter_in_stock_keeps_all_when_no_filter():
    products = [
        _make_shopify_product("a", available=True),
        _make_shopify_product("b", available=False),
    ]
    filtered = shopify._apply_client_filters(products, "")
    assert len(filtered) == 2


# ── Client-side filter: product type ─────────────────────────────────────────

def test_filter_product_type_single():
    products = [
        _make_shopify_product("a", available=True, product_type="Booster Box"),
        _make_shopify_product("b", available=True, product_type="Collection Box"),
        _make_shopify_product("c", available=True, product_type="Sleeve"),
    ]
    filtered = shopify._apply_client_filters(products, "pf_pt_product_type=Booster+Box")
    assert len(filtered) == 1
    assert filtered[0]["handle"] == "a"


def test_filter_product_type_multiple_values():
    products = [
        _make_shopify_product("a", available=True, product_type="Booster Box"),
        _make_shopify_product("b", available=True, product_type="Collection Box"),
        _make_shopify_product("c", available=True, product_type="Sleeve"),
    ]
    qs = "pf_pt_product_type=Booster+Box&pf_pt_product_type=Collection+Box"
    filtered = shopify._apply_client_filters(products, qs)
    assert len(filtered) == 2
    handles = {p["handle"] for p in filtered}
    assert handles == {"a", "b"}


# ── Preorder detection ────────────────────────────────────────────────────────

def test_preorder_detected_in_title():
    p = {"title": "OP09 Preorder Booster Box", "handle": "op09", "tags": []}
    assert shopify._is_preorder(p) is True


def test_preorder_detected_in_tag():
    p = {"title": "OP09 Booster Box", "handle": "op09", "tags": ["pre-order", "tcg"]}
    assert shopify._is_preorder(p) is True


def test_no_preorder_regular_product():
    p = {"title": "OP07 Booster Box", "handle": "op07", "tags": ["in-stock"]}
    assert shopify._is_preorder(p) is False


# ── fetch_collection: mocked HTTP ────────────────────────────────────────────

def _mock_session_collection(products: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"products": products}
    resp.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = resp
    return session


def test_fetch_collection_basic():
    raw_products = [_make_shopify_product("op09-booster", available=True)]
    session = _mock_session_collection(raw_products)
    results = shopify.fetch_collection("https://shop.com/collections/one-piece", session)
    assert len(results) == 1
    assert results[0]["title"] == "Op09 Booster"
    assert results[0]["available"] is True
    assert results[0]["product_url"] == "https://shop.com/products/op09-booster"


def test_fetch_collection_applies_filter():
    raw_products = [
        _make_shopify_product("a", available=True, product_type="Booster Box"),
        _make_shopify_product("b", available=False, product_type="Booster Box"),
    ]
    session = _mock_session_collection(raw_products)
    url = "https://shop.com/collections/one-piece?pf_st_availability=in-stock"
    results = shopify.fetch_collection(url, session)
    assert len(results) == 1
    assert results[0]["product_url"].endswith("/products/a")


# ── Adapter dispatch ──────────────────────────────────────────────────────────

def test_shopify_collections_url_dispatches_to_collection_fn():
    fn = get_adapter("https://shop.com/collections/one-piece")
    assert fn is shopify.fetch_collection


def test_shopify_products_url_dispatches_to_product_fn():
    fn = get_adapter("https://shop.com/products/booster-box")
    assert fn is shopify.fetch_product


def test_known_non_shopify_domain_dispatches_to_generic():
    fn = get_adapter("https://www.kaika.com.au/series/one-piece/")
    assert fn is generic_html.fetch


def test_gamersroom_dispatches_to_generic():
    fn = get_adapter("https://www.gamersroom.com.au/shop/category/one-piece-tcg/11249")
    assert fn is generic_html.fetch


def test_mg_dispatches_to_generic():
    fn = get_adapter("https://www.m-g.com.au/pre-orders/")
    assert fn is generic_html.fetch


def test_nj_dispatches_to_generic():
    fn = get_adapter("https://njcollectables.com.au/trading-card-games/one-piece")
    assert fn is generic_html.fetch


# ── is_shopify_url ────────────────────────────────────────────────────────────

def test_is_shopify_url_collections():
    assert is_shopify_url("https://shop.com/collections/one-piece") is True


def test_is_shopify_url_products():
    assert is_shopify_url("https://shop.com/products/booster") is True


def test_is_shopify_url_known_non_shopify():
    assert is_shopify_url("https://www.kaika.com.au/series/one-piece/") is False


# ── Generic HTML adapter ──────────────────────────────────────────────────────

_SAMPLE_HTML = """
<html><body>
<div class="product-item">
  <a href="/products/booster-a">OP09 Booster Box</a>
  <h3>OP09 Booster Box</h3>
  <span>$69.95</span>
  <button>Add to Cart</button>
</div>
<div class="product-item">
  <a href="/products/booster-b">OP08 Booster Box</a>
  <h3>OP08 Booster Box</h3>
  <span>$59.95</span>
  <span>Sold Out</span>
</div>
</body></html>
"""


def _mock_html_session(html: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = resp
    return session


def test_generic_html_parses_available():
    session = _mock_html_session(_SAMPLE_HTML)
    results = generic_html.fetch_page("https://shop.com/collection/one-piece", session)
    available = [r for r in results if r["available"]]
    assert any("OP09" in r["title"] for r in available)


def test_generic_html_parses_sold_out():
    session = _mock_html_session(_SAMPLE_HTML)
    results = generic_html.fetch_page("https://shop.com/collection/one-piece", session)
    oos = [r for r in results if not r["available"]]
    assert any("OP08" in r["title"] for r in oos)


def test_generic_html_extracts_price():
    session = _mock_html_session(_SAMPLE_HTML)
    results = generic_html.fetch_page("https://shop.com/collection/one-piece", session)
    prices = [r["price"] for r in results if r["price"]]
    assert any("69.95" in p or "59.95" in p for p in prices)


def test_generic_html_product_url_is_absolute():
    session = _mock_html_session(_SAMPLE_HTML)
    results = generic_html.fetch_page("https://shop.com/collection/one-piece", session)
    for r in results:
        assert r["product_url"].startswith("https://")
