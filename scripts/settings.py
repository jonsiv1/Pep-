"""Central config: non-secret values live here, secrets come from the environment
(populated from GitHub Actions encrypted secrets in CI, or a local .env for testing).
"""
import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
HISTORY_FILE = DATA_DIR / "history.json"
CATEGORIES_FILE = REPO_ROOT / "config" / "categories.yaml"

# --- WooCommerce ---
WOOCOMMERCE_URL = os.environ.get("WOOCOMMERCE_URL", "").rstrip("/")
WOOCOMMERCE_CONSUMER_KEY = os.environ.get("WOOCOMMERCE_CONSUMER_KEY", "")
WOOCOMMERCE_CONSUMER_SECRET = os.environ.get("WOOCOMMERCE_CONSUMER_SECRET", "")

# --- DineOut --- (login URL isn't secret, it lives in config/dineout_selectors.json)
DINEOUT_USERNAME = os.environ.get("DINEOUT_USERNAME", "")
DINEOUT_PASSWORD = os.environ.get("DINEOUT_PASSWORD", "")

# --- Email (SMTP) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USERNAME)
EMAIL_TO = [addr.strip() for addr in os.environ.get("EMAIL_TO", "").split(",") if addr.strip()]

# Report is generated for "yesterday" in this timezone (Iceland has no DST,
# always UTC+0 year-round).
REPORT_TIMEZONE = "Atlantic/Reykjavik"


def load_category_rules():
    with open(CATEGORIES_FILE) as f:
        return yaml.safe_load(f)
