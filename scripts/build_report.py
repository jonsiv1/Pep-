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


# Email-safe palette: warm paper background, dark ink, one pizza-oven accent.
INK = "#221C16"
INK_SOFT = "#6E635A"
INK_FAINT = "#9C9186"
PAPER = "#F2EDE6"
CARD = "#FFFFFF"
LINE = "#E7DFD4"
ACCENT = "#B3401F"       # pizza-oven red, dark enough for white text on top
ACCENT_DEEP = "#8F3010"
GOOD = "#1a7f37"
BAD = "#cf222e"

FONT = "font-family:Arial,Helvetica,sans-serif;"


def _pizzas_qty(day_record):
    if not day_record:
        return None
    return day_record["combined"]["categories"].get("Pizzas", {}).get("qty")


def _chip(label, pct):
    """Small pill showing a % change vs a named period."""
    if pct is None:
        return (f"<span style='display:inline-block;background:{PAPER};color:{INK_FAINT};"
                f"border-radius:12px;padding:4px 12px;font-size:12px;{FONT}'>{label}: no data</span>")
    color = GOOD if pct >= 0 else BAD
    arrow = "&#9650;" if pct >= 0 else "&#9660;"
    return (f"<span style='display:inline-block;background:{PAPER};color:{color};font-weight:bold;"
            f"border-radius:12px;padding:4px 12px;font-size:12px;{FONT}'>"
            f"{arrow} {abs(pct)}% <span style='color:{INK_FAINT};font-weight:normal'>{label}</span></span>")


