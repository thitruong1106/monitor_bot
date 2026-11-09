"""Main entry point for TCG stock watcher.

`run_pass()` does one full check of every due watch against an in-memory
state dict. `run()` is the cron entry point (GitHub Actions): load state, one
pass, save. A long-running service can instead build the context once and call
`run_pass()` every 30-60 s, saving state after each pass.
"""
import copy
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import requests

import adapters
import alert_log
import config
import discord_client
import state as st
from config import Watch, get_webhook, load_watches, load_webhooks
from ratelimit import DomainThrottle, RateLimited, host_of, parse_retry_after

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("watcher")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
INTER_DOMAIN_DELAY = 1.5  # seconds between requests to same domain


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _retry_get(session: requests.Session, url: str) -> requests.Response:
    """
    GET with exponential backoff on 5xx/timeout. Raises on 404.
    A 429 is NOT retried: it raises RateLimited carrying the Retry-After delay,
    and the caller cools the whole domain down.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                raise requests.HTTPError(f"404 Not Found: {url}", response=resp)
            if resp.status_code == 429:
                raise RateLimited(host_of(url), parse_retry_after(resp.headers.get("Retry-After")))
            if resp.status_code in (500, 502, 503, 504):
                wait = 2 ** attempt
                logger.warning("HTTP %d from %s, retrying in %ds", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.Timeout as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning("Timeout on %s, retrying in %ds", url, wait)
            time.sleep(wait)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise
            last_exc = exc
            wait = 2 ** attempt
            logger.warning("HTTP error on %s: %s, retrying in %ds", url, exc, wait)
            time.sleep(wait)
    raise last_exc or Exception(f"All retries failed for {url}")


def _should_check(watch: Watch, state: dict, now: float) -> bool:
    last = st.get_last_checked(state, watch.url)
    elapsed_minutes = (now - last) / 60.0
    return elapsed_minutes >= watch.interval_minutes


class _RetrySession:
    """Wraps a requests.Session so adapters get throttling, retries and 429 cool-downs."""

    def __init__(self, real_session: requests.Session, throttle: DomainThrottle):
        self._session = real_session
        self._throttle = throttle

    def get(self, url: str, **kwargs) -> requests.Response:
        remaining = self._throttle.remaining(url)
        if remaining > 0:
            raise RateLimited(host_of(url), remaining, cached=True)
        self._throttle.wait(url)
        try:
            return _retry_get(self._session, url)
        except RateLimited as exc:
            self._throttle.cool_down(url, exc.retry_after)
            raise

    def post(self, *args, **kwargs):
        return self._session.post(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


def _real_session(session):
    return session._session if isinstance(session, _RetrySession) else session


# ── Failure streaks ──────────────────────────────────────────────────────────

def _note_failure(
    watch: Watch, state: dict, error: str, fallback_webhook: str | None, session,
    notify: bool = True, title: str = "⚠️ Adapter Parse Failure", now: float | None = None,
) -> None:
    """
    Record that this watch failed. Only the FIRST failure in a streak posts to
    Discord; later passes just bump the counter in state.json. A recovery alert
    is sent by _note_success when the watch works again.
    """
    is_new = st.record_failure(state, watch.url, error, ts=now)
    if not is_new:
        failure = st.get_failure(state, watch.url) or {}
        logger.warning("  Still failing (%d passes): %s", failure.get("count", 0), error)
        return
    logger.error("  Watch started failing: %s", error)
    if fallback_webhook and notify:
        discord_client.send_adapter_failure_alert(
            watch.url, watch.retailer_name, error, fallback_webhook, _real_session(session), title=title,
        )


def _note_success(watch: Watch, state: dict, fallback_webhook: str | None, session, notify: bool = True) -> None:
    """Clear any failure streak and, if there was one, post a one-off recovery alert."""
    cleared = st.clear_failure(state, watch.url)
    if not cleared:
        return
    logger.info("  Watch recovered after %d failed pass(es)", cleared.get("count", 0))
    if fallback_webhook and notify:
        discord_client.send_watch_recovered_alert(
            watch.url, watch.retailer_name, cleared, fallback_webhook, _real_session(session)
        )


# ── One watch ────────────────────────────────────────────────────────────────

def process_watch(
    watch: Watch,
    state: dict,
    session,
    webhooks: dict[str, str],
    fallback_webhook: str | None,
    now: float | None = None,
    notify: bool = True,
) -> list[dict]:
    """Fetch a single watch entry, apply its filters, detect transitions, return alerts."""
    now = now if now is not None else time.time()
    logger.info("Checking [%s] %s (%s)", watch.game, watch.retailer_name, watch.url)

    try:
        fetch_fn = adapters.get_adapter(watch.url, session, platform=watch.platform)
        raw = fetch_fn(watch.url, session)
    except RateLimited as exc:
        if exc.cached:
            logger.info("  Skipped: %s", exc)
            return []
        _note_failure(
            watch, state,
            f"HTTP 429 (too many requests) from {exc.host}. Not contacting that site again for "
            f"{int(exc.retry_after)} s.",
            fallback_webhook, session, notify, title="⏳ Rate Limited", now=now,
        )
        return []
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            _note_failure(watch, state, f"404 Not Found: {watch.url}", fallback_webhook, session, notify, now=now)
        else:
            _note_failure(watch, state, f"HTTP error: {exc}", fallback_webhook, session, notify, now=now)
        return []
    except Exception as exc:
        _note_failure(watch, state, f"Fetch failed: {exc}", fallback_webhook, session, notify, now=now)
        return []

    if not raw:
        _note_failure(
            watch, state, "Adapter returned 0 products — possible parse failure.",
            fallback_webhook, session, notify, now=now,
        )
        return []

    products = watch.apply_filters(raw)
    listed = {p["product_url"] for p in raw}
    excluded = listed - {p["product_url"] for p in products}
    if excluded:
        forgotten = st.forget_products(state, watch.url, excluded)
        if forgotten:
            logger.info("  Forgot %d product(s) now excluded by this watch's filters", forgotten)
    if len(products) == len(raw):
        logger.info("  Found %d products", len(raw))
    else:
        logger.info("  Found %d products, %d after title filters", len(raw), len(products))

    _note_success(watch, state, fallback_webhook, session, notify)
    alerts = st.detect_transitions(
        state, watch.url, products, now,
        listed_urls=listed,
        missing_means_unavailable=watch.missing_means_unavailable,
        missing_grace_passes=watch.missing_grace_passes,
        filters_fingerprint=watch.filters_fingerprint,
    )
    st.set_last_checked(state, watch.url, now)

    if alerts:
        logger.info("  %d alert(s) to send", len(alerts))
    return alerts


# ── Alert post-processing ────────────────────────────────────────────────────

def dedupe_alerts(alert_watch_pairs: list[tuple[dict, Watch]]) -> list[tuple[dict, Watch]]:
    """
    Drop repeat alerts for the same product URL within one pass.

    Two watches on the same retailer can resolve to the same product list,
    so the same restock would otherwise be posted once per watch. The first
    watch to report a product wins; the rest are discarded.
    """
    seen: set[str] = set()
    kept: list[tuple[dict, Watch]] = []
    for alert, watch in alert_watch_pairs:
        key = alert.get("product_url") or f"{watch.url}||{alert.get('title')}"
        if key in seen:
            logger.info(
                "  Skipping duplicate alert for %s (already reported via another %s watch)",
                key, watch.retailer_name,
            )
            continue
        seen.add(key)
        kept.append((alert, watch))
    return kept


def apply_cooldown(
    alert_watch_pairs: list[tuple[dict, Watch]], state: dict, now: float,
) -> tuple[list[tuple[dict, Watch]], int]:
    """
    Drop alerts for products that were alerted less than the watch's
    cooldown_hours ago (any watch, any alert type). Returns (kept, suppressed).
    """
    kept: list[tuple[dict, Watch]] = []
    suppressed = 0
    for alert, watch in alert_watch_pairs:
        url = alert["product_url"]
        if st.in_cooldown(state, url, watch.cooldown_hours, now):
            last = st.get_last_alert(state, url) or now
            logger.info(
                "  Cool-down: not re-alerting %s (last alert %.1f h ago, cool-down %.1f h)",
                url, (now - last) / 3600, watch.cooldown_hours,
            )
            suppressed += 1
            continue
        kept.append((alert, watch))
    return kept, suppressed


# ── One pass ─────────────────────────────────────────────────────────────────

@dataclass
class WatcherContext:
    """Everything a pass needs that is NOT state. Build once; reuse across passes."""
    watches: list[Watch]
    webhooks: dict[str, str]
    raw_session: requests.Session
    session: _RetrySession
    throttle: DomainThrottle
    role_id: str | None = None
    heartbeat_url: str | None = None
    dry_run: bool = False
    missed_run_alerts: bool = config.MISSED_RUN_ALERTS
    missed_run_minutes: float = config.MISSED_RUN_MINUTES
    missed_run_warn_every_hours: float = config.MISSED_RUN_WARN_EVERY_HOURS
    prune_after_days: float = config.PRUNE_AFTER_DAYS

    @property
    def fallback_webhook(self) -> str | None:
        return self.webhooks.get("_fallback")


def build_context(dry_run: bool = False, watches: list[Watch] | None = None,
                  webhooks: dict[str, str] | None = None) -> WatcherContext:
    raw_session = _make_session()
    throttle = DomainThrottle(INTER_DOMAIN_DELAY)
    return WatcherContext(
        watches=watches if watches is not None else load_watches(),
        webhooks=webhooks if webhooks is not None else load_webhooks(),
        raw_session=raw_session,
        session=_RetrySession(raw_session, throttle),
        throttle=throttle,
        role_id=config.load_role_id(),
        missed_run_alerts=config._env_flag("MISSED_RUN_ALERTS", False),
        heartbeat_url=config.load_heartbeat_url(),
        dry_run=dry_run,
    )


def _ping_heartbeat(ctx: WatcherContext) -> None:
    if not ctx.heartbeat_url or ctx.dry_run:
        return
    try:
        ctx.raw_session.get(ctx.heartbeat_url, timeout=10)
    except Exception as exc:
        logger.warning("Heartbeat ping failed: %s", exc)


def run_pass(ctx: WatcherContext, state: dict, now: float | None = None) -> dict:
    """
    One complete pass: check due watches, send alerts, update and prune state.
    Mutates `state` in place; the caller decides when to save it.
    Returns a small summary dict (handy for logs and tests).
    """
    now = now if now is not None else time.time()
    st.migrate_state(state, now)
    summary = {"checked": 0, "skipped_rate_limited": 0, "alerts_sent": 0,
               "suppressed_cooldown": 0, "missed_run_warning": False}

    # 1. Did we fall behind since the last good pass? (Posting is opt-in.)
    if ctx.missed_run_alerts:
        warning = st.check_missed_run(state, now, ctx.missed_run_minutes, ctx.missed_run_warn_every_hours)
        if warning:
            summary["missed_run_warning"] = True
            logger.warning("No successful pass for %.1f h before this one", warning["gap_seconds"] / 3600)
            if ctx.fallback_webhook and not ctx.dry_run:
                discord_client.send_missed_run_alert(warning, ctx.fallback_webhook, ctx.raw_session)
    else:
        last = state.get("run_health", {}).get("last_success")
        if last is not None and now - float(last) > ctx.missed_run_minutes * 60:
            logger.info("Last successful pass was %.1f h ago (MISSED_RUN_ALERTS is off, not posting)",
                        (now - float(last)) / 3600)

    # 2. Check every due watch.
    ctx.throttle.load(st.get_domain_cooldowns(state))
    alerts_by_game: dict[str, list[tuple[dict, Watch]]] = defaultdict(list)
    for watch in ctx.watches:
        if not _should_check(watch, state, now):
            logger.debug("Skipping %s (not due yet)", watch.url)
            continue
        wait = ctx.throttle.remaining(watch.url)
        if wait > 0:
            logger.info("Skipping %s — %s is cooling down for %d more s", watch.retailer_name,
                        host_of(watch.url), int(wait))
            summary["skipped_rate_limited"] += 1
            continue
        summary["checked"] += 1
        for alert in process_watch(watch, state, ctx.session, ctx.webhooks, ctx.fallback_webhook,
                                   now=now, notify=not ctx.dry_run):
            alerts_by_game[watch.game].append((alert, watch))

    # 3. De-duplicate, apply cool-down, send.
    sent_records: list[dict] = []
    for game, pairs in alerts_by_game.items():
        webhook_url = get_webhook(ctx.webhooks, game)
        if not webhook_url:
            logger.warning("No webhook configured for game '%s' — skipping alerts", game)
            continue
        pairs = dedupe_alerts(pairs)
        pairs, suppressed = apply_cooldown(pairs, state, now)
        summary["suppressed_cooldown"] += suppressed

        by_retailer: dict[str, list[dict]] = defaultdict(list)
        for alert, watch in pairs:
            by_retailer[watch.retailer_name].append(alert)

        for retailer_name, retailer_alerts in by_retailer.items():
            if ctx.dry_run:
                logger.info("[DRY RUN] Would send %d alert(s) to %s/%s", len(retailer_alerts), game, retailer_name)
                for a in retailer_alerts:
                    logger.info("  [%s] %s — %s", a["alert_type"], a["title"], a.get("price", ""))
                continue
            discord_client.send_alerts(retailer_alerts, retailer_name, webhook_url, ctx.raw_session,
                                       role_id=ctx.role_id)
            for a in retailer_alerts:
                st.record_alert(state, a["product_url"], now)
            sent_records.extend(alert_log.make_record(a, retailer_name) for a in retailer_alerts)
            summary["alerts_sent"] += len(retailer_alerts)

    # 4. Housekeeping.
    st.set_domain_cooldowns(state, ctx.throttle.export(), time.time())
    st.prune_state(state, now, {w.url for w in ctx.watches}, ctx.prune_after_days)
    st.record_successful_pass(state, now)
    if not ctx.dry_run:
        alert_log.append_records(sent_records)
        _ping_heartbeat(ctx)
    return summary


def run(dry_run: bool = False) -> None:
    """Cron entry point: load state, one pass, save. A dry run never writes any file."""
    ctx = build_context(dry_run=dry_run)
    state = st.load_state()
    if dry_run:
        logger.info("DRY RUN: state changes below happen on a throwaway copy; "
                    "state.json and alerts.jsonl will not be written.")
        run_pass(ctx, copy.deepcopy(state))
        logger.info("DRY RUN finished: nothing was saved and nothing was posted.")
        return
    run_pass(ctx, state)
    st.save_state(state)
    logger.info("State saved.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TCG stock watcher")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending or saving state")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
