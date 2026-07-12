"""Pull one day's sales from WooCommerce via the REST API (v3) and return a
normalized report dict. No scraping needed here - WooCommerce has a real API.
"""
import sys
import time
from collections import defaultdict

import requests

import settings

# Order statuses counted as a completed sale. Adjust if the restaurant uses
# different statuses (e.g. "on-hold") to mean "paid".
SALE_STATUSES = {"completed", "processing"}

API_BASE = f"{settings.WOOCOMMERCE_URL}/wp-json/wc/v3"


def _get(path, **params):
    for attempt in range(3):
        resp = requests.get(
            f"{API_BASE}/{path}",
            params=params,
            auth=(settings.WOOCOMMERCE_CONSUMER_KEY, settings.WOOCOMMERCE_CONSUMER_SECRET),
            timeout=30,
        )
        if resp.status_code == 200:
            return resp
        if resp.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    resp.raise_for_status()


def fetch_all_orders(date_str):
    """date_str: 'YYYY-MM-DD', pulls all orders placed that day (any status)."""
    orders = []
    page = 1
    after = f"{date_str}T00:00:00"
    before = f"{date_str}T23:59:59"
    while True:
        resp = _get("orders", after=after, before=before, per_page=100, page=page, orderby="date", order="asc")
        batch = resp.json()
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return orders


def fetch_product_categories():
    """Returns {product_id: [category names]}. Paginates through all products."""
    mapping = {}
    page = 1
    while True:
        resp = _get("products", per_page=100, page=page)
        batch = resp.json()
        if not batch:
            break
        for product in batch:
            mapping[product["id"]] = [c["name"] for c in product.get("categories", [])]
        if len(batch) < 100:
            break
        page += 1
    return mapping


def build_report(date_str):
    rules = settings.load_category_rules()
    fallback_rate = rules.get("vat_fallback_rate", 0.11)
    merge_map = {}
    for dest, sources in (rules.get("woocommerce_merge_into") or {}).items():
        for src in sources:
            merge_map[src] = dest
    exclude = set(rules.get("woocommerce_exclude") or [])

    orders = fetch_all_orders(date_str)
    product_categories = fetch_product_categories()

    sale_orders = [o for o in orders if o["status"] in SALE_STATUSES]

    total_incl = total_excl = total_vat = 0.0
    categories = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0, "excl_vat": 0.0, "vat": 0.0})
    items = defaultdict(lambda: {"qty": 0, "incl_vat": 0.0})

    for order in sale_orders:
        order_tax = float(order.get("total_tax") or 0)
        order_uses_fallback = order_tax <= 0

        for item in order.get("line_items", []):
            line_total = float(item.get("total") or 0)  # excl. tax per WooCommerce convention
            line_tax = float(item.get("total_tax") or 0)

            if order_uses_fallback:
                incl = line_total  # assume price already included VAT, tax just wasn't recorded
                excl = incl / (1 + fallback_rate)
                vat = incl - excl
            else:
                excl = line_total
                vat = line_tax
                incl = excl + vat

            total_incl += incl
            total_excl += excl
            total_vat += vat

            item_bucket = items[item.get("name", "Unknown")]
            item_bucket["qty"] += item.get("quantity", 0)
            item_bucket["incl_vat"] += incl

            cats = product_categories.get(item.get("product_id"), []) or ["Uncategorized"]
            cat_name = cats[0]
            cat_name = merge_map.get(cat_name, cat_name)
            if cat_name in exclude:
                continue
            bucket = categories[cat_name]
            bucket["qty"] += item.get("quantity", 0)
            bucket["incl_vat"] += incl
            bucket["excl_vat"] += excl
            bucket["vat"] += vat

    return {
        "date": date_str,
        "source": "woocommerce",
        "order_count": len(sale_orders),
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
        print("Usage: python woocommerce_pull.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    import json

    print(json.dumps(build_report(date_arg), indent=2))
