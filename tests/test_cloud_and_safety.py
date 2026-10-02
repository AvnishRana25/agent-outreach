"""Tests for cloud execution, persistent remote storage, and send safety."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from outreach import config, db, personalize, sender, transport, turso


def _py(a):
    t = a.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(a["value"])
    if t == "float":
        return float(a["value"])
    return a.get("value")


def _cell(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def fake_turso_server(db_path: str):
    """Local HTTP mock server for Turso's /v2/pipeline API."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v2/pipeline"
            assert self.headers.get("Authorization", "").startswith("Bearer ")
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            reqs = body.get("requests", [])
            conn = sqlite3.connect(db_path, isolation_level=None)
            results = []
            for r in reqs:
                if r["type"] == "close":
                    results.append({"type": "ok", "response": {"type": "close"}})
                    continue
                sql = r["stmt"]["sql"]
                args = [_py(a) for a in r["stmt"].get("args", [])]
                try:
                    cur = conn.execute(sql, args)
                    cols = [{"name": d[0]} for d in cur.description or []]
                    rows = [[_cell(v) for v in row] for row in cur.fetchall()]
                    last_id = str(cur.lastrowid) if cur.lastrowid is not None and cur.lastrowid > 0 else None
                    results.append({
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": cols,
                                "rows": rows,
                                "affected_row_count": cur.rowcount if cur.rowcount >= 0 else 0,
                                "last_insert_rowid": last_id,
                            }
                        }
                    })
                except sqlite3.Error as e:
                    results.append({"type": "error", "error": {"message": str(e)}})
            conn.close()
            resp_body = json.dumps({"baton": "test-baton:1", "results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_env_configuration(monkeypatch):
    """Test OUTREACH_ENV distinction and YAML environment overrides."""
    monkeypatch.setenv("OUTREACH_ENV", "local")
    assert config.env() == "local"
    assert not config.is_production()

    monkeypatch.setenv("OUTREACH_ENV", "production")
    assert config.env() == "production"
    assert config.is_production()

    # YAML environment overrides
    monkeypatch.setenv("SETTINGS_YAML", "sending:\n  home_timezone: Europe/London\n")
    loaded = config._load_yaml("settings.yaml")
    assert loaded["sending"]["home_timezone"] == "Europe/London"

    monkeypatch.setenv("PROFILE_YAML", "name: Cloud Runner\n")
    loaded_profile = config._load_yaml("profile.yaml")
    assert loaded_profile["name"] == "Cloud Runner"


def test_turso_connection_and_row_access(tmp_path, monkeypatch):
    """Test TursoConnection executes queries, returns dict/index accessible Rows, and handles PRAGMAs."""
    remote_db = str(tmp_path / "turso.db")
    srv = fake_turso_server(remote_db)
    monkeypatch.setenv("TURSO_DATABASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-token")

    try:
        conn = turso.connect()
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, score INT)")
        cur = conn.execute("INSERT INTO users (email, score) VALUES (?, ?)", ("test@example.com", 42))
        assert cur.rowcount == 1
        assert cur.lastrowid == 1

        cur2 = conn.execute("SELECT * FROM users WHERE email = ?", ("test@example.com",))
        row = cur2.fetchone()
        assert row is not None
        assert row["email"] == "test@example.com"
        assert row[1] == "test@example.com"
        assert row["score"] == 42
        assert row[2] == 42
        assert dict(row) == {"id": 1, "email": "test@example.com", "score": 42}

        # PRAGMA busy_timeout should be safely ignored
        prag = conn.execute("PRAGMA busy_timeout = 30000")
        assert prag.fetchall() == []

        conn.close()
    finally:
        srv.shutdown()


def test_persistence_across_separate_executions(tmp_path, monkeypatch):
    """Test that data written in one execution context persists into a subsequent separate execution in Turso."""
    remote_db = str(tmp_path / "turso_persist.db")
    srv = fake_turso_server(remote_db)
    monkeypatch.setenv("OUTREACH_ENV", "production")
    monkeypatch.setenv("TURSO_DATABASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-token")

    try:
        # Execution 1: initialize and insert data
        db.init()
        with db.connect() as conn:
            added = db.add_lead(conn, email="cloud@example.com", company="Cloud Corp",
                                domain="cloudcorp.com", segment="uk_agencies", status="approved", fit=9)
            assert added is True
            lead = conn.execute("SELECT id FROM leads WHERE email='cloud@example.com'").fetchone()
            lead_id = lead["id"]
            conn.execute(
                "INSERT INTO messages (lead_id, step, subject, body, status, confidence, idempotency_key) "
                "VALUES (?, 0, 'Hello Cloud', 'Body text', 'approved', 0.95, ?)",
                (lead_id, f"lead:{lead_id}:step:0")
            )
            db.add_prospect(conn, full_name="Jane Doe", company="Cloud Corp", domain="cloudcorp.com",
                            final_email="jane@cloudcorp.com", confidence_score=90, confidence_level="verified")

        # Execution 2: a separate process/runner starts up with fresh connection
        with db.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE email='cloud@example.com'").fetchone()
            assert lead is not None
            assert lead["company"] == "Cloud Corp"
            assert lead["fit"] == 9
            assert lead["status"] == "approved"

            msg = conn.execute("SELECT * FROM messages WHERE lead_id=? AND step=0", (lead["id"],)).fetchone()
            assert msg is not None
            assert msg["subject"] == "Hello Cloud"
            assert msg["idempotency_key"] == f"lead:{lead['id']}:step:0"
            assert msg["status"] == "approved"

            prospects = db.list_prospects(conn, company="Cloud")
            assert len(prospects) == 1
            assert prospects[0]["full_name"] == "Jane Doe"
            assert prospects[0]["final_email"] == "jane@cloudcorp.com"
    finally:
        srv.shutdown()


def test_duplicate_send_prevention_and_idempotency(tmp_path, monkeypatch):
    """Test idempotency: unique constraints prevent duplicate sequence steps."""
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "idempotent.db"))
    monkeypatch.setenv("OUTREACH_ENV", "local")
    db.init()

    with db.connect() as conn:
        db.add_lead(conn, email="buyer@test.com", company="Buyer Co", domain="buyer.com",
                    segment="uk_agencies", status="approved", fit=8)
        lead_id = conn.execute("SELECT id FROM leads WHERE email='buyer@test.com'").fetchone()[0]

        # Insert step 0
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, idempotency_key) "
                     "VALUES (?, 0, 'Step 0', 'Hello', 'approved', ?)",
                     (lead_id, f"lead:{lead_id}:step:0"))

        # Attempting to insert duplicate step 0 with the same lead_id and step must violate constraint
        with pytest.raises(Exception):
            conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, idempotency_key) "
                         "VALUES (?, 0, 'Step 0 duplicate', 'Hello again', 'approved', ?)",
                         (lead_id, f"lead:{lead_id}:step:0:dup"))

        # Attempting to insert duplicate with same idempotency_key must violate unique constraint
        with pytest.raises(Exception):
            conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, idempotency_key) "
                         "VALUES (?, 1, 'Step 1 duplicate key', 'Hello', 'approved', ?)",
                         (lead_id, f"lead:{lead_id}:step:0"))


