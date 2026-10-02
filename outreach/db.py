"""SQLite state: leads, the messages of each lead's sequence, replies, and daily send counts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, turso

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
    score_total INTEGER DEFAULT 0,
    score_components TEXT DEFAULT '{}',
    score_version TEXT DEFAULT '',
    score_reason_summary TEXT DEFAULT '',
    score_timestamp TEXT DEFAULT '',
    verified_evidence TEXT DEFAULT '[]',
    email_verification_detail TEXT DEFAULT '{}',
    research TEXT DEFAULT '',                -- JSON brief from the research step
    fit INTEGER,                             -- 0-10 from the research step
    linkedin_note TEXT DEFAULT '', linkedin_dm TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    -- new -> enriched -> verified -> researched -> drafted -> approved -> active
    --   -> replied | bounced | unsubscribed | finished | rejected | invalid | unfit
    inbox TEXT,
    angle TEXT DEFAULT '',
    deal_stage TEXT DEFAULT '',
    deal_value REAL,
    deal_note TEXT DEFAULT '',
    deal_updated TEXT,
    deal_currency TEXT DEFAULT 'USD',
    deal_next_action TEXT DEFAULT '',
    deal_next_due TEXT DEFAULT '',
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
    approved_by TEXT DEFAULT '',
    hold TEXT DEFAULT '',
    override INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    idempotency_key TEXT,
    sending_at TEXT,
    UNIQUE(lead_id, step)
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    inbox TEXT, imap_uid TEXT, message_id TEXT UNIQUE,
    from_addr TEXT, subject TEXT, body TEXT, received_at TEXT,
    category TEXT, summary TEXT, suggested_reply TEXT,
    handled INTEGER DEFAULT 0,
    plan TEXT DEFAULT ''
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
    prospect_id INTEGER,
    lookup_key TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_credits_provider_time ON provider_credits(provider, request_timestamp);

CREATE TABLE IF NOT EXISTS message_outcomes (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    lead_source TEXT DEFAULT '',
    source_type TEXT DEFAULT '',
    mode TEXT NOT NULL,                  -- freelance | internship
    opportunity_score INTEGER DEFAULT 0,
    company_stage TEXT DEFAULT '',
    contact_role TEXT DEFAULT '',
    email_confidence INTEGER DEFAULT 0,
    evidence_confidence REAL DEFAULT 0.0,
    personalization_confidence REAL DEFAULT 0.0,
    message_angle TEXT DEFAULT '',
    cta_type TEXT DEFAULT '',
    subject_variant TEXT DEFAULT '',
    sequence_variant TEXT DEFAULT '',
    sent_at TEXT,
    outcome TEXT DEFAULT 'delivered',    -- delivered | bounced | replied | positive_reply | negative_reply | meeting | interview | project_discussion | offer | won_project | lost_project | no_response
    outcome_updated_at TEXT,
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outcomes_mode ON message_outcomes(mode);
CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON message_outcomes(outcome);
CREATE INDEX IF NOT EXISTS idx_outcomes_lead_msg ON message_outcomes(lead_id, message_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_wal_set: dict[str, bool] = {}


@contextmanager
def connect():
    if config.is_production():
        conn = turso.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Several processes share this file (engine run, inbox/assist/prepare jobs, dashboard). WAL lets reads
    # carry on while one process writes, and a writer waits up to 30 s for its turn instead of failing.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not _wal_set.get(str(path)):
        try:
            conn.execute("PRAGMA journal_mode=WAL")   # persistent; once per file per process is enough
            _wal_set[str(path)] = True
        except sqlite3.OperationalError:
            pass
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
                      "deal_next_due TEXT DEFAULT ''", "opportunity_type TEXT DEFAULT 'contract'",
                      "score_total INTEGER DEFAULT 0", "score_components TEXT DEFAULT '{}'",
                      "score_version TEXT DEFAULT ''", "score_reason_summary TEXT DEFAULT ''",
                      "score_timestamp TEXT DEFAULT ''", "verified_evidence TEXT DEFAULT '[]'",
                      "email_verification_detail TEXT DEFAULT '{}'"],
            "messages": ["provider_id TEXT", "approved_by TEXT DEFAULT ''", "hold TEXT DEFAULT ''",
                         "override INTEGER DEFAULT 0", "attempts INTEGER DEFAULT 0",
                         "idempotency_key TEXT DEFAULT ''", "sending_at TEXT DEFAULT ''"],
            "replies": ["plan TEXT DEFAULT ''"],
            "provider_credits": ["lookup_key TEXT DEFAULT ''"],
        }.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col.split()[0] not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        try:
            conn.execute("UPDATE messages SET idempotency_key = 'lead:' || lead_id || ':step:' || step "
                         "WHERE (idempotency_key IS NULL OR idempotency_key = '') AND lead_id IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency ON messages(idempotency_key)")
        except Exception:
            pass


def migrate_to_turso(local_path=None) -> dict[str, int]:
    """Copy all tables from local SQLite database into Turso production database."""
    from pathlib import Path
    path = Path(local_path) if local_path else config.db_path()
    if not path.exists():
        raise FileNotFoundError(f"Local database not found at {path}")
    counts: dict[str, int] = {}
    local_conn = sqlite3.connect(path)
    local_conn.row_factory = sqlite3.Row
    turso_conn = turso.connect()
    try:
        turso_conn.executescript(SCHEMA)
        tables = ["leads", "messages", "replies", "send_log", "suppression",
                  "prospect_state", "posts", "prospects", "prospect_domain_cache", "provider_credits",
                  "message_outcomes"]
        for table in tables:
            rows = local_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                counts[table] = 0
                continue
            turso_info = turso_conn.execute(f"PRAGMA table_info({table})").fetchall()
            turso_cols = {r["name"] for r in turso_info} if turso_info else set()
            all_cols = [d[0] for d in local_conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
            cols = [c for c in all_cols if not turso_cols or c in turso_cols]
            placeholders = ", ".join("?" * len(cols))
            col_names = ", ".join(cols)
            sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
            inserted = 0
            for r in rows:
                turso_conn.execute(sql, tuple(r[c] for c in cols))
                inserted += 1
            counts[table] = inserted
        turso_conn.commit()
    finally:
        local_conn.close()
        turso_conn.close()
    return counts


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


def get_daily_allocation_counts(conn, day: str) -> dict[str, int]:
    """Retrieve counts for total, freelance new, internship new, and follow-ups sent on the given day."""
    rows = conn.execute(
        "SELECT kind, COALESCE(SUM(count), 0) FROM send_log WHERE day=? GROUP BY kind",
        (day,)
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}
    fl = counts.get("first_freelance", 0)
    it = counts.get("first_internship", 0)
    fo = counts.get("followup", 0)

    # Fallback if first_freelance / first_internship are not partitioned in send_log
    first_total = counts.get("first", 0)
    if first_total > 0 and (fl + it == 0):
        try:
            fl_row = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN leads l ON l.id=m.lead_id "
                "WHERE m.status='sent' AND m.step=0 AND date(m.sent_at)=? "
                "AND (l.segment NOT LIKE '%intern%' AND COALESCE(l.opportunity_type, '') NOT IN ('internship', 'intern'))",
                (day,)
            ).fetchone()
            it_row = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN leads l ON l.id=m.lead_id "
                "WHERE m.status='sent' AND m.step=0 AND date(m.sent_at)=? "
                "AND (l.segment LIKE '%intern%' OR l.opportunity_type IN ('internship', 'intern'))",
                (day,)
            ).fetchone()
            fl = fl_row[0] if fl_row else 0
            it = it_row[0] if it_row else 0
        except Exception:
            pass
        if fl + it == 0:
            fl = first_total

    total = sum(counts.get(k, 0) for k in ("first", "followup")) or (fl + it + fo)
    return {
        "total": total,
        "freelance": fl,
        "internship": it,
        "followup": fo,
    }


def record_message_outcome(
    conn,
    message_id: int,
    lead_id: int,
    lead_source: str = "",
    source_type: str = "",
    mode: str = "freelance",
    opportunity_score: int = 0,
    company_stage: str = "",
    contact_role: str = "",
    email_confidence: int = 0,
    evidence_confidence: float = 0.0,
    personalization_confidence: float = 0.0,
    message_angle: str = "",
    cta_type: str = "",
    subject_variant: str = "",
    sequence_variant: str = "",
    sent_at: str | None = None,
    outcome: str = "delivered",
    notes: str = ""
) -> int:
    """Record a tracking outcome row for a sent message."""
    sent_time = sent_at or now()
    cur = conn.execute(
        """INSERT INTO message_outcomes (
            message_id, lead_id, lead_source, source_type, mode,
            opportunity_score, company_stage, contact_role, email_confidence,
            evidence_confidence, personalization_confidence, message_angle,
            cta_type, subject_variant, sequence_variant, sent_at, outcome,
            outcome_updated_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id, lead_id, lead_source, source_type, mode,
            opportunity_score, company_stage, contact_role, email_confidence,
            evidence_confidence, personalization_confidence, message_angle,
            cta_type, subject_variant, sequence_variant, sent_time, outcome,
            sent_time, notes
        )
    )
    return cur.lastrowid if cur.lastrowid is not None else 0


