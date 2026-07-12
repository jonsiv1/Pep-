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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def business_date_from_iso(created_iso):
    """'2026-03-29T21:17:04...' -> business date, applying the before-06:00
    rule. DineOut timestamps are Iceland time = UTC year-round."""
    dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    d = dt.date()
    if dt.hour < CUTOFF_HOUR:
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


def download_matching_settlements(page, sel, target_date, tmp_dir):
    """Paginate the settlements list; for every settlement whose corrected
    business date matches target_date, click through to its detail page,
    download the PDF, and come back. Returns the list of downloaded PDF paths.

    The "Open" control is a plain <button> with a React click-handler - no
    href, no id anywhere in the markup - so there's no link to collect and
    visit later; each match has to be clicked through immediately.
    Rows are assumed newest-first, so we stop once we've paged past the target.
    """
    downloaded = []
    page.goto(sel["settlements_list_url"])
    page.wait_for_selector(sel["list_row_selector"], timeout=20000)

    while True:
        rows = page.query_selector_all(sel["list_row_selector"])
        if not rows:
            break

        oldest_business_date_seen = None
        matching_indices = []
        for i, row in enumerate(rows):
            cells = row.query_selector_all(sel["list_cell_selector"])
            created_date = cells[sel["list_col_created"]].inner_text().strip()
            created_time = cells[sel["list_col_time"]].inner_text().strip()
            bdate = business_date_for(created_date, created_time)
            oldest_business_date_seen = bdate
            if bdate == target_date:
                matching_indices.append(i)

        for i in matching_indices:
            # Re-query fresh each time: after go_back() below, any handles
            # from the loop above are stale (detached from the new DOM).
            rows = page.query_selector_all(sel["list_row_selector"])
            open_button = rows[i].query_selector(sel["list_open_link_selector"])
            if not open_button:
                continue
            open_button.click()
            page.wait_for_selector(sel["download_button_selector"], timeout=20000)
            with page.expect_download() as dl_info:
                page.click(sel["download_button_selector"])
            download = dl_info.value
            pdf_path = Path(tmp_dir) / download.suggested_filename
            download.save_as(pdf_path)
            downloaded.append(pdf_path)

            page.go_back()
            page.wait_for_selector(sel["list_row_selector"], timeout=20000)

        if oldest_business_date_seen and oldest_business_date_seen < target_date:
            break  # paged past the target date; rows only get older from here

        next_button = page.query_selector(sel["list_next_page_selector"])
        if not next_button or next_button.is_disabled():
            break
        next_button.click()
        page.wait_for_timeout(1000)

    if not downloaded:
        # Found nothing for the target date - could be legitimate (no
        # settlements that day) or a parsing mismatch (wrong column index,
        # unexpected date format). Capture what the scraper actually saw so
        # this doesn't fail silently.
        _save_debug(page, "no_settlements_found")

    return downloaded


def category_merge_map(rules):
    merge_map = {}
    for dest, sources in (rules.get("dineout_merge_into") or {}).items():
        for src in sources:
            merge_map[src] = dest
    return merge_map


def login(page, sel):
    """Log into the DineOut Partner backend.

    domcontentloaded (not the default "load") so we don't wait on
    trackers/polling that may never go idle; then wait specifically for the
    field we need, which is what actually matters here. The form is
    Material-UI: "Email address"/"Password" are floating <label>s, not
    native placeholders, so get_by_label (which resolves the label->input
    association) is used instead of a placeholder-based CSS selector, which
    never matched anything.
    """
    page.goto(sel["login_url"], wait_until="domcontentloaded")
    email_field = page.get_by_label(sel["username_label"])
    email_field.wait_for(state="visible", timeout=45000)
    email_field.fill(settings.DINEOUT_USERNAME)
    page.get_by_label(sel["password_label"]).fill(settings.DINEOUT_PASSWORD)
    page.click(sel["submit_selector"])
    page.wait_for_selector(sel["login_success_selector"], timeout=20000)


