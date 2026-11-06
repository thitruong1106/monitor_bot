# TCG Stock Watcher

Monitors Australian TCG retailer pages for new products and restocks, posts alerts to Discord. Runs on GitHub Actions every 5 minutes.

## How it works

- **Every 5 min** (in theory: GitHub often runs it hours late), GitHub Actions runs `watcher.py`.
- The script checks each watch entry according to its **tier** (hot=5m, normal=15m, slow=60m).
- For Shopify stores it hits the public `/products.json` API (collections) or `/products/<handle>.js` (single product pages). No HTML scraping.
- For non-Shopify stores it scrapes HTML using `beautifulsoup4`.
- Each watch says which platform it is on (`platform: shopify` or `other`), so runs never spend requests probing.
- State is saved to `state.json` and committed back to the repo after each run.
- Alerts fire only on **transitions**: unavailable to available, or a new product appearing. Going out of stock never alerts.
- **A new watch is silent on its first successful check.** It records what is already listed and alerts only on changes after that. Changing a watch's `include`/`exclude` filters re-runs that silent baseline.
- **In-stock-only lists** (`missing_means_unavailable: true`): a product that drops off the list counts as sold out, so when it comes back you get BACK IN STOCK.
- **Cool-down:** once a product alerts, the same product URL will not alert again for `cooldown_hours` (default 6), from any watch.
- If two watches report the same product in one pass, it is posted **once**.
- Alerts ping a Discord role if `DISCORD_ROLE_ID` is set, and show the product image plus **Add to cart** / **Straight to checkout** links for Shopify stores.
- Every alert that is posted is also appended to `alerts.jsonl` (time, retailer, kind, title, price, url). The file keeps the most recent 500 rows.
- When a watch starts failing (fetch error, 404, 429 rate limit, or 0 products parsed) one warning goes to the `_fallback` webhook, and one "Watch Recovered" message when it works again.
- On HTTP 429 the bot honours `Retry-After` and leaves that whole site alone until it expires (60 s to 1 h).
- **Missed-run warning:** if more than 30 minutes passed since the last successful run, the next run posts "Watcher fell behind" to `_fallback` (at most once every 6 h, summarising the gaps in between).
- Product records not seen for 30 days of successful checks, and records of watches removed from `watches.yaml`, are pruned automatically.

---

## Setup

### 1. Create Discord webhooks

For each game you want to track, create a Discord webhook:

1. Open the Discord channel where you want alerts.
2. **Channel Settings → Integrations → Webhooks → New Webhook**.
3. Copy the webhook URL.

Create at least one webhook for `one-piece`. Optionally create a `_fallback` webhook for adapter-failure warnings.

### 2. Add the `WEBHOOKS_JSON` GitHub secret

1. Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `WEBHOOKS_JSON`
3. Value: a JSON object mapping game names to webhook URLs:

```json
{
  "one-piece": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
  "pokemon":   "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
  "_fallback": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN"
}
```

### 3. Optional secrets

Add these the same way as `WEBHOOKS_JSON` (repo → **Settings → Secrets and variables → Actions → New repository secret**). Both can be left unset.

| Secret | What it does | How to get it |
|---|---|---|
| `DISCORD_ROLE_ID` | Stock alerts @mention this role, so members get a push notification. | In Discord: **User Settings → Advanced → Developer Mode** on. Then **Server Settings → Roles**, right-click the role → **Copy Role ID**. It is a long number. Paste only the number. |
| `HEARTBEAT_URL` | Pinged after every successful run. A service such as healthchecks.io then alerts you if the pings *stop*, which catches a bot that is not running at all. | Create a free check on healthchecks.io with a 30-minute period, connect its Discord integration, and paste the ping URL. |

### 4. Enable GitHub Actions write permission

Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions** (needed to commit `state.json` back).

### 5. Push the repo

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

The workflow will start running on its 5-minute schedule automatically.

---

## Running locally

### Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure local webhooks

```bash
cp webhooks.json.example webhooks.json
# Edit webhooks.json with your real Discord webhook URLs
```

### Test an adapter without sending alerts

```bash
python manage.py test https://www.goodgames.com.au/collections/one-piece-card-game
```

This fetches the URL, prints what the adapter sees, and exits without touching state or Discord.

### Dry-run the full watcher

```bash
python watcher.py --dry-run
```

Processes all due watches, logs what alerts *would* be sent, but does not post to Discord or save state.

### Run the watcher for real

```bash
python watcher.py
```

---

## Managing the watchlist

```bash
# Add a new watch (type auto-detected from URL)
python manage.py add https://shop.com/collections/pokemon --game pokemon --retailer "My Shop" --tier normal

# List all watches
python manage.py list

# List watches for one game
python manage.py list --game one-piece

# Remove a watch
python manage.py remove https://shop.com/collections/pokemon

# Change tier
python manage.py set-tier https://shop.com/collections/pokemon hot

# Store a Discord webhook locally
python manage.py set-webhook pokemon https://discord.com/api/webhooks/ID/TOKEN
```

