"""State management: load/save state.json, detect product transitions."""
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            logger.warning("state.json is malformed, starting fresh")
            return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# ── Timestamps ──────────────────────────────────────────────────────────────

def get_last_checked(state: dict, watch_url: str) -> float:
    return state.get("last_checked", {}).get(watch_url, 0.0)


def set_last_checked(state: dict, watch_url: str, ts: float | None = None) -> None:
    state.setdefault("last_checked", {})[watch_url] = ts if ts is not None else time.time()


# ── Watch failure tracking ───────────────────────────────────────────────────

def get_failure(state: dict, watch_url: str) -> dict | None:
    """Return the active failure record for a watch, or None if it is healthy."""
    return state.get("failures", {}).get(watch_url)


def record_failure(state: dict, watch_url: str, error: str, ts: float | None = None) -> bool:
    """
    Mark a watch as failing. Returns True if this is the START of a failure
    streak (i.e. the caller should alert), False if it was already failing.
    """
    now = ts if ts is not None else time.time()
    failures = state.setdefault("failures", {})
    existing = failures.get(watch_url)
    if existing:
        existing["count"] = int(existing.get("count", 1)) + 1
        existing["last_error"] = str(error)[:500]
        existing["last_seen"] = now
        return False
    failures[watch_url] = {
        "since": now,
        "last_seen": now,
        "count": 1,
        "last_error": str(error)[:500],
    }
    return True


def clear_failure(state: dict, watch_url: str) -> dict | None:
    """
    Mark a watch as healthy. Returns the failure record that was cleared
    (so the caller can send a recovery alert), or None if it wasn't failing.
    """
    failures = state.get("failures", {})
    return failures.pop(watch_url, None)


# ── Category state ───────────────────────────────────────────────────────────

def get_known_urls(state: dict, watch_url: str) -> set[str]:
    return set(state.get("category_urls", {}).get(watch_url, []))


def set_known_urls(state: dict, watch_url: str, urls: set[str]) -> None:
    state.setdefault("category_urls", {})[watch_url] = sorted(urls)


# ── Product availability state ───────────────────────────────────────────────

def _product_key(watch_url: str, product_url: str) -> str:
    return f"{watch_url}||{product_url}"


def get_product_state(state: dict, watch_url: str, product_url: str) -> dict | None:
    return state.get("products", {}).get(_product_key(watch_url, product_url))


def set_product_state(state: dict, watch_url: str, product_url: str, product_state: dict) -> None:
    state.setdefault("products", {})[_product_key(watch_url, product_url)] = product_state


# ── Transition detection ─────────────────────────────────────────────────────

def _classify(available: bool, is_preorder: bool) -> str:
    """Return a canonical state string for comparison."""
    if available and is_preorder:
        return "preorder_open"
    if available:
        return "in_stock"
    return "unavailable"


def detect_transitions(
    state: dict,
    watch_url: str,
    current_products: list[dict],
) -> list[dict]:
    """
    Compare current product list against saved state and return alert dicts.

    Each product dict must have: product_url, title, available (bool),
    is_preorder (bool), price (str|None).

    Returns a list of alert dicts:
      {product_url, title, price, alert_type}
    where alert_type is "NEW_LISTING", "BACK_IN_STOCK", or "PREORDER_OPEN".

    First-time-seen products are recorded silently (no alert).
    """
    alerts: list[dict] = []
    current_urls = {p["product_url"] for p in current_products}
    known_urls = get_known_urls(state, watch_url)
    # First run is per watch: no known URLs for this watch yet.
    is_first_run = not known_urls

    for product in current_products:
        url = product["product_url"]
        available = bool(product.get("available"))
        is_preorder = bool(product.get("is_preorder"))
        current_cls = _classify(available, is_preorder)

        saved = get_product_state(state, watch_url, url)
        new_to_category = url not in known_urls

        if saved is None:
            # First time we see this product — record silently
            set_product_state(state, watch_url, url, {
                "available": available,
                "is_preorder": is_preorder,
                "state": current_cls,
            })
            # If this is a brand-new category page run AND product is available, alert
            # (but only if it's not the very first population of state)
            if new_to_category and not is_first_run:
                if available:
                    alert_type = "PREORDER_OPEN" if is_preorder else "NEW_LISTING"
                    alerts.append({
                        "product_url": url,
                        "title": product.get("title", "Unknown Product"),
                        "price": product.get("price"),
                        "alert_type": alert_type,
                    })
            continue

        prev_cls = saved.get("state", _classify(saved.get("available", False), saved.get("is_preorder", False)))

        if prev_cls == current_cls:
            # No change — update nothing, no alert
            pass
        else:
            # State flipped
            if current_cls in ("in_stock", "preorder_open"):
                if prev_cls == "unavailable":
                    alert_type = "PREORDER_OPEN" if is_preorder else "BACK_IN_STOCK"
                elif prev_cls == "preorder_open" and current_cls == "in_stock":
                    alert_type = "BACK_IN_STOCK"
                else:
                    alert_type = None
                if alert_type:
                    alerts.append({
                        "product_url": url,
                        "title": product.get("title", "Unknown Product"),
                        "price": product.get("price"),
                        "alert_type": alert_type,
                    })
            # For any transition, update stored state
            set_product_state(state, watch_url, url, {
                "available": available,
                "is_preorder": is_preorder,
                "state": current_cls,
            })

    # Update category known-URL set
    set_known_urls(state, watch_url, known_urls | current_urls)
    return alerts
