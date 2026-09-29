"""SQLite state: leads, the messages of each lead's sequence, replies, and daily send counts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    first_name TEXT, last_name TEXT, title TEXT,
    company TEXT, website TEXT, domain TEXT,
    country TEXT, city TEXT, linkedin TEXT,
    segment TEXT NOT NULL,
    source TEXT, notes TEXT,
    email_status TEXT DEFAULT 'unchecked',   -- unchecked | valid | risky | invalid | guessed
    signals TEXT DEFAULT '{}',               -- JSON from enrich
    site_text TEXT DEFAULT '',
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',
    -- new -> enriched -> verified -> drafted -> approved -> active
    --   -> replied | bounced | unsubscribed | finished | rejected | invalid
    inbox TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    step INTEGER NOT NULL,                   -- 0 = first email, 1..3 = follow-ups
    subject TEXT, body TEXT,
    status TEXT DEFAULT 'draft',             -- draft | approved | sent | cancelled | failed
    confidence REAL, review_note TEXT,
    due_at TEXT, sent_at TEXT,
    message_id TEXT, inbox TEXT, error TEXT,
    UNIQUE(lead_id, step)
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    inbox TEXT, imap_uid TEXT, message_id TEXT UNIQUE,
    from_addr TEXT, subject TEXT, body TEXT, received_at TEXT,
    category TEXT, summary TEXT, suggested_reply TEXT,
    handled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS send_log (
    day TEXT, inbox TEXT, kind TEXT, count INTEGER DEFAULT 0,
    PRIMARY KEY (day, inbox, kind)
);
CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY, reason TEXT, added_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def set_lead(conn, lead_id: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))


def signals(row) -> dict:
    try:
        return json.loads(row["signals"] or "{}")
    except json.JSONDecodeError:
        return {}


def suppressed(conn, email: str) -> bool:
    email = email.lower()
    domain = email.split("@")[-1]
    return conn.execute(
        "SELECT 1 FROM suppression WHERE email IN (?, ?)", (email, "@" + domain)
    ).fetchone() is not None


def suppress(conn, email: str, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO suppression (email, reason, added_at) VALUES (?, ?, ?)",
        (email.lower(), reason, now()),
    )


def bump_send_count(conn, day: str, inbox: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO send_log (day, inbox, kind, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(day, inbox, kind) DO UPDATE SET count = count + 1",
        (day, inbox, kind),
    )


def send_count(conn, day: str, inbox: str, kind: str | None = None) -> int:
    if kind:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM send_log WHERE day=? AND inbox=? AND kind=?",
            (day, inbox, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM send_log WHERE day=? AND inbox=?",
            (day, inbox),
        ).fetchone()
    return row[0]
