"""Historical backfill: populate data/history.json for a date range.

One DineOut session for the entire range (see dineout_backfill.py), one
WooCommerce product-catalog fetch, then per-day order pulls. Never sends
emails - the daily workflow handles the ongoing reports.

Usage: python backfill.py 2025-01-01 [end-date, default yesterday]
"""
import sys
from datetime import datetime, timedelta

import dineout_backfill
from build_report import find_day, load_history, merge_reports, save_history
from woocommerce_pull import build_report as build_woocommerce_report
from woocommerce_pull import fetch_product_categories


def empty_dineout_day(date_str):
    return {
        "date": date_str,
        "source": "dineout",
        "settlement_count": 0,
        "total_incl_vat": 0.0,
        "total_excl_vat": 0.0,
        "total_vat": 0.0,
        "categories": {},
        "items": {},
    }


def daterange(start, end):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= stop:
        yield d.isoformat()
        d += timedelta(days=1)


def upsert(history, day_record):
    existing = find_day(history, day_record["date"])
    if existing:
        history["daily"][history["daily"].index(existing)] = day_record
    else:
        history["daily"].append(day_record)


def main(start, end):
    print(f"Backfilling {start}..{end}", flush=True)

    dineout_days = dineout_backfill.collect_range(start, end)
    print(f"[dineout] built day-reports for {len(dineout_days)} days", flush=True)

    product_categories = fetch_product_categories()
    print(f"[woocommerce] product catalog loaded ({len(product_categories)} products)", flush=True)

    history = load_history()
    days = list(daterange(start, end))
    for n, day in enumerate(days, start=1):
        wc = build_woocommerce_report(day, product_categories=product_categories)
        dineout = dineout_days.get(day) or empty_dineout_day(day)
        upsert(history, {
            "date": day,
            "woocommerce": wc,
            "dineout": dineout,
            "combined": merge_reports(wc, dineout),
        })
        if n % 25 == 0 or n == len(days):
            print(f"[combine] {n}/{len(days)} days ({day})", flush=True)
            # checkpoint so a late crash doesn't lose everything
            history["daily"].sort(key=lambda d: d["date"])
            save_history(history)

    history["daily"].sort(key=lambda d: d["date"])
    save_history(history)
    print("Backfill complete.", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python backfill.py YYYY-MM-DD [YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)
    start_arg = sys.argv[1]
    end_arg = sys.argv[2] if len(sys.argv) > 2 else (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    main(start_arg, end_arg)
