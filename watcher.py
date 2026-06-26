"""Main entry point for TCG stock watcher — run by GitHub Actions."""
import logging
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

import requests

import adapters
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
            logger.error("404 for %s — skipping permanently", watch.url)
            return []
        logger.error("HTTP error fetching %s: %s", watch.url, exc)
        return []
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", watch.url, exc)
        if fallback_webhook:
            real_session = session._session if isinstance(session, _RetrySession) else session
            discord_client.send_adapter_failure_alert(
                watch.url, watch.retailer_name, str(exc), fallback_webhook, real_session
            )
        return []

    if not products:
        logger.warning("No products found at %s — check adapter", watch.url)
        if fallback_webhook:
            real_session = session._session if isinstance(session, _RetrySession) else session
            discord_client.send_adapter_failure_alert(
                watch.url, watch.retailer_name,
                "Adapter returned 0 products — possible parse failure.",
                fallback_webhook, real_session,
            )
        return []

    logger.info("  Found %d products", len(products))
    alerts = st.detect_transitions(state, watch.url, products)
    st.set_last_checked(state, watch.url)

    if alerts:
        logger.info("  %d alert(s) to send", len(alerts))
    return alerts


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
    for game, alert_watch_pairs in alerts_by_game.items():
        webhook_url = get_webhook(webhooks, game)
        if not webhook_url:
            logger.warning("No webhook configured for game '%s' — skipping alerts", game)
            continue

        # Group by retailer for clarity
        by_retailer: dict[str, list[dict]] = defaultdict(list)
        for alert, watch in alert_watch_pairs:
            by_retailer[watch.retailer_name].append(alert)

        for retailer_name, retailer_alerts in by_retailer.items():
            if not dry_run:
                discord_client.send_alerts(retailer_alerts, retailer_name, webhook_url, raw_session)
            else:
                logger.info("[DRY RUN] Would send %d alert(s) to %s/%s", len(retailer_alerts), game, retailer_name)
                for a in retailer_alerts:
                    logger.info("  [%s] %s — %s", a["alert_type"], a["title"], a.get("price", ""))

    if not dry_run:
        st.save_state(state)
        logger.info("State saved.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TCG stock watcher")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending or saving state")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
