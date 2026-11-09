"""
Phase 1 state logic, tested with fixture data only (no network):
  * per-watch silent baseline (and re-baseline when filters change)
  * missing-means-unavailable for in-stock-only lists
  * per-product cool-down
  * pruning
  * v1 -> v2 migration
  * missed-run detection
"""
import copy

import state as st

DAY = 86400.0
HOUR = 3600.0
T0 = 1_800_000_000.0          # fixed clock for every test

WATCH_A = "https://shop.example/collections/one-piece"
WATCH_B = "https://shop.example/collections/one-piece?pf_st_availability=in-stock"


def p(slug, available=True, preorder=False, title=None, price="$50.00", **extra):
    return {
        "product_url": f"https://shop.example/products/{slug}",
        "title": title or slug.replace("-", " ").title(),
        "available": available,
        "is_preorder": preorder,
        "price": price,
        **extra,
    }


def types(alerts):
    return sorted((a["alert_type"], a["product_url"].rsplit("/", 1)[1]) for a in alerts)


# ── 1. Per-watch silent baseline ─────────────────────────────────────────────

def test_new_watch_first_pass_is_silent_even_when_state_already_has_other_watches():
    s = {}
    st.detect_transitions(s, WATCH_A, [p("old", True)], T0)            # watch A baselined
    # Watch B is added later. The old code only had a GLOBAL first-run flag,
    # so this would have alerted NEW_LISTING for every in-stock product.
    alerts = st.detect_transitions(s, WATCH_B, [p("x", True), p("y", True), p("z", False)], T0 + 60)
    assert alerts == []
    assert st.get_known_urls(s, WATCH_B) == {
        "https://shop.example/products/x", "https://shop.example/products/y", "https://shop.example/products/z",
    }


def test_after_baseline_new_and_restocked_products_alert():
    s = {}
    st.detect_transitions(s, WATCH_A, [p("a", False)], T0)
    alerts = st.detect_transitions(s, WATCH_A, [p("a", True), p("b", True), p("c", True, preorder=True)], T0 + 60)
    assert types(alerts) == [("BACK_IN_STOCK", "a"), ("NEW_LISTING", "b"), ("PREORDER_OPEN", "c")]


def test_baseline_pass_records_state_so_the_next_flip_alerts():
    s = {}
    st.detect_transitions(s, WATCH_A, [p("op18", False)], T0)            # silent
    assert st.get_product_state(s, WATCH_A, "https://shop.example/products/op18")["state"] == "unavailable"
    alerts = st.detect_transitions(s, WATCH_A, [p("op18", True, preorder=True)], T0 + 300)
    assert types(alerts) == [("PREORDER_OPEN", "op18")]


def test_changing_a_watchs_filters_re_baselines_silently():
    s = {}
    st.detect_transitions(s, WATCH_A, [p("a")], T0, filters_fingerprint="")
    # Filters loosened: products the watch never stored come into view.
    alerts = st.detect_transitions(s, WATCH_A, [p("a"), p("b"), p("c")], T0 + 60, filters_fingerprint="abc123")
    assert alerts == []
    # Same filters next pass: normal alerting resumes.
    alerts = st.detect_transitions(s, WATCH_A, [p("a"), p("b"), p("c"), p("d")], T0 + 120, filters_fingerprint="abc123")
    assert types(alerts) == [("NEW_LISTING", "d")]


def test_failed_first_fetch_leaves_watch_unbaselined():
    # process_watch never calls detect_transitions on a failed fetch, so the
    # first SUCCESSFUL pass is still the silent one.
    s = {}
    assert st.needs_baseline(s, WATCH_A) is True


# ── 2. Missing means unavailable ─────────────────────────────────────────────

def _mmu(s, products, now, grace=1, listed=None):
    return st.detect_transitions(
        s, WATCH_B, products, now,
        listed_urls=listed, missing_means_unavailable=True, missing_grace_passes=grace,
    )


