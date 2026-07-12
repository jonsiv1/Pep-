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
via a headless browser, the same way a person would.

Here's how it actually works, confirmed against a real settlement from Askur
Taproom & Pizzeria:

- DineOut's "Settlements" list shows one row per till closing (Z-report),
  not one row per calendar day. A shift that runs past midnight (weekends,
  holidays) gets filed under the *next* morning's date. The scraper corrects
  for this: any settlement closed before 06:00 is counted toward the
  previous day (`CUTOFF_HOUR` in `scripts/dineout_scrape.py`).
- Each settlement's detail page is actually a PDF viewer, not a normal
  webpage — so instead of screen-scraping it, the scraper clicks the same
  **Download** button a person would, then reads the real PDF file.
- DineOut gives one accurate VAT total for the *whole settlement*, so the
  day's overall total/VAT numbers are exact. It does not break VAT down by
  category, so the category/item breakdown uses the same 11%-fallback
  estimate as WooCommerce for that split only.
- Toppings aren't a separate DineOut category — they're filed under
  "Pizzas" alongside the real pizzas. The scraper tells them apart because
  every real menu pizza is numbered ("1. Með allt á hreinu," "19.
  Margarita"); anything in that category without a number, plus loose
  add-ons and "Skip X" modifiers, is treated as a topping and left out of
  the category breakdown (the revenue still counts in the day's total,
  it's just not attributed to "Pizzas"). Items starting with "Buffet" count
  as pizzas too, per how the restaurant wants them tracked.
- One known quirk: adding up every individual item's price comes out
  slightly *higher* than DineOut's official settlement total (roughly 1%
  higher on the sample day tested) — DineOut appears to apply some kind of
  blanket discount/voucher that isn't reflected in individual item prices.
  The day's total/VAT use DineOut's official number either way; only the
  category/item breakdown is built from menu prices and won't tie out to
  the penny.

**What's confirmed vs. still best-effort** in `config/dineout_selectors.json`:
the login fields, the settlement list URL, and the PDF download flow are
built from real data. The exact way settlement rows are found on the list
page (`list_row_selector` etc.) is a reasonable best guess from screenshots,
not real page source — it may need a small adjustment after the first real
test run. Trigger the workflow manually once secrets are in place (see step
4) and send me any error from the Action's log if it doesn't find rows
correctly; I'll fix the selector.

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

- **VAT**: WooCommerce and DineOut's day-level totals use real tax figures
  where available (DineOut always has one; WooCommerce falls back to an
  assumed 11% VAT-inclusive price when no tax is recorded, which is the
  normal case for this store). Category/item-level VAT is always the 11%
  estimate for both sources, since neither exposes real tax per category.
- **Category rollup** (`config/categories.yaml`): "Sides" and "Desserts"
  are folded into "Pizzas" for DineOut. Edit this file — no code changes
  needed — to change the merges.
- **Pizza vs. topping** (DineOut only, `is_real_pizza()` in
  `scripts/dineout_scrape.py`): numbered menu items and anything named
  "Buffet…" count as pizzas; everything else filed under "Pizzas" (loose
  toppings, "Skip X" modifiers) is left out of the breakdown.
- **Past-midnight settlements** (DineOut only): a settlement closed before
  06:00 counts toward the previous calendar day.
- **"Sale" orders**: WooCommerce orders with status `completed` or
  `processing` count as sales; cancelled/refunded/failed/pending do not.
- **Report window**: always "yesterday" in UTC, which equals Iceland time
  year-round.

## Files

```
scripts/
  settings.py           - central config (reads env vars)
  woocommerce_pull.py    - WooCommerce REST API client
  dineout_scrape.py      - Playwright login + settlement PDF download/parse
  build_report.py        - combines both, emails report, updates history.json
  backfill.py             - one-off historical backfill (no emails)
config/
  categories.yaml         - category merge/exclude rules + VAT fallback rate
  dineout_selectors.json  - DineOut login/list/download selectors
data/
  history.json             - append-only daily history, powers the dashboard
dashboard/
  index.html                - static dashboard, reads ../data/history.json
.github/workflows/
  daily-report.yml           - the scheduled job
```
