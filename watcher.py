"""Main entry point for TCG stock watcher — run by GitHub Actions."""
import logging
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

import requests

import adapters
import alert_log
import discord_client
import state as st
from config import TIER_INTERVALS, Watch, get_webhook, load_watches, load_webhooks

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
    """GET with exponential backoff on 5xx/429/timeout. Raises on 404."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                raise requests.HTTPError(f"404 Not Found: {url}", response=resp)
            if resp.status_code in (429, 500, 502, 503, 504):
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


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


class DomainThrottle:
    """Track last-request time per domain to enforce inter-request delays."""

    def __init__(self, delay: float = INTER_DOMAIN_DELAY):
        self._last: dict[str, float] = {}
        self._delay = delay

    def wait(self, url: str) -> None:
        host = _domain(url)
        since = time.time() - self._last.get(host, 0.0)
        if since < self._delay:
            time.sleep(self._delay - since)
        self._last[host] = time.time()


class _RetrySession:
    """Wraps a requests.Session so adapters can call session.get() with auto-retry."""

    def __init__(self, real_session: requests.Session, throttle: DomainThrottle):
        self._session = real_session
        self._throttle = throttle

    def get(self, url: str, **kwargs) -> requests.Response:
        self._throttle.wait(url)
        return _retry_get(self._session, url)

    def post(self, *args, **kwargs):
        return self._session.post(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


def _real_session(session):
    return session._session if isinstance(session, _RetrySession) else session


def _note_failure(watch: Watch, state: dict, error: str, fallback_webhook: str | None, session) -> None:
    """
    Record that this watch failed. Only the FIRST failure in a streak posts to
    Discord; later passes just bump the counter in state.json. A recovery alert
    is sent by _note_success when the watch works again.
    """
    is_new = st.record_failure(state, watch.url, error)
    if not is_new:
        failure = st.get_failure(state, watch.url) or {}
        logger.warning("  Still failing (%d passes): %s", failure.get("count", 0), error)
        return
    logger.error("  Watch started failing: %s", error)
    if fallback_webhook:
        discord_client.send_adapter_failure_alert(
            watch.url, watch.retailer_name, error, fallback_webhook, _real_session(session)
        )


def _note_success(watch: Watch, state: dict, fallback_webhook: str | None, session) -> None:
    """Clear any failure streak and, if there was one, post a one-off recovery alert."""
    cleared = st.clear_failure(state, watch.url)
    if not cleared:
        return
    logger.info("  Watch recovered after %d failed pass(es)", cleared.get("count", 0))
    if fallback_webhook:
        discord_client.send_watch_recovered_alert(
            watch.url, watch.retailer_name, cleared, fallback_webhook, _real_session(session)
        )


def process_watch(
    watch: Watch,
    state: dict,
    session,
    webhooks: dict[str, str],
    fallback_webhook: str | None,
) -> list[dict]:
    """Fetch a single watch entry, detect transitions, return list of alerts."""
    logger.info("Checking [%s] %s (%s)", watch.game, watch.retailer_name, watch.url)

    fetch_fn = adapters.get_adapter(watch.url, session)
    try:
        products = fetch_fn(watch.url, session)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            _note_failure(watch, state, f"404 Not Found: {watch.url}", fallback_webhook, session)
        else:
            _note_failure(watch, state, f"HTTP error: {exc}", fallback_webhook, session)
        return []
    except Exception as exc:
        _note_failure(watch, state, f"Fetch failed: {exc}", fallback_webhook, session)
        return []

    if not products:
        _note_failure(
            watch, state,
            "Adapter returned 0 products — possible parse failure.",
            fallback_webhook, session,
        )
        return []

    logger.info("  Found %d products", len(products))
    _note_success(watch, state, fallback_webhook, session)
    alerts = st.detect_transitions(state, watch.url, products)
    st.set_last_checked(state, watch.url)

    if alerts:
        logger.info("  %d alert(s) to send", len(alerts))
    return alerts


def dedupe_alerts(alert_watch_pairs: list[tuple[dict, Watch]]) -> list[tuple[dict, Watch]]:
    """
    Drop repeat alerts for the same product URL within one pass.

    Two watches on the same retailer can resolve to the same product list
    (e.g. a collection URL with and without a client-side filter fragment),
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


def run(dry_run: bool = False) -> None:
    watches = load_watches()
    webhooks = load_webhooks()
    fallback_webhook = webhooks.get("_fallback")
    state = st.load_state()
    now = time.time()

    raw_session = _make_session()
    throttle = DomainThrottle()
    session = _RetrySession(raw_session, throttle)

    # Bucket alerts per game so we can send one Discord message per game
    alerts_by_game: dict[str, list[tuple[dict, Watch]]] = defaultdict(list)

    for watch in watches:
        if not _should_check(watch, state, now):
            logger.debug("Skipping %s (not due yet)", watch.url)
            continue

        alerts = process_watch(watch, state, session, webhooks, fallback_webhook)
        for alert in alerts:
            alerts_by_game[watch.game].append((alert, watch))

    # Send grouped Discord alerts
    sent_records: list[dict] = []
    for game, alert_watch_pairs in alerts_by_game.items():
        webhook_url = get_webhook(webhooks, game)
        if not webhook_url:
            logger.warning("No webhook configured for game '%s' — skipping alerts", game)
            continue

        alert_watch_pairs = dedupe_alerts(alert_watch_pairs)

        # Group by retailer for clarity
        by_retailer: dict[str, list[dict]] = defaultdict(list)
        for alert, watch in alert_watch_pairs:
            by_retailer[watch.retailer_name].append(alert)

        for retailer_name, retailer_alerts in by_retailer.items():
            if not dry_run:
                discord_client.send_alerts(retailer_alerts, retailer_name, webhook_url, raw_session)
                sent_records.extend(alert_log.make_record(a, retailer_name) for a in retailer_alerts)
            else:
                logger.info("[DRY RUN] Would send %d alert(s) to %s/%s", len(retailer_alerts), game, retailer_name)
                for a in retailer_alerts:
                    logger.info("  [%s] %s — %s", a["alert_type"], a["title"], a.get("price", ""))

    if not dry_run:
        alert_log.append_records(sent_records)
        st.save_state(state)
        logger.info("State saved.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TCG stock watcher")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending or saving state")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