def test_in_stock_list_restock_fires_after_product_disappears_and_returns():
    s = {}
    _mmu(s, [p("box"), p("deck")], T0)                    # baseline: both in stock
    _mmu(s, [p("deck")], T0 + 300)                        # box sold out -> vanished from list
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/box")["state"] == "unavailable"
    alerts = _mmu(s, [p("box"), p("deck")], T0 + 600)     # box restocked
    assert types(alerts) == [("BACK_IN_STOCK", "box")]


def test_without_the_flag_a_returning_product_stays_silent():
    """The old behaviour, kept for normal lists: vanish + return is not a restock."""
    s = {}
    st.detect_transitions(s, WATCH_B, [p("box"), p("deck")], T0)
    st.detect_transitions(s, WATCH_B, [p("deck")], T0 + 300)
    assert st.detect_transitions(s, WATCH_B, [p("box"), p("deck")], T0 + 600) == []


def test_grace_passes_delay_marking():
    s = {}
    _mmu(s, [p("box"), p("deck")], T0, grace=2)
    _mmu(s, [p("deck")], T0 + 300, grace=2)               # 1st miss: still in_stock
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/box")["state"] == "in_stock"
    _mmu(s, [p("deck")], T0 + 600, grace=2)               # 2nd miss: now unavailable
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/box")["state"] == "unavailable"


def test_reappearing_resets_the_miss_counter():
    s = {}
    _mmu(s, [p("box"), p("deck")], T0, grace=2)
    _mmu(s, [p("deck")], T0 + 300, grace=2)
    _mmu(s, [p("box"), p("deck")], T0 + 600, grace=2)     # back before grace ran out
    _mmu(s, [p("deck")], T0 + 900, grace=2)               # one miss again -> not yet
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/box")["state"] == "in_stock"


def test_bulk_disappearance_waits_three_passes_to_survive_a_partial_fetch():
    s = {}
    full = [p(f"item-{i}") for i in range(20)]
    _mmu(s, full, T0)
    partial = full[:5]                                    # 15 of 20 vanished at once: suspicious
    _mmu(s, partial, T0 + 300)
    _mmu(s, partial, T0 + 600)
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/item-19")["state"] == "in_stock"
    # Full list comes back on pass 3: nothing was marked, so nothing alerts.
    assert _mmu(s, full, T0 + 900) == []


def test_bulk_disappearance_is_believed_if_it_persists():
    s = {}
    full = [p(f"item-{i}") for i in range(20)]
    _mmu(s, full, T0)
    for k in range(1, 4):
        _mmu(s, full[:5], T0 + 300 * k)
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/item-19")["state"] == "unavailable"
    alerts = _mmu(s, full, T0 + 1500)
    assert len(alerts) == 15 and {a["alert_type"] for a in alerts} == {"BACK_IN_STOCK"}


def test_products_hidden_only_by_title_filter_are_not_missing():
    """listed_urls (pre-filter) decides 'missing', not the filtered list."""
    s = {}
    _mmu(s, [p("box")], T0, listed={"https://shop.example/products/box", "https://shop.example/products/single"})
    alerts = _mmu(s, [p("box")], T0 + 300,
                  listed={"https://shop.example/products/box", "https://shop.example/products/single"})
    assert alerts == []
    assert st.get_product_state(s, WATCH_B, "https://shop.example/products/box")["state"] == "in_stock"


def test_missing_marking_is_silent():
    s = {}
    _mmu(s, [p("box"), p("deck")], T0)
    assert _mmu(s, [p("deck")], T0 + 300) == []


# ── 3. Per-product cool-down ─────────────────────────────────────────────────

def test_in_cooldown_window():
    s = {}
    st.record_alert(s, "https://shop.example/products/box", T0)
    assert st.in_cooldown(s, "https://shop.example/products/box", 6, T0 + 5 * HOUR)
    assert not st.in_cooldown(s, "https://shop.example/products/box", 6, T0 + 6 * HOUR + 1)
    assert not st.in_cooldown(s, "https://shop.example/products/other", 6, T0 + HOUR)
    assert not st.in_cooldown(s, "https://shop.example/products/box", 0, T0 + 1)   # 0 disables


