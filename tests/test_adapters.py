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


# ── Fix 3: Kaika (Neto storefront) ───────────────────────────────────────────
#
# What state.json showed for the Kaika watch: nine "products" whose URLs were
# https://www.kaika.com.au/series/one-piece/?pr=0-100&rf=pr, ?pr=100-200&rf=pr,
# ... i.e. the price-range filter links in the sidebar, all "unavailable".

_KAIKA_PAGE_URL = "https://www.kaika.com.au/series/one-piece/"

_KAIKA_PRICE_SIDEBAR = """
<div class="sidebar">
  <h4>Price</h4>
  <ul class="list-unstyled">
    <li><a href="/series/one-piece/?pr=0-100&amp;rf=pr">$0 - $100</a></li>
    <li><a href="/series/one-piece/?pr=100-200&amp;rf=pr">$100 - $200</a></li>
    <li><a href="/series/one-piece/?pr=200-300&amp;rf=pr">$200 - $300</a></li>
    <li><a href="/series/one-piece/?pr=300-400&amp;rf=pr">$300 - $400</a></li>
    <li><a href="/series/one-piece/?pr=400-500&amp;rf=pr">$400 - $500</a></li>
    <li><a href="/series/one-piece/?pr=500-600&amp;rf=pr">$500 - $600</a></li>
    <li><a href="/series/one-piece/?pr=600-700&amp;rf=pr">$600 - $700</a></li>
    <li><a href="/series/one-piece/?pr=600-0&amp;rf=pr">$600 +</a></li>
    <li><a href="/series/one-piece/?pr=700-0&amp;rf=pr">$700 +</a></li>
  </ul>
</div>
"""


def _neto_card(slug: str, title: str, price: str, availability: str, button: str) -> str:
    return f"""
<div class="thumbnail" itemscope itemtype="http://schema.org/Product">
  <a href="/{slug}" itemprop="url"><img itemprop="image" src="/x.jpg" alt=""></a>
  <div class="caption">
    <h3 itemprop="name"><a href="/{slug}">{title}</a></h3>
    <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
      <span class="productprice" itemprop="price" content="{price}">${price}</span>
      <meta itemprop="priceCurrency" content="AUD">
      <link itemprop="availability" href="http://schema.org/{availability}">
    </div>
    {button}
  </div>
</div>
"""


_KAIKA_PAGE_1 = "<html><body>" + _KAIKA_PRICE_SIDEBAR + _neto_card(
    "op-12-booster-box", "One Piece OP-12 Booster Box", "149.00", "InStock",
    '<button class="btn addtocart">Add to Cart</button>',
) + _neto_card(
    "op-11-booster-box", "One Piece OP-11 Booster Box", "139.00", "OutOfStock",
    '<span class="label">Out of Stock</span>',
) + _neto_card(
    "op-13-booster-box", "One Piece OP-13 Booster Box (Pre-Order)", "159.00", "PreOrder",
    '<button class="btn addtocart">Pre-Order</button>',
) + '<a rel="next" href="/series/one-piece/?pgnum=2">Next</a></body></html>'

_KAIKA_PAGE_2 = "<html><body>" + _KAIKA_PRICE_SIDEBAR + _neto_card(
    "op-10-booster-box", "One Piece OP-10 Booster Box", "129.00", "InStock",
    '<button class="btn addtocart">Add to Cart</button>',
) + "</body></html>"


def _kaika_session(pages: dict[str, str]) -> MagicMock:
    def get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = pages[url]
        resp.raise_for_status = MagicMock()
        return resp
    session = MagicMock()
    session.get.side_effect = get
    return session


def test_old_bug_price_filter_links_are_no_longer_parsed_as_products():
    """The generic fallback must not turn the nine price buckets into products."""
    html = "<html><body>" + _KAIKA_PRICE_SIDEBAR + "</body></html>"
    results = generic_html._parse_page(html, _KAIKA_PAGE_URL)
    assert results == []


