"""State management: load/save state.json, detect product transitions.

Layout of state.json (schema 2):

  last_checked     {watch_url: ts}            last successful check per watch
  baselined        {watch_url: {at, filters}} watches that have had their silent first pass
  products         {"watch||product": {available, is_preorder, state,
                                       last_seen, missing_passes}}
  alerted          {product_url: ts}          last time an alert was SENT (cool-down)
  failures         {watch_url: {...}}         active failure streaks
  domain_cooldowns {host: until_ts}           hosts that answered 429
  run_health       {last_success, last_warning, pending_gaps, pending_longest}
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SCHEMA_VERSION = 2

# Missing-means-unavailable: if this many products vanish at once (and they are
# more than half of what was listed), suspect a partial fetch and wait for
# BULK_CONFIRM_PASSES passes before believing it.
BULK_MIN_MISSING = 5
BULK_FRACTION = 0.5
BULK_CONFIRM_PASSES = 3

AVAILABLE_STATES = ("in_stock", "preorder_open")


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


def migrate_state(state: dict, now: float | None = None) -> dict:
    """
    Upgrade an older state.json in place. Safe to call on every pass.

    v1 -> v2:
      * every watch in `category_urls` counts as already baselined, so existing
        watches do not re-baseline (and do not miss anything) after the upgrade;
      * `category_urls` is dropped — `products` already holds the same URLs;
      * products get `last_seen = now` so pruning has a starting point.
    """
    if state.get("schema") == SCHEMA_VERSION:
        return state
    now = now if now is not None else time.time()
    legacy = state.pop("category_urls", None) or {}
    baselined = state.setdefault("baselined", {})
    for watch_url in legacy:
        # filters=None means "accept whatever filters the watch has now"
        baselined.setdefault(watch_url, {"at": now, "filters": None})
    for entry in state.get("products", {}).values():
        entry.setdefault("last_seen", now)
    state["schema"] = SCHEMA_VERSION
    if legacy:
        logger.info("Migrated state to schema %d (%d watches marked baselined)", SCHEMA_VERSION, len(legacy))
    return state


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


# ── Baseline (silent first pass per watch) ──────────────────────────────────

def needs_baseline(state: dict, watch_url: str, filters_fingerprint: str = "") -> bool:
    """
    True when this watch has never completed a pass, or its include/exclude
    filters changed since its baseline. Either way the next pass records what
    it sees without alerting, so adding or re-filtering a watch never floods.
    """
    record = state.get("baselined", {}).get(watch_url)
    if record is None:
        return True
    stored = record.get("filters")
    if stored is None:          # migrated from v1: adopt the current filters
        return False
    return stored != filters_fingerprint


def mark_baselined(state: dict, watch_url: str, filters_fingerprint: str = "", ts: float | None = None) -> None:
    baselined = state.setdefault("baselined", {})
    record = baselined.get(watch_url)
    if record is not None and record.get("filters") == filters_fingerprint:
        return
    at = record.get("at") if record and record.get("filters") is None else None
    baselined[watch_url] = {
        "at": at if at is not None else (ts if ts is not None else time.time()),
        "filters": filters_fingerprint,
    }


# ── Product availability state ───────────────────────────────────────────────

def _product_key(watch_url: str, product_url: str) -> str:
    return f"{watch_url}||{product_url}"


def _split_key(key: str) -> tuple[str, str]:
    watch_url, _, product_url = key.partition("||")
    return watch_url, product_url


def get_product_state(state: dict, watch_url: str, product_url: str) -> dict | None:
    return state.get("products", {}).get(_product_key(watch_url, product_url))


def set_product_state(state: dict, watch_url: str, product_url: str, product_state: dict) -> None:
    state.setdefault("products", {})[_product_key(watch_url, product_url)] = product_state


def get_known_urls(state: dict, watch_url: str) -> set[str]:
    """Every product URL this watch currently has state for."""
    prefix = watch_url + "||"
    return {k[len(prefix):] for k in state.get("products", {}) if k.startswith(prefix)}


def forget_products(state: dict, watch_url: str, product_urls) -> int:
    """Drop this watch's state for the given product URLs (e.g. now filtered out)."""
    products = state.get("products", {})
    removed = 0
    for url in product_urls:
        if products.pop(_product_key(watch_url, url), None) is not None:
            removed += 1
    return removed


# ── Alert cool-down (per product URL, across all watches) ───────────────────

def get_last_alert(state: dict, product_url: str) -> float | None:
    return state.get("alerted", {}).get(product_url)


