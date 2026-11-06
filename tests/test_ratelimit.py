"""HTTP 429 handling: honour Retry-After, cool the domain down, never retry within seconds."""
from unittest.mock import MagicMock

import pytest

import discord_client
import watcher
from config import Watch
from ratelimit import DomainThrottle, RateLimited, parse_retry_after


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _resp(status, headers=None, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = json_body or {}
    r.raise_for_status = MagicMock()
    return r


# ── Retry-After parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("120", 120.0),            # honoured as given
    (None, 300.0),             # missing -> default 5 min
    ("soon", 300.0),           # garbage -> default
    ("2", 60.0),               # never shorter than 60 s
    ("86400", 3600.0),         # never longer than 1 h
])
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    now = 1445412480.0 - 900      # header date is 15 min after "now"
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=now) == 900.0


# ── _retry_get / _RetrySession ───────────────────────────────────────────────

def test_429_is_not_retried_and_raises_with_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(watcher.time, "sleep", lambda s: sleeps.append(s))
    session = MagicMock()
    session.get.return_value = _resp(429, {"Retry-After": "240"})
    with pytest.raises(RateLimited) as info:
        watcher._retry_get(session, "https://shop.example/collections/x/products.json")
    assert info.value.retry_after == 240.0
    assert session.get.call_count == 1          # old code retried 3 times within ~7 s
    assert sleeps == []


def test_retry_session_cools_domain_and_blocks_further_requests():
    clock = FakeClock()
    throttle = DomainThrottle(delay=0, clock=clock, sleep=lambda s: None)
    real = MagicMock()
    real.get.return_value = _resp(429, {"Retry-After": "600"})
    session = watcher._RetrySession(real, throttle)

    with pytest.raises(RateLimited):
        session.get("https://shop.example/a")
    # Same host, different path: refused locally, no request made.
    with pytest.raises(RateLimited) as info:
        session.get("https://shop.example/b")
    assert info.value.cached is True
    assert real.get.call_count == 1
    # Another host is unaffected.
    real.get.return_value = _resp(200)
    session.get("https://other.example/a")
    assert real.get.call_count == 2
    # After the cool-down, the host is usable again.
    clock.t += 601
    session.get("https://shop.example/a")
    assert real.get.call_count == 3


def test_5xx_is_still_retried_with_backoff(monkeypatch):
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    session = MagicMock()
    session.get.side_effect = [_resp(503), _resp(200)]
    watcher._retry_get(session, "https://shop.example/x")
    assert session.get.call_count == 2


# ── Pass-level behaviour ─────────────────────────────────────────────────────

def _ctx(watches, real_session, clock):
    throttle = DomainThrottle(delay=0, clock=clock, sleep=lambda s: None)
    return watcher.WatcherContext(
        watches=watches,
        webhooks={"one-piece": "https://hook/op", "_fallback": "https://hook/fb"},
        raw_session=MagicMock(),
        session=watcher._RetrySession(real_session, throttle),
        throttle=throttle,
    )


def _w(url, name):
    return Watch(url=url, game="one-piece", tier="normal", type="category", retailer_name=name, platform="shopify")


def test_429_skips_other_watches_on_that_domain_and_alerts_once(monkeypatch):
    failure = MagicMock()
    monkeypatch.setattr(discord_client, "send_adapter_failure_alert", failure)
    monkeypatch.setattr(discord_client, "send_alerts", MagicMock())
    clock = FakeClock()
    monkeypatch.setattr(watcher.time, "time", clock)

    real = MagicMock()
    def get(url, **kw):
        if "goodgames" in url:
            return _resp(429, {"Retry-After": "900"})
        return _resp(200, json_body={"products": [{"handle": "box", "title": "Box", "variants": [{"id": 1, "available": True, "price": "10"}]}]})
    real.get.side_effect = get

    watches = [
        _w("https://www.goodgames.com.au/collections/a", "GG A"),
        _w("https://www.goodgames.com.au/collections/b", "GG B"),
        _w("https://www.gameology.com.au/collections/c", "Gameology"),
    ]
    ctx = _ctx(watches, real, clock)
    state = {}
    summary = watcher.run_pass(ctx, state, now=clock.t)

    gg_requests = [c for c in real.get.call_args_list if "goodgames" in c.args[0]]
    assert len(gg_requests) == 1                       # second Good Games watch never hit the site
    assert summary["skipped_rate_limited"] == 1
    assert failure.call_count == 1
    assert failure.call_args.kwargs["title"] == "⏳ Rate Limited"
    assert "900 s" in failure.call_args.args[2]
    # Cool-down is saved so a cron run starting soon after also respects it.
    assert state["domain_cooldowns"]["www.goodgames.com.au"] == pytest.approx(clock.t + 900)
    # Gameology was checked normally.
    assert "https://www.gameology.com.au/collections/c" in state["last_checked"]
    # Skipped/limited watches are not marked as checked, so they retry once the cool-down ends.
    assert "https://www.goodgames.com.au/collections/b" not in state["last_checked"]


def test_saved_cooldown_is_respected_by_the_next_run(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(watcher.time, "time", clock)
    real = MagicMock()
    ctx = _ctx([_w("https://www.goodgames.com.au/collections/a", "GG")], real, clock)
    state = {"domain_cooldowns": {"www.goodgames.com.au": clock.t + 120}}
    summary = watcher.run_pass(ctx, state, now=clock.t)
    assert real.get.call_count == 0 and summary["skipped_rate_limited"] == 1
    # Expired cool-downs are cleaned out of state.
    clock.t += 121
    ctx2 = _ctx([], real, clock)
    watcher.run_pass(ctx2, state, now=clock.t)
    assert "domain_cooldowns" not in state


def test_is_shopify_probe_does_not_swallow_rate_limit():
    from adapters import shopify
    session = MagicMock()
    session.get.side_effect = RateLimited("x.example", 60)
    with pytest.raises(RateLimited):
        shopify.is_shopify("https://x.example/collections/a", session)