def test_is_facet_link_recognises_neto_refine_and_self_links():
    assert generic_html._is_facet_link("https://www.kaika.com.au/series/one-piece/?pr=0-100&rf=pr", _KAIKA_PAGE_URL)
    assert generic_html._is_facet_link("https://www.kaika.com.au/series/one-piece?pgnum=2", _KAIKA_PAGE_URL)
    assert not generic_html._is_facet_link("https://www.kaika.com.au/op-12-booster-box", _KAIKA_PAGE_URL)
    assert not generic_html._is_facet_link("https://www.kaika.com.au/products/op-12?variant=1", _KAIKA_PAGE_URL)


def test_kaika_adapter_reads_microdata_availability_price_and_follows_pagination():
    session = _kaika_session({
        _KAIKA_PAGE_URL: _KAIKA_PAGE_1,
        "https://www.kaika.com.au/series/one-piece/?pgnum=2": _KAIKA_PAGE_2,
    })
    results = generic_html.fetch(_KAIKA_PAGE_URL, session)
    by_url = {r["product_url"]: r for r in results}

    assert set(by_url) == {
        "https://www.kaika.com.au/op-12-booster-box",
        "https://www.kaika.com.au/op-11-booster-box",
        "https://www.kaika.com.au/op-13-booster-box",
        "https://www.kaika.com.au/op-10-booster-box",
    }
    assert not any("rf=pr" in u for u in by_url)

    op12 = by_url["https://www.kaika.com.au/op-12-booster-box"]
    assert op12["title"] == "One Piece OP-12 Booster Box"
    assert op12["available"] is True and op12["is_preorder"] is False
    assert op12["price"] == "$149.00"

    op11 = by_url["https://www.kaika.com.au/op-11-booster-box"]
    assert op11["available"] is False

    op13 = by_url["https://www.kaika.com.au/op-13-booster-box"]
    assert op13["available"] is True and op13["is_preorder"] is True

    assert by_url["https://www.kaika.com.au/op-10-booster-box"]["available"] is True
    assert session.get.call_count == 2


def test_kaika_adapter_stops_when_no_next_link():
    session = _kaika_session({_KAIKA_PAGE_URL: _KAIKA_PAGE_2})
    results = generic_html.fetch(_KAIKA_PAGE_URL, session)
    assert len(results) == 1
    assert session.get.call_count == 1


def test_kaika_adapter_returns_empty_not_junk_when_markup_unrecognised():
    """If the real page has neither microdata nor product cards, report 0 so the
    failure alert fires once, instead of 9 phantom unavailable products."""
    html = "<html><body>" + _KAIKA_PRICE_SIDEBAR + "<p>Nothing here</p></body></html>"
    session = _kaika_session({_KAIKA_PAGE_URL: html})
    assert generic_html.fetch(_KAIKA_PAGE_URL, session) == []


def test_microdata_availability_without_schema_link_falls_back_to_text():
    html = """<html><body>
    <div itemscope itemtype="https://schema.org/Product">
      <a href="/thing" itemprop="url">Thing</a><span itemprop="name">Thing</span>
      <span>$10.00</span><button>Add to Cart</button>
    </div>
    <div itemscope itemtype="https://schema.org/Product">
      <a href="/other" itemprop="url">Other</a><span itemprop="name">Other</span>
      <span>$12.00</span><span>Sold Out</span>
    </div></body></html>"""
    soup = generic_html.BeautifulSoup(html, "html.parser")
    results = {r["product_url"]: r for r in generic_html._parse_microdata_products(soup, "https://s.com/cat/")}
    assert results["https://s.com/thing"]["available"] is True
    assert results["https://s.com/thing"]["price"] == "$10.00"
    assert results["https://s.com/other"]["available"] is False


def test_generic_fallback_still_parses_real_cards_next_to_a_facet_sidebar():
    """Other generic-adapter sites (M-G, NJ) keep working; only facet links are dropped."""
    html = "<html><body>" + _KAIKA_PRICE_SIDEBAR + _SAMPLE_HTML + "</body></html>"
    results = generic_html._parse_page(html, "https://shop.com/collection/one-piece")
    urls = {r["product_url"] for r in results}
    assert urls == {"https://shop.com/products/booster-a", "https://shop.com/products/booster-b"}
