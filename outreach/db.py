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
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT,
    company TEXT,
    title TEXT,
    linkedin_url TEXT,
    domain TEXT,
    candidate_email TEXT,
    final_email TEXT,
    email_pattern TEXT,
    confidence_score INTEGER DEFAULT 0,
    confidence_level TEXT DEFAULT 'unresolved',
    email_status TEXT DEFAULT 'new',
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    mx_valid INTEGER DEFAULT 0,
    catch_all INTEGER DEFAULT 0,
    role_email INTEGER DEFAULT 0,
    verification_provider TEXT DEFAULT '',
    verification_result TEXT DEFAULT '',
    provenance TEXT DEFAULT '[]',
    created_at TEXT,
    updated_at TEXT,
    last_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_prospects_final_email ON prospects(final_email);
CREATE INDEX IF NOT EXISTS idx_prospects_domain ON prospects(domain);
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(email_status);
CREATE INDEX IF NOT EXISTS idx_prospects_full_name_company ON prospects(full_name, company);

CREATE TABLE IF NOT EXISTS prospect_domain_cache (
    domain TEXT PRIMARY KEY,
    detected_pattern TEXT,
    pattern_confidence REAL DEFAULT 0.0,
    known_emails TEXT DEFAULT '[]',
    mx_valid INTEGER DEFAULT 0,
    is_catch_all INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS provider_credits (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    credits_used INTEGER DEFAULT 1,
    request_timestamp TEXT NOT NULL,
    result TEXT DEFAULT '',
    prospect_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_credits_provider_time ON provider_credits(provider, request_timestamp);
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
            "messages": ["provider_id TEXT", "approved_by TEXT DEFAULT ''", "hold TEXT DEFAULT ''",
                         "override INTEGER DEFAULT 0", "attempts INTEGER DEFAULT 0"],
            "replies": ["plan TEXT DEFAULT ''"],
            "provider_credits": ["lookup_key TEXT DEFAULT ''"],
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


# --------------------------------------------------------------------------- Prospects Helpers

def find_prospect_duplicate(
    conn,
    email: str | None = None,
    linkedin_url: str | None = None,
    full_name: str | None = None,
    company: str | None = None,
    domain: str | None = None
) -> dict | None:
    """Check for existing duplicate prospect by linkedin, email, name+company, or name+domain."""
    if linkedin_url and linkedin_url.strip():
        clean_li = linkedin_url.strip().lower().rstrip("/")
        row = conn.execute("SELECT * FROM prospects WHERE lower(rtrim(linkedin_url, '/')) = ?", (clean_li,)).fetchone()
        if row:
            return dict(row)

    if email and email.strip():
        clean_email = email.strip().lower()
        row = conn.execute("SELECT * FROM prospects WHERE lower(final_email) = ?", (clean_email,)).fetchone()
        if row:
            return dict(row)

    if full_name and full_name.strip():
        name_clean = full_name.strip().lower()
        if domain and domain.strip():
            row = conn.execute(
                "SELECT * FROM prospects WHERE lower(full_name) = ? AND lower(domain) = ?",
                (name_clean, domain.strip().lower())
            ).fetchone()
            if row:
                return dict(row)
        if company and company.strip():
            row = conn.execute(
                "SELECT * FROM prospects WHERE lower(full_name) = ? AND lower(company) = ?",
                (name_clean, company.strip().lower())
            ).fetchone()
            if row:
                return dict(row)

    return None


def add_prospect(conn, **fields) -> int:
    """Insert a new prospect and return its row ID."""
    t = now()
    if "created_at" not in fields:
        fields["created_at"] = t
    if "updated_at" not in fields:
        fields["updated_at"] = t
    if "last_checked_at" not in fields:
        fields["last_checked_at"] = t
    cols = ", ".join(fields)
    conn.execute(
        f"INSERT INTO prospects ({cols}) VALUES ({', '.join('?' * len(fields))})",
        tuple(fields.values())
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_prospect(conn, prospect_id: int, **fields) -> None:
    """Update fields on an existing prospect record."""
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE prospects SET {cols} WHERE id = ?", (*fields.values(), prospect_id))


def get_prospect(conn, prospect_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return dict(row) if row else None


def list_prospects(
    conn,
    status: str | None = None,
    confidence_level: str | None = None,
    company: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    clauses, args = [], []
    if status:
        clauses.append("email_status = ?")
        args.append(status)
    if confidence_level:
        clauses.append("confidence_level = ?")
        args.append(confidence_level)
    if company:
        clauses.append("lower(company) LIKE ?")
        args.append(f"%{company.lower().strip()}%")
    if search:
        s = f"%{search.lower().strip()}%"
        clauses.append("(lower(full_name) LIKE ? OR lower(company) LIKE ? OR lower(domain) LIKE ? OR lower(final_email) LIKE ?)")
        args.extend([s, s, s, s])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM prospects {where} ORDER BY confidence_score DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    rows = conn.execute(query, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def count_prospects(conn, status: str | None = None) -> int:
    if status:
        row = conn.execute("SELECT count(*) FROM prospects WHERE email_status = ?", (status,)).fetchone()
    else:
        row = conn.execute("SELECT count(*) FROM prospects").fetchone()
    return int(row[0]) if row else 0


def get_prospect_domain_cache(conn, domain: str) -> dict | None:
    dom = domain.strip().lower().removeprefix("www.")
    row = conn.execute("SELECT * FROM prospect_domain_cache WHERE domain = ?", (dom,)).fetchone()
    return dict(row) if row else None


def set_prospect_domain_cache(conn, domain: str, **fields) -> None:
    dom = domain.strip().lower().removeprefix("www.")
    fields["domain"] = dom
    fields["updated_at"] = now()
    cols = ", ".join(fields)
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields if k != "domain")
    conn.execute(
        f"INSERT INTO prospect_domain_cache ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(domain) DO UPDATE SET {updates}",
        tuple(fields.values())
    )


def get_prospecting_stats(conn) -> dict:
    """Return summary statistics and KPIs for the prospecting engine."""
    total = count_prospects(conn)
    counts_by_status = {}
    for row in conn.execute("SELECT email_status, count(*) FROM prospects GROUP BY email_status"):
        counts_by_status[row[0] or "unverified"] = int(row[1])

    counts_by_confidence = {}
    for row in conn.execute("SELECT confidence_level, count(*) FROM prospects GROUP BY confidence_level"):
        counts_by_confidence[row[0] or "unresolved"] = int(row[1])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row_today = conn.execute(
        "SELECT count(*) FROM prospects WHERE email_status IN ('verified', 'high_confidence') "
        "AND (date(created_at) = ? OR date(last_checked_at) = ?)",
        (today, today)
    ).fetchone()
    verified_today = int(row_today[0]) if row_today else 0

    start_month = f"{datetime.now(timezone.utc).strftime('%Y-%m')}-01T00:00:00Z"
    credits_used = {}
    for row in conn.execute(
        "SELECT provider, COALESCE(sum(credits_used), 0) FROM provider_credits WHERE request_timestamp >= ? GROUP BY provider",
        (start_month,)
    ):
        credits_used[row[0]] = int(row[1])

    return {
        "total": total,
        "verified_today": verified_today,
        "counts_by_status": counts_by_status,
        "counts_by_confidence": counts_by_confidence,
        "credits_used": credits_used,
    }

