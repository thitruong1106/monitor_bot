#!/usr/bin/env python3
"""CLI tool to manage the TCG stock watcher watchlist."""
import argparse
import json
import os
import sys

import requests
import yaml

from config import (
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

def cmd_add(args):
    watches = load_watches()
    if any(w.url == args.url for w in watches):
        print(f"Watch already exists: {args.url}")
        return

    watch_type = args.type or infer_watch_type(args.url)
    watch = Watch(
        url=args.url,
        game=args.game,
        tier=args.tier,
        type=watch_type,
        retailer_name=args.retailer,
    )
    watches.append(watch)
    save_watches(watches)
    print(f"Added [{watch.game}] {watch.retailer_name} — {watch.url} (tier={watch.tier}, type={watch.type})")


# ── list ──────────────────────────────────────────────────────────────────────

def cmd_list(args):
    watches = load_watches()
    if args.game:
        watches = [w for w in watches if w.game == args.game]
    if not watches:
        print("No watches found.")
        return
    for w in watches:
        print(f"  [{w.game}] {w.retailer_name:30s}  {w.tier:6s}  {w.type:8s}  {w.url}")


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
    print(f"Testing adapter for: {url}")
    fetch_fn = get_adapter(url, session)
    print(f"Using adapter: {fetch_fn.__module__}.{fetch_fn.__name__}")
    try:
        products = fetch_fn(url, session)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"\nFound {len(products)} product(s):\n")
    for p in products:
        avail = "IN STOCK" if p["available"] else "OOS"
        preorder = " [PREORDER]" if p["is_preorder"] else ""
        print(f"  [{avail}{preorder}] {p['title']}")
        print(f"    URL:   {p['product_url']}")
        print(f"    Price: {p.get('price', 'N/A')}")


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
