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

-- Follow-up sequences: ordered steps, each a template sent N days after the previous one.
CREATE TABLE IF NOT EXISTS sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',       -- json [{"template_id": 1, "delay_days": 0}, ...]
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    step INTEGER NOT NULL DEFAULT 0,        -- index of the next step to send
    status TEXT NOT NULL DEFAULT 'active',  -- active|done|stopped
    stop_reason TEXT NOT NULL DEFAULT '',
    next_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (lead_id, sequence_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                     -- report_view|reply|bounce|status|...
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);

CREATE TABLE IF NOT EXISTS inbox_seen (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release. init_db() adds any that are missing,
# so existing databases upgrade in place.
MIGRATIONS = {
    "leads": {
        "tech": "TEXT NOT NULL DEFAULT '[]'",
        "audit": "TEXT NOT NULL DEFAULT '{}'",
        "email_status": "TEXT NOT NULL DEFAULT ''",   # valid|invalid|unknown
        "report_token": "TEXT",
        "report_views": "INTEGER NOT NULL DEFAULT 0",
        "last_activity_at": "TEXT",
    },
    "messages": {
        "enrollment_id": "INTEGER",
        "message_id": "TEXT NOT NULL DEFAULT ''",
    },
}

DEFAULT_TEMPLATES = [
    (
        "Free website check (with report link)",
        "I checked {{business_name}}'s website",
        "Hi {{business_name}} team,\n\n"
        "I was looking at {{category_plural}}{{ ' in ' + city if city else '' }} and ran a quick check of your online presence. "
        "{{signal_line}}\n\n"
        "I put the results on a 1-page report for you (free, no sign-up): {{report_link}}\n\n"
        "We help {{category_plural}} fix exactly these things with {{services}}. "
        "Happy to walk you through it in 15 minutes{{ ' - you can pick a time here: ' + calendar_url if calendar_url else '' }}.\n\n"
        "Best,\n{{my_name}}\n{{my_company}}",
    ),
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
    (
        "Last check-in",
        "Should I close your file, {{business_name}}?",
        "Hi,\n\nI haven't heard back, so I'll assume the timing isn't right and won't email again. "
        "If things change, your free report stays here: {{report_link}}\n\nAll the best,\n{{my_name}}",
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
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_report ON leads(report_token)")
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO templates (name, subject, body, created_at) VALUES (?, ?, ?, ?)",
                [(n, s, b, now()) for n, s, b in DEFAULT_TEMPLATES],
            )
        if conn.execute("SELECT COUNT(*) FROM sequences").fetchone()[0] == 0:
            ids = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM templates")}
            first = ids.get("Free website check (with report link)") or min(ids.values(), default=None)
            steps = [{"template_id": first, "delay_days": 0}]
            if ids.get("Short follow-up"):
                steps.append({"template_id": ids["Short follow-up"], "delay_days": 3})
            if ids.get("Last check-in"):
                steps.append({"template_id": ids["Last check-in"], "delay_days": 7})
            if first:
                conn.execute("INSERT INTO sequences (name, steps, created_at) VALUES (?, ?, ?)",
                             ("3-step: report, follow-up, last check-in", json.dumps(steps), now()))


JSON_FIELDS = {"emails": list, "phones": list, "socials": dict, "signals": list, "sources": list,
               "tech": list, "audit": dict, "steps": list}


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
                    signals: list[str], address: str, score: int, name: str = "",
                    tech: list[str] | None = None, audit: dict | None = None, email_status: str = "") -> None:
    execute(
        "UPDATE leads SET emails=?, phones=?, socials=?, signals=?, address=CASE WHEN address='' THEN ? ELSE address END, "
        "score=?, enriched_at=?, name=CASE WHEN ?<>'' THEN ? ELSE name END, tech=?, audit=?, email_status=? WHERE id=?",
        (json.dumps(emails), json.dumps(phones), json.dumps(socials), json.dumps(signals), address, score, now(),
         name, name, json.dumps(tech or []), json.dumps(audit or {}), email_status, lead_id),
    )


def save_lead_enrichment(lead: dict) -> None:
    save_enrichment(lead["id"], lead["emails"], lead["phones"], lead["socials"], lead["signals"],
                    lead.get("address", ""), lead["score"], lead.get("name", ""), lead.get("tech"),
                    lead.get("audit"), lead.get("email_status", ""))


def log_event(lead_id: int | None, kind: str, detail: str = "") -> None:
    execute("INSERT INTO events (lead_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, kind, detail[:2000], now()))
    if lead_id:
        execute("UPDATE leads SET last_activity_at = ? WHERE id = ?", (now(), lead_id))


def is_suppressed(email: str) -> bool:
    return fetch_one("SELECT 1 AS x FROM suppression WHERE email = ?", (email.lower(),)) is not None


def suppress(email: str, reason: str) -> None:
    execute("INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?, ?, ?)",
            (email.lower(), reason, now()))
