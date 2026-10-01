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
    opportunity_type TEXT DEFAULT 'contract', -- contract | internship
    source TEXT, notes TEXT,
    source_text TEXT DEFAULT '',             -- the directory entry / job post the lead came from
    email_source TEXT DEFAULT '',            -- csv | website | osm | post | registry | maps | guess
    email_status TEXT DEFAULT 'unchecked',   -- unchecked | valid | risky | invalid | guessed
    signals TEXT DEFAULT '{}',               -- JSON from enrich
    site_text TEXT DEFAULT '',
    score INTEGER DEFAULT 0,
    research TEXT DEFAULT '',                -- JSON brief from the research step
    fit INTEGER,                             -- 0-10 from the research step
    linkedin_note TEXT DEFAULT '', linkedin_dm TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    -- new -> enriched -> verified -> researched -> drafted -> approved -> active
    --   -> replied | bounced | unsubscribed | finished | rejected | invalid | unfit
    inbox TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    step INTEGER NOT NULL,                   -- 0 = first email, 1..3 = follow-ups
    subject TEXT, body TEXT,
    status TEXT DEFAULT 'draft',             -- draft | approved | sending | needs_reconciliation | sent | cancelled | failed
    confidence REAL, review_note TEXT,
    due_at TEXT, sent_at TEXT,
    message_id TEXT, provider_id TEXT, inbox TEXT, error TEXT,
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
CREATE TABLE IF NOT EXISTS prospect_state (   -- where each paged source got to (so runs don't repeat)
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS posts (            -- community "[Hiring]" posts you answer by hand, fast
    id INTEGER PRIMARY KEY,
    source TEXT, ext_id TEXT, title TEXT, url TEXT, author TEXT, body TEXT,
    posted_at TEXT, found_at TEXT,
    relevant INTEGER, reason TEXT, draft_reply TEXT,
    status TEXT DEFAULT 'new',               -- new | notified | done
    UNIQUE(source, ext_id)
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
        # Add columns introduced after a database was first created.
        for table, cols in {
            "leads": ["source_text TEXT DEFAULT ''", "email_source TEXT DEFAULT ''",
                      "research TEXT DEFAULT ''", "fit INTEGER",
                      "linkedin_note TEXT DEFAULT ''", "linkedin_dm TEXT DEFAULT ''",
                      "angle TEXT DEFAULT ''", "deal_stage TEXT DEFAULT ''", "deal_value REAL",
                      "deal_note TEXT DEFAULT ''", "deal_updated TEXT",
                      "deal_currency TEXT DEFAULT 'USD'", "deal_next_action TEXT DEFAULT ''",
                      "deal_next_due TEXT DEFAULT ''", "opportunity_type TEXT DEFAULT 'contract'"],
            "messages": ["provider_id TEXT"],
            "replies": ["plan TEXT DEFAULT ''"],
        }.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col.split()[0] not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")


def set_lead(conn, lead_id: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))


def signals(row) -> dict:
    try:
        return json.loads(row["signals"] or "{}")
    except json.JSONDecodeError:
        return {}


def research(row) -> dict:
    try:
        return json.loads(row["research"] or "{}")
    except json.JSONDecodeError:
        return {}


def add_lead(conn, **fields) -> bool:
    """Insert a lead unless its email or company domain is already known or suppressed."""
    email = (fields.get("email") or "").lower() or None
    domain = fields.get("domain") or ""
    if email and suppressed(conn, email):
        return False
    if domain and (suppressed(conn, "x@" + domain) or
                   conn.execute("SELECT 1 FROM leads WHERE domain = ?", (domain,)).fetchone()):
        return False
    if email and conn.execute("SELECT 1 FROM leads WHERE email = ?", (email,)).fetchone():
        return False
    if "opportunity_type" not in fields:
        seg = fields.get("segment", "")
        fields["opportunity_type"] = "internship" if "intern" in seg else "contract"
    fields.update(email=email, created_at=now(), updated_at=now())
    cols = ", ".join(fields)
    conn.execute(f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(fields))})",
                 tuple(fields.values()))
    return True


MOCK_MARK = "[MOCK"


def is_mock_brief(raw: str) -> bool:
    try:
        b = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return False
    return b.get("fit_reason") == "mock" and str(b.get("company_summary", "")).endswith("(mock)")


def purge_mock(conn) -> int:
    """Undo `--mock` output that reached this database: placeholder drafts are deleted and their leads go
    back to 'enriched', so real verification, research and drafting run on them. Sent mail is never touched."""
    ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT lead_id FROM messages WHERE status IN ('draft','approved','cancelled') "
        "AND (review_note='mock draft' OR body LIKE '%[MOCK%')")}
    ids |= {r["id"] for r in conn.execute("SELECT id, research FROM leads WHERE research LIKE '%mock%'")
            if is_mock_brief(r["research"])}
    for lead_id in ids:
        conn.execute("DELETE FROM messages WHERE lead_id=? AND status IN ('draft','approved','cancelled')", (lead_id,))
        conn.execute("UPDATE leads SET status='enriched', research='', fit=NULL, email_status='unchecked', "
                     "linkedin_note='', linkedin_dm='', updated_at=? WHERE id=? AND status IN "
                     "('researched','drafted','approved','unfit','verified')", (now(), lead_id))
    return len(ids)


def get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM prospect_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO prospect_state (key, value) VALUES (?, ?)", (key, str(value)))


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