def update_message_outcome(conn, lead_id: int, outcome: str, notes: str = "", message_id: int | None = None) -> int:
    """Update the outcome state for messages belonging to a lead."""
    updated_at = now()
    if message_id:
        cur = conn.execute(
            "UPDATE message_outcomes SET outcome = ?, outcome_updated_at = ?, "
            "notes = CASE WHEN ? != '' THEN ? ELSE notes END WHERE message_id = ?",
            (outcome, updated_at, notes, notes, message_id)
        )
        if cur.rowcount > 0:
            return cur.rowcount
        # If no row existed in message_outcomes, create one
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if lead:
            mode = "internship" if ("intern" in str(lead["segment"] or "").lower() or lead["opportunity_type"] in ("internship", "intern")) else "freelance"
            score = (lead["score_total"] if "score_total" in lead.keys() and lead["score_total"] else lead["score"]) or 0
            record_message_outcome(
                conn,
                message_id=message_id,
                lead_id=lead_id,
                mode=mode,
                opportunity_score=score,
                outcome=outcome,
                notes=notes,
                lead_source=lead["source"] or "",
                contact_role=lead["title"] or "",
            )
            return 1
        return 0
    else:
        cur = conn.execute(
            "UPDATE message_outcomes SET outcome = ?, outcome_updated_at = ?, "
            "notes = CASE WHEN ? != '' THEN ? ELSE notes END "
            "WHERE lead_id = ? AND id = (SELECT id FROM message_outcomes WHERE lead_id = ? ORDER BY id DESC LIMIT 1)",
            (outcome, updated_at, notes, notes, lead_id, lead_id)
        )
        return cur.rowcount


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


