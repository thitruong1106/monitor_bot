"""Load watches.yaml and webhooks config."""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

WATCHES_FILE = "watches.yaml"
WEBHOOKS_FILE = "webhooks.json"

TIER_INTERVALS = {
    "hot": 5,
    "normal": 15,
    "slow": 60,
}


@dataclass
class Watch:
    url: str
    game: str
    tier: str
    type: str          # "category" or "product"
    retailer_name: str

    @property
    def interval_minutes(self) -> int:
        return TIER_INTERVALS.get(self.tier, 15)


def load_watches(path: str = WATCHES_FILE) -> list[Watch]:
    if not os.path.exists(path):
        logger.warning("watches.yaml not found at %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    watches = []
    for entry in raw:
        watches.append(Watch(
            url=entry["url"],
            game=entry["game"],
            tier=entry.get("tier", "normal"),
            type=entry.get("type", "category"),
            retailer_name=entry.get("retailer_name", "Unknown"),
        ))
    return watches


def save_watches(watches: list[Watch], path: str = WATCHES_FILE) -> None:
    data = [
        {
            "url": w.url,
            "game": w.game,
            "tier": w.tier,
            "type": w.type,
            "retailer_name": w.retailer_name,
        }
        for w in watches
    ]
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_webhooks() -> dict[str, str]:
    """
    Load webhook map. Priority:
    1. WEBHOOKS_JSON env var (JSON string)
    2. webhooks.json file
    """
    env_val = os.environ.get("WEBHOOKS_JSON")
    if env_val:
        try:
            return json.loads(env_val)
        except json.JSONDecodeError:
            logger.error("WEBHOOKS_JSON env var is not valid JSON")

    if os.path.exists(WEBHOOKS_FILE):
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.error("webhooks.json is malformed")

    return {}


def get_webhook(webhooks: dict[str, str], game: str) -> str | None:
    return webhooks.get(game) or webhooks.get("_fallback")


def infer_watch_type(url: str) -> str:
    """Infer watch type from URL path structure."""
    category_markers = [
        "/collections/", "/category/", "/shop/category/",
        "/series/", "/pre-orders/", "/trading-card-games/",
    ]
    for marker in category_markers:
        if marker in url:
            return "category"
    return "product"
