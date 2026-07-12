"""Log into the DineOut backend and scrape one day's sales report.

DineOut has no API - this drives a real headless browser against the
web backend. Selectors are NOT hardcoded here; they live in
config/dineout_selectors.json so they can be fixed without touching code
once someone has looked at the real login/report pages.
"""
import json
import sys
from collections import defaultdict

from playwright.sync_api import sync_playwright

import settings

SELECTORS_FILE = settings.REPO_ROOT / "config" / "dineout_selectors.json"


def load_selectors():
    with open(SELECTORS_FILE) as f:
        return json.load(f)


def scrape_day(date_str):
    sel = load_selectors()
    rules = settings.load_category_rules()
    fallback_rate = rules.get("vat_fallback_rate", 0.11)
    exclude = set(rules.get("dineout_exclude") or [])
    merge_map = {}
    for dest, sources in (rules.get("dineout_merge_into") or {}).items():
        for src in sources:
            merge_map[src] = dest

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(sel["login_url"])
        page.fill(sel["username_selector"], settings.DINEOUT_USERNAME)
        page.fill(sel["password_selector"], settings.DINEOUT_PASSWORD)
        page.click(sel["submit_selector"])
        page.wait_for_selector(sel["login_success_selector"], timeout=15000)

        report_url = sel["report_url_template"].format(date=date_str)
        page.goto(report_url)
        page.wait_for_selector(sel["report_table_selector"], timeout=15000)

        rows = page.query_selector_all(sel["report_row_selector"])
        categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0, "excl_vat": 0.0, "vat": 0.0})
        items = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})
        total_incl = 0.0

        for row in rows:
            cat_name = row.query_selector(sel["row_category_selector"]).inner_text().strip()
            item_name = row.query_selector(sel["row_item_selector"]).inner_text().strip()
            qty_text = row.query_selector(sel["row_qty_selector"]).inner_text().strip()
            amount_text = row.query_selector(sel["row_amount_incl_vat_selector"]).inner_text().strip()

            qty = _parse_number(qty_text)
            incl = _parse_number(amount_text)
            excl = incl / (1 + fallback_rate)
            vat = incl - excl

            item_bucket = items[item_name]
            item_bucket["qty"] += qty
            item_bucket["incl_vat"] += incl
            total_incl += incl

            cat_name = merge_map.get(cat_name, cat_name)
            if cat_name in exclude:
                continue

            bucket = categories[cat_name]
            bucket["qty"] += qty
            bucket["incl_vat"] += incl
            bucket["excl_vat"] += excl
            bucket["vat"] += vat

        browser.close()

    total_excl = total_incl / (1 + fallback_rate)
    total_vat = total_incl - total_excl

    return {
        "date": date_str,
        "source": "dineout",
        "total_incl_vat": round(total_incl, 2),
        "total_excl_vat": round(total_excl, 2),
        "total_vat": round(total_vat, 2),
        "categories": {
            name: {k: (round(v, 2) if k != "qty" else v) for k, v in vals.items()}
            for name, vals in categories.items()
        },
        "items": {
            name: {k: (round(v, 2) if k != "qty" else v) for k, v in vals.items()}
            for name, vals in items.items()
        },
    }


def _parse_number(text):
    cleaned = text.replace("kr", "").replace(".", "").replace(",", ".").strip()
    cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch == "." or ch == "-")
    return float(cleaned) if cleaned else 0.0


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_arg:
        print("Usage: python dineout_scrape.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(scrape_day(date_arg), indent=2))