def reset_database(keep_suppression: bool = True, backup: bool = True) -> dict:
    """Wipes all pipeline leads, drafts, messages, outcomes, replies, and logs for a completely fresh start.

    Creates a timestamped backup of the local SQLite database first.
    Cleans local SQLite and remote Turso database (if configured).
    Resets sending_paused to '0' and sets fresh engine:heartbeat.
    """
    import os
    import shutil
    from pathlib import Path

    counts = {}
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # 1. Backup local DB if it exists
    path = config.db_path()
    if backup and path.exists():
        backup_path = path.parent / f"outreach.db.bak_{now_str}"
        shutil.copyfile(path, backup_path)
        counts["backup_file"] = str(backup_path)

    # Tables to clear (child tables first to satisfy foreign keys)
    tables_to_clear = [
        "message_outcomes", "replies", "messages", "leads", "send_log",
        "posts", "prospects", "prospect_domain_cache", "provider_credits"
    ]
    if not keep_suppression:
        tables_to_clear.append("suppression")

    # 2. Clear local SQLite database
    with connect() as conn:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass
        for t in tables_to_clear:
            try:
                c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                conn.execute(f"DELETE FROM {t}")
                counts[t] = c
            except Exception:
                pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        # Clear prospect_state except essential system keys
        try:
            conn.execute("DELETE FROM prospect_state WHERE key NOT LIKE 'config:%'")
            # Set fresh heartbeat and unpause sending
            set_state(conn, "sending_paused", "0")
            set_state(conn, "engine:heartbeat", now())
        except Exception:
            pass
        # Reset sqlite_sequence
        try:
            placeholders = ",".join("?" for _ in tables_to_clear)
            conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tuple(tables_to_clear))
        except Exception:
            pass

    # 3. Clear Turso tables if configured
    if os.getenv("TURSO_DATABASE_URL"):
        try:
            turso_stmts = [
                "DELETE FROM dash_items",
                "DELETE FROM actions",
                "DELETE FROM dash_meta",
            ]
            for t in tables_to_clear:
                turso_stmts.append(f"DELETE FROM {t}")
            turso.run(turso_stmts)
            counts["turso_cleared"] = True
        except Exception as e:
            counts["turso_cleared_error"] = str(e)

    # 4. Remove stale lock files in data/
    for lock in config.DATA_DIR.glob("*.lock"):
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass

    return counts

