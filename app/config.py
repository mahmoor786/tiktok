"""Paths, environment and fixed lists."""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
UK = ZoneInfo("Europe/London")


def load_env(path: Path) -> None:
    """Tiny .env reader: KEY=VALUE lines, existing environment wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_env(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("TRENDPACK_DATA", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trendpack.db"
DEMO = os.environ.get("TRENDPACK_DEMO") == "1"


def app_password() -> str:
    return os.environ.get("APP_PASSWORD", "").strip()


def secret_key() -> str:
    """Signing key for the login cookie. Created once and kept in data/."""
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = DATA_DIR / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip()


CATEGORIES = [
    {"id": "home", "label": "Home & living", "terms": "home decor, furniture, storage and cosy living"},
    {"id": "seasonal", "label": "Seasonal & gifts", "terms": ""},  # terms come from the month
    {"id": "kitchen", "label": "Kitchen", "terms": "kitchen gadgets, organisers and small appliances"},
    {"id": "beauty", "label": "Beauty", "terms": "beauty, skincare and haircare"},
    {"id": "gadgets", "label": "Gadgets", "terms": "gadgets, tech accessories and useful tools"},
    {"id": "fashion", "label": "Fashion", "terms": "women's fashion and accessories"},
    {"id": "kids", "label": "Kids & toys", "terms": "kids, toys and nursery"},
    {"id": "pets", "label": "Pets", "terms": "pet products"},
    {"id": "fitness", "label": "Fitness", "terms": "fitness and home workout"},
    {"id": "any", "label": "Anything trending", "terms": ""},
]

# What UK shoppers are buying around each month. Used to steer the research.
SEASONS = {
    1: "January reset: home organisation, fitness, cosy winter, Valentine's gifts",
    2: "Valentine's Day gifts, Mother's Day (UK, March) gifts, spring cleaning",
    3: "Mother's Day (UK) gifts, Easter, spring cleaning, garden",
    4: "Easter, garden, spring home refresh",
    5: "garden, bank holiday, summer holiday prep",
    6: "Father's Day gifts, summer, holidays, festivals",
    7: "summer holidays, travel, garden",
    8: "back to school, late summer, autumn prep",
    9: "back to school, autumn cosy home, early Halloween",
    10: "Halloween, autumn cosy home, early Christmas gifts",
    11: "Black Friday deals, Christmas gifts, Christmas decorations, winter cosy home",
    12: "Christmas gifts, last-minute gifts, Christmas decorations, New Year",
}


def category(cat_id: str) -> dict:
    for c in CATEGORIES:
        if c["id"] == cat_id:
            return c
    return CATEGORIES[-1]


def uk_now() -> datetime:
    return datetime.now(UK)


def season_hint(now: datetime | None = None) -> str:
    return SEASONS[(now or uk_now()).month]


DEFAULT_SETTINGS = {
    "gemini_key": "",
    "tavily_key": "",
    "model_main": "",       # picked automatically from the key's model list
    "model_lite": "",
    "grounding_model": "",  # set by the connection test if Google Search works on this key
    "search_provider": "auto",  # auto | tavily | gemini
    "clip_seconds": "8",
    "scenes": "1",          # 1 = one prompt, one clip, which is the whole video
    "default_category": "home",
    "auto_daily": "0",
    "auto_time": "07:30",
    "last_auto_date": "",
    "account_notes": (
        "Home and seasonal products do best on this account (a Christmas wreath, "
        "a comfy chair and curtain fairy lights were the top videos; the chair made sales). "
        "Clothing videos got very few views. Videos of an AI presenter standing next to "
        "the product mostly stalled at 100-400 views, so show the product in use instead."
    ),
}

SECRET_SETTINGS = {"gemini_key", "tavily_key"}
INTERNAL_SETTINGS = {"last_auto_date"}
