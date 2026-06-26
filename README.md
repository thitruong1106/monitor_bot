# TCG Stock Watcher

Monitors Australian TCG retailer pages for new products and restocks, posts alerts to Discord. Runs on GitHub Actions every 5 minutes.

## How it works

- **Every 5 min**, GitHub Actions runs `watcher.py`.
- The script checks each watch entry according to its **tier** (hot=5m, normal=15m, slow=60m).
- For Shopify stores it hits the public `/products.json` API — fast, reliable, no HTML scraping.
- For non-Shopify stores it scrapes HTML using `beautifulsoup4`.
- State (which products exist, their availability) is saved to `state.json` and committed back to the repo after each run.
- Alerts fire only on **transitions** — available→unavailable changes do not alert; only unavailable→available (or new product appearance) do.

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

### 3. Enable GitHub Actions write permission

Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions** (needed to commit `state.json` back).

### 4. Push the repo

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
| Kaika          | kaika.com.au            | Generic adapter — test before deploying  |
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
- The site may need a custom adapter — you'll also get a Discord alert to the `_fallback` webhook.

**State keeps resetting**
- Make sure the workflow has `contents: write` permission and the commit step is not skipped.

**Too many alerts on first run**
- This shouldn't happen — first run silently populates state. If it does, delete `state.json` and run with `--dry-run` first to verify.