def record_alert(state: dict, product_url: str, ts: float | None = None) -> None:
    state.setdefault("alerted", {})[product_url] = ts if ts is not None else time.time()


def in_cooldown(state: dict, product_url: str, cooldown_hours: float, now: float) -> bool:
    if not cooldown_hours or cooldown_hours <= 0:
        return False
    last = get_last_alert(state, product_url)
    return last is not None and (now - last) < cooldown_hours * 3600


# ── Domain cool-downs (HTTP 429) ─────────────────────────────────────────────

def get_domain_cooldowns(state: dict) -> dict[str, float]:
    return dict(state.get("domain_cooldowns", {}))


def set_domain_cooldowns(state: dict, cooldowns: dict[str, float], now: float) -> None:
    live = {host: until for host, until in cooldowns.items() if until > now}
    if live:
        state["domain_cooldowns"] = live
    else:
        state.pop("domain_cooldowns", None)


# ── Run health (missed-run detector) ─────────────────────────────────────────

def check_missed_run(
    state: dict,
    now: float,
    threshold_minutes: float,
    warn_every_hours: float,
) -> dict | None:
    """
    Compare this pass's start time with the last successful pass.

    Returns a warning dict to post, or None. A gap longer than the threshold is
    always counted; a warning is returned at most once per `warn_every_hours`,
    and summarises any gaps that were held back since the previous warning.
    Works the same under cron or a long-running loop because it only looks at
    timestamps stored in state.
    """
    health = state.setdefault("run_health", {})
    last = health.get("last_success")
    if last is None:
        return None
    gap = now - float(last)
    if gap <= threshold_minutes * 60:
        return None

    held_back = int(health.get("pending_gaps", 0))
    longest = max(gap, float(health.get("pending_longest", 0.0)))
    last_warning = health.get("last_warning")
    if last_warning is not None and (now - float(last_warning)) < warn_every_hours * 3600:
        health["pending_gaps"] = held_back + 1
        health["pending_longest"] = longest
        return None

    health["last_warning"] = now
    health.pop("pending_gaps", None)
    health.pop("pending_longest", None)
    return {
        "gap_start": float(last),
        "gap_end": now,
        "gap_seconds": gap,
        "earlier_gaps": held_back,
        "longest_seconds": longest,
        "threshold_minutes": threshold_minutes,
    }


def record_successful_pass(state: dict, ts: float) -> None:
    state.setdefault("run_health", {})["last_success"] = ts


# ── Transition detection ─────────────────────────────────────────────────────

def _classify(available: bool, is_preorder: bool) -> str:
    """Return a canonical state string for comparison."""
    if available and is_preorder:
        return "preorder_open"
    if available:
        return "in_stock"
    return "unavailable"


_ALERT_EXTRAS = ("image_url", "cart_url", "checkout_url")


def _make_alert(product: dict, alert_type: str) -> dict:
    alert = {
        "product_url": product["product_url"],
        "title": product.get("title", "Unknown Product"),
        "price": product.get("price"),
        "alert_type": alert_type,
    }
    for key in _ALERT_EXTRAS:
        if product.get(key):
            alert[key] = product[key]
    return alert


def _mark_missing(
    state: dict,
    watch_url: str,
    listed_urls: set[str],
    grace_passes: int,
) -> int:
    """Count consecutive misses for products that left the list; mark them unavailable."""
    prefix = watch_url + "||"
    candidates = [
        entry for key, entry in state.get("products", {}).items()
        if key.startswith(prefix)
        and key[len(prefix):] not in listed_urls
        and entry.get("state") in AVAILABLE_STATES
    ]
    if not candidates:
        return 0
    bulk = (
        len(candidates) >= BULK_MIN_MISSING
        and len(candidates) > BULK_FRACTION * (len(candidates) + len(listed_urls))
    )
    required = max(grace_passes, BULK_CONFIRM_PASSES) if bulk else grace_passes
    if bulk:
        logger.warning(
            "  %d products vanished from %s at once — waiting %d passes before marking them unavailable",
            len(candidates), watch_url, required,
        )
    marked = 0
    for entry in candidates:
        entry["missing_passes"] = int(entry.get("missing_passes", 0)) + 1
        if entry["missing_passes"] >= required:
            entry.update(available=False, is_preorder=False, state="unavailable")
            marked += 1
    if marked:
        logger.info("  Marked %d product(s) unavailable (no longer listed)", marked)
    return marked


