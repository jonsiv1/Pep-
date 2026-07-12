"""Combine the WooCommerce + DineOut daily reports, compare against history,
email an HTML summary, and append the day to data/history.json.

Usage: python build_report.py YYYY-MM-DD
"""
import json
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import settings
from dineout_scrape import scrape_day
from woocommerce_pull import build_report as build_woocommerce_report


def merge_reports(wc, dineout):
    combined_categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0, "excl_vat": 0.0, "vat": 0.0})
    for report in (wc, dineout):
        for name, vals in report["categories"].items():
            bucket = combined_categories[name]
            bucket["qty"] += vals["qty"]
            bucket["incl_vat"] += vals["incl_vat"]
            bucket["excl_vat"] += vals["excl_vat"]
            bucket["vat"] += vals["vat"]

    return {
        "total_incl_vat": round(wc["total_incl_vat"] + dineout["total_incl_vat"], 2),
        "total_excl_vat": round(wc["total_excl_vat"] + dineout["total_excl_vat"], 2),
        "total_vat": round(wc["total_vat"] + dineout["total_vat"], 2),
        "order_count": wc["order_count"],
        "categories": {
            name: {k: (round(v, 2) if k != "qty" else v) for k, v in vals.items()}
            for name, vals in combined_categories.items()
        },
    }


def load_history():
    if settings.HISTORY_FILE.exists():
        with open(settings.HISTORY_FILE) as f:
            return json.load(f)
    return {"daily": []}


def save_history(history):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(settings.HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, sort_keys=True)


def find_day(history, date_str):
    for day in history["daily"]:
        if day["date"] == date_str:
            return day
    return None


def top_items(wc, dineout, n=5):
    combined = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})
    for report in (wc, dineout):
        for name, vals in report.get("items", {}).items():
            combined[name]["qty"] += vals["qty"]
            combined[name]["incl_vat"] += vals["incl_vat"]
    ranked = sorted(combined.items(), key=lambda kv: kv[1]["incl_vat"], reverse=True)
    return ranked[:n]


def pct_change(current, previous):
    if not previous:
        return None
    return round(((current - previous) / previous) * 100, 1)


def render_email_html(date_str, wc, dineout, combined, history, items):
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    last_week = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_day = find_day(history, yesterday)
    prev_week = find_day(history, last_week)

    vs_yesterday = pct_change(combined["total_incl_vat"], prev_day["combined"]["total_incl_vat"]) if prev_day else None
    vs_last_week = pct_change(combined["total_incl_vat"], prev_week["combined"]["total_incl_vat"]) if prev_week else None

    def trend(pct):
        if pct is None:
            return "<span style='color:#888'>no data</span>"
        color = "#1a7f37" if pct >= 0 else "#cf222e"
        arrow = "▲" if pct >= 0 else "▼"
        return f"<span style='color:{color}'>{arrow} {abs(pct)}%</span>"

    category_rows = "".join(
        f"<tr><td>{name}</td><td style='text-align:right'>{vals['qty']}</td>"
        f"<td style='text-align:right'>{vals['excl_vat']:,.0f} kr</td>"
        f"<td style='text-align:right'>{vals['vat']:,.0f} kr</td>"
        f"<td style='text-align:right'>{vals['incl_vat']:,.0f} kr</td></tr>"
        for name, vals in sorted(combined["categories"].items(), key=lambda kv: -kv[1]["incl_vat"])
    )

    item_rows = "".join(
        f"<tr><td>{name}</td><td style='text-align:right'>{vals['qty']}</td>"
        f"<td style='text-align:right'>{vals['incl_vat']:,.0f} kr</td></tr>"
        for name, vals in items
    )

    return f"""
    <html><body style="font-family:Arial,sans-serif;color:#222;max-width:640px;margin:auto">
      <h2 style="margin-bottom:4px">Daily sales report - {date_str}</h2>
      <p style="color:#666;margin-top:0">WooCommerce + DineOut, combined</p>

      <table style="width:100%;border-collapse:collapse;margin-bottom:20px">
        <tr><td style="padding:6px 0">Total (incl. VAT)</td><td style="text-align:right;font-weight:bold">{combined['total_incl_vat']:,.0f} kr</td></tr>
        <tr><td style="padding:6px 0">Total (excl. VAT)</td><td style="text-align:right">{combined['total_excl_vat']:,.0f} kr</td></tr>
        <tr><td style="padding:6px 0">VAT collected</td><td style="text-align:right">{combined['total_vat']:,.0f} kr</td></tr>
        <tr><td style="padding:6px 0">WooCommerce orders</td><td style="text-align:right">{wc['order_count']}</td></tr>
        <tr><td style="padding:6px 0">vs. yesterday</td><td style="text-align:right">{trend(vs_yesterday)}</td></tr>
        <tr><td style="padding:6px 0">vs. same day last week</td><td style="text-align:right">{trend(vs_last_week)}</td></tr>
      </table>

      <h3>By category</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr style="border-bottom:1px solid #ccc"><th style="text-align:left">Category</th><th style="text-align:right">Qty</th><th style="text-align:right">Excl. VAT</th><th style="text-align:right">VAT</th><th style="text-align:right">Incl. VAT</th></tr>
        {category_rows}
      </table>

      <h3>Top-selling items</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr style="border-bottom:1px solid #ccc"><th style="text-align:left">Item</th><th style="text-align:right">Qty</th><th style="text-align:right">Sales (incl. VAT)</th></tr>
        {item_rows}
      </table>

      <p style="color:#999;font-size:12px;margin-top:24px">
        VAT is taken from source data where available; a fallback 11% rate is applied where no
        tax figure was recorded. DineOut "Toppings" are excluded from the category breakdown;
        Sides, Desserts and Baby Pizza are folded into "Pizzas".
      </p>
    </body></html>
    """


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(settings.EMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAIL_FROM, settings.EMAIL_TO, msg.as_string())


def run(date_str, send_report_email=True):
    wc = build_woocommerce_report(date_str)
    dineout = scrape_day(date_str)
    combined = merge_reports(wc, dineout)
    items = top_items(wc, dineout)

    history = load_history()
    existing = find_day(history, date_str)
    day_record = {
        "date": date_str,
        "woocommerce": wc,
        "dineout": dineout,
        "combined": combined,
    }
    if existing:
        history["daily"][history["daily"].index(existing)] = day_record
    else:
        history["daily"].append(day_record)
    history["daily"].sort(key=lambda d: d["date"])
    save_history(history)

    if send_report_email:
        html = render_email_html(date_str, wc, dineout, combined, history, items)
        send_email(f"Sales report - {date_str}", html)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    run(date_arg)
