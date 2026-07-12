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


# Email-safe palette: warm paper, dark ink, one deep-red accent used sparingly.
INK = "#221C16"
INK_SOFT = "#6E635A"
INK_FAINT = "#9C9186"
PAPER = "#EFEBE4"
SHEET = "#FFFFFF"
LINE = "#E3DCD1"
LINE_STRONG = "#C9BFB0"
ACCENT = "#8F3010"
GOOD = "#1a6b32"
BAD = "#b3261e"

SANS = "font-family:Arial,Helvetica,sans-serif;"
# Display style for headings and large figures - same sans stack, bolded,
# with slight negative tracking so big numbers hold together.
DISPLAY = "font-weight:bold;letter-spacing:-0.5px;font-family:Arial,Helvetica,sans-serif;"

# Icelandic names hardcoded - the CI runner has no is_IS locale installed.
IS_WEEKDAYS = ["mánudagur", "þriðjudagur", "miðvikudagur", "fimmtudagur",
               "föstudagur", "laugardagur", "sunnudagur"]
IS_MONTHS = ["janúar", "febrúar", "mars", "apríl", "maí", "júní", "júlí",
             "ágúst", "september", "október", "nóvember", "desember"]


def isk(n):
    """Icelandic number format: period as thousands separator, 'kr.' suffix."""
    return f"{n:,.0f}".replace(",", ".") + " kr."


def is_pct(p):
    """Icelandic decimal comma: 12,3%"""
    return f"{abs(p):.1f}".replace(".", ",") + "%"


def is_date_long(dt):
    return f"{IS_WEEKDAYS[dt.weekday()].capitalize()}, {dt.day}. {IS_MONTHS[dt.month - 1]} {dt.year}"


def is_date_short(dt):
    return dt.strftime("%d.%m.%Y")


def _pizzas_qty(day_record):
    if not day_record:
        return None
    return day_record["combined"]["categories"].get("Pizzas", {}).get("qty")


def _trend(label, pct):
    if pct is None:
        return (f"<span style='color:{INK_FAINT};font-size:13px;{SANS}'>"
                f"{label}: no data</span>")
    color = GOOD if pct >= 0 else BAD
    sign = "+" if pct >= 0 else "&#8722;"
    return (f"<span style='font-size:13px;{SANS}'>"
            f"<span style='color:{color};font-weight:bold;'>{sign}{is_pct(pct)}</span>"
            f"<span style='color:{INK_SOFT};'> {label}</span></span>")


def render_email_html(date_str, wc, dineout, combined, history, items):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    yesterday = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    last_week = (dt - timedelta(days=7)).strftime("%Y-%m-%d")
    prev_day = find_day(history, yesterday)
    prev_week = find_day(history, last_week)
    weekday_en = dt.strftime("%A")

    vs_yesterday = pct_change(combined["total_incl_vat"], prev_day["combined"]["total_incl_vat"]) if prev_day else None
    vs_last_week = pct_change(combined["total_incl_vat"], prev_week["combined"]["total_incl_vat"]) if prev_week else None

    pizzas = combined["categories"].get("Pizzas", {}).get("qty", 0)
    pizzas_last_week = _pizzas_qty(prev_week)
    pizzas_vs_week = pct_change(pizzas, pizzas_last_week) if pizzas_last_week else None

    label_style = f"color:{INK_FAINT};font-size:11px;letter-spacing:2px;font-weight:bold;{SANS}"
    th = f"padding:0 0 8px;color:{INK_FAINT};font-size:11px;letter-spacing:1px;font-weight:bold;{SANS}"
    td = f"padding:8px 0;border-top:1px solid {LINE};font-size:14px;{SANS}"
    # Amounts must never wrap mid-number on narrow phone screens; names may wrap.
    num = "white-space:nowrap;"

    # Three columns only: five columns of long kr. amounts collide on phones,
    # and the per-category excl-VAT/VAT split is an 11% estimate anyway - the
    # real VAT detail is in the day totals at the top.
    category_rows = "".join(
        f"<tr>"
        f"<td style='{td}color:{INK};'>{name}</td>"
        f"<td align='right' style='{td}{num}color:{INK_SOFT};padding-left:12px;'>{vals['qty']}</td>"
        f"<td align='right' style='{td}{num}color:{INK};font-weight:bold;padding-left:12px;'>{isk(vals['incl_vat'])}</td>"
        f"</tr>"
        for name, vals in sorted(combined["categories"].items(), key=lambda kv: -kv[1]["incl_vat"])
    )

    item_rows = "".join(
        f"<tr>"
        f"<td style='{td}color:{INK_FAINT};width:22px;'>{rank}.</td>"
        f"<td style='{td}color:{INK};'>{name}</td>"
        f"<td align='right' style='{td}{num}color:{INK_SOFT};padding-left:12px;'>{vals['qty']} stk.</td>"
        f"<td align='right' style='{td}{num}color:{INK};font-weight:bold;padding-left:12px;'>{isk(vals['incl_vat'])}</td>"
        f"</tr>"
        for rank, (name, vals) in enumerate(items, start=1)
    )

    rule = f"<div style='border-top:1px solid {LINE_STRONG};font-size:0;line-height:0;'>&nbsp;</div>"
    section_pad = "padding:26px 34px;"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<style>