def detect_transitions(
    state: dict,
    watch_url: str,
    current_products: list[dict],
    now: float | None = None,
    *,
    listed_urls: set[str] | None = None,
    missing_means_unavailable: bool = False,
    missing_grace_passes: int = 1,
    filters_fingerprint: str = "",
) -> list[dict]:
    """
    Compare the current product list against saved state and return alert dicts.

    Each product dict must have: product_url, title, available (bool),
    is_preorder (bool), price (str|None); optionally image_url, cart_url,
    checkout_url, which are copied onto the alert.

    Alert types: NEW_LISTING, BACK_IN_STOCK, PREORDER_OPEN.

    * Baseline: a watch with no state yet (or whose filters changed) records
      everything silently and returns [].
    * listed_urls: every URL the retailer listed this pass BEFORE title
      filters; used for missing detection. Defaults to the current products.
    * missing_means_unavailable: a previously available product that is no
      longer listed is marked unavailable after `missing_grace_passes`, so its
      return fires BACK_IN_STOCK. Meant for in-stock-only lists.
    """
    now = now if now is not None else time.time()
    baseline = needs_baseline(state, watch_url, filters_fingerprint)
    alerts: list[dict] = []
    seen: set[str] = set()

    for product in current_products:
        url = product["product_url"]
        if url in seen:
            continue
        seen.add(url)
        available = bool(product.get("available"))
        is_preorder = bool(product.get("is_preorder"))
        current_cls = _classify(available, is_preorder)

        saved = get_product_state(state, watch_url, url)
        fresh = {
            "available": available,
            "is_preorder": is_preorder,
            "state": current_cls,
            "last_seen": now,
            "missing_passes": 0,
        }

        if saved is None:
            set_product_state(state, watch_url, url, fresh)
            if not baseline and available:
                alerts.append(_make_alert(product, "PREORDER_OPEN" if is_preorder else "NEW_LISTING"))
            continue

        prev_cls = saved.get("state") or _classify(saved.get("available", False), saved.get("is_preorder", False))
        if not baseline and prev_cls == "unavailable" and current_cls in AVAILABLE_STATES:
            alerts.append(_make_alert(product, "PREORDER_OPEN" if is_preorder else "BACK_IN_STOCK"))
        saved.update(fresh)

    if missing_means_unavailable and not baseline:
        _mark_missing(
            state, watch_url,
            listed_urls if listed_urls is not None else seen,
            missing_grace_passes,
        )

    if baseline:
        logger.info("  Baseline pass for %s: recorded %d products silently", watch_url, len(seen))
    mark_baselined(state, watch_url, filters_fingerprint, now)
    return alerts


# ── Pruning ──────────────────────────────────────────────────────────────────

def prune_state(
    state: dict,
    now: float,
    active_watch_urls: set[str],
    max_age_days: float = 30.0,
) -> dict[str, int]:
    """
    Keep state.json from growing forever.

    * Products are dropped once they have gone unseen for `max_age_days` of
      SUCCESSFUL checks of their watch (measured against the watch's
      last_checked), so a watch that is failing for weeks keeps its memory and
      does not flood when it recovers.
    * Per-watch records for watches no longer in watches.yaml are dropped
      (skipped entirely if no watches are configured, to survive a bad config).
    * Cool-down records older than `max_age_days` are dropped.
    """
    max_age = max_age_days * 86400
    counts = {"products": 0, "watches": 0, "alerted": 0}
    have_watches = bool(active_watch_urls)
    last_checked = state.get("last_checked", {})

    products = state.get("products", {})
    for key in list(products):
        watch_url, _ = _split_key(key)
        if have_watches and watch_url not in active_watch_urls:
            del products[key]
            counts["products"] += 1
            continue
        last_seen = products[key].get("last_seen")
        checked = last_checked.get(watch_url)
        if last_seen is not None and checked is not None and checked - float(last_seen) > max_age:
            del products[key]
            counts["products"] += 1

    if have_watches:
        for section in ("last_checked", "baselined", "failures"):
            records = state.get(section, {})
            for watch_url in list(records):
                if watch_url not in active_watch_urls:
                    del records[watch_url]
                    if section == "last_checked":
                        counts["watches"] += 1

    alerted = state.get("alerted", {})
    for url in list(alerted):
        if now - float(alerted[url]) > max_age:
            del alerted[url]
            counts["alerted"] += 1

    if any(counts.values()):
        logger.info(
            "Pruned state: %d product record(s), %d removed watch(es), %d old cool-down(s)",
            counts["products"], counts["watches"], counts["alerted"],
        )
    return counts