**Tiers:**

| Tier   | Interval | Use for                       |
|--------|----------|-------------------------------|
| hot    | 5 min    | Active release days           |
| normal | 15 min   | Standard monitoring           |
| slow   | 60 min   | Low-priority or slow sites    |

### Watch options in `watches.yaml`

| Field | Default | Meaning |
|---|---|---|
| `url`, `game`, `retailer_name` | required | What to watch and where alerts go. |
| `tier` | `normal` | `hot` 5 min, `normal` 15 min, `slow` 60 min. |
| `type` | `category` | `category` (a list) or `product` (one product page). |
| `platform` | probed | `shopify` or `other`. Set it so runs never probe. `manage.py add` fills it in. |
| `include` | none | List of title regexes (case-insensitive). If given, a product must match at least one. |
| `exclude` | none | List of title regexes. A product matching any is ignored. |
| `cooldown_hours` | `6` | Hours before the same product can alert again. `0` turns it off. |
| `missing_means_unavailable` | `false` | For in-stock-only lists: a product that disappears is treated as sold out. |
| `missing_grace_passes` | `1` | How many passes a product must be missing before it counts as sold out. |

Example: keep only sealed One Piece product from a mixed pre-order page.

```yaml
- url: https://www.m-g.com.au/pre-orders/
  game: one-piece
  tier: normal
  type: category
  retailer_name: M-G
  platform: other
  include:
  - \bone\s*piece\b
  - \b(?:OP|EB|PRB)-?\d{2}\b
```

`python manage.py test <url>` uses the watch's settings when the URL is in `watches.yaml`, and marks products its filters drop as `[FILTERED OUT]`.

---

## Adding a custom adapter

When `python manage.py test <url>` shows 0 products or clearly wrong results, write a custom adapter:

1. Open `adapters/generic_html.py`.
2. Add a function `_fetch_mysite(url, session)` that uses BeautifulSoup to extract products, returning a list of:
   ```python
   {
       "product_url": str,   # absolute URL
       "title": str,
       "available": bool,
       "is_preorder": bool,
       "price": str | None,
   }
   ```
3. Register it in `DOMAIN_ADAPTERS` at the bottom of the file:
   ```python
   DOMAIN_ADAPTERS = {
       ...
       "www.mysite.com.au": _fetch_mysite,
   }
   ```
4. Run `python manage.py test <url>` again to verify.

### Non-Shopify retailers in this list

| Retailer       | Domain                  | Notes                                    |
|----------------|-------------------------|------------------------------------------|
| Kaika          | kaika.com.au            | Neto storefront; the One Piece series page is mostly figures, so the watch only keeps card products (title filter) and runs on the slow tier |
| Gamers Room    | gamersroom.com.au       | Generic adapter — custom may be needed   |
| M-G            | m-g.com.au              | Pre-orders page, all games mixed in      |
| NJ Collectables | njcollectables.com.au  | Generic adapter                          |

Run `python manage.py test <url>` against each one and check the output before the first real run.

---

## File reference

| File               | Purpose                                  |
|--------------------|------------------------------------------|
| `watches.yaml`     | Committed watchlist                      |
| `webhooks.json`    | Gitignored; local webhook config         |
| `state.json`       | Machine-managed; committed by bot        |
| `ratelimit.py`     | 429 handling and per-site cool-downs     |
| `alerts.jsonl`     | Machine-managed; last 500 alerts posted  |
| `alert_log.py`     | Appends to and trims `alerts.jsonl`      |
| `watcher.py`       | Main entry point                         |
| `manage.py`        | CLI watchlist manager                    |
| `config.py`        | Loads watches.yaml + webhooks            |
| `state.py`         | State R/W and transition detection       |
| `discord_client.py`| Formats and posts Discord embeds         |
| `adapters/`        | Shopify + generic HTML adapters          |

---

## Troubleshooting

**No alerts showing up**
- Check Actions logs for errors.
- Make sure `WEBHOOKS_JSON` secret is set correctly.
- Run `python watcher.py --dry-run` locally.

**"0 products found" warning**
- Run `python manage.py test <url>` to debug the adapter.
- The site may need a custom adapter — you'll also get one Discord alert to the `_fallback` webhook when the watch starts failing, and one more when it recovers.

**State keeps resetting**
- Make sure the workflow has `contents: write` permission and the commit step is not skipped.

**Too many alerts on first run**
- This shouldn't happen: each watch's first successful check silently populates state. If it does, run `python watcher.py --dry-run` to see which watch produces them.

**"Watcher fell behind" messages**
- GitHub runs scheduled workflows late when it is busy, often by hours on free private repos. The message tells you the window that was not checked. Moving the bot to an always-on machine is the fix (planned as Phase 2).

**"Rate Limited" message**
- A site answered HTTP 429. The bot leaves it alone for the time the site asked for (1 min to 1 h) and posts "Watch Recovered" when it works again. If it keeps happening, move that watch to a slower tier.
