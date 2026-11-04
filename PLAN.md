# TCG Stock Watcher: findings and plan (4 Nov 2026)

Goal: alert on One Piece card game pre-orders and restocks before the paid Discord channels do.

## 1. What the data says today

### The schedule is not running every 5 minutes
Measured from the GitHub Actions run history (1,817 runs since 26 Jun 2026):

| Period | Runs per day | Gap between runs (median) | Longest gap |
|---|---|---|---|
| 26 Jun (day 1) | ~14 | 65 min | 4 h |
| 22–24 Aug | ~20 | 23 min | 101 min |
| 17 Oct – 4 Nov | ~5 | 4.3 h | 10 h |

The cron says `*/5 * * * *` but GitHub has never honoured it for this repo. Scheduled workflows are "best effort" and GitHub de-prioritises them heavily on private repos on the free plan. Each run itself is fast (median 40 s for the watcher, ~52 s for the whole job). So right now a restock can sit unnoticed for 4 to 10 hours. Changing the cron to 3 minutes would do nothing: 5 minutes is GitHub's minimum, and the real bottleneck is the throttling.

### What has been alerting (alerts.jsonl, 30 rows since 2 Nov)
- 21 Kaika alerts on 3 Nov, all figures and merchandise (S.H.Figuarts, Wallstar, Yuracolle), not cards. The Kaika `/series/one-piece/` page is their One Piece merchandise category. This watch is off-target for a card-game bot.
- Gameology OP-17 booster box: 4 BACK_IN_STOCK alerts in 2 days. Either real micro-restocks or availability flapping.
- Games Portal EB-05, Good Games OP-17: one each.
- 5 rows are junk from my own test run (`shop.com`, retailer "Shop"). They were written before the alerts-log path fix and I committed them by mistake. They need deleting.

### Watch-by-watch health (from the 4 Nov run log)
| Watch | Products | Verdict |
|---|---|---|
| TCG Group AU (filtered) | 38 | OK |
| Treasure Collectables `other-tcgs` | 1 | Nearly useless, replace with a One Piece collection if they have one |
| Games Portal | 37 | OK |
| Good Games (Coming Soon) | 6 | OK, good signal |
| Good Games (In Stock, filtered) | 1,077 | Works for new listings only. Restocks can never fire (see 2.2). Mostly singles. |
| Kaika | 24 | Parses now, but it is figures. Drop or point at a card category. |
| Collectible Madness `one-piece-card-game` | 14 | OK, but they have a dedicated `one-piece-card-game-preorder` collection we are not watching |
| Gamers Room | 38 | OK |
| Gameology | 50 | OK |
| Gameology (Preorder, `#/filter`) | 50 | Exact duplicate of the above. Remove. |
| M-G `/pre-orders/` | 24 | All games mixed; no One Piece filter, so non-OP pre-orders would alert |
| NJ Collectables | 8 | OK |
| EE TCG | 5 | OK |
| Rhystic Nostalgia `sealed-instock` | 76 | In-stock-only collection, so restocks can never fire (see 2.2) |

Dead links: none right now (`failures` in state.json is empty). The only dead one was the Games Portal `/Preorder` URL, removed in PR #1.

## 2. How memory works and where it leaks

**It is stateful.** `state.json` is committed after every run and holds: `last_checked` per watch, `category_urls` (every product URL ever seen per watch), `products` (per watch + product: available, is_preorder, state), and `failures`.

**After an alert the status does change.** The product's saved state flips to `in_stock` or `preorder_open`, so it will not alert again until the bot sees it go `unavailable` and then available again. That part is correct. The leaks:

1. **Filtered and in-stock-only watches can never detect a restock.** When a product sells out it simply disappears from the list (the in-stock filter removes it; an "instock" collection drops it). Nothing marks it unavailable, so when it comes back it compares as in_stock == in_stock and stays silent. Affects Good Games (In Stock) and Rhystic Nostalgia today. Fix: treat "was in the list, now missing" as unavailable.
2. **A new watch floods the channel.** `is_first_run` is global, so the only silent baseline is the very first run ever. Adding any watch alerts on everything currently in stock (the 21 Kaika alerts on 3 Nov were this). Fix: per-watch baseline, silent on the first pass for that watch.
3. **State never shrinks.** `category_urls` for Good Games is 1,094 entries and only grows. Harmless for now, but it is why state.json is 30 KB and climbing.
4. **No per-product cool-down.** A product that flaps in and out of stock every pass alerts every pass (Gameology OP-17 four times). A 30-minute cool-down per product URL would quiet that without missing a real restock.
5. **Pre-order classification is by words only** (`pre-order`, `preorder`, `coming soon` in title, handle or tags). A pre-order that opens on an existing page like Good Games' OP-18 (page exists, "preorders not open yet") will come through as BACK_IN_STOCK rather than PREORDER_OPEN unless the title says preorder. Matters for Jarvis' classification, not for the ping itself.
6. **Every run re-probes every Shopify domain** (`/products.json?limit=1` per domain, plus the 1.5 s per-domain delay) because the "is this Shopify" cache is in-memory. About 15 s of every run is wasted. Fix: store `platform: shopify` in watches.yaml.
7. **Alerts are sent before state is saved.** If the final push fails, the next run re-sends. Rare, but a per-product "last alerted at" in state would make it safe.

## 3. Plan

