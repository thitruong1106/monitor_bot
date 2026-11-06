"""Discord alert formatting and webhook delivery."""
import logging
from datetime import datetime, timezone
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_embed(alert: dict, retailer_name: str) -> dict:
    """Build a single Discord embed dict for one alert."""
    alert_type = alert.get("alert_type", "NEW_LISTING")
    label = ALERT_LABELS.get(alert_type, alert_type)
    color = ALERT_COLORS.get(alert_type, 0x99AAB5)

    title = f"{label} — {alert.get('title', 'Unknown Product')}"
    url = alert.get("product_url", "")
    price = alert.get("price") or "Price unknown"

    fields = [
        {"name": "Retailer", "value": retailer_name, "inline": True},
        {"name": "Price", "value": price, "inline": True},
    ]
    links = []
    if alert.get("cart_url"):
        links.append(f"[Add to cart]({alert['cart_url']})")
    if alert.get("checkout_url"):
        links.append(f"[Straight to checkout]({alert['checkout_url']})")
    if links:
        fields.append({"name": "Quick buy", "value": " · ".join(links), "inline": False})

    embed: dict[str, Any] = {
        "title": title[:256],
        "url": url,
        "color": color,
        "fields": fields,
        "timestamp": _now_iso(),
    }
    if alert.get("image_url"):
        embed["thumbnail"] = {"url": alert["image_url"]}
    return embed


def build_messages(alerts: list[dict], retailer_name: str, role_id: str | None = None) -> list[dict]:
    """
    Group alerts into Discord webhook payloads (max 10 embeds each).
    Returns a list of payload dicts ready to POST.

    With a role_id, the FIRST payload pings that role (one ping per retailer
    batch). allowed_mentions is always set so nothing else can ever ping.
    """
    if not alerts:
        return []

    payloads: list[dict] = []
    for i in range(0, len(alerts), DISCORD_MAX_EMBEDS):
        chunk = alerts[i: i + DISCORD_MAX_EMBEDS]
        embeds = [build_embed(a, retailer_name) for a in chunk]
        payload: dict[str, Any] = {"embeds": embeds, "allowed_mentions": {"parse": []}}
        if role_id and i == 0:
            payload["content"] = f"<@&{role_id}>"
            payload["allowed_mentions"] = {"parse": [], "roles": [role_id]}
        payloads.append(payload)
    return payloads


def send_alerts(
    alerts: list[dict],
    retailer_name: str,
    webhook_url: str,
    session: requests.Session,
    role_id: str | None = None,
) -> None:
    """Post one or more Discord messages for the given alerts."""
    payloads = build_messages(alerts, retailer_name, role_id=role_id)
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
    title: str = "⚠️ Adapter Parse Failure",
) -> None:
    """
    Posted once when a watch STARTS failing (fetch error, 404, 429, or 0
    products parsed). The watcher tracks the failure in state.json and will not
    post again until the watch recovers, at which point
    send_watch_recovered_alert fires.
    """
    embed = {
        "title": title,
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


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{max(1, int(seconds // 60))} min"


def build_missed_run_embed(warning: dict) -> dict:
    start = int(warning["gap_start"])
    end = int(warning["gap_end"])
    lines = [
        f"No successful check between <t:{start}:f> and <t:{end}:f> ({_hours(warning['gap_seconds'])}).",
        "Restocks in that window may have been missed or alerted late.",
    ]
    if warning.get("earlier_gaps"):
        lines.append(
            f"Also {warning['earlier_gaps']} earlier gap(s) over {int(warning['threshold_minutes'])} min "
            f"since the last warning; longest {_hours(warning['longest_seconds'])}."
        )
    return {
        "title": "⏰ Watcher fell behind",
        "color": 0xFEE75C,
        "description": "\n".join(lines),
        "timestamp": _now_iso(),
    }


def send_missed_run_alert(warning: dict, webhook_url: str, session: requests.Session) -> None:
    """Posted when the gap since the last successful pass exceeds the threshold."""
    payload = {"embeds": [build_missed_run_embed(warning)], "allowed_mentions": {"parse": []}}
    try:
        resp = session.post(webhook_url, json=payload, timeout=15)
        if resp.status_code != 204:
            logger.warning("Missed-run alert responded %d", resp.status_code)
    except Exception as exc:
        logger.error("Failed to send missed-run alert: %s", exc)
