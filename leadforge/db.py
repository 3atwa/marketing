"""SQLite storage. One file, no server, safe for a single-process app."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    services TEXT NOT NULL DEFAULT '',
    targets TEXT NOT NULL DEFAULT '',      -- comma separated business types
    locations TEXT NOT NULL DEFAULT '',    -- one per line
    sources TEXT NOT NULL DEFAULT '[]',    -- json list
    max_per_query INTEGER NOT NULL DEFAULT 40,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft|running|done|failed
    progress TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    emails TEXT NOT NULL DEFAULT '[]',
    phones TEXT NOT NULL DEFAULT '[]',
    socials TEXT NOT NULL DEFAULT '{}',
    signals TEXT NOT NULL DEFAULT '[]',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    rating REAL,
    review_count INTEGER,
    source TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',     -- new|contacted|replied|interested|won|lost|do_not_contact
    notes TEXT NOT NULL DEFAULT '',
    enriched_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (campaign_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,                  -- email|whatsapp|call
    recipient TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',   -- draft|queued|sent|failed|skipped
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);

CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

DEFAULT_TEMPLATES = [
    (
        "Intro - app for your business",
        "Quick idea for {{business_name}}",
        "Hi {{business_name}} team,\n\n"
        "I came across {{business_name}}{{ ' in ' + city if city else '' }} and wanted to reach out. "
        "{{signal_line}}\n\n"
        "What we offer {{category_plural}}: {{services}}.\n\n"
        "Would you be open to a 15-minute call next week? I'm happy to show a short demo tailored to {{business_name}}.\n\n"
        "Best,\n{{my_name}}\n{{my_company}}",
    ),
    (
        "Short follow-up",
        "Re: Quick idea for {{business_name}}",
        "Hi again,\n\nJust floating this back to the top of your inbox. "
        "If {{services}} isn't a priority right now, no worries - just let me know and I won't follow up.\n\n"
        "Thanks,\n{{my_name}}",
    ),
]

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        with _lock:
            yield conn
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO templates (name, subject, body, created_at) VALUES (?, ?, ?, ?)",
                [(n, s, b, now()) for n, s, b in DEFAULT_TEMPLATES],
            )


JSON_FIELDS = {"emails": list, "phones": list, "socials": dict, "signals": list, "sources": list}


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, kind in JSON_FIELDS.items():
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except json.JSONDecodeError:
                data[key] = kind()
    return data


def fetch_all(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute(sql, params).fetchone())


def execute(sql: str, params: tuple | list = ()) -> int:
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


# ---------------------------------------------------------------- campaigns

def create_campaign(name: str, services: str, targets: str, locations: str,
                    sources: list[str], max_per_query: int) -> int:
    return execute(
        "INSERT INTO campaigns (name, services, targets, locations, sources, max_per_query, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, services, targets, locations, json.dumps(sources), max_per_query, now()),
    )


def update_campaign(campaign_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE campaigns SET {cols} WHERE id = ?", (*fields.values(), campaign_id))


def append_log(campaign_id: int, line: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET log = substr(log || ?, -20000), progress = ? WHERE id = ?",
            (f"[{stamp}] {line}\n", line, campaign_id),
        )


# ---------------------------------------------------------------- leads

def upsert_lead(campaign_id: int, lead: dict[str, Any]) -> int | None:
    """Insert a lead, or merge new contact details into an existing duplicate."""
    key = lead["dedupe_key"]
    with get_conn() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND dedupe_key = ?", (campaign_id, key)
        ).fetchone())
        if existing is None:
            cur = conn.execute(
                "INSERT INTO leads (campaign_id, dedupe_key, name, category, website, emails, phones, socials, "
                "signals, address, city, country, lat, lon, rating, review_count, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id, key, lead.get("name", ""), lead.get("category", ""), lead.get("website", ""),
                    json.dumps(lead.get("emails", [])), json.dumps(lead.get("phones", [])),
                    json.dumps(lead.get("socials", {})), json.dumps(lead.get("signals", [])),
                    lead.get("address", ""), lead.get("city", ""), lead.get("country", ""),
                    lead.get("lat"), lead.get("lon"), lead.get("rating"), lead.get("review_count"),
                    lead.get("source", ""), now(),
                ),
            )
            return cur.lastrowid

        merged = merge_lead(existing, lead)
        conn.execute(
            "UPDATE leads SET name=?, category=?, website=?, emails=?, phones=?, socials=?, address=?, city=?, "
            "country=?, lat=?, lon=?, rating=?, review_count=?, source=? WHERE id=?",
            (
                merged["name"], merged["category"], merged["website"], json.dumps(merged["emails"]),
                json.dumps(merged["phones"]), json.dumps(merged["socials"]), merged["address"], merged["city"],
                merged["country"], merged["lat"], merged["lon"], merged["rating"], merged["review_count"],
                merged["source"], existing["id"],
            ),
        )
        return existing["id"]


def merge_lead(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing)
    for k in ("name", "category", "website", "address", "city", "country", "lat", "lon", "rating", "review_count"):
        if not out.get(k) and new.get(k):
            out[k] = new[k]
    for k in ("emails", "phones"):
        out[k] = list(dict.fromkeys([*(out.get(k) or []), *(new.get(k) or [])]))
    out["socials"] = {**(new.get("socials") or {}), **(out.get("socials") or {})}
    sources = set(filter(None, (out.get("source") or "").split(","))) | set(filter(None, [new.get("source")]))
    out["source"] = ",".join(sorted(sources))
    return out


def save_enrichment(lead_id: int, emails: list[str], phones: list[str], socials: dict,
                    signals: list[str], address: str, score: int) -> None:
    execute(
        "UPDATE leads SET emails=?, phones=?, socials=?, signals=?, address=CASE WHEN address='' THEN ? ELSE address END, "
        "score=?, enriched_at=? WHERE id=?",
        (json.dumps(emails), json.dumps(phones), json.dumps(socials), json.dumps(signals), address, score, now(), lead_id),
    )


def is_suppressed(email: str) -> bool:
    return fetch_one("SELECT 1 AS x FROM suppression WHERE email = ?", (email.lower(),)) is not None


def suppress(email: str, reason: str) -> None:
    execute("INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?, ?, ?)",
            (email.lower(), reason, now()))
