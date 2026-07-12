"""Collect DineOut day-reports for a whole date range in ONE session.

Strategy (from the recon run against the live backend, 2026-07-12):
- The settlements list is fed by POST api.dineout.is/api/partner/
  settlementreport/filter/ and each PDF is served from GET .../
  settlementreport/download/{id}/{locationId}, both authorized by the
  session's bearer token.
- Rows-per-page can be raised to 100, so ~950 settlements = ~10 pages.
- Pagination state resets after opening a settlement and going back, so
  clicking into each settlement is a non-starter for bulk work.

So: log in once, set rows-per-page to 100, click through every list page
while capturing the app's own /filter/ API responses (settlement ids +
timestamps), then fetch each needed PDF directly from the download
endpoint with the captured bearer token - no per-settlement UI navigation
at all. PDFs are parsed and grouped by corrected business date.
"""
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import settings
from dineout_scrape import (
    _save_debug,
    build_day_report,
    business_date_from_iso,
    category_merge_map,
    load_selectors,
    login,
    parse_settlement_pdf,
)

FILTER_PATH_FRAGMENT = "settlementreport/filter"


def extract_items(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("items", "results", "data", "settlements", "reports", "list", "rows"):
            v = body.get(key)
            if isinstance(v, list):
                return v
        for v in body.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def map_item(item):
    sid = item.get("id")
    created = None
    # "dateTime" is the real field name (confirmed from the first live run's
    # error output); the rest are kept as fallbacks in case it ever changes.
    for key in ("dateTime", "createdAt", "created", "createdDate", "createdOn", "date"):
        if item.get(key):
            created = item[key]
            break
    loc = item.get("locationId") or item.get("restaurantLocationId")
    if loc is None and isinstance(item.get("location"), dict):
        loc = item["location"].get("id")
    return sid, created, loc


def collect_range(start_date, end_date):
    """Returns {business_date_iso: dineout_day_report_dict} covering every
    settlement whose business date falls in [start_date, end_date]."""
    sel = load_selectors()
    rules = settings.load_category_rules()
    fallback_rate = rules.get("vat_fallback_rate", 0.11)
    merge_map = category_merge_map(rules)

    captured = {"auth": None, "api_base": None, "filter_bodies": []}

    def on_request(request):
        if FILTER_PATH_FRAGMENT in request.url and request.headers.get("authorization"):
            captured["auth"] = request.headers["authorization"]
            u = urlparse(request.url)
            captured["api_base"] = f"{u.scheme}://{u.netloc}"

    def on_response(response):
        if FILTER_PATH_FRAGMENT in response.url and response.status == 200:
            try:
                captured["filter_bodies"].append(response.json())
            except Exception:
                pass

    parsed_by_day = defaultdict(list)

    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        try:
            login(page, sel)
            page.goto(sel["settlements_list_url"])
            page.wait_for_selector(sel["list_row_selector"], timeout=20000)

            # 100 rows per page (verified option) -> ~10 pages for ~950 rows
            page.click(sel["rows_per_page_select"])
            page.wait_for_selector(sel["rows_per_page_option_100"], timeout=5000)
            page.click(sel["rows_per_page_option_100"])
            page.wait_for_timeout(2500)

            # walk every page; the app's own /filter/ responses give us ids
            for _ in range(200):
                next_button = page.query_selector(sel["list_next_page_selector"])
                if not next_button or next_button.is_disabled():
                    break
                next_button.click()
                page.wait_for_timeout(2000)

            # index all captured settlements (page-1 refetches dedupe by id)
            index = {}
            for body in captured["filter_bodies"]:
                for item in extract_items(body):
                    sid, created, loc = map_item(item)
                    if sid is not None and created and loc is not None:
                        index[sid] = (created, loc)

            if not index:
                sample_keys = None
                for body in captured["filter_bodies"]:
                    items = extract_items(body)
                    if items:
                        sample_keys = sorted(items[0].keys())
                        break
                _save_debug(page, "backfill_no_index")
                raise RuntimeError(
                    f"Could not map settlements from {len(captured['filter_bodies'])} "
                    f"captured /filter/ responses; sample item keys: {sample_keys}"
                )
            if not captured["auth"]:
                raise RuntimeError("Never captured an authorization header from the app")

            in_range = {
                sid: (created, loc)
                for sid, (created, loc) in index.items()
                if start_date <= business_date_from_iso(created) <= end_date
            }
            print(f"[dineout] indexed {len(index)} settlements total, "
                  f"{len(in_range)} in {start_date}..{end_date}", flush=True)

            # fetch each PDF straight from the API - no UI navigation
            failures = []
            for n, (sid, (created, loc)) in enumerate(sorted(in_range.items()), start=1):
                url = f"{captured['api_base']}/api/partner/settlementreport/download/{sid}/{loc}"
                resp = context.request.get(url, headers={"authorization": captured["auth"]}, timeout=60000)
                if resp.status != 200:
                    failures.append((sid, resp.status))
                    print(f"[dineout] {n}/{len(in_range)} settlement {sid}: HTTP {resp.status}", flush=True)
                    continue
                pdf_path = Path(tmp) / f"{sid}.pdf"
                pdf_path.write_bytes(resp.body())
                parsed = parse_settlement_pdf(pdf_path, fallback_rate=fallback_rate)
                parsed_by_day[business_date_from_iso(created)].append(parsed)
                if n % 25 == 0 or n == len(in_range):
                    print(f"[dineout] downloaded+parsed {n}/{len(in_range)}", flush=True)
                time.sleep(0.3)  # be gentle

            if failures:
                raise RuntimeError(f"{len(failures)} settlement downloads failed: {failures[:10]}")
        except Exception:
            _save_debug(page, "backfill_failure")
            raise
        finally:
            browser.close()

    return {
        day: build_day_report(day, parsed_list, merge_map, fallback_rate)
        for day, parsed_list in parsed_by_day.items()
    }
