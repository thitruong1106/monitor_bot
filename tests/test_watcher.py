"""
End-to-end-ish tests for watcher.py with HTTP and Discord mocked out.

Covers the review fixes:
  1. dead Games Portal (Preorder) watch is gone
  2. one alert per product URL even when two watches see the same product
  4. failure alert once at the start of a streak, recovery alert once at the end
  5. every sent alert is appended to alerts.jsonl and the workflow commits it
"""
import json
import os
from unittest.mock import MagicMock

import pytest
import yaml

import adapters
import alert_log
import discord_client
import state as st
import watcher
from config import Watch, load_watches

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _watch(url, retailer="Gameology", tier="normal"):
    return Watch(url=url, game="one-piece", tier=tier, type="category", retailer_name=retailer)


def _product(url, available=True, title="OP09 Booster Box", price="$69.95", is_preorder=False):
    return {"product_url": url, "title": title, "available": available, "is_preorder": is_preorder, "price": price}


@pytest.fixture
def discord(monkeypatch):
    """Replace every Discord sender with a mock and return them."""
    mocks = {
        "alerts": MagicMock(),
        "failure": MagicMock(),
        "recovered": MagicMock(),
    }
    monkeypatch.setattr(discord_client, "send_alerts", mocks["alerts"])
    monkeypatch.setattr(discord_client, "send_adapter_failure_alert", mocks["failure"])
    monkeypatch.setattr(discord_client, "send_watch_recovered_alert", mocks["recovered"])
    return mocks


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(alert_log, "ALERTS_FILE", str(tmp_path / "alerts.jsonl"))
    return tmp_path


