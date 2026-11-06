"""Alert content: role mention, product image, quick-buy links, title filters."""
import json
import os
from unittest.mock import MagicMock

import pytest

import config
import discord_client as dc
from adapters import generic_html, shopify
from config import Watch

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ROLE = "123456789012345678"


def _alert(**extra):
    return {"product_url": "https://shop.example/products/op18", "title": "OP-18 Booster Box",
            "price": "$214.99", "alert_type": "PREORDER_OPEN", **extra}


# ── Role mention ─────────────────────────────────────────────────────────────

def test_role_id_read_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_ROLE_ID", ROLE)
    assert config.load_role_id() == ROLE


@pytest.mark.parametrize("bad", ["@everyone", "here", "12<@&1>", "abc", "123"])
def test_non_numeric_role_id_is_ignored(monkeypatch, bad):
    monkeypatch.setenv("DISCORD_ROLE_ID", bad)
    assert config.load_role_id() is None


def test_unset_role_id_means_no_ping():
    assert config.load_role_id() is None
    payload = dc.build_messages([_alert()], "Shop")[0]
    assert "content" not in payload
    assert payload["allowed_mentions"] == {"parse": []}


def test_role_is_pinged_once_per_batch_and_only_that_role():
    alerts = [_alert(title=f"P{i}") for i in range(12)]           # -> 2 payloads
    payloads = dc.build_messages(alerts, "Shop", role_id=ROLE)
    assert payloads[0]["content"] == f"<@&{ROLE}>"
    assert payloads[0]["allowed_mentions"] == {"parse": [], "roles": [ROLE]}
    assert "content" not in payloads[1]
    assert payloads[1]["allowed_mentions"] == {"parse": []}


def test_send_alerts_passes_role_through():
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=204)
    dc.send_alerts([_alert()], "Shop", "https://hook", session, role_id=ROLE)
    assert session.post.call_args.kwargs["json"]["content"] == f"<@&{ROLE}>"


# ── Embed: image and quick-buy links ─────────────────────────────────────────

def test_embed_has_thumbnail_and_quick_buy_links():
    embed = dc.build_embed(_alert(
        image_url="https://cdn.shopify.com/op18.jpg",
        cart_url="https://shop.example/cart/add?id=42&quantity=1",
        checkout_url="https://shop.example/cart/42:1",
    ), "Shop")
    assert embed["thumbnail"] == {"url": "https://cdn.shopify.com/op18.jpg"}
    quick = next(f for f in embed["fields"] if f["name"] == "Quick buy")
    assert "[Add to cart](https://shop.example/cart/add?id=42&quantity=1)" in quick["value"]
    assert "[Straight to checkout](https://shop.example/cart/42:1)" in quick["value"]
    assert embed["url"] == "https://shop.example/products/op18"
    assert "timestamp" in embed


def test_embed_without_extras_still_works():
    embed = dc.build_embed(_alert(), "Shop")
    assert "thumbnail" not in embed
    assert all(f["name"] != "Quick buy" for f in embed["fields"])


def _collection_session(products):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"products": products}
    resp.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = resp
    return session


def test_shopify_collection_gives_image_and_links_for_first_available_variant():
    raw = [{
        "handle": "op18-box", "title": "OP-18 Booster Box", "tags": [],
        "images": [{"src": "https://cdn.shopify.com/op18.jpg"}],
        "variants": [
            {"id": 111, "available": False, "price": "250.00"},
            {"id": 222, "available": True, "price": "214.99"},
        ],
    }]
    item = shopify.fetch_collection("https://shop.example/collections/op", _collection_session(raw))[0]
    assert item["image_url"] == "https://cdn.shopify.com/op18.jpg"
    assert item["cart_url"] == "https://shop.example/cart/add?id=222&quantity=1"
    assert item["checkout_url"] == "https://shop.example/cart/222:1"


def test_shopify_collection_without_images_or_variant_ids():
    raw = [{"handle": "x", "title": "X", "tags": [], "variants": [{"available": True, "price": "1.00"}]}]
    item = shopify.fetch_collection("https://shop.example/collections/op", _collection_session(raw))[0]
    assert "image_url" not in item and "cart_url" not in item


# ── Shopify product-page watches (the OP-18 pages) ───────────────────────────

_JS_PRODUCT = {
    "handle": "one-piece-card-game-op-18-booster-box",
    "title": "One Piece Card Game OP-18 Booster Box",
    "tags": ["One Piece", "Pre-Order"],
    "available": False,
    "price": 21499,
    "featured_image": "//cdn.shopify.com/s/files/op18.jpg",
    "variants": [{"id": 4242, "available": False, "price": 21499}],
}


def _product_session(js_body=None, js_is_json=True, json_body=None):
    def get(url, **kw):
        r = MagicMock(status_code=200)
        r.raise_for_status = MagicMock()
        if url.endswith(".js"):
            if js_is_json:
                r.json.return_value = js_body
            else:
                r.json.side_effect = ValueError("not json")
        else:
            r.json.return_value = json_body
        return r
    s = MagicMock()
    s.get.side_effect = get
    return s