@media only screen and (max-width:480px) {{
  .pad {{ padding-left:18px !important; padding-right:18px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:{PAPER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:32px 14px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="max-width:640px;width:100%;background:{SHEET};border:1px solid {LINE_STRONG};">

  <tr><td class="pad" style="{section_pad}padding-bottom:20px;border-bottom:3px double {INK};">
    <div style="color:{ACCENT};font-size:12px;letter-spacing:3px;font-weight:bold;{SANS}">ASKUR TAPROOM &amp; PIZZERIA</div>
    <div style="color:{INK};font-size:27px;padding-top:6px;{DISPLAY}">Daily sales report</div>
    <div style="color:{INK_SOFT};font-size:14px;padding-top:3px;{SANS}">{is_date_long(dt)}</div>
  </td></tr>

  <tr><td class="pad" style="{section_pad}">
    <div style="{label_style}">TOTAL SALES</div>
    <div style="color:{INK};font-size:46px;line-height:1.1;padding:8px 0 5px;{DISPLAY}">{isk(combined['total_incl_vat'])}</div>
    <div style="color:{INK_SOFT};font-size:13px;padding-bottom:10px;{SANS}">
      <span style="white-space:nowrap;">{isk(combined['total_excl_vat'])} excl. VAT</span> &nbsp;&#183;&nbsp;
      <span style="white-space:nowrap;">{isk(combined['total_vat'])} VAT</span>
    </div>
    <div>{_trend("vs. yesterday", vs_yesterday)} &nbsp;&nbsp;&nbsp; {_trend(f"vs. last {weekday_en}", vs_last_week)}</div>
  </td></tr>

  <tr><td class="pad" style="padding:0 34px;">{rule}</td></tr>

  <tr><td class="pad" style="{section_pad}">
    <div style="{label_style}">PIZZAS SOLD</div>
    <div style="color:{ACCENT};font-size:40px;line-height:1.1;padding:8px 0 5px;{DISPLAY}">{pizzas}</div>
    <div>{_trend(f"vs. last {weekday_en}", pizzas_vs_week)}</div>
  </td></tr>

  <tr><td class="pad" style="padding:0 34px;">{rule}</td></tr>

  <tr><td class="pad" style="{section_pad}">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="50%" style="border-right:1px solid {LINE};padding-right:18px;">
          <div style="{label_style}">RESTAURANT (DINEOUT)</div>
          <div style="color:{INK};font-size:21px;padding-top:6px;white-space:nowrap;{DISPLAY}">{isk(dineout['total_incl_vat'])}</div>
          <div style="color:{INK_SOFT};font-size:12px;padding-top:2px;{SANS}">{dineout.get('settlement_count', 0)} settlement{'s' if dineout.get('settlement_count', 0) != 1 else ''}</div>
        </td>
        <td width="50%" style="padding-left:18px;">
          <div style="{label_style}">WEBSHOP</div>
          <div style="color:{INK};font-size:21px;padding-top:6px;white-space:nowrap;{DISPLAY}">{isk(wc['total_incl_vat'])}</div>
          <div style="color:{INK_SOFT};font-size:12px;padding-top:2px;{SANS}">{wc['order_count']} orders</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td class="pad" style="padding:0 34px;">{rule}</td></tr>

  <tr><td class="pad" style="{section_pad}">
    <div style="color:{INK};font-size:18px;padding-bottom:14px;{DISPLAY}">By category</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="{th}">CATEGORY</td>
        <td align="right" style="{th}white-space:nowrap;">QTY</td>
        <td align="right" style="{th}white-space:nowrap;">SALES (INCL. VAT)</td>
      </tr>
      {category_rows}
    </table>
  </td></tr>

  <tr><td class="pad" style="padding:0 34px;">{rule}</td></tr>

  <tr><td class="pad" style="{section_pad}">
    <div style="color:{INK};font-size:18px;padding-bottom:14px;{DISPLAY}">Top sellers</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {item_rows}
    </table>
  </td></tr>

  <tr><td class="pad" style="{section_pad}padding-top:18px;border-top:1px solid {LINE_STRONG};">
    <div style="color:{INK_FAINT};font-size:11px;line-height:1.5;{SANS}">
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
        day_short = is_date_short(datetime.strptime(date_str, "%Y-%m-%d"))
        subject = f"Daily sales {day_short}: {isk(combined['total_incl_vat'])} · {pizzas} pizzas"
        send_email(subject, html)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    run(date_arg)
