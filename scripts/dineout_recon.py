"""One-off reconnaissance of DineOut Partner pages, to design integrations
on facts rather than guesses. Current probe: the ORDERS report
(/pos/reports/orders), which will power a cross-check failsafe against
settlement misattribution (unsettled tills lumping two days together).

Captures, with auth material and query strings redacted (public repo):
- every JSON API request/response shape while the orders page loads
- POST request body shapes (these carry the date-filter parameters)
- a screenshot of the page and its table structure

Findings land in dineout_debug/recon.json; the workflow uploads that
directory as an artifact. Read-only.
"""
import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import settings
from dineout_scrape import DEBUG_DIR, _save_debug, load_selectors

ORDERS_URL = "https://partner.dineout.is/pos/reports/orders"

findings = {"network": []}


def describe_shape(obj, depth=0):
    """Structure only - key names and types, no values (public artifact)."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: describe_shape(v, depth + 1) for k, v in list(obj.items())[:30]}
    if isinstance(obj, list):
        return [describe_shape(obj[0], depth + 1)] + [f"... {len(obj)} items"] if obj else []
    return type(obj).__name__


def log_response(response):
    try:
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype:
            return
        url = urlparse(response.url)
        if "dineout" not in url.netloc:
            return
        req = response.request
        entry = {
            "method": req.method,
            "url": f"{url.scheme}://{url.netloc}{url.path}",
            "had_query": bool(url.query),
            "status": response.status,
            "auth_headers_present": sorted(
                h for h in ("authorization", "cookie", "x-api-key")
                if req.headers.get(h)
            ),
        }
        if req.method == "POST" and req.post_data:
            try:
                entry["post_body_shape"] = describe_shape(json.loads(req.post_data))
            except Exception:
                entry["post_body_shape"] = "non-json"
        try:
            entry["json_shape"] = describe_shape(response.json())
        except Exception:
            pass
        findings["network"].append(entry)
    except Exception:
        pass


def main():
    sel = load_selectors()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on("response", log_response)

        try:
            page.goto(sel["login_url"], wait_until="domcontentloaded")
            email_field = page.get_by_label(sel["username_label"])
            email_field.wait_for(state="visible", timeout=45000)
            email_field.fill(settings.DINEOUT_USERNAME)
            page.get_by_label(sel["password_label"]).fill(settings.DINEOUT_PASSWORD)
            page.click(sel["submit_selector"])
            page.wait_for_selector(sel["login_success_selector"], timeout=20000)
            findings["login"] = "ok"

            page.goto(ORDERS_URL)
            page.wait_for_timeout(6000)  # let the report and its XHRs settle
            findings["orders_page_url"] = page.url

            # describe visible table structure, if any
            headers = [h.inner_text().strip() for h in page.query_selector_all("thead th")]
            findings["orders_table_headers"] = headers
            rows = page.query_selector_all("table tbody tr")
            findings["orders_row_count_visible"] = len(rows)
            date_inputs = page.query_selector_all("input[type='date'], input[placeholder*='date' i], .MuiInputBase-input")
            findings["input_count_on_page"] = len(date_inputs)

            _save_debug(page, "recon_orders")
        except Exception as exc:
            findings["fatal"] = str(exc)[:500]
            _save_debug(page, "recon_fatal")
            raise
        finally:
            DEBUG_DIR.mkdir(exist_ok=True)
            with open(DEBUG_DIR / "recon.json", "w") as f:
                json.dump(findings, f, indent=2)
            browser.close()


if __name__ == "__main__":
    main()