def test_product_page_reads_availability_from_js_endpoint():
    url = "https://www.gameology.com.au/products/one-piece-card-game-op-18-booster-box"
    closed = shopify.fetch_product(url, _product_session(_JS_PRODUCT))[0]
    assert closed["available"] is False
    assert closed["price"] == "$214.99"                        # cents -> dollars
    assert closed["image_url"] == "https://cdn.shopify.com/s/files/op18.jpg"
    assert closed["checkout_url"] == "https://www.gameology.com.au/cart/4242:1"
    assert closed["product_url"] == url

    opened = dict(_JS_PRODUCT, available=True, variants=[{"id": 4242, "available": True, "price": 21499}])
    item = shopify.fetch_product(url, _product_session(opened))[0]
    assert item["available"] is True and item["is_preorder"] is True


def test_product_page_requests_the_js_url():
    session = _product_session(_JS_PRODUCT)
    shopify.fetch_product("https://www.gameology.com.au/collections/x/products/one-piece-card-game-op-18-booster-box/", session)
    assert session.get.call_args_list[0].args[0] == \
        "https://www.gameology.com.au/products/one-piece-card-game-op-18-booster-box.js"


def test_product_json_fallback_without_availability_raises_instead_of_reading_sold_out():
    body = {"product": {"handle": "h", "title": "T", "tags": "", "variants": [{"id": 1, "price": "10.00"}]}}
    with pytest.raises(ValueError, match="no variant availability"):
        shopify.fetch_product("https://shop.example/products/h", _product_session(js_is_json=False, json_body=body))


def test_product_json_fallback_with_availability_works():
    body = {"product": {"handle": "h", "title": "T", "tags": [], "variants": [{"id": 1, "price": "10.00", "available": True}]}}
    item = shopify.fetch_product("https://shop.example/products/h", _product_session(js_is_json=False, json_body=body))[0]
    assert item["available"] is True


# ── Generic HTML images ──────────────────────────────────────────────────────

def test_generic_card_image_prefers_lazy_src():
    html = """<html><body>
    <div class="product-item"><a href="/p/a">A</a><img data-src="/img/a.jpg" src="data:image/gif;base64,xx">
      <span>$10.00</span><button>Add to Cart</button></div>
    <div class="product-item"><a href="/p/b">B</a><img src="https://cdn.example/b.jpg"><span>$12.00</span></div>
    </body></html>"""
    results = {r["product_url"]: r for r in generic_html._parse_page(html, "https://s.example/cat/")}
    assert results["https://s.example/p/a"]["image_url"] == "https://s.example/img/a.jpg"
    assert results["https://s.example/p/b"]["image_url"] == "https://cdn.example/b.jpg"


# ── Title filters on the REAL alerts that caused noise (fixture) ─────────────

@pytest.fixture(scope="module")
def titles():
    with open(os.path.join(FIXTURES, "alert_titles.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def watches():
    return {w.retailer_name: w for w in config.load_watches(os.path.join(os.path.dirname(FIXTURES), "..", "watches.yaml"))}


def test_good_games_filter_drops_all_102_singles_but_keeps_sealed(titles, watches):
    gg = watches["Good Games (In Stock)"]
    kept = [t for t in titles["good_games_in_stock"] if gg.title_passes(t)]
    assert kept == ["One Piece Card Game - The World's Strongest Warriors Booster Box [OP-17]"]
    assert len(titles["good_games_in_stock"]) - len(kept) == 102
    for sealed in titles["sealed_elsewhere"]:
        assert gg.title_passes(sealed), sealed


def test_kaika_filter_blocks_every_figure_it_ever_alerted(titles, watches):
    kaika = watches["Kaika"]
    assert [t for t in titles["kaika"] if kaika.title_passes(t)] == []
    for card in ["One Piece Card Game OP-18 Booster Box", "One Piece Card Game Starter Deck ST-29",
                 "ONE PIECE CARD GAME EB-03 Booster Pack"]:
        assert kaika.title_passes(card), card


def test_m_g_filter_blocks_yugioh_keeps_one_piece(titles, watches):
    mg = watches["M-G"]
    assert [t for t in titles["m_g"] if mg.title_passes(t)] == []
    assert mg.title_passes("One Piece – The Dominance of God – Booster (Box)")
    assert mg.title_passes("OP-18 Booster Box")


def test_include_and_exclude_combine():
    w = Watch(url="u", game="g", tier="normal", type="category", retailer_name="r",
              include=[r"one piece"], exclude=[r"sleeve"])
    assert w.title_passes("One Piece Booster Box")
    assert not w.title_passes("One Piece Card Sleeves")
    assert not w.title_passes("Pokemon Booster Box")


def test_no_filters_keeps_everything():
    w = Watch(url="u", game="g", tier="normal", type="category", retailer_name="r")
    prods = [{"title": "anything"}, {"title": ""}]
    assert w.apply_filters(prods) == prods
    assert w.filters_fingerprint == ""


def test_neto_microdata_product_image():
    from bs4 import BeautifulSoup
    html = """<div itemscope itemtype="http://schema.org/Product">
      <a href="/op18-box" itemprop="url"><img itemprop="image" src="/assets/op18.jpg"></a>
      <h3 itemprop="name">One Piece Card Game OP-18 Booster Box</h3>
      <link itemprop="availability" href="http://schema.org/InStock"></div>"""
    item = generic_html._parse_microdata_products(BeautifulSoup(html, "html.parser"), "https://www.kaika.com.au/series/one-piece/")[0]
    assert item["image_url"] == "https://www.kaika.com.au/assets/op18.jpg"