def build_day_report(date_str, parsed_settlements, merge_map, fallback_rate):
    """Aggregate one business day's parsed settlement PDFs into the report
    dict shape the rest of the pipeline (merge_reports, email, dashboard)
    consumes."""
    total_incl = total_vat = 0.0
    categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0, "excl_vat": 0.0, "vat": 0.0})
    items = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})

    for parsed in parsed_settlements:
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

    total_excl = total_incl - total_vat

    return {
        "date": date_str,
        "source": "dineout",
        "settlement_count": len(parsed_settlements),
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


# ---------------------------------------------------------------------------
# Data-integrity cross-check against the POS order log.
#
# Settlements say WHEN the till was closed, not when the sales happened - an
# unsettled till lumps two days into one settlement (observed on ~25 days over
# 18 months). Orders carry their own timestamps, so summing the order log for
# the same business day gives an independent total to compare against.
#
# The orders API (GET api.dineout.is/api/partner/OrderReport/filter, bearer
# auth, date-filter query params - confirmed by recon) is driven by rewriting
# the app's own captured request URL, so no query parameter names are
# hardcoded or persisted.
# ---------------------------------------------------------------------------

_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_PAGE_SIZE_PARAM_NAMES = {"take", "limit", "pagesize", "size", "top", "rows", "perpage", "count"}
_SKIP_PARAM_NAMES = {"skip", "offset", "start"}
CHECK_TOLERANCE_PCT = 5.0
CHECK_MIN_TOTAL = 10000  # ignore mismatch noise below this many kr


def _set_query_param(url, name, value):
    parts = urlsplit(url)
    params = [(k, str(value) if k == name else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _rewrite_for_day(url, day):
    """Rewrite every date-valued query param to `day`, keeping any time
    suffix (from=2026-07-13T00:00 -> from=<day>T00:00). Also raises any
    recognizable page-size param to 500. Returns (url, found_dates)."""
    parts = urlsplit(url)
    out, found_dates = [], False
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if _DATE_PREFIX_RE.match(v):
            out.append((k, day + v[10:]))
            found_dates = True
        elif k.lower() in _PAGE_SIZE_PARAM_NAMES and v.isdigit():
            out.append((k, "500"))
        else:
            out.append((k, v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(out), parts.fragment)), found_dates


def _order_value_key(item):
    for key in ("totalPaid", "total", "totalAmount", "amount"):
        if key in item:
            return key
    return None


def _fetch_calendar_day_orders(context, template_url, auth, day):
    """All order items for one calendar day, following skip-style pagination."""
    url, found_dates = _rewrite_for_day(template_url, day)
    if not found_dates:
        raise RuntimeError("no date-valued query params found in captured orders request")

    skip_param = next(
        (k for k, _ in parse_qsl(urlsplit(url).query) if k.lower() in _SKIP_PARAM_NAMES), None
    )
    items = []
    for _ in range(40):
        resp = context.request.get(url if not items else _set_query_param(url, skip_param, len(items)),
                                   headers={"authorization": auth}, timeout=60000)
        if resp.status != 200:
            raise RuntimeError(f"orders API HTTP {resp.status}")
        body = resp.json()
        result = body.get("result") or {}
        page_items = result.get("result") or []
        hits = result.get("hits", len(page_items))
        items.extend(page_items)
        if len(items) >= hits or not page_items or skip_param is None:
            break
    return items


def orders_cross_check(context, page, sel, date_str, settlement_total, settlement_count):
    """Never raises - the report must go out even if the check can't run."""
    try:
        captured = {}

        def on_request(request):
            if "OrderReport/filter" in request.url and request.headers.get("authorization"):
                captured["url"] = request.url
                captured["auth"] = request.headers["authorization"]

        page.on("request", on_request)
        page.goto(sel["orders_report_url"])
        page.wait_for_timeout(6000)
        page.remove_listener("request", on_request)

        if "url" not in captured:
            return {"status": "unavailable", "reason": "orders API request not captured"}

        next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        items = []
        for day in (date_str, next_day):
            items.extend(_fetch_calendar_day_orders(context, captured["url"], captured["auth"], day))

        orders_total, orders_counted, value_key = 0.0, 0, None
        for item in items:
            created = item.get("created") or item.get("designatedTime")
            if not created or business_date_from_iso(created) != date_str:
                continue
            if value_key is None:
                value_key = _order_value_key(item)
                if value_key is None:
                    return {"status": "unavailable",
                            "reason": f"no total field on order items (keys: {sorted(item.keys())[:12]})"}
            orders_total += float(item.get(value_key) or 0)
            orders_counted += 1

        result = {
            "orders_total": round(orders_total, 2),
            "orders_counted": orders_counted,
            "settlement_total": round(settlement_total, 2),
        }
        if settlement_count == 0 and orders_total > CHECK_MIN_TOTAL:
            result["status"] = "missing_settlement"
        elif orders_total <= CHECK_MIN_TOTAL and settlement_total <= CHECK_MIN_TOTAL:
            result["status"] = "ok"
            result["delta_pct"] = 0.0
        elif orders_total <= 0:
            result["status"] = "unavailable"
            result["reason"] = "order log returned no orders for this day"
        else:
            delta = (settlement_total - orders_total) / orders_total * 100
            result["delta_pct"] = round(delta, 1)
            result["status"] = "ok" if abs(delta) <= CHECK_TOLERANCE_PCT else "mismatch"
        return result
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)[:200]}


def scrape_day(date_str):
    sel = load_selectors()
    rules = settings.load_category_rules()
    fallback_rate = rules.get("vat_fallback_rate", 0.11)
    merge_map = category_merge_map(rules)

    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            login(page, sel)
            pdf_paths = download_matching_settlements(page, sel, date_str, tmp)
            parsed = [parse_settlement_pdf(p_, fallback_rate=fallback_rate) for p_ in pdf_paths]
            report = build_day_report(date_str, parsed, merge_map, fallback_rate)
            report["orders_check"] = orders_cross_check(
                context, page, sel, date_str,
                report["total_incl_vat"], report["settlement_count"],
            )
        except Exception:
            _save_debug(page, "failure")
            raise
        finally:
            browser.close()

    return report


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_arg:
        print("Usage: python dineout_scrape.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(scrape_day(date_arg), indent=2))