def test_concurrency_atomic_claim_protection(tmp_path, monkeypatch):
    """Test that atomic status update prevents concurrent workers from sending the same email."""
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "concurrency.db"))
    monkeypatch.setenv("OUTREACH_ENV", "local")
    db.init()

    with db.connect() as conn:
        db.add_lead(conn, email="ceo@race.com", company="Race Inc", domain="race.com",
                    segment="uk_agencies", status="approved", fit=8)
        lead_id = conn.execute("SELECT id FROM leads WHERE email='ceo@race.com'").fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, idempotency_key) "
                     "VALUES (?, 0, 'Fast idea', 'Quick pitch', 'approved', ?)",
                     (lead_id, f"lead:{lead_id}:step:0"))
        msg_id = conn.execute("SELECT id FROM messages WHERE lead_id=?", (lead_id,)).fetchone()[0]

    # Worker 1 attempts to claim
    with db.connect() as conn:
        c1 = conn.execute("UPDATE messages SET status='sending', inbox='box@test.com', sending_at=? "
                          "WHERE id=? AND status='approved'", (datetime.now(timezone.utc).isoformat(), msg_id))
        assert c1.rowcount == 1

    # Worker 2 attempts to claim the same message concurrently
    with db.connect() as conn:
        c2 = conn.execute("UPDATE messages SET status='sending', inbox='box@test.com', sending_at=? "
                          "WHERE id=? AND status='approved'", (datetime.now(timezone.utc).isoformat(), msg_id))
        assert c2.rowcount == 0  # Safely rejected! Only 1 worker can claim it.