def render_email_html(date_str, wc, dineout, combined, history, items):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    nice_date = f"{dt:%A} {dt.day}. {dt:%B %Y}"
    yesterday = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    last_week = (dt - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_day = find_day(history, yesterday)
    prev_week = find_day(history, last_week)

    vs_yesterday = pct_change(combined["total_incl_vat"], prev_day["combined"]["total_incl_vat"]) if prev_day else None
    vs_last_week = pct_change(combined["total_incl_vat"], prev_week["combined"]["total_incl_vat"]) if prev_week else None

    pizzas = combined["categories"].get("Pizzas", {}).get("qty", 0)
    pizzas_last_week = _pizzas_qty(prev_week)
    pizzas_vs_week = pct_change(pizzas, pizzas_last_week) if pizzas_last_week else None
    if pizzas_vs_week is not None:
        pz_color = "#C9E5C9" if pizzas_vs_week >= 0 else "#F5C6BC"
        pz_arrow = "&#9650;" if pizzas_vs_week >= 0 else "&#9660;"
        pizza_delta = (f"<div style='color:{pz_color};font-size:13px;padding-top:6px;{FONT}'>"
                       f"{pz_arrow} {abs(pizzas_vs_week)}% vs. last {dt:%A}</div>")
    else:
        pizza_delta = ""

    category_rows = "".join(
        f"<tr>"
        f"<td style='padding:9px 0;border-top:1px solid {LINE};color:{INK};font-size:14px;{FONT}'>{name}</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK_SOFT};font-size:14px;{FONT}'>{vals['qty']}</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK_SOFT};font-size:14px;{FONT}'>{vals['excl_vat']:,.0f}</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK_SOFT};font-size:14px;{FONT}'>{vals['vat']:,.0f}</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK};font-weight:bold;font-size:14px;{FONT}'>{vals['incl_vat']:,.0f}</td>"
        f"</tr>"
        for name, vals in sorted(combined["categories"].items(), key=lambda kv: -kv[1]["incl_vat"])
    )

    item_rows = "".join(
        f"<tr>"
        f"<td style='padding:9px 0;border-top:1px solid {LINE};color:{INK_FAINT};font-size:14px;width:24px;{FONT}'>{rank}</td>"
        f"<td style='padding:9px 0;border-top:1px solid {LINE};color:{INK};font-size:14px;{FONT}'>{name}</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK_SOFT};font-size:14px;{FONT}'>{vals['qty']}&#215;</td>"
        f"<td align='right' style='padding:9px 0;border-top:1px solid {LINE};color:{INK};font-weight:bold;font-size:14px;{FONT}'>{vals['incl_vat']:,.0f} kr</td>"
        f"</tr>"
        for rank, (name, vals) in enumerate(items, start=1)
    )

    card_open = (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
                 f"style='background:{CARD};border-radius:14px;'><tr><td style='padding:22px 26px;'>")
    card_close = "</td></tr></table>"
    gap = "<div style='height:14px;line-height:14px;'>&nbsp;</div>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
</head>
<body style="margin:0;padding:0;background:{PAPER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:28px 14px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <tr><td style="padding:0 6px 16px;">
    <div style="color:{ACCENT};font-size:12px;letter-spacing:3px;font-weight:bold;{FONT}">ASKUR TAPROOM &amp; PIZZERIA</div>
    <div style="color:{INK};font-size:24px;font-weight:bold;padding-top:4px;{FONT}">Daily sales report</div>
    <div style="color:{INK_SOFT};font-size:14px;padding-top:2px;{FONT}">{nice_date}</div>
  </td></tr>

  <tr><td>
    {card_open}
      <div align="center" style="text-align:center;">
        <div style="color:{INK_FAINT};font-size:12px;letter-spacing:2px;font-weight:bold;{FONT}">TOTAL SALES</div>
        <div style="color:{INK};font-size:48px;font-weight:bold;line-height:1.1;padding:10px 0 4px;{FONT}">{combined['total_incl_vat']:,.0f} kr</div>
        <div style="color:{INK_SOFT};font-size:13px;padding-bottom:14px;{FONT}">
          {combined['total_excl_vat']:,.0f} kr excl. VAT &nbsp;&#183;&nbsp; {combined['total_vat']:,.0f} kr VAT
        </div>
        <div>{_chip("vs. yesterday", vs_yesterday)} &nbsp; {_chip("vs. last " + f"{dt:%A}", vs_last_week)}</div>
      </div>
    {card_close}
  </td></tr>

  <tr><td>{gap}</td></tr>

  <tr><td>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{ACCENT};border-radius:14px;">
      <tr><td style="padding:20px 26px;" align="center">
        <div style="color:#F5D9CE;font-size:12px;letter-spacing:2px;font-weight:bold;{FONT}">&#127829; PIZZAS SOLD</div>
        <div style="color:#FFFFFF;font-size:40px;font-weight:bold;line-height:1.1;padding-top:6px;{FONT}">{pizzas}</div>
        {pizza_delta}
      </td></tr>
    </table>
  </td></tr>

  <tr><td>{gap}</td></tr>

  <tr><td>
    {card_open}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="50%" style="border-right:1px solid {LINE};padding-right:16px;">
            <div style="color:{INK_FAINT};font-size:12px;letter-spacing:1px;font-weight:bold;{FONT}">RESTAURANT (DINEOUT)</div>
            <div style="color:{INK};font-size:22px;font-weight:bold;padding-top:4px;{FONT}">{dineout['total_incl_vat']:,.0f} kr</div>
            <div style="color:{INK_SOFT};font-size:12px;padding-top:2px;{FONT}">{dineout.get('settlement_count', 0)} settlement{'s' if dineout.get('settlement_count', 0) != 1 else ''}</div>
          </td>
          <td width="50%" style="padding-left:16px;">
            <div style="color:{INK_FAINT};font-size:12px;letter-spacing:1px;font-weight:bold;{FONT}">WEBSHOP</div>
            <div style="color:{INK};font-size:22px;font-weight:bold;padding-top:4px;{FONT}">{wc['total_incl_vat']:,.0f} kr</div>
            <div style="color:{INK_SOFT};font-size:12px;padding-top:2px;{FONT}">{wc['order_count']} orders</div>
          </td>
        </tr>
      </table>
    {card_close}
  </td></tr>

  <tr><td>{gap}</td></tr>

  <tr><td>
    {card_open}
      <div style="color:{INK};font-size:16px;font-weight:bold;padding-bottom:12px;{FONT}">By category</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding-bottom:7px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{FONT}">CATEGORY</td>
          <td align="right" style="padding-bottom:7px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{FONT}">QTY</td>
          <td align="right" style="padding-bottom:7px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{FONT}">EXCL. VAT</td>
          <td align="right" style="padding-bottom:7px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{FONT}">VAT</td>
          <td align="right" style="padding-bottom:7px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{FONT}">INCL. VAT</td>
        </tr>
        {category_rows}
      </table>
    {card_close}
  </td></tr>

  <tr><td>{gap}</td></tr>

  <tr><td>
    {card_open}
      <div style="color:{INK};font-size:16px;font-weight:bold;padding-bottom:12px;{FONT}">Top sellers</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {item_rows}
      </table>
    {card_close}
  </td></tr>

  <tr><td style="padding:20px 8px 0;">
    <div style="color:{INK_FAINT};font-size:11px;line-height:1.5;{FONT}">
      Day totals use each system's real VAT figures where available; an 11% fallback applies where
      no tax was recorded (webshop orders, and category-level splits). Pizza modifiers/toppings are
      excluded from the category counts; sides, desserts, kids menu and buffets count as pizzas.
      Settlements closed before 06:00 belong to the previous business day.
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(settings.EMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))

    # Port 465 is implicit TLS (connect already encrypted); 587/25 use STARTTLS
    # (plain connection, then upgrade) - most cPanel-style mail hosts use 465.
    if settings.SMTP_PORT == 465:
        server_cm = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT)
    else:
        server_cm = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)

    with server_cm as server:
        if settings.SMTP_PORT != 465:
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
        pizzas = combined["categories"].get("Pizzas", {}).get("qty", 0)
        subject = f"\U0001F355 {date_str}: {combined['total_incl_vat']:,.0f} kr · {pizzas} pizzas"
        send_email(subject, html)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    run(date_arg)
