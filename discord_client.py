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
    """
    Posted once when a watch STARTS failing (fetch error, 404, or 0 products
    parsed). The watcher tracks the failure in state.json and will not post
    again until the watch recovers, at which point send_watch_recovered_alert fires.
    """
    embed = {
        "title": "⚠️ Adapter Parse Failure",
        "color": 0xED4245,  # red
        "fields": [
            {"name": "Retailer", "value": retailer_name, "inline": True},
            {"name": "URL", "value": watch_url, "inline": False},
            {"name": "Error", "value": error[:1000], "inline": False},
            {
                "name": "What happens next",
                "value": (
                    "This watch keeps being retried every pass but you won't hear about it "
                    "again until it recovers. If it stays broken, check the URL or write a "
                    "custom adapter for this domain in `adapters/generic_html.py`."
                ),
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


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def send_watch_recovered_alert(
    watch_url: str,
    retailer_name: str,
    failure: dict,
    webhook_url: str,
    session: requests.Session,
) -> None:
    """One-time alert when a previously failing watch fetches and parses again."""
    since = float(failure.get("since") or 0.0)
    last_seen = float(failure.get("last_seen") or since)
    count = int(failure.get("count") or 0)
    embed = {
        "title": "✅ Watch Recovered",
        "color": 0x57F287,  # green
        "fields": [
            {"name": "Retailer", "value": retailer_name, "inline": True},
            {"name": "Failed passes", "value": str(count), "inline": True},
            {"name": "Down for", "value": _format_duration(last_seen - since), "inline": True},
            {"name": "URL", "value": watch_url, "inline": False},
            {"name": "Last error", "value": str(failure.get("last_error", ""))[:1000] or "n/a", "inline": False},
        ],
    }
    payload = {"embeds": [embed]}
    try:
        resp = session.post(webhook_url, json=payload, timeout=15)
        if resp.status_code != 204:
            logger.warning("Watch recovered alert responded %d", resp.status_code)
    except Exception as exc:
        logger.error("Failed to send watch recovered alert: %s", exc)