def test_stale_sending_reconciliation(tmp_path, monkeypatch):
    """Test that a message left in status='sending' from a crashed runner is moved to 'needs_reconciliation'."""
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "stale.db"))
    monkeypatch.setenv("OUTREACH_ENV", "local")
    db.init()

    with db.connect() as conn:
        db.add_lead(conn, email="crash@example.com", company="Crash Test", domain="crash.com",
                    segment="uk_agencies", status="approved", fit=8)
        lead_id = conn.execute("SELECT id FROM leads WHERE email='crash@example.com'").fetchone()[0]

        # Simulate a send that crashed 30 minutes ago
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, sending_at, idempotency_key) "
                     "VALUES (?, 0, 'Subject', 'Body', 'sending', ?, ?)",
                     (lead_id, past_time, f"lead:{lead_id}:step:0"))

        reconciled = sender.reconcile_stale_sending(conn, max_age_minutes=15)
        assert reconciled == 1

        msg = conn.execute("SELECT status, error FROM messages WHERE lead_id=?", (lead_id,)).fetchone()
        assert msg["status"] == "needs_reconciliation"
        assert "timed out or process terminated" in msg["error"]


def test_human_approval_enforced_before_first_contact(tmp_path, monkeypatch):
    """Verify that unapproved drafts are never sent by sender.tick(), enforcing human approval."""
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "unapproved.db"))
    monkeypatch.setenv("OUTREACH_ENV", "local")
    db.init()

    with db.connect() as conn:
        db.add_lead(conn, email="draft@example.com", company="Draft Co", domain="draftco.com",
                    segment="uk_agencies", status="drafted", fit=8)
        lead_id = conn.execute("SELECT id FROM leads WHERE email='draft@example.com'").fetchone()[0]
        # Draft created, status='draft', lead status='drafted'
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, confidence, idempotency_key) "
                     "VALUES (?, 0, 'Pitch', 'Body', 'draft', 0.9, ?)",
                     (lead_id, f"lead:{lead_id}:step:0"))

    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "sender@test.com", "max_per_day": 35}])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Test Sign")

    sent_count = sender.tick(5)
    assert sent_count == 0  # Must NEVER send unapproved draft!

    with db.connect() as conn:
        msg = conn.execute("SELECT status FROM messages WHERE lead_id=?", (lead_id,)).fetchone()
        assert msg["status"] == "draft"


def test_migrate_to_turso(tmp_path, monkeypatch):
    """Test copying local SQLite database tables into Turso remote storage."""
    local_db = tmp_path / "local.db"
    remote_db = str(tmp_path / "turso_dest.db")

    srv = fake_turso_server(remote_db)
    monkeypatch.setenv("TURSO_DATABASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-token")

    try:
        # Populate local db
        monkeypatch.setenv("OUTREACH_DB", str(local_db))
        monkeypatch.setenv("OUTREACH_ENV", "local")
        db.init()
        with db.connect() as conn:
            db.add_lead(conn, email="migrate@test.com", company="Migrate Inc", domain="migrate.com",
                        segment="uk_agencies", status="active")
            db.suppress(conn, "bad@spam.com", "unsubscribed")

        # Run migration
        counts = db.migrate_to_turso(local_db)
        assert counts["leads"] >= 1
        assert counts["suppression"] >= 1

        # Verify on remote Turso
        conn = turso.connect()
        cur = conn.execute("SELECT email, company FROM leads WHERE email='migrate@test.com'")
        row = cur.fetchone()
        assert row is not None
        assert row["company"] == "Migrate Inc"

        supp = conn.execute("SELECT * FROM suppression WHERE email='bad@spam.com'").fetchone()
        assert supp is not None
        conn.close()
    finally:
        srv.shutdown()
