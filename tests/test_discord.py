"""Tests for discord_client.py: embed formatting, batching, HTTP mocking."""
from unittest.mock import MagicMock, call

import pytest

import discord_client as dc


RETAILER = "Test Retailer"
WEBHOOK = "https://discord.com/api/webhooks/123/abc"


def make_alert(alert_type: str, title: str = "OP09 Booster Box") -> dict:
    return {
        "product_url": "https://shop.com/products/op09-booster",
        "title": title,
        "price": "$69.95",
        "alert_type": alert_type,
    }


# ── build_embed ───────────────────────────────────────────────────────────────

def test_build_embed_new_listing_has_label():
    embed = dc.build_embed(make_alert("NEW_LISTING"), RETAILER)
    assert "NEW LISTING" in embed["title"]
    assert "🆕" in embed["title"]


def test_build_embed_back_in_stock_has_label():
    embed = dc.build_embed(make_alert("BACK_IN_STOCK"), RETAILER)
    assert "BACK IN STOCK" in embed["title"]
    assert "✅" in embed["title"]


def test_build_embed_preorder_open_has_label():
    embed = dc.build_embed(make_alert("PREORDER_OPEN"), RETAILER)
    assert "PREORDER OPEN" in embed["title"]
    assert "📦" in embed["title"]


def test_build_embed_contains_product_title():
    embed = dc.build_embed(make_alert("NEW_LISTING", "Awesome Set"), RETAILER)
    assert "Awesome Set" in embed["title"]


def test_build_embed_contains_url():
    embed = dc.build_embed(make_alert("NEW_LISTING"), RETAILER)
    assert embed["url"] == "https://shop.com/products/op09-booster"


def test_build_embed_contains_price():
    embed = dc.build_embed(make_alert("NEW_LISTING"), RETAILER)
    field_values = [f["value"] for f in embed["fields"]]
    assert any("$69.95" in v for v in field_values)


def test_build_embed_contains_retailer():
    embed = dc.build_embed(make_alert("NEW_LISTING"), RETAILER)
    field_values = [f["value"] for f in embed["fields"]]
    assert any(RETAILER in v for v in field_values)


def test_build_embed_missing_price_shows_fallback():
    alert = make_alert("NEW_LISTING")
    alert["price"] = None
    embed = dc.build_embed(alert, RETAILER)
    field_values = [f["value"] for f in embed["fields"]]
    assert any("unknown" in v.lower() for v in field_values)


# ── build_messages batching ───────────────────────────────────────────────────

def test_single_alert_produces_one_message():
    alerts = [make_alert("NEW_LISTING")]
    payloads = dc.build_messages(alerts, RETAILER)
    assert len(payloads) == 1
    assert len(payloads[0]["embeds"]) == 1


def test_ten_alerts_produces_one_message():
    alerts = [make_alert("NEW_LISTING", f"Product {i}") for i in range(10)]
    payloads = dc.build_messages(alerts, RETAILER)
    assert len(payloads) == 1
    assert len(payloads[0]["embeds"]) == 10


def test_eleven_alerts_produce_two_messages():
    alerts = [make_alert("NEW_LISTING", f"Product {i}") for i in range(11)]
    payloads = dc.build_messages(alerts, RETAILER)
    assert len(payloads) == 2
    assert len(payloads[0]["embeds"]) == 10
    assert len(payloads[1]["embeds"]) == 1


def test_empty_alerts_produces_no_messages():
    payloads = dc.build_messages([], RETAILER)
    assert payloads == []


# ── send_alerts: mocked HTTP ──────────────────────────────────────────────────

def _ok_session() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 204
    resp.text = ""
    session = MagicMock()
    session.post.return_value = resp
    return session


def _error_session(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = "Bad request"
    session = MagicMock()
    session.post.return_value = resp
    return session


def test_send_alerts_calls_post_once_for_one_alert():
    session = _ok_session()
    dc.send_alerts([make_alert("NEW_LISTING")], RETAILER, WEBHOOK, session)
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args[0] == WEBHOOK
    assert "embeds" in kwargs["json"]


def test_send_alerts_calls_post_twice_for_11_alerts():
    session = _ok_session()
    alerts = [make_alert("BACK_IN_STOCK", f"P{i}") for i in range(11)]
    dc.send_alerts(alerts, RETAILER, WEBHOOK, session)
    assert session.post.call_count == 2


def test_send_alerts_does_not_raise_on_discord_error():
    session = _error_session(400)
    # Should NOT raise
    dc.send_alerts([make_alert("NEW_LISTING")], RETAILER, WEBHOOK, session)


def test_send_alerts_does_not_raise_on_network_error():
    session = MagicMock()
    session.post.side_effect = Exception("Connection refused")
    dc.send_alerts([make_alert("NEW_LISTING")], RETAILER, WEBHOOK, session)