### Phase 0: quick wins on the current setup (one PR, this week)
- Delete the 5 test rows from alerts.jsonl.
- Per-watch first-run baseline (no flood when adding a watch).
- Missing-from-list means unavailable, with a one-pass grace to survive pagination hiccups.
- Per-watch `include` / `exclude` title filters (regex). Use them on M-G, Good Games (In Stock) and any mixed-TCG collection.
- Remove Gameology (Preorder). Retarget or drop Kaika and Treasure Collectables.
- Make the Discord embed worth more than the paid channels': role mention (`<@&ROLE_ID>` with `allowed_mentions`), product image, and for Shopify a direct add-to-cart link `https://store/cart/VARIANT_ID:1` (variant IDs are already in products.json).
- Missed-run detector: if the time since the previous run is more than 2x what we expect, post once to the fallback channel. Nobody noticed the 4-hour gaps for months.
- 429 handling: honour `Retry-After`, and after a 429 put that domain on a 5-minute cool-down instead of retrying 1, 2, 4 s later.

### Phase 1: fix the cadence (the decision that matters)
Two realistic options:

**A. Stay on GitHub Actions, run a loop instead of a cron.** One job polls every 30 to 60 s for up to 5 h 55 min (6 h job limit), then triggers the next run itself with `workflow_dispatch` (works with the built-in token). State lives in memory and is committed every 15 min or when an alert fires. Catch: a private repo gets 2,000 free minutes a month and this uses ~43,000. Either make the repo public (unlimited minutes; state and alerts become visible, the webhook stays a secret) or pay about A$500 a month. Also GitHub runner IPs are shared, so Cloudflare-fronted shops may start challenging us.

**B. Always-on box (recommended).** A Raspberry Pi or old laptop at home (residential IP, least likely to be blocked), Oracle Cloud's Always Free ARM VM in Sydney, or a A$8-a-month VPS in Sydney. Run `watcher.py --loop` as a systemd service, 20 to 30 s per store with jitter, state in a local SQLite or JSON file, alerts.jsonl pushed to GitHub hourly for Jarvis. GitHub keeps the code and tests only.

Either way, polling every 30 s across 14 watches is ~16 requests per cycle, roughly 2 requests a minute per store, which is far below Shopify's storefront limits; the 429 risk is bursts, not steady rate. Two things to test on the box before going faster than 30 s: whether each store's `products.json` is edge-cached (then polling faster than the cache TTL gains nothing) and whether `/products/<handle>.js` is fresher for the hot SKUs.

### Phase 2: widen coverage (after cadence is fixed)
Candidates found by search, none verified from this environment. Run `python manage.py test <url>` on each before adding:
- [Collectible Madness pre-order collection](https://collectiblemadness.com.au/collections/one-piece-card-game-preorder) and [booster boxes](https://collectiblemadness.com.au/collections/one-piece-card-game-booster-boxes)
- [Good Games special pre-orders](https://tcg.goodgames.com.au/collections/one-piece-card-game-special-preorders). Note the `tcg.` subdomain; check whether it is a separate Shopify store from `www.`
- [Rogue Online](https://www.rogueonline.com.au/collections/one-piece-tcg)
- [Eclipse Games pre-orders](https://eclipsegames.com.au/collections/preorders) (all TCGs, needs a title filter)
- [Omega Games](https://omegagames.com.au/products/one-piece-card-game-premium-booster-box-prb-02-preorder) (Shopify; find their One Piece collection)
- [Game Traders Ingle Farm](https://www.gametradersinglefarm.com.au/products/pre-order-one-piece-card-game-booster-display-tba-op-18-20th-november) (Shopify)
- [PokeBox](https://www.pokebox.com.au/collections/one-piece-card-games), [Troll Australia](https://www.trollaustralia.com.au/collections/one-piece-cards), [Frenly Bricks](https://frenlybricks.store/collections/one-piece-tcg), [Grailborne](https://grailborne.com.au/blogs/news/one-piece-card-game-australia-beginners-guide-best-sets-to-buy), [Hazz Card Shop](https://hazzcardshop.com.au/blogs/news/one-piece-tcg-beginners-australia)
- [Good Deals Games](https://www.gdgames.com.au/collections/trading-card-games/one-piece-tcg): same `/collections/x/y` shape that killed the Games Portal watch; find the real collection handle first
- [Let's Play Games boosters](https://letsplaygames.com.au/card-dice-games/trading-card-games/one-piece-card-game/one-piece-boosters): not Shopify, needs an adapter
- [Den Den Hobby](https://dendenhobby.com.au/products/pre-order-one-piece-card-game-booster-op-18-japanese-version-box-of-24-packs): Japanese imports, Shopify

Sharper signals than collection pages:
- **Product-page watches on upcoming SKUs** at `tier: hot`: the [Good Games OP-18 page](https://www.goodgames.com.au/products/one-piece-card-game-booster-box-tba-op-18) and [Gameology OP-18 page](https://www.gameology.com.au/products/one-piece-card-game-op-18-booster-box) exist now with pre-orders closed. The moment they flip is exactly the event the paid channels sell.
- **Store-wide new-product detection** for each Shopify store: poll `/products.json?limit=50` and the products sitemap, filter titles for `one piece|OP-\d\d|EB-\d\d|PRB|ST-\d\d`. New pre-order products are usually published minutes before they are added to a collection.

### Phase 3: the hard retailers
EB Games/Zing, JB Hi-Fi, Big W, Kmart, Target, Amazon AU and Mighty Ape open One Piece pre-orders at RRP and are where the paid channels earn their keep. None are Shopify; each needs its own adapter and most have bot protection. Only worth doing from a residential IP (option B).

### Housekeeping
- `.claude/settings.local.json` is committed; add it to `.gitignore`.
- `pytest` is installed in production; split requirements.
- `actions/checkout@v4` and `setup-python@v5` are on a deprecated Node 20 runtime; bump when convenient.
- If staying on Actions, one commit per run at 1-minute cadence is 1,440 commits a day. Keep state out of git (Actions cache or a gist) in that case.
