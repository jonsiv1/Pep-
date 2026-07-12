"""One-off reconnaissance of the DineOut settlements list, to design the
historical backfill on facts rather than guesses. Answers:

1. Can rows-per-page be raised (MUI TablePagination combobox), and to what?
2. Does "Go to next page" (aria-label) work, and what does page 2 show?
3. After opening a settlement and going back, is pagination state preserved
   or reset to page 1? (Determines whether a simple walk works or every
   settlement needs re-navigation.)
4. What API endpoints feed the list and the PDF? (URLs logged with query
   strings and auth material redacted - the repo is public.)

Writes findings to dineout_debug/recon.json plus screenshots; the workflow
uploads that directory as an artifact. Read-only apart from one settlement
PDF download.
"""
import json
import re
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import settings
from dineout_scrape import DEBUG_DIR, _save_debug, load_selectors

findings = {"network": []}


def log_response(response):
    try:
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype and "pdf" not in ctype:
            return
        url = urlparse(response.url)
        if "dineout" not in url.netloc:
            return
        req = response.request
        entry = {
            "method": req.method,
            # path only - query strings can carry signed tokens
            "url": f"{url.scheme}://{url.netloc}{url.path}",
            "had_query": bool(url.query),
            "status": response.status,
            "content_type": ctype.split(";")[0],
            "auth_headers_present": sorted(
                h for h in ("authorization", "cookie", "x-api-key")
                if req.headers.get(h)
            ),
        }
        if "json" in ctype and req.method == "GET":
            try:
                body = response.json()
                entry["json_shape"] = describe_shape(body)
            except Exception:
                pass
        findings["network"].append(entry)
    except Exception:
        pass


def describe_shape(obj, depth=0):
    """Structure only - key names and types, no values (public artifact)."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: describe_shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [describe_shape(obj[0], depth + 1)] + [f"... {len(obj)} items"] if obj else []
    return type(obj).__name__


def displayed_rows_text(page):
    el = page.query_selector(".MuiTablePagination-displayedRows")
    return el.inner_text().strip() if el else None


def first_row_summary(page, sel):
    rows = page.query_selector_all(sel["list_row_selector"])
    if not rows:
        return {"row_count": 0}
    cells = rows[0].query_selector_all(sel["list_cell_selector"])
    return {
        "row_count": len(rows),
        "first_row_date": cells[0].inner_text().strip() if cells else None,
        "first_row_time": cells[1].inner_text().strip() if len(cells) > 1 else None,
    }


def main():
    sel = load_selectors()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
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

            page.goto(sel["settlements_list_url"])
            page.wait_for_selector(sel["list_row_selector"], timeout=20000)
            findings["page1_initial"] = first_row_summary(page, sel)
            findings["displayed_initial"] = displayed_rows_text(page)

            # 1: try raising rows-per-page via the MUI combobox
            try:
                page.click(".MuiTablePagination-select")
                page.wait_for_selector("li[role='option']", timeout=5000)
                options = [o.inner_text().strip() for o in page.query_selector_all("li[role='option']")]
                findings["rows_per_page_options"] = options
                best = max((o for o in options if o.isdigit()), key=int, default=None)
                if best and best != "10":
                    page.click(f"li[role='option']:has-text('{best}')")
                    page.wait_for_timeout(2500)
                    findings["rows_per_page_set_to"] = best
                    findings["page1_after_resize"] = first_row_summary(page, sel)
                    findings["displayed_after_resize"] = displayed_rows_text(page)
            except Exception as exc:
                findings["rows_per_page_error"] = str(exc)[:300]

            # 2: next page via aria-label
            try:
                page.click("button[aria-label='Go to next page']")
                page.wait_for_timeout(2500)
                findings["page2"] = first_row_summary(page, sel)
                findings["displayed_page2"] = displayed_rows_text(page)
            except Exception as exc:
                findings["next_page_error"] = str(exc)[:300]

            # 3: open first settlement on page 2, download PDF, go back -
            # is pagination state preserved?
            try:
                rows = page.query_selector_all(sel["list_row_selector"])
                open_button = rows[0].query_selector(sel["list_open_link_selector"])
                open_button.click()
                page.wait_for_selector(sel["download_button_selector"], timeout=20000)
                findings["detail_url_path"] = urlparse(page.url).path
                with page.expect_download() as dl_info:
                    page.click(sel["download_button_selector"])
                findings["pdf_download_filename"] = dl_info.value.suggested_filename

                page.go_back()
                page.wait_for_selector(sel["list_row_selector"], timeout=20000)
                page.wait_for_timeout(2000)
                findings["after_go_back"] = first_row_summary(page, sel)
                findings["displayed_after_go_back"] = displayed_rows_text(page)
            except Exception as exc:
                findings["open_settlement_error"] = str(exc)[:300]
                _save_debug(page, "recon_open_failure")

            _save_debug(page, "recon_final_state")
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