def test_flapping_product_alerts_once_per_cooldown(monkeypatch):
    """Sold out / restocked every 30 min for 7 h -> 2 alerts with a 6 h cool-down, not 14."""
    import watcher
    from config import Watch

    w = Watch(url=WATCH_A, game="one-piece", tier="hot", type="category", retailer_name="Shop", cooldown_hours=6)
    s = {}
    st.detect_transitions(s, WATCH_A, [p("op17", False)], T0)
    sent = []
    now = T0
    for step in range(28):                                  # 28 x 15 min = 7 h
        now += 15 * 60
        alerts = st.detect_transitions(s, WATCH_A, [p("op17", available=(step % 2 == 0))], now)
        kept, _ = watcher.apply_cooldown([(a, w) for a in alerts], s, now)
        for a, _w in kept:
            st.record_alert(s, a["product_url"], now)
            sent.append(now)
    assert len(sent) == 2
    assert sent[1] - sent[0] >= 6 * HOUR


def test_cooldown_is_shared_across_watches_and_configurable_per_watch():
    import watcher
    from config import Watch

    slow = Watch(url=WATCH_A, game="g", tier="normal", type="category", retailer_name="A", cooldown_hours=6)
    fast = Watch(url=WATCH_B, game="g", tier="hot", type="category", retailer_name="B", cooldown_hours=1)
    s = {}
    url = "https://shop.example/products/box"
    st.record_alert(s, url, T0)                      # alerted via any watch
    alert = {"product_url": url, "title": "Box", "alert_type": "BACK_IN_STOCK"}
    kept, suppressed = watcher.apply_cooldown([(alert, slow)], s, T0 + 2 * HOUR)
    assert (kept, suppressed) == ([], 1)
    kept, suppressed = watcher.apply_cooldown([(alert, fast)], s, T0 + 2 * HOUR)
    assert len(kept) == 1 and suppressed == 0


# ── 4. Pruning ───────────────────────────────────────────────────────────────

def _state_with(products_last_seen: dict, last_checked: float):
    s = {"last_checked": {WATCH_A: last_checked}, "products": {}}
    for slug, seen in products_last_seen.items():
        s["products"][f"{WATCH_A}||https://shop.example/products/{slug}"] = {
            "available": False, "is_preorder": False, "state": "unavailable", "last_seen": seen,
        }
    return s


def test_prune_drops_products_unseen_for_30_days_of_successful_checks():
    now = T0 + 40 * DAY
    s = _state_with({"fresh": now - 2 * DAY, "stale": now - 31 * DAY}, last_checked=now)
    counts = st.prune_state(s, now, {WATCH_A}, max_age_days=30)
    assert counts["products"] == 1
    assert st.get_known_urls(s, WATCH_A) == {"https://shop.example/products/fresh"}


def test_prune_keeps_products_of_a_watch_that_has_been_failing():
    """last_checked only moves on success; a dead watch must not forget (and later flood)."""
    now = T0 + 60 * DAY
    s = _state_with({"old": T0}, last_checked=T0 + 1 * DAY)   # last success 59 days ago
    st.prune_state(s, now, {WATCH_A}, max_age_days=30)
    assert st.get_known_urls(s, WATCH_A) == {"https://shop.example/products/old"}


def test_prune_drops_state_of_watches_removed_from_watches_yaml():
    s = _state_with({"a": T0}, last_checked=T0)
    s["products"][f"{WATCH_B}||https://shop.example/products/b"] = {"state": "in_stock", "last_seen": T0}
    s["last_checked"][WATCH_B] = T0
    s["baselined"] = {WATCH_A: {"at": T0, "filters": ""}, WATCH_B: {"at": T0, "filters": ""}}
    s["failures"] = {WATCH_B: {"since": T0, "count": 3}}
    st.prune_state(s, T0, {WATCH_A})
    assert st.get_known_urls(s, WATCH_B) == set()
    assert WATCH_B not in s["last_checked"] and WATCH_B not in s["baselined"] and WATCH_B not in s["failures"]
    assert st.get_known_urls(s, WATCH_A) == {"https://shop.example/products/a"}


