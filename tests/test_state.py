"""Tests for state.py: transition detection, save/load, timestamp helpers."""
import json
import os
import tempfile

import pytest

os.environ["STATE_FILE"] = ":memory:"  # patched per-test anyway


import state as st


WATCH_URL = "https://example.com/collections/one-piece"


def make_product(url: str, title: str, available: bool, is_preorder: bool = False, price: str = "$50.00"):
    return {
        "product_url": url,
        "title": title,
        "available": available,
        "is_preorder": is_preorder,
        "price": price,
    }


# ── save / load round-trip ───────────────────────────────────────────────────

def test_save_load_roundtrip(tmp_path, monkeypatch):
    state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(st, "STATE_FILE", state_file)

    data = {"key": "value", "nested": {"a": 1}}
    st.save_state(data)
    loaded = st.load_state()
    assert loaded == data


def test_load_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", str(tmp_path / "nonexistent.json"))
    assert st.load_state() == {}


def test_load_malformed_json(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text("{bad json")
    monkeypatch.setattr(st, "STATE_FILE", str(state_file))
    assert st.load_state() == {}


# ── no alert on first sight ──────────────────────────────────────────────────

def test_no_alert_on_first_population():
    """Populating state from scratch must produce zero alerts."""
    s = {}
    products = [
        make_product("https://example.com/p/booster-a", "Booster A", available=True),
        make_product("https://example.com/p/booster-b", "Booster B", available=False),
    ]
    alerts = st.detect_transitions(s, WATCH_URL, products)
    assert alerts == []


def test_first_run_sets_known_urls():
    s = {}
    products = [make_product("https://example.com/p/a", "A", available=True)]
    st.detect_transitions(s, WATCH_URL, products)
    known = st.get_known_urls(s, WATCH_URL)
    assert "https://example.com/p/a" in known


# ── alert on flip unavailable → available ────────────────────────────────────

def test_alert_on_restock():
    """Product that was unavailable and flips to available triggers BACK_IN_STOCK."""
    s = {}
    products = [make_product("https://example.com/p/a", "A", available=False)]
    st.detect_transitions(s, WATCH_URL, products)  # first run, silent

    products[0]["available"] = True
    alerts = st.detect_transitions(s, WATCH_URL, products)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "BACK_IN_STOCK"
    assert alerts[0]["product_url"] == "https://example.com/p/a"


def test_alert_on_preorder_open():
    """Product that was unavailable and flips to preorder triggers PREORDER_OPEN."""
    s = {}
    products = [make_product("https://example.com/p/a", "A", available=False, is_preorder=False)]
    st.detect_transitions(s, WATCH_URL, products)

    products[0]["available"] = True
    products[0]["is_preorder"] = True
    alerts = st.detect_transitions(s, WATCH_URL, products)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "PREORDER_OPEN"


# ── no re-alert on steady state ──────────────────────────────────────────────

def test_no_re_alert_on_steady_available():
    s = {}
    products = [make_product("https://example.com/p/a", "A", available=False)]
    st.detect_transitions(s, WATCH_URL, products)

    products[0]["available"] = True
    st.detect_transitions(s, WATCH_URL, products)  # flip — produces alert

    # Third run: still available — should NOT re-alert
    alerts = st.detect_transitions(s, WATCH_URL, products)
    assert alerts == []


def test_no_re_alert_on_steady_unavailable():
    s = {}
    products = [make_product("https://example.com/p/a", "A", available=False)]
    st.detect_transitions(s, WATCH_URL, products)

    alerts = st.detect_transitions(s, WATCH_URL, products)
    assert alerts == []


# ── new product appearing in category ────────────────────────────────────────

def test_alert_on_new_category_product():
    """A product URL not previously seen in a category should alert as NEW_LISTING."""
    s = {}
    products = [make_product("https://example.com/p/old", "Old", available=True)]
    st.detect_transitions(s, WATCH_URL, products)  # populate

    new_product = make_product("https://example.com/p/brand-new", "Brand New", available=True)
    alerts = st.detect_transitions(s, WATCH_URL, [products[0], new_product])
    assert any(a["product_url"] == "https://example.com/p/brand-new" for a in alerts)
    assert any(a["alert_type"] == "NEW_LISTING" for a in alerts)


def test_no_alert_for_new_unavailable_category_product():
    """New product in category that is unavailable should NOT alert."""
    s = {}
    products = [make_product("https://example.com/p/old", "Old", available=True)]
    st.detect_transitions(s, WATCH_URL, products)

    new_product = make_product("https://example.com/p/new-oos", "New OOS", available=False)
    alerts = st.detect_transitions(s, WATCH_URL, [products[0], new_product])
    assert alerts == []


# ── timestamps ───────────────────────────────────────────────────────────────

def test_last_checked_defaults_to_zero():
    s = {}
    assert st.get_last_checked(s, "https://example.com") == 0.0


def test_set_and_get_last_checked():
    s = {}
    st.set_last_checked(s, "https://example.com", 12345.0)
    assert st.get_last_checked(s, "https://example.com") == 12345.0


# ── M1: per-watch first run & preorder→in_stock ──────────────────────────────

def test_new_watch_on_populated_state_first_scan_silent_then_alerts():
    s = {}
    st.detect_transitions(s, WATCH_URL, [make_product("https://a.test/p/1", "A", True)])
    assert s.get("products")  # state is populated

    other = "https://other.test/collections/one-piece"
    p1 = make_product("https://b.test/p/1", "B1", True)
    assert st.detect_transitions(s, other, [p1]) == []  # first scan of new watch

    p2 = make_product("https://b.test/p/2", "B2", True)
    alerts = st.detect_transitions(s, other, [p1, p2])
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "NEW_LISTING"
    assert alerts[0]["product_url"] == "https://b.test/p/2"


def test_preorder_open_to_in_stock_alerts_back_in_stock():
    s = {}
    url = "https://example.com/p/x"
    st.detect_transitions(s, WATCH_URL, [make_product(url, "X", True, is_preorder=True)])
    alerts = st.detect_transitions(s, WATCH_URL, [make_product(url, "X", True, is_preorder=False)])
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "BACK_IN_STOCK"


def test_in_stock_to_preorder_open_no_alert():
    s = {}
    url = "https://example.com/p/x"
    st.detect_transitions(s, WATCH_URL, [make_product(url, "X", True, is_preorder=False)])
    alerts = st.detect_transitions(s, WATCH_URL, [make_product(url, "X", True, is_preorder=True)])
    assert alerts == []
