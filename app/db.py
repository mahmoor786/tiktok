"""SQLite storage: settings, research sessions, packs and background jobs."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    products TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS packs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_id INTEGER,
    created_at TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    link_url TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    pack TEXT NOT NULL DEFAULT '{}',
    sources TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    posted INTEGER NOT NULL DEFAULT 0,
    views INTEGER,
    sales INTEGER,
    hook_used TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    results_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        # Anything still marked as running belongs to a previous process.
        conn.execute(
            "UPDATE jobs SET status='error', error='The server restarted while this was running. Start it again.' "
            "WHERE status IN ('queued','running')"
        )
        conn.execute(
            "UPDATE packs SET status='error', error='The server restarted while this pack was being made. Tap Try again.' "
            "WHERE status IN ('queued','working')"
        )
        conn.execute(
            "UPDATE research SET status='error', error='The server restarted during the search. Start a new one.' "
            "WHERE status IN ('queued','working')"
        )
    try:
        os.chmod(config.DB_PATH, 0o600)  # holds API keys
    except OSError:
        pass


# ---------- settings ----------

def get_settings() -> dict:
    out = dict(config.DEFAULT_SETTINGS)
    env_map = {"gemini_key": "GEMINI_API_KEY", "tavily_key": "TAVILY_API_KEY"}
    for key, env in env_map.items():
        if os.environ.get(env):
            out[key] = os.environ[env].strip()
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] in out and (row["value"] != "" or row["key"] not in env_map):
                out[row["key"]] = row["value"]
    return out


def set_settings(values: dict) -> None:
    with _lock, connect() as conn:
        for key, value in values.items():
            if key in config.DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )


# ---------- generic helpers ----------

def _row(row: sqlite3.Row | None, json_fields=()) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for field in json_fields:
        try:
            out[field] = json.loads(out.get(field) or "null")
        except json.JSONDecodeError:
            out[field] = None
    return out


def insert(table: str, values: dict) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with _lock, connect() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
        return int(cur.lastrowid)


def update(table: str, row_id: int, values: dict) -> None:
    if not values:
        return
    sets = ", ".join(f"{k}=?" for k in values)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*values.values(), row_id])


def delete(table: str, row_id: int) -> None:
    with _lock, connect() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


# ---------- research ----------

RESEARCH_JSON = ("products", "sources")
PACK_JSON = ("product", "pack", "sources")
JOB_JSON = ("ref",)


def get_research(research_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM research WHERE id=?", (research_id,)).fetchone(), RESEARCH_JSON)


def list_research(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, category, origin, status, error, products FROM research ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = _row(row, ("products",))
        item["count"] = len(item.pop("products") or [])
        out.append(item)
    return out


# ---------- packs ----------

def get_pack(pack_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone(), PACK_JSON)


def list_packs(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM packs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = _row(row, PACK_JSON)
        pack = item.pop("pack") or {}
        item.pop("sources", None)
        item.pop("product", None)
        item["format"] = (pack.get("format") or {}).get("name", "") if isinstance(pack, dict) else ""
        out.append(item)
    return out


def packs_with_results() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM packs WHERE status='ready' AND views IS NOT NULL ORDER BY id DESC LIMIT 60"
        ).fetchall()
    return [_row(r, PACK_JSON) for r in rows]


def recent_product_names(days: int = 30) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT product_name FROM packs WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 60",
            (f"-{int(days)} days",),
        ).fetchall()
    return [r["product_name"] for r in rows]


# ---------- jobs ----------

def get_job(job_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), JOB_JSON)


def active_jobs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id"
        ).fetchall()
    return [_row(r, JOB_JSON) for r in rows]
