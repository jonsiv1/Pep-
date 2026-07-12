"""Log into the DineOut backend, find the settlement(s) for one business day,
download each one's PDF settlement report, and parse it.

Why a PDF: DineOut has no API, and the settlement detail page turns out to
render its report as a PDF inside the browser (not real page text) - so
instead of screen-scraping a picture, we download the same PDF a human would
and read its actual text.

Why "business day" needs correction: DineOut files a settlement under the
date/time its till was closed, not the date the sales happened. A shift that
runs past midnight (weekends, holidays) gets timestamped in the early hours
of the *next* calendar day. Any settlement closed before DINEOUT_CUTOFF_HOUR
is attributed to the previous day instead - confirmed against real data from
Askur Taproom & Pizzeria (see config/dineout_selectors.json).
"""
import json
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
from playwright.sync_api import sync_playwright

import settings

SELECTORS_FILE = settings.REPO_ROOT / "config" / "dineout_selectors.json"

# Screenshot + HTML dump saved here if anything in the browser session fails,
# so a failure can be diagnosed from the workflow's uploaded artifact instead
# of needing someone to reproduce it manually.
DEBUG_DIR = settings.REPO_ROOT / "dineout_debug"


def _save_debug(page, name):
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.content())
    except Exception:
        pass

# A settlement closed before this hour belongs to the previous business day.
CUTOFF_HOUR = 6

# Marks the row of three stat tiles ("Debit Sales | Total Vat | Total Sales")
# at both the top of the report and its trailing recap. The trailing one is
# our signal that the item table has ended.
STAT_LINE = "Debit Sales Total Vat Total Sales"
STAT_VALUES_RE = re.compile(r"^ISK\s([\d,]+)\s+ISK\s([\d,]+)\s+ISK\s([\d,]+)$")
ITEM_LINE_RE = re.compile(r"^(.+?)\s+(\d+)\s+ISK\s([\d,]+)\s+ISK\s([\d,]+)\s+ISK\s([\d,]+)\s+ISK\s([\d,]+)$")
CATEGORY_LINE_RE = re.compile(r"^(.+?)\s+ISK\s([\d,]+)$")


def load_selectors():
    with open(SELECTORS_FILE) as f:
        return json.load(f)


def parse_isk(text):
    return float(text.replace("ISK", "").replace(",", "").strip())


def parse_list_amount(text):
    """The settlements *list* page uses Icelandic formatting: '1.581.350 kr.'"""
    cleaned = text.replace("kr", "").replace(".", "").replace(",", ".").strip()
    cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch in ".-")
    return float(cleaned) if cleaned else 0.0


def business_date_for(created_date, created_time):
    hour = int(created_time.split(":")[0])
    d = datetime.strptime(created_date, "%Y-%m-%d").date()
    if hour < CUTOFF_HOUR:
        d -= timedelta(days=1)
    return d.isoformat()


def is_real_pizza(item_name):
    """Menu pizzas are numbered ('1. Með allt á hreinu'); buffets count too
    (confirmed with the restaurant). Everything else in the Pizzas category
    (loose toppings, 'Skip X' modifiers) is a modifier, not a separate sale.
    """
    return bool(re.match(r"^\d+\.\s", item_name)) or item_name.startswith("Buffet")


def parse_settlement_pdf(pdf_path, fallback_rate=0.11):
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(l.strip() for l in text.split("\n") if l.strip())

    total_vat = total_sales = None
    for i, line in enumerate(lines):
        if line == STAT_LINE:
            m = STAT_VALUES_RE.match(lines[i + 1])
            if m:
                total_vat = parse_isk(m.group(2))
                total_sales = parse_isk(m.group(3))
            break

    categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})
    items = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})
    current_category = None
    started = False
    # Per-item VAT rate isn't in the PDF (only an aggregate for the whole
    # settlement), so category/item splits use the same fallback rate as
    # WooCommerce - this means category-level VAT won't tie out exactly to
    # the settlement's real total_vat above, which uses the authoritative figure.

    for line in lines:
        if not started:
            if line == "Debit":
                started = True
            continue
        if line == STAT_LINE:
            break
        if line in ("Debit", "Credit"):
            continue

        m_item = ITEM_LINE_RE.match(line)
        if m_item:
            name, qty, _unit_price, _total_no_discount, _discount, total = m_item.groups()
            if current_category == "Pizzas" and not is_real_pizza(name):
                continue  # topping/modifier - money already in total_sales, just not attributed
            qty = int(qty)
            total = parse_isk(total)
            items[name]["qty"] += qty
            items[name]["incl_vat"] += total
            categories[current_category]["qty"] += qty
            categories[current_category]["incl_vat"] += total
            continue

        m_cat = CATEGORY_LINE_RE.match(line)
        if m_cat:
            current_category = m_cat.group(1).strip()
            continue

    for vals in categories.values():
        vals["excl_vat"] = vals["incl_vat"] / (1 + fallback_rate)
        vals["vat"] = vals["incl_vat"] - vals["excl_vat"]

    return {
        "total_incl_vat": total_sales or 0.0,
        "total_vat": total_vat or 0.0,
        "categories": dict(categories),
        "items": dict(items),
    }


