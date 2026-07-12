# Daily sales report + dashboard (WooCommerce + DineOut)

Pulls yesterday's sales from WooCommerce (via its REST API) and DineOut (via a
scripted browser login, since DineOut has no API), emails a combined report,
and appends the day to `data/history.json`, which powers the dashboard at
`/dashboard/`.

## How it runs

`.github/workflows/daily-report.yml` runs on GitHub Actions every day at
05:00 UTC (= 05:00 Iceland time — Iceland has no DST). It:

1. Pulls yesterday's WooCommerce orders (`scripts/woocommerce_pull.py`)
2. Logs into DineOut and scrapes yesterday's report (`scripts/dineout_scrape.py`)
3. Combines both, computes VAT and category/item breakdowns, emails the report
   (`scripts/build_report.py`)
4. Commits the updated `data/history.json` back to the repo
5. Redeploys the dashboard to GitHub Pages

You can also trigger it manually from the repo's **Actions** tab
("Daily sales report" → "Run workflow"), optionally specifying a date to
regenerate a single day.

## One-time setup

### 1. Generate WooCommerce API keys

In your WordPress admin:

1. Go to **WooCommerce → Settings → Advanced → REST API**
2. Click **Add key**
3. Description: `Daily report (read-only)`
4. Permissions: **Read**
5. Click **Generate API key**
6. Copy the **Consumer key** and **Consumer secret** immediately — WooCommerce
   only shows the secret once.

You'll also need your store's base URL (e.g. `https://yourrestaurant.is`).

### 2. DineOut credentials

Since DineOut has no API, the scraper logs in with a real username/password
via a headless browser. Use a dedicated login if DineOut supports creating
one (least-privilege), otherwise your existing login works.

**The scraper's selectors are placeholders** in
`config/dineout_selectors.json` and will not work until they're pointed at
the real backend. To fill them in:

1. Log into the DineOut backend in a normal browser
2. Open DevTools (F12) on the login page → right-click the username field →
   **Inspect** → note its CSS selector (id, name, or class); same for the
   password field and the submit button
3. Navigate to the daily sales report page → note the URL pattern (does it
   take a date query param? a date picker you'd need to interact with?) and
   the CSS selectors for the report table, its rows, and the category / item
   / quantity / amount columns
4. Update `config/dineout_selectors.json` with the real values (a PR/commit,
   not a secret — there's nothing sensitive in selector names)

I can do this myself if you share a couple of screenshots of the login page
and the sales report page (with the URL bar visible), or the page's HTML
source — then I'll fill in the real selectors and test the flow.

### 3. Email (SMTP)

Any SMTP provider works (Gmail with an app password, SendGrid, Mailgun,
your host's mail server, etc). You'll need: host, port, username, password,
a from-address, and the recipient address(es).

### 4. Add GitHub Actions secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of:

| Secret | Value |
|---|---|
| `WOOCOMMERCE_URL` | e.g. `https://yourrestaurant.is` |
| `WOOCOMMERCE_CONSUMER_KEY` | from step 1 |
| `WOOCOMMERCE_CONSUMER_SECRET` | from step 1 |
| `DINEOUT_URL` | DineOut backend login URL |
| `DINEOUT_USERNAME` | DineOut login username |
| `DINEOUT_PASSWORD` | DineOut login password |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USERNAME` | SMTP login |
| `SMTP_PASSWORD` | SMTP password / app password |
| `EMAIL_FROM` | sender address |
| `EMAIL_TO` | recipient(s), comma-separated |

### 5. Enable GitHub Pages

**Settings → Pages → Source → GitHub Actions.** The dashboard will then be
live at `https://<your-github-username>.github.io/<repo-name>/dashboard/`
after the workflow's first successful run.

### 6. Backfill history back to Jan 1, 2025

Once secrets/selectors work (test with a single manual workflow run first),
backfill historical data locally:

```bash
cd scripts
pip install -r requirements.txt
playwright install --with-deps chromium
export WOOCOMMERCE_URL=... WOOCOMMERCE_CONSUMER_KEY=... WOOCOMMERCE_CONSUMER_SECRET=...
export DINEOUT_URL=... DINEOUT_USERNAME=... DINEOUT_PASSWORD=...
python backfill.py 2025-01-01
```

This does **not** send emails (one per historical day would be spam) — it
only populates `data/history.json`. Commit and push the result when done.
This is slow (one DineOut login + scrape per day), so expect it to take a
while for ~550 days.

## Assumptions baked in (adjust if wrong)

- **VAT**: uses each source's real tax figures where recorded; falls back to
  an assumed 11% VAT-inclusive price when no tax figure is present.
- **Category rollup** (`config/categories.yaml`): DineOut's "Toppings"
  category is excluded from the category breakdown (still counted in
  totals); Sides, Desserts, and Baby Pizza are folded into "Pizzas". Edit
  this file — no code changes needed — to adjust.
- **"Sale" orders**: WooCommerce orders with status `completed` or
  `processing` count as sales; cancelled/refunded/failed/pending do not.
- **Report window**: always "yesterday" in UTC, which equals Iceland time
  year-round.

## Files

```
scripts/
  settings.py           - central config (reads env vars)
  woocommerce_pull.py    - WooCommerce REST API client
  dineout_scrape.py      - Playwright login + scrape (needs real selectors)
  build_report.py        - combines both, emails report, updates history.json
  backfill.py             - one-off historical backfill (no emails)
config/
  categories.yaml         - category merge/exclude rules + VAT fallback rate
  dineout_selectors.json  - CSS selectors for the DineOut scraper (placeholders)
data/
  history.json             - append-only daily history, powers the dashboard
dashboard/
  index.html                - static dashboard, reads ../data/history.json
.github/workflows/
  daily-report.yml           - the scheduled job
```
