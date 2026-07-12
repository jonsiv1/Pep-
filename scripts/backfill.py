"""One-off backfill: build history for every day from a start date through
yesterday. Run this manually once credentials/selectors are working -
it's slow (one DineOut login+scrape per day) and not part of the daily job.

Usage: python backfill.py 2025-01-01 [end-date, default yesterday]
"""
import sys
import time
from datetime import datetime, timedelta

from build_report import run

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python backfill.py YYYY-MM-DD [YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)

    start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = (
        datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        if len(sys.argv) > 2
        else (datetime.utcnow() - timedelta(days=1)).date()
    )

    day = start
    while day <= end:
        date_str = day.strftime("%Y-%m-%d")
        print(f"Backfilling {date_str}...")
        try:
            run(date_str, send_report_email=False)
        except Exception as exc:
            print(f"  FAILED {date_str}: {exc}", file=sys.stderr)
        time.sleep(1)
        day += timedelta(days=1)