def find_settlements_for_day(page, sel, target_date):
    """Paginate the settlements list, return [(created_date, created_time, open_href), ...]
    for every settlement whose corrected business date matches target_date.
    Rows are assumed newest-first, so we stop once we've paged past the target.
    """
    matches = []
    page.goto(sel["settlements_list_url"])
    page.wait_for_selector(sel["list_row_selector"], timeout=20000)

    while True:
        rows = page.query_selector_all(sel["list_row_selector"])
        if not rows:
            break

        oldest_business_date_seen = None
        for row in rows:
            cells = row.query_selector_all(sel["list_cell_selector"])
            created_date = cells[sel["list_col_created"]].inner_text().strip()
            created_time = cells[sel["list_col_time"]].inner_text().strip()
            link = row.query_selector(sel["list_open_link_selector"])
            href = link.get_attribute("href") if link else None

            bdate = business_date_for(created_date, created_time)
            oldest_business_date_seen = bdate
            if bdate == target_date and href:
                matches.append(href)

        if oldest_business_date_seen and oldest_business_date_seen < target_date:
            break  # paged past the target date; rows only get older from here

        next_button = page.query_selector(sel["list_next_page_selector"])
        if not next_button or next_button.is_disabled():
            break
        next_button.click()
        page.wait_for_timeout(1000)

    return matches


def scrape_day(date_str):
    sel = load_selectors()
    rules = settings.load_category_rules()
    fallback_rate = rules.get("vat_fallback_rate", 0.11)
    merge_map = {}
    for dest, sources in (rules.get("dineout_merge_into") or {}).items():
        for src in sources:
            merge_map[src] = dest

    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            # domcontentloaded (not the default "load") so we don't wait on
            # trackers/polling that may never go idle; then wait specifically
            # for the field we need, which is what actually matters here.
            # The form is Material-UI: "Email address"/"Password" are floating
            # <label>s, not native placeholders, so get_by_label (which
            # resolves the label->input association) is used instead of a
            # placeholder-based CSS selector, which never matched anything.
            page.goto(sel["login_url"], wait_until="domcontentloaded")
            email_field = page.get_by_label(sel["username_label"])
            email_field.wait_for(state="visible", timeout=45000)
            email_field.fill(settings.DINEOUT_USERNAME)
            page.get_by_label(sel["password_label"]).fill(settings.DINEOUT_PASSWORD)
            page.click(sel["submit_selector"])
            page.wait_for_selector(sel["login_success_selector"], timeout=20000)

            settlement_hrefs = find_settlements_for_day(page, sel, date_str)

            total_incl = total_vat = 0.0
            categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0, "excl_vat": 0.0, "vat": 0.0})
            items = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})

            for href in settlement_hrefs:
                page.goto(href)
                page.wait_for_selector(sel["download_button_selector"], timeout=20000)
                with page.expect_download() as dl_info:
                    page.click(sel["download_button_selector"])
                download = dl_info.value
                pdf_path = Path(tmp) / download.suggested_filename
                download.save_as(pdf_path)

                parsed = parse_settlement_pdf(pdf_path, fallback_rate=fallback_rate)
                total_incl += parsed["total_incl_vat"]
                total_vat += parsed["total_vat"]
                for name, vals in parsed["categories"].items():
                    name = merge_map.get(name, name)
                    categories[name]["qty"] += vals["qty"]
                    categories[name]["incl_vat"] += vals["incl_vat"]
                    categories[name]["excl_vat"] += vals["excl_vat"]
                    categories[name]["vat"] += vals["vat"]
                for name, vals in parsed["items"].items():
                    items[name]["qty"] += vals["qty"]
                    items[name]["incl_vat"] += vals["incl_vat"]
        except Exception:
            _save_debug(page, "failure")
            raise
        finally:
            browser.close()

    total_excl = total_incl - total_vat

    return {
        "date": date_str,
        "source": "dineout",
        "settlement_count": len(settlement_hrefs),
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


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_arg:
        print("Usage: python dineout_scrape.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(scrape_day(date_arg), indent=2))
