#!/usr/bin/env python3
"""CLI tool to manage the TCG stock watcher watchlist."""
import argparse
import json
import os
import sys

import requests
import yaml

from config import (
    DEFAULT_COOLDOWN_HOURS,
    WEBHOOKS_FILE,
    WATCHES_FILE,
    Watch,
    infer_watch_type,
    load_watches,
    load_webhooks,
    save_watches,
)
from adapters import get_adapter
from adapters.shopify import is_shopify

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


# ── add ───────────────────────────────────────────────────────────────────────

def _detect_platform(url: str) -> str | None:
    try:
        return "shopify" if is_shopify(url, _session()) else "other"
    except Exception as exc:
        print(f"Could not detect platform ({exc}); it will be probed at run time.")
        return None


def cmd_add(args):
    watches = load_watches()
    if any(w.url == args.url for w in watches):
        print(f"Watch already exists: {args.url}")
        return

    watch_type = args.type or infer_watch_type(args.url)
    platform = args.platform or _detect_platform(args.url)
    watch = Watch(
        url=args.url,
        game=args.game,
        tier=args.tier,
        type=watch_type,
        retailer_name=args.retailer,
        platform=platform,
        include=args.include or [],
        exclude=args.exclude or [],
        cooldown_hours=args.cooldown_hours if args.cooldown_hours is not None else DEFAULT_COOLDOWN_HOURS,
        missing_means_unavailable=args.missing_means_unavailable,
    )
    watches.append(watch)
    save_watches(watches)
    print(
        f"Added [{watch.game}] {watch.retailer_name} — {watch.url} "
        f"(tier={watch.tier}, type={watch.type}, platform={watch.platform or 'probe'})"
    )
    print("Its first successful check is a silent baseline: no alerts for what is already listed.")


# ── list ──────────────────────────────────────────────────────────────────────

def cmd_list(args):
    watches = load_watches()
    if args.game:
        watches = [w for w in watches if w.game == args.game]
    if not watches:
        print("No watches found.")
        return
    for w in watches:
        extras = []
        if w.include:
            extras.append(f"include={len(w.include)}")
        if w.exclude:
            extras.append(f"exclude={len(w.exclude)}")
        if w.missing_means_unavailable:
            extras.append("missing=unavailable")
        extra = f"  [{', '.join(extras)}]" if extras else ""
        print(f"  [{w.game}] {w.retailer_name:30s}  {w.tier:6s}  {w.type:8s}  {(w.platform or 'probe'):7s}  {w.url}{extra}")


# ── remove ────────────────────────────────────────────────────────────────────

def cmd_remove(args):
    watches = load_watches()
    before = len(watches)
    watches = [w for w in watches if w.url != args.url]
    if len(watches) == before:
        print(f"No watch found for: {args.url}")
        return
    save_watches(watches)
    print(f"Removed: {args.url}")


# ── set-tier ──────────────────────────────────────────────────────────────────

def cmd_set_tier(args):
    watches = load_watches()
    found = False
    for w in watches:
        if w.url == args.url:
            w.tier = args.tier
            found = True
            break
    if not found:
        print(f"No watch found for: {args.url}")
        return
    save_watches(watches)
    print(f"Set tier to '{args.tier}' for: {args.url}")


# ── test ──────────────────────────────────────────────────────────────────────

def cmd_test(args):
    url = args.url
    session = _session()
    watch = next((w for w in load_watches() if w.url == url), None)
    platform = args.platform or (watch.platform if watch else None)
    print(f"Testing adapter for: {url}")
    if watch:
        print(f"Using the settings of watch '{watch.retailer_name}' from {WATCHES_FILE}")
    fetch_fn = get_adapter(url, session, platform=platform)
    print(f"Using adapter: {fetch_fn.__module__}.{fetch_fn.__name__} (platform={platform or 'probed'})")
    try:
        products = fetch_fn(url, session)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    kept = watch.apply_filters(products) if watch else products
    kept_urls = {p["product_url"] for p in kept}
    if watch and len(kept) != len(products):
        print(f"\nFound {len(products)} product(s), {len(kept)} pass this watch's title filters:\n")
    else:
        print(f"\nFound {len(products)} product(s):\n")
    for p in products:
        avail = "IN STOCK" if p["available"] else "OOS"
        preorder = " [PREORDER]" if p["is_preorder"] else ""
        filtered = "" if p["product_url"] in kept_urls else " [FILTERED OUT]"
        print(f"  [{avail}{preorder}]{filtered} {p['title']}")
        print(f"    URL:   {p['product_url']}")
        print(f"    Price: {p.get('price', 'N/A')}")
        if p.get("image_url"):
            print(f"    Image: {p['image_url']}")
        if p.get("cart_url"):
            print(f"    Cart:  {p['cart_url']}")


# ── set-webhook ───────────────────────────────────────────────────────────────

def cmd_set_webhook(args):
    webhooks: dict = {}
    if os.path.exists(WEBHOOKS_FILE):
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            try:
                webhooks = json.load(f)
            except json.JSONDecodeError:
                pass
    webhooks[args.game] = args.webhook_url
    with open(WEBHOOKS_FILE, "w", encoding="utf-8") as f:
        json.dump(webhooks, f, indent=2)
        f.write("\n")
    print(f"Webhook set for game '{args.game}' in {WEBHOOKS_FILE} (gitignored)")


# ── CLI definition ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Manage the TCG stock watcher watchlist.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Add a watch URL")
    p_add.add_argument("url")
    p_add.add_argument("--game", required=True)
    p_add.add_argument("--tier", choices=["hot", "normal", "slow"], default="normal")
    p_add.add_argument("--retailer", required=True)
    p_add.add_argument("--type", choices=["category", "product"], default=None, dest="type")
    p_add.add_argument("--platform", choices=["shopify", "other"], default=None,
                       help="Skip auto-detection (saved so runs never probe this site)")
    p_add.add_argument("--include", action="append", help="Title regex to keep (repeatable)")
    p_add.add_argument("--exclude", action="append", help="Title regex to drop (repeatable)")
    p_add.add_argument("--cooldown-hours", type=float, default=None,
                       help=f"Hours before the same product can alert again (default {DEFAULT_COOLDOWN_HOURS:g})")
    p_add.add_argument("--missing-means-unavailable", action="store_true",
                       help="For in-stock-only lists: a product that disappears counts as sold out")

    # list
    p_list = sub.add_parser("list", help="List watches")
    p_list.add_argument("--game", default=None)

    # remove
    p_remove = sub.add_parser("remove", help="Remove a watch URL")
    p_remove.add_argument("url")

    # set-tier
    p_tier = sub.add_parser("set-tier", help="Change check tier for a URL")
    p_tier.add_argument("url")
    p_tier.add_argument("tier", choices=["hot", "normal", "slow"])

    # test
    p_test = sub.add_parser("test", help="Fetch a URL and print adapter results")
    p_test.add_argument("url")
    p_test.add_argument("--platform", choices=["shopify", "other"], default=None)

    # set-webhook
    p_hook = sub.add_parser("set-webhook", help="Store a Discord webhook URL")
    p_hook.add_argument("game")
    p_hook.add_argument("webhook_url")

    args = parser.parse_args()
    {
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
        "set-tier": cmd_set_tier,
        "test": cmd_test,
        "set-webhook": cmd_set_webhook,
    }[args.command](args)


if __name__ == "__main__":
    main()