def _install_adapter(monkeypatch, fetch_results: dict):
    """
    fetch_results maps watch URL -> list[dict] | Exception.
    Installs a fake adapter and stops the watcher from sleeping or touching the network.
    """
    def fake_fetch(url, session):
        result = fetch_results[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(adapters, "get_adapter", lambda url, session=None: fake_fetch)
    monkeypatch.setattr(watcher.time, "sleep", lambda *_: None)


def _run_with(monkeypatch, watches, fetch_results, webhooks=None, dry_run=False):
    _install_adapter(monkeypatch, fetch_results)
    monkeypatch.setattr(watcher, "load_watches", lambda: watches)
    monkeypatch.setattr(
        watcher, "load_webhooks",
        lambda: webhooks if webhooks is not None else {"one-piece": "https://hook/op", "_fallback": "https://hook/fb"},
    )
    watcher.run(dry_run=dry_run)


# ── Fix 1: dead Games Portal (Preorder) watch ─────────────────────────────────

def test_dead_gamesportal_preorder_watch_removed_from_watches_yaml():
    urls = [w.url for w in load_watches(os.path.join(REPO, "watches.yaml"))]
    assert "https://gamesportal.com.au/collections/one-piece-card-game/Preorder" not in urls
    # The working Games Portal watch is still there
    assert "https://gamesportal.com.au/collections/one-piece-card-game" in urls


def test_every_shopify_collection_watch_has_a_single_path_segment_after_collections():
    """
    A Shopify collection URL is /collections/<handle>; anything after the handle
    turns into /collections/<handle>/<extra>/products.json, which 404s.
    """
    from urllib.parse import urlparse
    for w in load_watches(os.path.join(REPO, "watches.yaml")):
        path = urlparse(w.url).path
        if "/collections/" not in path:
            continue
        after = path.split("/collections/", 1)[1].strip("/")
        assert "/" not in after, f"{w.url} has extra path after the collection handle"


# ── Fix 2: one alert per product across watches on the same retailer ─────────

def test_dedupe_alerts_keeps_first_occurrence_per_product_url():
    w1 = _watch("https://shop.com/collections/a", "Gameology")
    w2 = _watch("https://shop.com/collections/a#/filter:preorder", "Gameology (Preorder)")
    a1 = {"product_url": "https://shop.com/products/x", "title": "X", "alert_type": "BACK_IN_STOCK"}
    a2 = {"product_url": "https://shop.com/products/x", "title": "X", "alert_type": "BACK_IN_STOCK"}
    a3 = {"product_url": "https://shop.com/products/y", "title": "Y", "alert_type": "NEW_LISTING"}
    kept = watcher.dedupe_alerts([(a1, w1), (a2, w2), (a3, w2)])
    assert [(a["product_url"], w.retailer_name) for a, w in kept] == [
        ("https://shop.com/products/x", "Gameology"),
        ("https://shop.com/products/y", "Gameology (Preorder)"),
    ]


def test_same_restock_seen_by_two_gameology_watches_is_posted_once(monkeypatch, discord, files):
    plain = "https://www.gameology.com.au/collections/one-piece-booster-boxs"
    frag = plain + "#/filter:ss_filter_availability:Show$2520Pre-Order"
    product = "https://www.gameology.com.au/products/op09-booster-box"
    watches = [_watch(plain, "Gameology"), _watch(frag, "Gameology (Preorder)")]

    # Pass 1: both watches see the product out of stock (baseline, no alerts)
    _run_with(monkeypatch, watches, {plain: [_product(product, False)], frag: [_product(product, False)]})
    assert discord["alerts"].call_count == 0

    # Pass 2: it restocks — both watches detect the flip, Discord gets it once
    st.save_state({**st.load_state(), "last_checked": {}})  # make both due again
    _run_with(monkeypatch, watches, {plain: [_product(product, True)], frag: [_product(product, True)]})
    assert discord["alerts"].call_count == 1
    sent_alerts, retailer, _hook, _sess = discord["alerts"].call_args[0]
    assert [a["product_url"] for a in sent_alerts] == [product]
    assert retailer == "Gameology"

    # Both watches' state was still updated, so pass 3 is quiet
    st.save_state({**st.load_state(), "last_checked": {}})
    _run_with(monkeypatch, watches, {plain: [_product(product, True)], frag: [_product(product, True)]})
    assert discord["alerts"].call_count == 1


def test_different_products_on_two_watches_both_alert(monkeypatch, discord, files):
    w_a = "https://shop.com/collections/a"
    w_b = "https://shop.com/collections/b"
    watches = [_watch(w_a, "Shop"), _watch(w_b, "Shop")]
    _run_with(monkeypatch, watches, {w_a: [_product("https://shop.com/products/1", False)],
                                     w_b: [_product("https://shop.com/products/2", False)]})
    st.save_state({**st.load_state(), "last_checked": {}})
    _run_with(monkeypatch, watches, {w_a: [_product("https://shop.com/products/1", True)],
                                     w_b: [_product("https://shop.com/products/2", True)]})
    sent_alerts = discord["alerts"].call_args[0][0]
    assert sorted(a["product_url"] for a in sent_alerts) == ["https://shop.com/products/1", "https://shop.com/products/2"]


# ── Fix 4: failure alert once, recovery alert once ───────────────────────────

def _process(monkeypatch, watch, state, result, discord):
    _install_adapter(monkeypatch, {watch.url: result})
    return watcher.process_watch(watch, state, MagicMock(), {}, "https://hook/fb")


def test_failure_alert_only_once_per_streak(monkeypatch, discord):
    watch = _watch("https://broken.example/collections/x", "Broken")
    state = {}
    for _ in range(5):
        _process(monkeypatch, watch, state, RuntimeError("boom"), discord)
    assert discord["failure"].call_count == 1
    assert st.get_failure(state, watch.url)["count"] == 5
    assert discord["recovered"].call_count == 0


def test_zero_products_counts_as_failure_and_alerts_once(monkeypatch, discord):
    watch = _watch("https://empty.example/series/x", "Empty")
    state = {}
    _process(monkeypatch, watch, state, [], discord)
    _process(monkeypatch, watch, state, [], discord)
    assert discord["failure"].call_count == 1
    error_text = discord["failure"].call_args[0][2]
    assert "0 products" in error_text


def test_404_is_tracked_as_a_failure(monkeypatch, discord):
    import requests
    resp = MagicMock(status_code=404)
    err = requests.HTTPError("404", response=resp)
    watch = _watch("https://shop.example/collections/gone/Preorder", "Gone")
    state = {}
    _process(monkeypatch, watch, state, err, discord)
    _process(monkeypatch, watch, state, err, discord)
    assert discord["failure"].call_count == 1
    assert "404" in discord["failure"].call_args[0][2]


def test_recovery_alert_once_then_silence(monkeypatch, discord):
    watch = _watch("https://flaky.example/collections/x", "Flaky")
    state = {}
    _process(monkeypatch, watch, state, RuntimeError("down"), discord)
    _process(monkeypatch, watch, state, RuntimeError("down"), discord)
    _process(monkeypatch, watch, state, [_product("https://flaky.example/products/1", False)], discord)
    _process(monkeypatch, watch, state, [_product("https://flaky.example/products/1", False)], discord)
    assert discord["failure"].call_count == 1
    assert discord["recovered"].call_count == 1
    failure_record = discord["recovered"].call_args[0][2]
    assert failure_record["count"] == 2
    assert st.get_failure(state, watch.url) is None


def test_healthy_watch_never_posts_to_fallback(monkeypatch, discord):
    watch = _watch("https://fine.example/collections/x", "Fine")
    state = {}
    for _ in range(3):
        _process(monkeypatch, watch, state, [_product("https://fine.example/products/1", False)], discord)
    assert discord["failure"].call_count == 0
    assert discord["recovered"].call_count == 0


def test_new_failure_after_recovery_alerts_again(monkeypatch, discord):
    watch = _watch("https://flaky.example/collections/x", "Flaky")
    state = {}
    _process(monkeypatch, watch, state, RuntimeError("down"), discord)
    _process(monkeypatch, watch, state, [_product("https://flaky.example/products/1", False)], discord)
    _process(monkeypatch, watch, state, RuntimeError("down again"), discord)
    assert discord["failure"].call_count == 2
    assert discord["recovered"].call_count == 1


def test_failure_state_survives_save_and_load(files):
    state = {}
    st.record_failure(state, "https://x", "boom", ts=100.0)
    st.save_state(state)
    loaded = st.load_state()
    assert loaded["failures"]["https://x"]["since"] == 100.0
    assert st.record_failure(loaded, "https://x", "boom", ts=200.0) is False


# ── Fix 5: alerts.jsonl ───────────────────────────────────────────────────────

def test_sent_alerts_are_appended_to_alerts_jsonl(monkeypatch, discord, files):
    url = "https://shop.com/collections/a"
    product = "https://shop.com/products/op09"
    watches = [_watch(url, "Gameology")]
    _run_with(monkeypatch, watches, {url: [_product(product, False)]})
    st.save_state({**st.load_state(), "last_checked": {}})
    _run_with(monkeypatch, watches, {url: [_product(product, True, title="OP09 Booster Box", price="$69.95")]})

    rows = [json.loads(l) for l in (files / "alerts.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"time", "retailer", "kind", "title", "price", "url"}
    assert row["retailer"] == "Gameology"
    assert row["kind"] == "BACK_IN_STOCK"
    assert row["title"] == "OP09 Booster Box"
    assert row["price"] == "$69.95"
    assert row["url"] == product
    assert row["time"].endswith("Z")


def test_dry_run_does_not_write_alerts_jsonl_or_state(monkeypatch, discord, files):
    url = "https://shop.com/collections/a"
    product = "https://shop.com/products/op09"
    watches = [_watch(url, "Gameology")]
    _run_with(monkeypatch, watches, {url: [_product(product, False)]})
    st.save_state({**st.load_state(), "last_checked": {}})
    before = (files / "state.json").read_text()
    _run_with(monkeypatch, watches, {url: [_product(product, True)]}, dry_run=True)
    assert not (files / "alerts.jsonl").exists()
    assert (files / "state.json").read_text() == before
    assert discord["alerts"].call_count == 0


def test_deduped_alert_is_logged_once(monkeypatch, discord, files):
    plain = "https://shop.com/collections/a"
    frag = plain + "#/filter:x"
    product = "https://shop.com/products/op09"
    watches = [_watch(plain, "Gameology"), _watch(frag, "Gameology (Preorder)")]
    _run_with(monkeypatch, watches, {plain: [_product(product, False)], frag: [_product(product, False)]})
    st.save_state({**st.load_state(), "last_checked": {}})
    _run_with(monkeypatch, watches, {plain: [_product(product, True)], frag: [_product(product, True)]})
    rows = (files / "alerts.jsonl").read_text().splitlines()
    assert len(rows) == 1


def test_workflow_commits_alerts_jsonl_and_schedule_is_unchanged():
    with open(os.path.join(REPO, ".github", "workflows", "watcher.yml"), encoding="utf-8") as f:
        raw = f.read()
    wf = yaml.safe_load(raw)
    assert wf[True]["schedule"] == [{"cron": "*/5 * * * *"}]  # yaml parses the `on:` key as True
    commit_step = [s for s in wf["jobs"]["watch"]["steps"] if s.get("name") == "Commit updated state"][0]
    assert "git add state.json alerts.jsonl" in commit_step["run"]
    assert "secrets.WEBHOOKS_JSON" in raw


def test_alerts_jsonl_is_tracked_in_repo():
    assert os.path.exists(os.path.join(REPO, "alerts.jsonl"))