def test_prune_with_no_watches_configured_does_not_wipe_state():
    s = _state_with({"a": T0}, last_checked=T0)
    before = copy.deepcopy(s)
    st.prune_state(s, T0, set())
    assert s == before


def test_prune_drops_old_cooldown_records():
    s = {"alerted": {"old": T0 - 31 * DAY, "new": T0 - DAY}}
    st.prune_state(s, T0, {WATCH_A})
    assert s["alerted"] == {"new": T0 - DAY}


def test_seen_products_refresh_last_seen():
    s = {}
    st.detect_transitions(s, WATCH_A, [p("a")], T0)
    st.detect_transitions(s, WATCH_A, [p("a")], T0 + 10 * DAY)
    assert st.get_product_state(s, WATCH_A, "https://shop.example/products/a")["last_seen"] == T0 + 10 * DAY


# ── 5. Migration from the v1 state.json on main ──────────────────────────────

def _v1_state():
    return {
        "category_urls": {WATCH_A: ["https://shop.example/products/a", "https://shop.example/products/b"]},
        "last_checked": {WATCH_A: T0},
        "products": {
            f"{WATCH_A}||https://shop.example/products/a": {"available": True, "is_preorder": False, "state": "in_stock"},
            f"{WATCH_A}||https://shop.example/products/b": {"available": False, "is_preorder": False, "state": "unavailable"},
        },
    }


def test_migration_marks_existing_watches_baselined_and_drops_category_urls():
    s = st.migrate_state(_v1_state(), T0 + 60)
    assert "category_urls" not in s
    assert s["schema"] == st.SCHEMA_VERSION
    assert st.needs_baseline(s, WATCH_A, "any-filters") is False
    assert all(e["last_seen"] == T0 + 60 for e in s["products"].values())


def test_migrated_watch_keeps_alerting_normally_with_no_flood():
    s = st.migrate_state(_v1_state(), T0 + 60)
    unchanged = [p("a", True), p("b", False)]
    assert st.detect_transitions(s, WATCH_A, unchanged, T0 + 120) == []
    alerts = st.detect_transitions(s, WATCH_A, [p("a", True), p("b", True), p("c", True)], T0 + 180)
    assert types(alerts) == [("BACK_IN_STOCK", "b"), ("NEW_LISTING", "c")]


def test_migration_is_idempotent():
    s = st.migrate_state(_v1_state(), T0)
    snapshot = copy.deepcopy(s)
    st.migrate_state(s, T0 + DAY)
    assert s == snapshot


# ── 6. Missed-run detector ───────────────────────────────────────────────────

def test_no_warning_on_first_ever_pass_or_short_gap():
    s = {}
    assert st.check_missed_run(s, T0, 30, 6) is None
    st.record_successful_pass(s, T0)
    assert st.check_missed_run(s, T0 + 29 * 60, 30, 6) is None


def test_warning_when_gap_exceeds_threshold():
    s = {}
    st.record_successful_pass(s, T0)
    w = st.check_missed_run(s, T0 + 4 * HOUR, 30, 6)
    assert w["gap_start"] == T0 and w["gap_end"] == T0 + 4 * HOUR
    assert w["gap_seconds"] == 4 * HOUR and w["earlier_gaps"] == 0


def test_warnings_are_rate_limited_and_summarise_held_back_gaps():
    s = {}
    now = T0
    st.record_successful_pass(s, now)
    posted = []
    for gap_h in (4, 2, 5, 3):                          # four late runs, 14 h total
        now += gap_h * HOUR
        w = st.check_missed_run(s, now, 30, 6)
        if w:
            posted.append(w)
        st.record_successful_pass(s, now)
    assert len(posted) == 2                              # at T0+4h and T0+11h (>= 6 h apart)
    assert posted[1]["earlier_gaps"] == 1                # the 2 h gap was held back
    assert posted[1]["longest_seconds"] == 5 * HOUR
