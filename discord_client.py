"""Discord alert formatting and webhook delivery."""
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

DISCORD_MAX_EMBEDS = 10  # Discord's per-message limit

ALERT_LABELS = {
    "NEW_LISTING": "🆕 NEW LISTING",
    "BACK_IN_STOCK": "✅ BACK IN STOCK",
    "PREORDER_OPEN": "📦 PREORDER OPEN",
}

ALERT_COLORS = {
    "NEW_LISTING": 0x5865F2,    # blurple
    "BACK_IN_STOCK": 0x57F287,  # green
    "PREORDER_OPEN": 0xFEE75C,  # yellow
}


def build_embed(alert: dict, retailer_name: str) -> dict:
    """Build a single Discord embed dict for one alert."""
    alert_type = alert.get("alert_type", "NEW_LISTING")
    label = ALERT_LABELS.get(alert_type, alert_type)
    color = ALERT_COLORS.get(alert_type, 0x99AAB5)

    title = f"{label} — {alert.get('title', 'Unknown Product')}"
    url = alert.get("product_url", "")
    price = alert.get("price") or "Price unknown"

    embed: dict[str, Any] = {
        "title": title,
        "url": url,
        "color": color,
        "fields": [
            {"name": "Retailer", "value": retailer_name, "inline": True},
            {"name": "Price", "value": price, "inline": True},
        ],
    }
    return embed


def build_messages(alerts: list[dict], retailer_name: str) -> list[dict]:
    """
    Group alerts into Discord webhook payloads (max 10 embeds each).
    Returns a list of payload dicts ready to POST.
    """
    if not alerts:
        return []

    payloads: list[dict] = []
    for i in range(0, len(alerts), DISCORD_MAX_EMBEDS):
        chunk = alerts[i: i + DISCORD_MAX_EMBEDS]
        embeds = [build_embed(a, retailer_name) for a in chunk]
        payloads.append({"embeds": embeds})
    return payloads


def send_alerts(
    alerts: list[dict],
    retailer_name: str,
    webhook_url: str,
    session: requests.Session,
) -> None:
    """Post one or more Discord messages for the given alerts."""
    payloads = build_messages(alerts, retailer_name)
    for payload in payloads:
        try:
            resp = session.post(webhook_url, json=payload, timeout=15)
            if resp.status_code == 204:
                logger.info("Discord alert sent (%d embeds)", len(payload["embeds"]))
            else:
                logger.warning(
                    "Discord responded %d: %s", resp.status_code, resp.text[:200]
                )
        except Exception as exc:
            logger.error("Discord webhook failed: %s", exc)


def send_adapter_failure_alert(
    watch_url: str,
    retailer_name: str,
    error: str,
    webhook_url: str,
    session: requests.Session,
) -> None:
    """One-time alert when an adapter can't parse a site."""
    embed = {
        "title": "⚠️ Adapter Parse Failure",
        "color": 0xED4245,  # red
        "fields": [
            {"name": "Retailer", "value": retailer_name, "inline": True},
            {"name": "URL", "value": watch_url, "inline": False},
            {"name": "Error", "value": error[:1000], "inline": False},
            {
                "name": "Action needed",
                "value": "Write a custom adapter for this domain in `adapters/generic_html.py`.",
                "inline": False,
            },
        ],
    }
    payload = {"embeds": [embed]}
    try:
        resp = session.post(webhook_url, json=payload, timeout=15)
        if resp.status_code != 204:
            logger.warning("Adapter failure alert responded %d", resp.status_code)
    except Exception as exc:
        logger.error("Failed to send adapter failure alert: %s", exc)
