"""HTTP 429 handling: parse Retry-After and keep per-domain cool-downs."""
import logging
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 300.0   # when a 429 has no usable Retry-After
MIN_COOLDOWN_SECONDS = 60.0        # never hammer a site that just said "slow down"
MAX_COOLDOWN_SECONDS = 3600.0      # cap absurd values so one bad header can't mute a site for days


class RateLimited(Exception):
    """A site answered 429, or is still inside the cool-down from an earlier 429."""

    def __init__(self, host: str, retry_after: float, cached: bool = False):
        self.host = host
        self.retry_after = float(retry_after)
        self.cached = cached
        what = "still cooling down" if cached else "HTTP 429"
        super().__init__(f"{what} for {host}: retry in {int(self.retry_after)} s")


def host_of(url_or_host: str) -> str:
    if "://" in url_or_host:
        return urlparse(url_or_host).netloc.lower()
    return url_or_host.lower()


def parse_retry_after(value, now: float | None = None) -> float:
    """
    Seconds to wait from a Retry-After header (delta-seconds or HTTP-date),
    clamped to [MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS].
    """
    now = now if now is not None else time.time()
    seconds = None
    if value is not None:
        text = str(value).strip()
        if text.isdigit():
            seconds = float(text)
        elif text:
            try:
                when = parsedate_to_datetime(text)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = when.timestamp() - now
            except (TypeError, ValueError, IndexError):
                seconds = None
    if seconds is None:
        seconds = DEFAULT_COOLDOWN_SECONDS
    return max(MIN_COOLDOWN_SECONDS, min(MAX_COOLDOWN_SECONDS, seconds))


class DomainThrottle:
    """
    Per-domain politeness:
      * wait():       keep `delay` seconds between requests to the same host;
      * cool_down():  after a 429, refuse requests to that host until it expires.
    Cool-downs can be exported to / loaded from state so they survive between
    cron runs, and simply persist in memory in a long-running loop.
    """

    def __init__(self, delay: float = 1.5, clock=time.time, sleep=time.sleep):
        self._last: dict[str, float] = {}
        self._until: dict[str, float] = {}
        self._delay = delay
        self._clock = clock
        self._sleep = sleep

    def wait(self, url: str) -> None:
        host = host_of(url)
        since = self._clock() - self._last.get(host, 0.0)
        if since < self._delay:
            self._sleep(self._delay - since)
        self._last[host] = self._clock()

    def cool_down(self, url_or_host: str, seconds: float) -> float:
        host = host_of(url_or_host)
        until = self._clock() + seconds
        self._until[host] = max(until, self._until.get(host, 0.0))
        logger.warning("Rate limited by %s — no requests to it for %d s", host, int(seconds))
        return self._until[host]

    def remaining(self, url_or_host: str) -> float:
        until = self._until.get(host_of(url_or_host), 0.0)
        return max(0.0, until - self._clock())

    def load(self, cooldowns: dict[str, float]) -> None:
        for host, until in (cooldowns or {}).items():
            self._until[host] = max(float(until), self._until.get(host, 0.0))

    def export(self) -> dict[str, float]:
        now = self._clock()
        return {h: u for h, u in self._until.items() if u > now}
